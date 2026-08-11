"""
shared/dashboard_cache.py — Parquet mirror + stats.json for dashboard fast reads.

Each pipeline runner calls refresh(stage) at the end of its run (and in its
finally block so partial progress is saved on Ctrl-C).  The dashboard API
endpoints check for Parquet / stats.json before falling back to CSV reads.

Stage 1's artifact is the survivor pool (``SNAPSHOT_POOL_DIR``), not a CSV: it is
a directory of parquet partitions written by the snapshot scan, so it has no
Parquet mirror and its stats are read from the pool itself. It is also several GB
and gitignored, which means a checkout without it is normal — every pool function
returns None rather than raising so the dashboard can say "not available here".

Public API
----------
  write_parquet(stage)   read stage CSV → write data/dashboard/{stage}.parquet
  update_stats(stage)    recompute counts → update stats.json
  refresh(stage)         write_parquet + update_stats (normal call site)
  pool_totals()          cheap survivor-pool row/file/byte counts (footers only)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from shared.config import DATA_DIR, SNAPSHOT_POOL_DIR

log = logging.getLogger("flora.dashboard_cache")

DASHBOARD_DIR   = DATA_DIR / "dashboard"
STATS_JSON_PATH = DASHBOARD_DIR / "stats.json"

_STAGE_CSV: dict[str, Path] = {
    "filtered":       DATA_DIR / "filtered.csv",
    "extracted":      DATA_DIR / "extracted.csv",
    "extracted-test": DATA_DIR / "extracted-test.csv",
}

# Stage 1 has no CSV — its stats come from the pool directory.
POOL_STAGE = "pool"

# Canonical outcome categories + pipeline-state markers (see shared/schema.py).
_OUTCOME_KEYS = (
    "successful", "failed", "mixed", "descriptive only",
    "statistically successful but flawed", "uninformative",
    "cannot_be_determined", "not_a_replication",
    "pending", "api_error",
)
_METHOD_KEYS = (
    # Granular rule-based resolution methods (formerly collapsed to author_year_match).
    "citation_context_match", "same_author_year_title_overlap",
    "single_candidate_after_requery", "title_pattern_match", "grobid_ref_match",
    # Legacy + un-migrated author_year_match rows.
    "author_year_match_legacy", "author_year_match",
    "llm_cited_candidates", "llm_fulltext",
    "no_original_found", "target_pending", "api_error",
)


# How a Stage-2 row got its verdict, recovered from filter_evidence rather than a
# dedicated column. `engine_route` is the current path: the filter engine writes
# `rule:<spec id>` and the pile did the deciding. The `r*` keys are the retired
# per-row classifier's exits, kept because rows on disk still carry them — that
# generation of Stage 2 prepended its rule evidence to an LLM verdict
# ("<rule> | llm:<...>"), so the marker survives even where the LLM reclassified.
_RULE_EXIT_KEYS = ("engine_route", "r1_exclusion", "r2_no_phrase", "r3_no_cite",
                   "r4_no_same_sentence", "r5_pass", "unknown")


def classify_rule_exit(evidence: str) -> str:
    """Which Stage-2 exit produced this row, from its filter_evidence string."""
    e = str(evidence or "")
    if e.startswith("rule:"):
        return "engine_route"
    if e.startswith("exclusion:"):
        return "r1_exclusion"
    if e.startswith("no replication phrase detected"):
        return "r2_no_phrase"
    if "; no author-year cite" in e:
        return "r3_no_cite"
    if "; no same-sentence cite" in e:
        return "r4_no_same_sentence"
    if "; cite:" in e:
        return "r5_pass"
    return "unknown"


def _year_counts(series: "pd.Series") -> dict[str, int]:
    """Count rows per publication year, dropping blanks and non-numeric junk."""
    s = series.fillna("").astype(str).str.strip().str.slice(0, 4)
    s = s[s.str.fullmatch(r"\d{4}", na=False)]
    return {str(k): int(v) for k, v in s.value_counts().items()}


def _merge_counts(target: dict[str, int], src: dict[str, int]) -> None:
    for k, v in src.items():
        target[k] = target.get(k, 0) + int(v)


def _parquet_path(stage: str) -> Path:
    return DASHBOARD_DIR / f"{stage}.parquet"


# ── Parquet writer ─────────────────────────────────────────────────────────────

def write_parquet(stage: str) -> None:
    """Read stage CSV in 50k-row chunks and write a Parquet file."""
    if stage not in _STAGE_CSV:
        raise ValueError(f"Unknown stage: {stage!r}")

    csv_path = _STAGE_CSV[stage]
    if not csv_path.exists():
        log.warning("dashboard_cache: %s CSV not found — skipping Parquet write", stage)
        return

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _parquet_path(stage)
    tmp_path = out_path.with_suffix(".tmp.parquet")

    writer: pq.ParquetWriter | None = None
    rows_written = 0
    t0 = time.monotonic()
    try:
        for chunk in pd.read_csv(
            csv_path, encoding="utf-8-sig", dtype=str,
            chunksize=50_000, on_bad_lines="skip",
        ):
            chunk = chunk.fillna("")
            # Truncate runaway strings — some abstracts exceed 100k chars and
            # cause PyArrow to fail on read with "Wrapping ... failed".
            str_cols = chunk.select_dtypes(include="object").columns
            chunk[str_cols] = chunk[str_cols].apply(
                lambda s: s.str.slice(0, 50_000)
            )
            table = pa.Table.from_pandas(chunk, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(tmp_path, table.schema, compression="snappy")
            writer.write_table(table)
            rows_written += len(chunk)
    finally:
        if writer is not None:
            writer.close()

    if rows_written > 0:
        tmp_path.replace(out_path)
        elapsed = time.monotonic() - t0
        log.info(
            "dashboard_cache: wrote %s → %s (%d rows, %.1fs)",
            stage, out_path.name, rows_written, elapsed,
        )
    else:
        if tmp_path.exists():
            tmp_path.unlink()
        log.warning("dashboard_cache: %s CSV was empty — no Parquet written", stage)


# ── Stats computation ──────────────────────────────────────────────────────────

def _model_family(s: str) -> str:
    m = str(s or "").lower().strip()
    if not m:               return "none"
    if m.startswith("gemini"): return "gemini"
    if m.startswith(("gpt-", "o1", "o3", "o4")): return "gpt"
    if "qwen" in m:         return "qwen"
    if "mistral" in m:      return "mistral"
    return "other"


def _vc(series: "pd.Series", keys: tuple[str, ...] | None = None) -> dict[str, int]:
    """Value counts as {key: int} dict, filtered to keys if given."""
    counts = series.fillna("").value_counts().to_dict()
    counts = {str(k): int(v) for k, v in counts.items()}
    if keys is not None:
        result = {k: counts.get(k, 0) for k in keys}
        other_keys = set(counts) - set(keys)
        if other_keys:
            result["_other"] = sum(counts[k] for k in other_keys)
        return result
    return counts


def _compute_extracted_stats(df: pd.DataFrame) -> dict[str, Any]:
    lm_col  = df["link_method"].fillna("")       if "link_method"         in df.columns else pd.Series([""] * len(df))
    mt_col  = df["original_match_type"].fillna("") if "original_match_type" in df.columns else pd.Series([""] * len(df))
    oc_col  = df["outcome"].fillna("")           if "outcome"             in df.columns else pd.Series([""] * len(df))
    dv_col  = df["doi_o_verification"].fillna("") if "doi_o_verification"  in df.columns else pd.Series([""] * len(df))
    mod_col = df["link_llm_model"].fillna("").apply(_model_family) if "link_llm_model" in df.columns else pd.Series(["none"] * len(df))
    ty_col  = df["type"].fillna("").str.strip().str.lower() if "type" in df.columns else pd.Series([""] * len(df))
    yr_col  = df["year_r"] if "year_r" in df.columns else pd.Series([""] * len(df))

    # Replications and reproductions use disjoint outcome vocabularies
    # (see shared/schema.py) — a single merged distribution is meaningless.
    is_repro = ty_col == "reproduction"
    return {
        "total":                  len(df),
        "target_pending_count":   int((lm_col == "target_pending").sum()),
        "by_match_type":          _vc(mt_col),
        "by_link_method":         _vc(lm_col, _METHOD_KEYS),
        "by_model":               _vc(mod_col),
        "by_outcome":             _vc(oc_col, _OUTCOME_KEYS),
        "by_doi_verification":    _vc(dv_col),
        "by_type":                _vc(ty_col),
        "by_outcome_replication": _vc(oc_col[~is_repro]),
        "by_outcome_reproduction": _vc(oc_col[is_repro]),
        "by_year":                _year_counts(yr_col),
    }


def _read_for_stats(stage: str) -> "pd.DataFrame | None":
    """Read only the columns needed for stats computation.

    For extracted/extracted-test (small files) loads the whole table at once.
    filtered is potentially millions of rows — callers should prefer
    _compute_large_stage_stats instead and only use this for small stages.
    """
    _STATS_COLS: dict[str, list[str]] = {
        "filtered":       ["doi_r", "url_r", "abstract_r", "year_r",
                           "paper_type", "filter_method", "filter_confidence",
                           "filter_evidence"],
        "extracted":      ["link_method", "link_llm_model", "original_match_type",
                           "outcome", "doi_o_verification", "type", "year_r"],
        "extracted-test": ["link_method", "link_llm_model", "original_match_type",
                           "outcome", "doi_o_verification", "type", "year_r"],
    }
    cols = _STATS_COLS[stage]
    pq_path = _parquet_path(stage)
    if pq_path.exists():
        try:
            existing = pq.read_schema(pq_path).names
            read_cols = [c for c in cols if c in existing]
            return pq.read_table(pq_path, columns=read_cols).to_pandas()
        except Exception as exc:
            log.warning("dashboard_cache: Parquet read failed for %s: %s", stage, exc)

    csv_path = _STAGE_CSV[stage]
    if not csv_path.exists():
        return None
    try:
        return pd.read_csv(
            csv_path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
            usecols=lambda c: c in cols,
        ).fillna("")
    except Exception as exc:
        log.warning("dashboard_cache: CSV read failed for %s: %s", stage, exc)
        return None


def _compute_large_stage_stats(stage: str) -> "dict[str, Any] | None":
    """Compute stats for the filtered stage without loading it fully into memory.

    Strategy:
    - Read only lightweight columns (no abstract_r) in 100k-row chunks to get
      all counts.
    - For filtered: use parquet predicate pushdown to read doi/url/abstract
      only for the small replication+reproduction subset.
    - Falls back to the CSV path if Parquet is unavailable.
    """
    pq_path  = _parquet_path(stage)
    csv_path = _STAGE_CSV[stage]

    if not pq_path.exists() and not csv_path.exists():
        return None

    # ── Filtered ────────────────────────────────────────────────────────────
    if stage == "filtered":
        total = 0
        status_counts: dict[str, int] = {}
        method_counts: dict[str, int] = {}
        conf_counts:   dict[str, int] = {}

        exit_counts:   dict[str, int] = {}
        year_counts:   dict[str, int] = {}
        # {rule exit → {final paper_type → n}} — lets the flowchart show what the
        # LLM did with the two needs_review arms it receives.
        exit_status:   dict[str, dict[str, int]] = {}

        # Pass 1: lightweight columns only — get all counts except data quality
        _light_cols = ("paper_type", "filter_method", "filter_confidence",
                       "filter_evidence", "year_r")

        def _process_filt_chunk(chunk: pd.DataFrame) -> None:
            nonlocal total
            chunk = chunk.fillna("")
            total += len(chunk)
            for k, v in chunk.get("paper_type", pd.Series(dtype=str)).value_counts().items():
                status_counts[str(k)] = status_counts.get(str(k), 0) + int(v)
            for k, v in chunk.get("filter_method", pd.Series(dtype=str)).value_counts().items():
                method_counts[str(k)] = method_counts.get(str(k), 0) + int(v)
            for k, v in chunk.get("filter_confidence", pd.Series(dtype=str)).value_counts().items():
                conf_counts[str(k)] = conf_counts.get(str(k), 0) + int(v)
            if "year_r" in chunk.columns:
                _merge_counts(year_counts, _year_counts(chunk["year_r"]))
            if "filter_evidence" in chunk.columns:
                exits = chunk["filter_evidence"].apply(classify_rule_exit)
                _merge_counts(exit_counts, exits.value_counts().to_dict())
                if "paper_type" in chunk.columns:
                    grouped = chunk.assign(_exit=exits).groupby(["_exit", "paper_type"]).size()
                    for (ex, st), n in grouped.items():
                        bucket = exit_status.setdefault(str(ex), {})
                        bucket[str(st)] = bucket.get(str(st), 0) + int(n)

        try:
            if pq_path.exists():
                pf = pq.ParquetFile(pq_path)
                existing = pf.schema_arrow.names
                read_cols = [c for c in _light_cols if c in existing]
                for batch in pf.iter_batches(batch_size=100_000, columns=read_cols):
                    _process_filt_chunk(batch.to_pandas())
            else:
                for chunk in pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str,
                                         chunksize=100_000, on_bad_lines="skip",
                                         usecols=lambda c: c in _light_cols):
                    _process_filt_chunk(chunk)
        except Exception as exc:
            log.warning("dashboard_cache: chunked filtered (pass 1) failed: %s", exc)
            return None

        rep_repro_total = (status_counts.get("replication", 0) +
                           status_counts.get("reproduction", 0))

        # Pass 2: data quality for replication+reproduction rows only.
        # This subset is small (tens of thousands), so loading it fully is safe.
        rr_no_doi = rr_no_doi_or_url = rr_no_abstract = 0
        _dq_cols = ("doi_r", "url_r", "abstract_r", "paper_type")
        try:
            if pq_path.exists() and "paper_type" in pq.read_schema(pq_path).names:
                import pyarrow.compute as pc
                pf = pq.ParquetFile(pq_path)
                existing = pf.schema_arrow.names
                read_cols = [c for c in _dq_cols if c in existing]
                filters = [("paper_type", "in", ["replication", "reproduction"])]
                rr = pq.read_table(pq_path, columns=read_cols, filters=filters).to_pandas().fillna("")
            else:
                rr_chunks = []
                for chunk in pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str,
                                         chunksize=100_000, on_bad_lines="skip",
                                         usecols=lambda c: c in _dq_cols):
                    sub = chunk[chunk["paper_type"].isin(["replication","reproduction"])]
                    if len(sub):
                        rr_chunks.append(sub)
                rr = pd.concat(rr_chunks, ignore_index=True).fillna("") if rr_chunks else pd.DataFrame()

            if len(rr):
                doi_c = rr["doi_r"] if "doi_r" in rr.columns else pd.Series([""] * len(rr))
                url_c = rr["url_r"] if "url_r" in rr.columns else pd.Series([""] * len(rr))
                abs_c = rr["abstract_r"] if "abstract_r" in rr.columns else pd.Series([""] * len(rr))
                rr_no_doi         = int((doi_c == "").sum())
                rr_no_doi_or_url  = int(((doi_c == "") & (url_c == "")).sum())
                rr_no_abstract    = int((abs_c == "").sum())
        except Exception as exc:
            log.warning("dashboard_cache: filtered data-quality pass failed: %s", exc)

        return {
            "total":                   total,
            "by_paper_type":        status_counts,
            "by_filter_method":        method_counts,
            "by_filter_confidence":    conf_counts,
            "by_rule_exit":            {k: exit_counts.get(k, 0) for k in _RULE_EXIT_KEYS},
            "rule_exit_status":        exit_status,
            "by_year":                 year_counts,
            "rep_repro_total":         rep_repro_total,
            "rep_repro_no_doi":        rr_no_doi,
            "rep_repro_no_doi_or_url": rr_no_doi_or_url,
            "rep_repro_no_abstract":   rr_no_abstract,
        }

    return None  # not a large stage


# ── Stage 1: the survivor pool ─────────────────────────────────────────────────
# The pool is a flat directory of parquet partitions (search/snapshot_scan.py,
# _POOL_SCHEMA). Two levels of detail, because they cost very different amounts:
# pool_totals() reads only parquet footers and is safe on a web request, while
# the breakdowns read columns off every partition and belong in a refresh.

_POOL_STAT_COLUMNS = ("doi", "publication_year",
                      "hit_token_title", "hit_token_abstract", "hit_concept")

_TOTALS_TTL_SECONDS = 60.0
_totals_memo: "tuple[float, str, dict[str, Any] | None] | None" = None


def pool_files(pool_dir: Path = SNAPSHOT_POOL_DIR) -> list[Path]:
    """The pool's parquet partitions, or [] when the pool is not on this machine."""
    if not pool_dir.is_dir():
        return []
    return sorted(pool_dir.glob("*.parquet"))


def pool_totals(pool_dir: Path = SNAPSHOT_POOL_DIR) -> "dict[str, Any] | None":
    """Row / file / byte counts from parquet footers. None when there is no pool.

    Memoised for a minute: the dashboard asks on every load and a few thousand
    footer reads should not be repeated per request.
    """
    global _totals_memo
    now = time.monotonic()
    if _totals_memo and _totals_memo[1] == str(pool_dir) and now - _totals_memo[0] < _TOTALS_TTL_SECONDS:
        return _totals_memo[2]

    files = pool_files(pool_dir)
    result: "dict[str, Any] | None"
    if not files:
        result = None
    else:
        rows = unreadable = 0
        size = 0
        for f in files:
            try:
                rows += pq.ParquetFile(f).metadata.num_rows
                size += f.stat().st_size
            except Exception:
                unreadable += 1
        result = {
            "total":      rows,
            "files":      len(files),
            "bytes":      size,
            "unreadable": unreadable,
            "pool_dir":   str(pool_dir),
        }
    _totals_memo = (now, str(pool_dir), result)
    return result


def compute_pool_stats(pool_dir: Path = SNAPSHOT_POOL_DIR) -> "dict[str, Any] | None":
    """Full survivor-pool stats: totals plus year and search-gate breakdowns.

    Reads five narrow columns off every partition — seconds to minutes over a
    full pool, so this is a refresh-time call, not a per-request one.
    """
    totals = pool_totals(pool_dir)
    if totals is None:
        return None

    no_doi = 0
    year_counts: dict[str, int] = {}
    gate_hits = {"title": 0, "abstract": 0, "concept": 0}
    for f in pool_files(pool_dir):
        try:
            pf = pq.ParquetFile(f)
            cols = [c for c in _POOL_STAT_COLUMNS if c in pf.schema_arrow.names]
            for batch in pf.iter_batches(batch_size=100_000, columns=cols):
                df = batch.to_pandas()
                if "doi" in df.columns:
                    no_doi += int(df["doi"].isna().sum() + (df["doi"] == "").sum())
                if "publication_year" in df.columns:
                    _merge_counts(year_counts, _year_counts(df["publication_year"]))
                for key, col in (("title",    "hit_token_title"),
                                 ("abstract", "hit_token_abstract"),
                                 ("concept",  "hit_concept")):
                    if col in df.columns:
                        gate_hits[key] += int(df[col].fillna(False).astype(bool).sum())
        except Exception as exc:
            log.warning("dashboard_cache: pool partition %s unreadable: %s", f.name, exc)

    return {**totals, "no_doi": no_doi, "by_year": year_counts, "gate_hits": gate_hits}


def compute_stage_stats(stage: str,
                        pool_dir: "Path | None" = None) -> "dict[str, Any] | None":
    """Compute one stage's stats live. None if there is no data.

    Same shape as the stage's entry in stats.json — the dashboard's slow path
    calls this instead of re-implementing the aggregations.

    *pool_dir* applies to the pool stage only, and exists because a scan may be
    told where to write with ``--survivor-pool``. Publishing the default
    directory's numbers after scanning a different one would be silently wrong,
    so the caller that knows which pool it wrote passes it. Readers that have no
    such knowledge (the dashboard) omit it and get ``SNAPSHOT_POOL_DIR``.
    """
    if stage == POOL_STAGE:
        return compute_pool_stats(pool_dir or SNAPSHOT_POOL_DIR)
    if stage not in _STAGE_CSV:
        raise ValueError(f"Unknown stage: {stage!r}")
    # filtered is too large to load fully into RAM
    if stage == "filtered":
        return _compute_large_stage_stats(stage)
    df = _read_for_stats(stage)
    return None if df is None else _compute_extracted_stats(df)


def update_stats(stage: str, pool_dir: "Path | None" = None) -> None:
    """Recompute counts for stage and merge into stats.json."""
    new_stats = compute_stage_stats(stage, pool_dir=pool_dir)
    if new_stats is None:
        log.warning("dashboard_cache: no data to compute stats for %s", stage)
        return


    stage_key = stage.replace("-", "_")
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if STATS_JSON_PATH.exists():
        try:
            existing = json.loads(STATS_JSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    existing[stage_key]  = new_stats
    existing["updated_at"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    STATS_JSON_PATH.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("dashboard_cache: updated stats.json for stage=%s (total=%s)", stage, new_stats.get("total"))


# ── Public entry point ─────────────────────────────────────────────────────────

def refresh(stage: str, pool_dir: "Path | None" = None) -> None:
    """Write Parquet mirror then update stats.json for this stage.

    The pool has no CSV and no mirror — it is already parquet — so refreshing it
    is stats-only. *pool_dir* is honoured for the pool stage only; see
    `compute_stage_stats` for why the writer, not the reader, names it.
    """
    if stage == POOL_STAGE:
        try:
            update_stats(POOL_STAGE, pool_dir=pool_dir)
        except Exception as exc:
            log.warning("dashboard_cache: update_stats failed for pool: %s", exc)
        return
    if stage not in _STAGE_CSV:
        log.warning("dashboard_cache.refresh: unknown stage %r — skipping", stage)
        return
    try:
        write_parquet(stage)
    except Exception as exc:
        log.warning("dashboard_cache: write_parquet failed for %s: %s", stage, exc)
    try:
        update_stats(stage)
    except Exception as exc:
        log.warning("dashboard_cache: update_stats failed for %s: %s", stage, exc)


def load_stats() -> dict[str, Any]:
    """Return the current stats.json contents, or {} if not present."""
    if STATS_JSON_PATH.exists():
        try:
            return json.loads(STATS_JSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}
