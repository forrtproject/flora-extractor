#!/usr/bin/env python3
"""
Recalibrate outcomes for extracted.csv using the improved classification logic.

This script:
1. Reads extracted.csv
2. For each row, re-runs outcome extraction with the new logic
3. Updates outcome columns in place
4. Writes back to extracted.csv

Optional: clear the outcome cache first to force fresh LLM calls.
"""
import argparse
import json
import time
from pathlib import Path
import pandas as pd

from extract.code_outcome import extract_outcome
from shared.config import DATA_DIR, LLM_CACHE_DIR, log
from shared.utils import cache_key, clean_doi


def clear_outcome_cache():
    """Delete all cached outcome extraction results."""
    outcome_files = list(LLM_CACHE_DIR.glob("outcome_*.json"))
    if not outcome_files:
        log.info("No outcome cache files found")
        return 0

    for f in outcome_files:
        try:
            f.unlink()
        except Exception as e:
            log.warning("Failed to delete %s: %s", f.name, e)

    deleted = len(outcome_files)
    log.info("Deleted %d cached outcome files", deleted)
    return deleted


def recalibrate_outcomes(
    input_csv: Path,
    output_csv: Path = None,
    clear_cache: bool = False,
    dry_run: bool = False,
    limit: int = None,
    only_uncertain: bool = True,
) -> dict:
    """
    Re-run outcome extraction for uncertain rows in a CSV.

    Parameters
    ----------
    input_csv : Path
        Path to extracted.csv (or similar structure)
    output_csv : Path, optional
        Where to write results. If None, overwrites input_csv.
    clear_cache : bool
        If True, delete cached outcomes for uncertain rows (forces fresh LLM calls).
    dry_run : bool
        If True, preview changes without writing.
    limit : int, optional
        Process only first N uncertain rows (for testing).
    only_uncertain : bool, default True
        If True, only process rows with outcome="cannot_be_determined" or no outcome.
        If False, reprocess all rows.

    Returns
    -------
    dict
        Statistics: rows_processed, rows_updated, cache_cleared, errors.
    """
    if output_csv is None:
        output_csv = input_csv

    log.info("=" * 70)
    log.info("Recalibrating Outcomes")
    log.info("=" * 70)
    log.info("Input:  %s", input_csv)
    log.info("Output: %s", output_csv if not dry_run else f"{output_csv} (DRY RUN)")
    log.info("Clear cache: %s", clear_cache)
    log.info("Limit: %s rows", limit or "unlimited")

    # Read input CSV
    if not input_csv.exists():
        log.error("File not found: %s", input_csv)
        return {"error": "File not found"}

    df = pd.read_csv(input_csv, encoding="utf-8-sig", low_memory=False)
    log.info("Loaded %d rows from %s", len(df), input_csv.name)

    # Check required columns
    required_cols = ["doi_r", "title_r", "abstract_r"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error("Missing columns: %s", missing)
        return {"error": f"Missing columns: {missing}"}

    # Filter to uncertain outcomes if requested
    if only_uncertain:
        uncertain_mask = (
            (df["outcome"].isna()) |
            (df["outcome"].astype(str).str.strip() == "") |
            (df["outcome"].astype(str).str.lower() == "cannot_be_determined") |
            (df["outcome"].astype(str).str.lower() == "pending")
        )
        df_to_process = df[uncertain_mask].reset_index(drop=True)
        df_unchanged = df[~uncertain_mask]
        log.info(
            "Filtering to uncertain outcomes only: %d uncertain, %d with good outcomes",
            len(df_to_process), len(df_unchanged)
        )
    else:
        df_to_process = df
        df_unchanged = None

    # Clear cache if requested (only for uncertain rows to be processed)
    cache_cleared = 0
    if clear_cache and only_uncertain:
        # Clear cache files for rows we're about to process
        for doi in df_to_process.get("doi_r", []):
            doi_clean = clean_doi(str(doi))
            if doi_clean:
                cache_file = LLM_CACHE_DIR / f"outcome_{cache_key(doi_clean)}.json"
                if cache_file.exists():
                    try:
                        cache_file.unlink()
                        cache_cleared += 1
                    except Exception as e:
                        log.warning("Failed to delete %s: %s", cache_file.name, e)
        log.info("Cleared %d outcome cache files for uncertain rows", cache_cleared)
    elif clear_cache and not only_uncertain:
        cache_cleared = clear_outcome_cache()

    # Process rows
    rows_processed = 0
    rows_updated = 0
    errors = []
    updated_rows = []

    # Subset if limit provided
    if limit:
        df_process = df_to_process.head(limit)
    else:
        df_process = df_to_process

    for idx, row in df_process.iterrows():
        rows_processed += 1
        doi_r = clean_doi(str(row.get("doi_r", "")))
        title_r = str(row.get("title_r", ""))
        abstract_r = str(row.get("abstract_r", ""))

        # Skip if missing critical fields
        if not abstract_r or not abstract_r.strip():
            log.debug("[%s] Skipping — no abstract", doi_r)
            continue

        log.info(
            "[%d/%d] [%s] Extracting outcome: %s...",
            idx + 1,
            len(df_process),
            doi_r,
            title_r[:60],
        )

        try:
            # Get original title/authors/year if available (for context)
            original_title = str(row.get("title_o", ""))
            original_authors = str(row.get("authors_o", ""))
            original_year = str(row.get("year_o", ""))

            # Call outcome extraction with improved logic
            result = extract_outcome(
                doi_r=doi_r,
                title_r=title_r,
                abstract_r=abstract_r,
                fulltext="",  # Not using fulltext for this recalibration
                original_title=original_title if original_title and original_title != "nan" else "",
                original_authors=original_authors if original_authors and original_authors != "nan" else "",
                original_year=original_year if original_year and original_year != "nan" else "",
            )

            # Check if outcome changed
            old_outcome = str(row.get("outcome", ""))
            new_outcome = result.get("outcome", "")
            changed = old_outcome != new_outcome

            if changed:
                rows_updated += 1
                log.info(
                    "  [%s] Outcome: %s → %s (confidence: %s)",
                    doi_r,
                    old_outcome or "(empty)",
                    new_outcome,
                    result.get("outcome_confidence", "unknown"),
                )
            else:
                log.debug("  [%s] Outcome unchanged: %s", doi_r, new_outcome)

            # Update row
            row_copy = row.copy()
            row_copy["outcome"] = result.get("outcome", "")
            row_copy["outcome_phrase"] = result.get("outcome_phrase", "")
            row_copy["outcome_confidence"] = result.get("outcome_confidence", "")
            row_copy["out_quote_source"] = result.get("out_quote_source", "")
            row_copy["outcome_reasoning"] = result.get("outcome_reasoning", "")
            updated_rows.append(row_copy)

        except Exception as e:
            log.error("[%s] Error: %s", doi_r, str(e))
            errors.append({"doi": doi_r, "error": str(e)})
            # Keep original row on error
            updated_rows.append(row)

        # Rate limiting
        time.sleep(0.5)

    # Reconstruct dataframe
    if updated_rows:
        df_updated = pd.DataFrame(updated_rows)
        # Ensure we have all original columns
        for col in df.columns:
            if col not in df_updated.columns:
                df_updated[col] = df[col]
        df_updated = df_updated[df.columns]  # Reorder to original column order

        # Append unchanged rows (those with good outcomes)
        if only_uncertain and df_unchanged is not None and len(df_unchanged) > 0:
            df_updated = pd.concat([df_updated, df_unchanged], ignore_index=True)

        # Append rows that weren't processed due to limit
        if limit and len(df_to_process) > limit:
            df_unprocessed = df_to_process.iloc[limit:]
            if only_uncertain:
                df_unprocessed = pd.concat([df_unprocessed, df_unchanged], ignore_index=True) if df_unchanged is not None else df_unprocessed
            df_updated = pd.concat([df_updated, df_unprocessed], ignore_index=True)
    else:
        df_updated = df.copy()

    # Write output
    if not dry_run:
        df_updated.to_csv(output_csv, index=False, encoding="utf-8-sig")
        log.info("Wrote %d rows to %s", len(df_updated), output_csv)
    else:
        log.info("DRY RUN: would write %d rows to %s", len(df_updated), output_csv)

    log.info("=" * 70)
    log.info("SUMMARY:")
    log.info("  Rows processed:  %d", rows_processed)
    log.info("  Rows updated:    %d", rows_updated)
    log.info("  Cache cleared:   %d files", cache_cleared)
    log.info("  Errors:          %d", len(errors))
    log.info("=" * 70)

    if errors:
        log.warning("\nFirst 5 errors:")
        for err in errors[:5]:
            log.warning("  [%s] %s", err["doi"], err["error"])

    return {
        "rows_processed": rows_processed,
        "rows_updated": rows_updated,
        "cache_cleared": cache_cleared,
        "errors": len(errors),
        "output_file": str(output_csv) if not dry_run else None,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Recalibrate outcomes for extracted.csv using improved classification logic."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DATA_DIR / "extracted.csv",
        help="Input CSV file (default: data/extracted.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV file (default: overwrites input)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete cached outcomes first (forces fresh LLM calls)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N uncertain rows (for testing)",
    )
    parser.add_argument(
        "--reprocess-all",
        action="store_true",
        help="Reprocess ALL rows (not just uncertain ones). Default: only uncertain rows.",
    )
    args = parser.parse_args()

    result = recalibrate_outcomes(
        input_csv=args.input,
        output_csv=args.output,
        clear_cache=args.clear_cache,
        dry_run=args.dry_run,
        limit=args.limit,
        only_uncertain=not args.reprocess_all,
    )

    return result


if __name__ == "__main__":
    result = main()
    print("\n" + json.dumps(result, indent=2))
