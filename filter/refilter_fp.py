"""
refilter_fp.py — Re-classify false_positive and needs_review rows in filtered.csv
using the expanded phrase set in phrase_detection.py.

Strategy
--------
1. Load filtered.csv and identify FP + needs_review rows.
2. Pre-filter with vectorized regex to only the rows where the NEW added phrases
   now fire (and no exclusion pattern fires) — typically ~10-18k rows.
3. Skip rows already processed (checkpoint index in cache/refilter_fp_index.txt).
4. Run the rule classifier on only those rows.
5. Call the LLM only for rows that become needs_review after the rule pass.
6. Write each change into filtered.csv immediately and append to the checkpoint
   index — so the run is fully resumable if interrupted.
7. Save a cumulative change log to data/refilter_fp_changes.csv.

Resumability
------------
Interrupt and restart anytime. The checkpoint index records every DOI processed
(whether changed or not). On restart the pre-filter runs again (fast, vectorized)
and skips anything already in the index. Progress is never lost.

Usage
-----
    python -m filter.refilter_fp                 # process all eligible rows
    python -m filter.refilter_fp --dry-run       # show pre-filter count only, no LLM/writes
    python -m filter.refilter_fp --limit 200     # process first 200 eligible rows (testing)
    python -m filter.refilter_fp --reset         # clear checkpoint and start fresh
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

from shared.config import CACHE_DIR, DATA_DIR, log
from shared import token_counter
from shared.dashboard_cache import _parquet_path, refresh as _dc_refresh
from filter.rule_filter import classify_row as _rule_classify
from filter.llm_filter import classify_with_llm as _llm_classify


# ---------------------------------------------------------------------------
# Added phrases — vectorized combined regex for pre-filtering
# ---------------------------------------------------------------------------
_ADDED_PHRASE_STRINGS: list[str] = [
    r"\battempt\w*\s+to\s+replicate\b",
    r"\baim\w*\s+to\s+replicate\b",
    r"\bset\s+out\s+to\s+replicate\b",
    r"\bsuccess\w*\s+replicat\w*\b",
    r"\bwe\s+(?:conducted|performed|carried\s+out)\s+a\s+replication\b",
    r"\b(?:many-?labs?|multi-?site)\s+replication\b",
    r"\breplicat\w*\s+and\s+exten\w*\b",
    r"\breplication\s+stud(?:y|ies)\s+of\b",
    r"\bstudy\s+replicate[sd]\b",
    r"\bour\s+replication\b",
    r"\bindependent\s+replication\b",
    r"\bexact\s+replication\b",
    r"\breplication\s+attempt\b",
    r"\bcross-?(?:cultural|national|lab(?:oratory)?)\s+replication\b",
]
_ADDED_COMBINED = "(?:" + "|".join(_ADDED_PHRASE_STRINGS) + ")"

_EXCLUSION_COMBINED = (
    r"(?:"
    r"\b(?:dna|rna|viral|virus|cell|cellular|chromosome|plasmid)\s+replication\b"
    r"|"
    r"\b(?:replication of (?:the )?(?:apparatus|code|dataset|data|database|model|method|"
    r"pipeline|protocol|software|simulation)|(?:apparatus|code|dataset|data|database|model|"
    r"method|pipeline|protocol|software|simulation)\s+replication)\b"
    r"|"
    r"\breplicat(?:e|ed|ing)\s+(?:the )?(?:apparatus|code|dataset|data|database|model|"
    r"method|pipeline|protocol|software|simulation)\b"
    r"|"
    r"\breplication\s+(?:fork|origin|stress|timing)\b"
    r")"
)

ELIGIBLE_STATUSES = {"false_positive", "needs_review"}
_CHECKPOINT_PATH  = CACHE_DIR / "refilter_fp_index.txt"
_CHANGES_PATH     = DATA_DIR  / "refilter_fp_changes.csv"


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> set[str]:
    if not _CHECKPOINT_PATH.exists():
        return set()
    with open(_CHECKPOINT_PATH, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def _append_checkpoint(doi: str) -> None:
    with open(_CHECKPOINT_PATH, "a", encoding="utf-8") as f:
        f.write(doi + "\n")


def _append_change_log(row: dict) -> None:
    row_df = pd.DataFrame([row])
    write_header = not _CHANGES_PATH.exists()
    row_df.to_csv(_CHANGES_PATH, mode="a", index=False,
                  encoding="utf-8-sig", header=write_header)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_refilter(dry_run: bool = False, limit: int | None = None) -> None:
    filtered_path = DATA_DIR / "filtered.csv"

    if not filtered_path.exists():
        sys.exit(f"ERROR: {filtered_path} not found. Run filter.run_filter first.")

    # ------------------------------------------------------------------
    # Load filtered data — Parquet if available (faster + less RAM), else CSV
    # ------------------------------------------------------------------
    pq_path = _parquet_path("filtered")
    if pq_path.exists():
        try:
            import pyarrow.parquet as pq
            log.info("Loading from Parquet: %s", pq_path)
            df = pq.read_table(pq_path).to_pandas().fillna("")
        except Exception as exc:
            log.warning("Parquet read failed (%s) — falling back to CSV", exc)
            df = pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig", low_memory=False).fillna("")
    else:
        log.info("Loading filtered.csv (no Parquet found)...")
        df = pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig", low_memory=False).fillna("")
    log.info("Loaded %d total rows.", len(df))

    eligible_mask = df["filter_status"].isin(ELIGIBLE_STATUSES)
    log.info("Eligible (FP + needs_review): %d rows", eligible_mask.sum())

    # ------------------------------------------------------------------
    # Step 1: vectorized pre-filter
    # ------------------------------------------------------------------
    log.info("Pre-filtering with vectorized regex (added phrases only)...")
    eligible_df = df[eligible_mask].copy()

    text = (eligible_df["title_r"] + " " + eligible_df["abstract_r"]).str.strip()
    has_abstract = eligible_df["abstract_r"].str.strip().astype(bool)
    added_fires  = text.str.contains(_ADDED_COMBINED,    flags=re.IGNORECASE, regex=True, na=False)
    excl_fires   = text.str.contains(_EXCLUSION_COMBINED, flags=re.IGNORECASE, regex=True, na=False)

    candidates = eligible_df[has_abstract & added_fires & ~excl_fires].copy()
    log.info("Rows passing pre-filter: %d", len(candidates))

    if dry_run:
        log.info("DRY RUN — stopping here (no LLM calls, no writes).")
        log.info("Re-run without --dry-run to process these rows.")
        return

    # ------------------------------------------------------------------
    # Step 2: skip already-processed rows (checkpoint)
    # ------------------------------------------------------------------
    done = _load_checkpoint()
    if done:
        log.info("Checkpoint loaded: %d rows already processed — skipping.", len(done))

    candidates = candidates[~candidates["doi_r"].isin(done)]
    log.info("Rows remaining after checkpoint skip: %d", len(candidates))

    if limit:
        candidates = candidates.head(limit)
        log.info("--limit %d applied: processing %d rows", limit, len(candidates))

    if candidates.empty:
        log.info("Nothing left to process. All done.")
        return

    # ------------------------------------------------------------------
    # Step 3: rule classifier + LLM, writing changes immediately
    # ------------------------------------------------------------------
    n_reclassified = 0
    n_llm_called   = 0
    n_unchanged    = 0

    for i, (idx, row) in enumerate(candidates.iterrows(), 1):
        if i % 500 == 0:
            log.info("  Progress: %d / %d  (reclassified: %d, LLM calls: %d)",
                     i, len(candidates), n_reclassified, n_llm_called)

        row_dict   = row.to_dict()
        old_status = row_dict.get("filter_status", "")
        doi        = str(row_dict.get("doi_r", "") or f"idx:{idx}")

        new_verdict = _rule_classify(row_dict)
        new_status  = new_verdict["filter_status"]

        if new_status == "needs_review":
            title    = str(row_dict.get("title_r",    "") or "")
            abstract = str(row_dict.get("abstract_r", "") or "")
            llm_verdict = _llm_classify(title, abstract)
            n_llm_called += 1
            if llm_verdict:
                new_status                       = llm_verdict["filter_status"]
                new_verdict["filter_status"]     = new_status
                new_verdict["filter_confidence"] = llm_verdict["filter_confidence"]
                prior = str(row_dict.get("filter_evidence") or "")
                new_verdict["filter_evidence"]   = (
                    f"{prior} | llm:{llm_verdict['filter_evidence']}" if prior
                    else f"llm:{llm_verdict['filter_evidence']}"
                )
                new_verdict["filter_method"] = (
                    "both" if "rule" in str(row_dict.get("filter_method", "")) else "llm"
                )

        # Mark as processed in checkpoint regardless of whether status changed
        _append_checkpoint(doi)

        if new_status == old_status:
            n_unchanged += 1
            continue

        # Write change into in-memory df
        n_reclassified += 1
        df.at[idx, "filter_status"]     = new_verdict["filter_status"]
        df.at[idx, "filter_confidence"] = new_verdict["filter_confidence"]
        df.at[idx, "filter_evidence"]   = new_verdict["filter_evidence"]
        df.at[idx, "filter_method"]     = new_verdict["filter_method"]

        # Append to change log immediately (survives interruption)
        _append_change_log({
            "doi_r":                 doi,
            "title_r":               row_dict.get("title_r", ""),
            "year_r":                row_dict.get("year_r",  ""),
            "old_filter_status":     old_status,
            "new_filter_status":     new_status,
            "new_filter_evidence":   new_verdict.get("filter_evidence",   ""),
            "new_filter_confidence": new_verdict.get("filter_confidence", ""),
            "new_filter_method":     new_verdict.get("filter_method",     ""),
        })

        # Flush filtered.csv every 100 reclassified rows to survive interruption
        if n_reclassified % 100 == 0:
            df.to_csv(filtered_path, index=False, encoding="utf-8-sig")
            log.info("  Flushed filtered.csv (%d changes so far)", n_reclassified)

    # Final flush to CSV, then rebuild Parquet mirror
    df.to_csv(filtered_path, index=False, encoding="utf-8-sig")
    _dc_refresh("filtered")

    log.info("=" * 60)
    log.info("REFILTER COMPLETE")
    log.info("=" * 60)
    log.info("Rows processed:   %d", len(candidates))
    log.info("Unchanged:        %d", n_unchanged)
    log.info("Reclassified:     %d", n_reclassified)
    log.info("LLM calls made:   %d", n_llm_called)
    if _CHANGES_PATH.exists():
        log.info("Change log:       %s", _CHANGES_PATH)
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-classify FP/needs_review rows with expanded phrases. Fully resumable."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show pre-filter count only — no LLM calls, no writes.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process only the first N unprocessed rows (for testing).")
    parser.add_argument("--reset", action="store_true",
                        help="Clear the checkpoint index and start from scratch.")
    args = parser.parse_args()

    if args.reset:
        if _CHECKPOINT_PATH.exists():
            _CHECKPOINT_PATH.unlink()
            print(f"Checkpoint cleared: {_CHECKPOINT_PATH}")
        else:
            print("No checkpoint to clear.")
        sys.exit(0)

    try:
        run_refilter(dry_run=args.dry_run, limit=args.limit)
    finally:
        token_counter.print_summary()
