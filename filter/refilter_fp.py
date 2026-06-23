"""
refilter_fp.py — Re-classify false_positive and needs_review rows in filtered.csv
using the expanded phrase set in phrase_detection.py.

Rows already classified as replication/reproduction are left untouched.
Only FP and needs_review rows are re-evaluated:
  - If the new phrases now fire AND an author-year cite is found → replication/reproduction
  - If the new phrases fire but no author-year cite → needs_review → LLM decides
  - If no new phrase fires → stays false_positive (no change written)

Results are written back into filtered.csv in-place (rows updated, order preserved).
A summary CSV is saved to data/refilter_fp_changes.csv.

Usage
-----
    python -m filter.refilter_fp                    # process all FP + needs_review
    python -m filter.refilter_fp --dry-run          # show counts, write nothing
    python -m filter.refilter_fp --limit 500        # process only first 500 eligible rows
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, log
from shared import token_counter
from filter.rule_filter import classify_row as _rule_classify
from filter.llm_filter import classify_with_llm as _llm_classify


ELIGIBLE_STATUSES = {"false_positive", "needs_review"}


def run_refilter(dry_run: bool = False, limit: int | None = None) -> None:
    filtered_path = DATA_DIR / "filtered.csv"
    changes_path  = DATA_DIR / "refilter_fp_changes.csv"

    if not filtered_path.exists():
        sys.exit(f"ERROR: {filtered_path} not found. Run filter.run_filter first.")

    # ------------------------------------------------------------------
    # Load filtered.csv
    # ------------------------------------------------------------------
    log.info("Loading filtered.csv...")
    df = pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig", low_memory=False)
    df = df.fillna("")
    log.info("Loaded %d rows.", len(df))

    eligible_mask = df["filter_status"].isin(ELIGIBLE_STATUSES)
    eligible_df   = df[eligible_mask].copy()
    log.info("Eligible rows (FP + needs_review): %d", len(eligible_df))

    if limit:
        eligible_df = eligible_df.head(limit)
        log.info("--limit %d applied: processing %d rows", limit, len(eligible_df))

    if eligible_df.empty:
        log.info("No eligible rows to reprocess. Exiting.")
        return

    # ------------------------------------------------------------------
    # Re-classify each eligible row
    # ------------------------------------------------------------------
    changes: list[dict] = []
    n_reclassified = 0
    n_llm_called   = 0
    n_unchanged    = 0

    for iloc_pos, (idx, row) in enumerate(eligible_df.iterrows(), 1):
        if iloc_pos % 5000 == 0:
            log.info("  Progress: %d / %d  (reclassified so far: %d)",
                     iloc_pos, len(eligible_df), n_reclassified)

        row_dict = row.to_dict()
        old_status = row_dict.get("filter_status", "")

        # Re-run rule classifier (now uses expanded REPLICATION_PHRASES)
        new_verdict = _rule_classify(row_dict)
        new_status  = new_verdict["filter_status"]

        # LLM uplift for rows the rule filter now flags as uncertain
        if new_status == "needs_review":
            title    = str(row_dict.get("title_r",    "") or "")
            abstract = str(row_dict.get("abstract_r", "") or "")
            llm_verdict = _llm_classify(title, abstract)
            n_llm_called += 1
            if llm_verdict:
                new_status                  = llm_verdict["filter_status"]
                new_verdict["filter_status"]     = new_status
                new_verdict["filter_confidence"] = llm_verdict["filter_confidence"]
                prior = str(row_dict.get("filter_evidence") or "")
                new_verdict["filter_evidence"] = (
                    f"{prior} | llm:{llm_verdict['filter_evidence']}" if prior
                    else f"llm:{llm_verdict['filter_evidence']}"
                )
                new_verdict["filter_method"] = "both" if "rule" in str(
                    row_dict.get("filter_method", "")) else "llm"

        if new_status == old_status:
            n_unchanged += 1
            continue

        # Record the change
        n_reclassified += 1
        changes.append({
            "idx":                idx,
            "doi_r":              row_dict.get("doi_r", ""),
            "title_r":            row_dict.get("title_r", ""),
            "year_r":             row_dict.get("year_r", ""),
            "old_filter_status":  old_status,
            "new_filter_status":  new_status,
            "new_filter_evidence": new_verdict.get("filter_evidence", ""),
            "new_filter_confidence": new_verdict.get("filter_confidence", ""),
            "new_filter_method":  new_verdict.get("filter_method", ""),
        })

        if not dry_run:
            df.at[idx, "filter_status"]     = new_verdict["filter_status"]
            df.at[idx, "filter_confidence"] = new_verdict["filter_confidence"]
            df.at[idx, "filter_evidence"]   = new_verdict["filter_evidence"]
            df.at[idx, "filter_method"]     = new_verdict["filter_method"]

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    changes_df = pd.DataFrame(changes)

    log.info("=" * 60)
    log.info("REFILTER SUMMARY")
    log.info("=" * 60)
    log.info("Eligible rows processed: %d", len(eligible_df))
    log.info("Unchanged:               %d", n_unchanged)
    log.info("Reclassified:            %d", n_reclassified)
    log.info("LLM calls made:          %d", n_llm_called)

    if not changes_df.empty:
        breakdown = (
            changes_df.groupby(["old_filter_status", "new_filter_status"])
            .size()
            .reset_index(name="count")
        )
        log.info("Change breakdown:")
        for _, br in breakdown.iterrows():
            log.info("  %s → %s : %d",
                     br["old_filter_status"], br["new_filter_status"], br["count"])

    if dry_run:
        log.info("DRY RUN — no files written.")
    else:
        # Write updated filtered.csv (overwrite in-place)
        df.to_csv(filtered_path, index=False, encoding="utf-8-sig")
        log.info("filtered.csv updated in-place.")

        # Save change log
        if not changes_df.empty:
            changes_df.to_csv(changes_path, index=False, encoding="utf-8-sig")
            log.info("Change log → %s  (%d rows)", changes_path.name, len(changes_df))

    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Re-classify FP/needs_review rows in filtered.csv with expanded phrases."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing anything.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Process only the first N eligible rows (for testing).",
    )
    args = parser.parse_args()

    try:
        run_refilter(dry_run=args.dry_run, limit=args.limit)
    finally:
        token_counter.print_summary()
