"""
shared/dashboard_cache.py — Parquet mirror + stats.json for dashboard fast reads.

Each pipeline runner calls refresh(stage) at the end of its run (and in its
finally block so partial progress is saved on Ctrl-C).  The dashboard API
endpoints check for Parquet / stats.json before falling back to CSV reads.

Only Stage 3's two CSVs have a mirror. The other two stages own artifacts that
are not CSVs at all, and each is read where it lives:

* **Stage 1** is the survivor pool (``SNAPSHOT_POOL_DIR``) — a directory of
  parquet partitions written by the snapshot scan. It is several GB and
  gitignored, so a checkout without it is normal: every pool function returns
  None rather than raising, and the dashboard says "not available here".
* **Stage 2** is the routing store (``filter/engine/store.py``, DuckDB). Its
  figures are queried live, per release, from the same store Stage 3 builds its
  worklist off. Nothing is mirrored, because the store already answers a count
  in milliseconds and a mirror is one more thing that can be stale.

Public API
----------
  write_parquet(stage)   read stage CSV → write data/dashboard/{stage}.parquet
  update_stats(stage)    recompute counts → update stats.json
  refresh(stage)         write_parquet + update_stats (normal call site)
  pool_totals()          cheap survivor-pool row/file/byte counts (footers only)
  compute_filtered_stats()  Stage 2 pile counts for the newest routed release
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
    "extracted":      DATA_DIR / "extracted.csv",
    "extracted-test": DATA_DIR / "extracted-test.csv",
}

# The two stages whose artifact is not a CSV: Stage 1's pool directory and
# Stage 2's routing store. Both are read live, and neither has a mirror.
POOL_STAGE = "pool"
FILTERED_STAGE = "filtered"

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
    """Read only the columns needed for stats computation, Parquet mirror first."""
    _STATS_COLS: dict[str, list[str]] = {
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


# ── Stage 2: the routing store ─────────────────────────────────────────────────
# One release's pile counts, straight out of DuckDB. This is the source Stage 3
# builds its worklist from, so the dashboard's Stage 2 figures and the works the
# extractor will actually see are the same fact, asked twice.
#
# Every failure here is a STATE the dashboard reports, not an error it hides: a
# checkout with no store, a store another process is rebuilding, a release whose
# sidecar record is missing. Each returns `available: False` with the reason, so
# the panel says which one it is instead of rendering a zero.


def compute_filtered_stats(store_path: "Path | None" = None) -> dict[str, Any]:
    """Pile counts for the newest routed release, read live from the store.

    *store_path* defaults to the engine's own store. The release is resolved the
    way `--release latest` resolves it — the newest `created_at` among the
    releases the store holds routing for — and its id is returned with the counts,
    because a pile count means nothing without the release it was counted under.
    """
    from filter.engine.store import (DEFAULT_STORE_PATH, StoreUnavailable,
                                     open_store, pile_counts, resolve_release)

    path = Path(store_path or DEFAULT_STORE_PATH)
    try:
        con = open_store(path, read_only=True)
    except StoreUnavailable as exc:
        return {"available": False, "reason": str(exc), "store": str(path)}
    try:
        # `resolve_release` exits the process on an unresolvable release, which is
        # right for a CLI and fatal in a web worker — so it is caught here and
        # turned back into a state.
        try:
            release_id = resolve_release(con, "latest", cache_dir=path.parent)
        except SystemExit as exc:
            return {"available": False, "reason": str(exc), "store": str(path)}
        piles = pile_counts(con, release_id)
    finally:
        con.close()

    created_at = ""
    try:
        from filter.engine.release import read_release
        created_at = str(read_release(release_id, cache_dir=path.parent)
                         .get("created_at") or "")
    except (FileNotFoundError, OSError, ValueError):
        pass

    return {
        "available": True,
        "store": str(path),
        "release_id": release_id,
        "release_created_at": created_at,
        "total": sum(piles.values()),
        "by_pile": piles,
    }


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
    if stage == FILTERED_STAGE:
        return compute_filtered_stats()
    if stage not in _STAGE_CSV:
        raise ValueError(f"Unknown stage: {stage!r}")
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

    The pool and the routing store have no CSV and no mirror, so refreshing
    either is stats-only. *pool_dir* is honoured for the pool stage only; see
    `compute_stage_stats` for why the writer, not the reader, names it.
    """
    if stage in (POOL_STAGE, FILTERED_STAGE):
        try:
            update_stats(stage, pool_dir=pool_dir)
        except Exception as exc:
            log.warning("dashboard_cache: update_stats failed for %s: %s", stage, exc)
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
