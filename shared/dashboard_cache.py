"""
shared/dashboard_cache.py — Parquet mirror + stats.json for dashboard fast reads.

Each pipeline runner calls refresh(stage) at the end of its run (and in its
finally block so partial progress is saved on Ctrl-C).  The dashboard API
endpoints check for Parquet / stats.json before falling back to CSV reads.

Public API
----------
  write_parquet(stage)   read stage CSV → write data/dashboard/{stage}.parquet
  update_stats(stage)    recompute counts from Parquet → update stats.json
  refresh(stage)         write_parquet + update_stats (normal call site)
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

from shared.config import DATA_DIR

log = logging.getLogger("flora.dashboard_cache")

DASHBOARD_DIR   = DATA_DIR / "dashboard"
STATS_JSON_PATH = DASHBOARD_DIR / "stats.json"

_STAGE_CSV: dict[str, Path] = {
    "candidates":     DATA_DIR / "candidates.csv",
    "filtered":       DATA_DIR / "filtered.csv",
    "extracted":      DATA_DIR / "extracted.csv",
    "extracted-test": DATA_DIR / "extracted-test.csv",
}

_OUTCOME_KEYS = (
    "success", "failure", "mixed", "uninformative",
    "cannot_be_determined", "descriptive", "pending", "api_error",
)
_METHOD_KEYS = (
    "author_year_match", "llm_abstract", "llm_fulltext",
    "no_original_found", "target_pending", "api_error",
)


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


def _compute_candidates_stats(df: pd.DataFrame) -> dict[str, Any]:
    doi_col  = df["doi_r"].fillna("")   if "doi_r"      in df.columns else pd.Series([""] * len(df))
    url_col  = df["url_r"].fillna("")   if "url_r"      in df.columns else pd.Series([""] * len(df))
    abs_col  = df["abstract_r"].fillna("") if "abstract_r" in df.columns else pd.Series([""] * len(df))
    src_col  = df["source"].fillna("") if "source"     in df.columns else pd.Series([""] * len(df))
    no_doi   = int((doi_col == "").sum())
    return {
        "total":           len(df),
        "no_doi":          no_doi,
        "no_doi_or_url":   int(((doi_col == "") & (url_col == "")).sum()),
        "no_abstract":     int((abs_col == "").sum()),
        "by_source":       _vc(src_col),
    }


def _compute_filtered_stats(df: pd.DataFrame) -> dict[str, Any]:
    by_status = _vc(df["filter_status"]) if "filter_status" in df.columns else {}

    # Data quality for replications + reproductions only
    rep_mask = pd.Series(False, index=df.index)
    if "filter_status" in df.columns:
        rep_mask = df["filter_status"].isin(["replication", "reproduction"])
    rr = df[rep_mask]
    doi_col = rr["doi_r"].fillna("") if "doi_r" in rr.columns else pd.Series([""] * len(rr))
    url_col = rr["url_r"].fillna("") if "url_r" in rr.columns else pd.Series([""] * len(rr))
    abs_col = rr["abstract_r"].fillna("") if "abstract_r" in rr.columns else pd.Series([""] * len(rr))

    return {
        "total":                  len(df),
        "by_filter_status":       by_status,
        "by_filter_method":       _vc(df["filter_method"])      if "filter_method"      in df.columns else {},
        "by_filter_confidence":   _vc(df["filter_confidence"])  if "filter_confidence"  in df.columns else {},
        "rep_repro_total":        int(rep_mask.sum()),
        "rep_repro_no_doi":       int((doi_col == "").sum()),
        "rep_repro_no_doi_or_url":int(((doi_col == "") & (url_col == "")).sum()),
        "rep_repro_no_abstract":  int((abs_col == "").sum()),
    }


def _compute_extracted_stats(df: pd.DataFrame) -> dict[str, Any]:
    lm_col  = df["link_method"].fillna("")       if "link_method"         in df.columns else pd.Series([""] * len(df))
    mt_col  = df["original_match_type"].fillna("") if "original_match_type" in df.columns else pd.Series([""] * len(df))
    oc_col  = df["outcome"].fillna("")           if "outcome"             in df.columns else pd.Series([""] * len(df))
    dv_col  = df["doi_o_verification"].fillna("") if "doi_o_verification"  in df.columns else pd.Series([""] * len(df))
    mod_col = df["link_llm_model"].fillna("").apply(_model_family) if "link_llm_model" in df.columns else pd.Series(["none"] * len(df))
    return {
        "total":                  len(df),
        "target_pending_count":   int((lm_col == "target_pending").sum()),
        "by_match_type":          _vc(mt_col),
        "by_link_method":         _vc(lm_col, _METHOD_KEYS),
        "by_model":               _vc(mod_col),
        "by_outcome":             _vc(oc_col, _OUTCOME_KEYS),
        "by_doi_verification":    _vc(dv_col),
    }


def _compute_stage_stats(stage: str, df: pd.DataFrame) -> dict[str, Any]:
    if stage == "candidates":
        return _compute_candidates_stats(df)
    if stage == "filtered":
        return _compute_filtered_stats(df)
    return _compute_extracted_stats(df)


def _read_for_stats(stage: str) -> "pd.DataFrame | None":
    """Read only the columns needed for stats computation.

    For extracted/extracted-test (small files) loads the whole table at once.
    candidates and filtered are potentially millions of rows — callers should
    prefer _compute_large_stage_stats instead and only use this for small stages.
    """
    _STATS_COLS: dict[str, list[str]] = {
        "candidates":     ["doi_r", "url_r", "abstract_r", "source"],
        "filtered":       ["doi_r", "url_r", "abstract_r",
                           "filter_status", "filter_method", "filter_confidence"],
        "extracted":      ["link_method", "link_llm_model", "original_match_type",
                           "outcome", "doi_o_verification"],
        "extracted-test": ["link_method", "link_llm_model", "original_match_type",
                           "outcome", "doi_o_verification"],
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
    """Compute stats for large stages (candidates, filtered) without loading
    the full DataFrame into memory.

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

    # ── Candidates ─────────────────────────────────────────────────────────
    if stage == "candidates":
        total = no_doi = no_doi_or_url = no_abstract = 0
        src_counts: dict[str, int] = {}

        def _process_cand_chunk(chunk: pd.DataFrame) -> None:
            nonlocal total, no_doi, no_doi_or_url, no_abstract
            chunk = chunk.fillna("")
            total          += len(chunk)
            doi_c           = chunk["doi_r"]      if "doi_r"      in chunk.columns else pd.Series([""] * len(chunk))
            url_c           = chunk["url_r"]      if "url_r"      in chunk.columns else pd.Series([""] * len(chunk))
            abs_c           = chunk["abstract_r"] if "abstract_r" in chunk.columns else pd.Series([""] * len(chunk))
            src_c           = chunk["source"]     if "source"     in chunk.columns else pd.Series([""] * len(chunk))
            no_doi          += int((doi_c == "").sum())
            no_doi_or_url   += int(((doi_c == "") & (url_c == "")).sum())
            no_abstract     += int((abs_c == "").sum())
            for k, v in src_c.value_counts().items():
                src_counts[str(k)] = src_counts.get(str(k), 0) + int(v)

        try:
            if pq_path.exists():
                cols = ["doi_r", "url_r", "abstract_r", "source"]
                pf = pq.ParquetFile(pq_path)
                existing = pf.schema_arrow.names
                read_cols = [c for c in cols if c in existing]
                for batch in pf.iter_batches(batch_size=100_000, columns=read_cols):
                    _process_cand_chunk(batch.to_pandas())
            else:
                for chunk in pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str,
                                         chunksize=100_000, on_bad_lines="skip",
                                         usecols=lambda c: c in ("doi_r","url_r","abstract_r","source")):
                    _process_cand_chunk(chunk)
        except Exception as exc:
            log.warning("dashboard_cache: chunked candidates read failed: %s", exc)
            return None

        return {
            "total": total, "no_doi": no_doi,
            "no_doi_or_url": no_doi_or_url, "no_abstract": no_abstract,
            "by_source": src_counts,
        }

    # ── Filtered ────────────────────────────────────────────────────────────
    if stage == "filtered":
        total = 0
        status_counts: dict[str, int] = {}
        method_counts: dict[str, int] = {}
        conf_counts:   dict[str, int] = {}

        # Pass 1: lightweight columns only — get all counts except data quality
        _light_cols = ("filter_status", "filter_method", "filter_confidence")

        def _process_filt_chunk(chunk: pd.DataFrame) -> None:
            nonlocal total
            chunk = chunk.fillna("")
            total += len(chunk)
            for k, v in chunk.get("filter_status", pd.Series(dtype=str)).value_counts().items():
                status_counts[str(k)] = status_counts.get(str(k), 0) + int(v)
            for k, v in chunk.get("filter_method", pd.Series(dtype=str)).value_counts().items():
                method_counts[str(k)] = method_counts.get(str(k), 0) + int(v)
            for k, v in chunk.get("filter_confidence", pd.Series(dtype=str)).value_counts().items():
                conf_counts[str(k)] = conf_counts.get(str(k), 0) + int(v)

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
        _dq_cols = ("doi_r", "url_r", "abstract_r", "filter_status")
        try:
            if pq_path.exists() and "filter_status" in pq.read_schema(pq_path).names:
                import pyarrow.compute as pc
                pf = pq.ParquetFile(pq_path)
                existing = pf.schema_arrow.names
                read_cols = [c for c in _dq_cols if c in existing]
                filters = [("filter_status", "in", ["replication", "reproduction"])]
                rr = pq.read_table(pq_path, columns=read_cols, filters=filters).to_pandas().fillna("")
            else:
                rr_chunks = []
                for chunk in pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str,
                                         chunksize=100_000, on_bad_lines="skip",
                                         usecols=lambda c: c in _dq_cols):
                    sub = chunk[chunk["filter_status"].isin(["replication","reproduction"])]
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
            "by_filter_status":        status_counts,
            "by_filter_method":        method_counts,
            "by_filter_confidence":    conf_counts,
            "rep_repro_total":         rep_repro_total,
            "rep_repro_no_doi":        rr_no_doi,
            "rep_repro_no_doi_or_url": rr_no_doi_or_url,
            "rep_repro_no_abstract":   rr_no_abstract,
        }

    return None  # not a large stage


def update_stats(stage: str) -> None:
    """Recompute counts for stage and merge into stats.json."""
    if stage not in _STAGE_CSV:
        raise ValueError(f"Unknown stage: {stage!r}")

    # candidates and filtered are too large to load fully into RAM
    if stage in ("candidates", "filtered"):
        new_stats = _compute_large_stage_stats(stage)
        if new_stats is None:
            log.warning("dashboard_cache: no data to compute stats for %s", stage)
            return
    else:
        df = _read_for_stats(stage)
        if df is None:
            log.warning("dashboard_cache: no data to compute stats for %s", stage)
            return
        new_stats = _compute_stage_stats(stage, df)

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

def refresh(stage: str) -> None:
    """Write Parquet mirror then update stats.json for this stage."""
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
