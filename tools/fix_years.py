#!/usr/bin/env python3
"""
fix_years.py — Retroactively correct year_o (and ref_o) in extracted CSVs.

Problem: CrossRef sometimes returns 'created' (DOI registration date) before
'published-print' for some publishers, causing year_o to show e.g. 2021 for a
paper published in 2022.

This script:
  1. Clears the OA doi_full cache for each doi_o present in the CSV.
  2. Re-fetches metadata via fetch_openalex_full_metadata (now using corrected
     year priority: published-print → published → published-online → issued → created).
  3. Updates year_o and ref_o columns in-place.

Usage:
    python -m tools.fix_years                          # fixes data/extracted.csv
    python -m tools.fix_years --extracted-test         # fixes data/extracted-test.csv
    python -m tools.fix_years --dry-run                # preview without writing
    python -m tools.fix_years --doi 10.xxx/y           # single doi_o only
"""
import argparse
import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, OA_CACHE_DIR, log
from shared.utils import cache_key, clean_doi


def _clear_oa_cache(doi_o: str) -> bool:
    cache_file = OA_CACHE_DIR / f"doi_full_{cache_key(doi_o)}.json"
    if cache_file.exists():
        cache_file.unlink()
        return True
    return False


def fix_years(
    csv_path: Path,
    dry_run: bool = False,
    doi_filter: "str | None" = None,
) -> dict:
    from extract.run_extract import _build_ref_o

    if not csv_path.exists():
        log.error("File not found: %s", csv_path)
        return {"error": "File not found"}

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Loaded %d rows from %s", len(df), csv_path.name)

    if not dry_run:
        bak = csv_path.with_suffix(".csv.bak")
        shutil.copy2(csv_path, bak)
        log.info("Backup: %s", bak)

    # Collect unique doi_o values to process
    mask = df["doi_o"].str.strip() != ""
    if doi_filter:
        target = clean_doi(doi_filter)
        mask = mask & (df["doi_o"].apply(clean_doi) == target)

    target_dois = df.loc[mask, "doi_o"].apply(clean_doi).unique().tolist()
    log.info("%d unique doi_o values to process", len(target_dois))

    # Step 1: clear OA cache for all targets
    cleared = sum(_clear_oa_cache(d) for d in target_dois if d)
    log.info("Cleared %d OA cache entries", cleared)

    # Step 2: re-fetch and update each matching row
    updated = 0
    errors = 0

    for doi_o in target_dois:
        if not doi_o:
            continue
        try:
            from shared.openalex_client import fetch_openalex_full_metadata as _oa_full_meta
            meta = _oa_full_meta(doi_o)
            if not meta:
                log.debug("[%s] no metadata returned — skipping", doi_o)
                continue

            new_year = str(meta.get("year") or "")
            new_ref, new_authors, new_bibtex = _build_ref_o(
                doi_o,
                "; ".join(meta.get("authors") or []),
                new_year,
                meta.get("title") or "",
            )

            row_mask = df["doi_o"].apply(clean_doi) == doi_o
            old_years = df.loc[row_mask, "year_o"].unique().tolist()

            if not dry_run:
                df.loc[row_mask, "year_o"]        = new_year
                df.loc[row_mask, "ref_o"]         = new_ref
                df.loc[row_mask, "authors_o"]     = new_authors
                df.loc[row_mask, "bibtex_ref_o"]  = new_bibtex

            changed = any(str(y) != new_year for y in old_years if str(y).strip())
            if changed or not old_years:
                log.info("[%s] year_o: %s → %s", doi_o,
                         "/".join(str(y) for y in old_years), new_year or "(empty)")
                updated += 1
            else:
                log.debug("[%s] year_o unchanged: %s", doi_o, new_year)

        except Exception as exc:
            log.error("[%s] error: %s", doi_o, exc)
            errors += 1

    if not dry_run and updated > 0:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=csv_path.parent, prefix=".fix_years_tmp_", suffix=".csv"
        )
        try:
            os.close(tmp_fd)
            df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
            os.replace(tmp_path, csv_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        log.info("Saved %d rows → %s", len(df), csv_path)
    elif dry_run:
        log.info("Dry run — no changes written")
    else:
        log.info("No year changes detected — file unchanged")

    log.info("Done: %d doi_o values updated, %d errors", updated, errors)
    return {"updated": updated, "errors": errors, "cleared_cache": cleared}


def main():
    parser = argparse.ArgumentParser(
        description="Retroactively fix year_o/ref_o in extracted CSVs using corrected CrossRef year priority."
    )
    parser.add_argument(
        "--extracted-test", action="store_true",
        help="Fix data/extracted-test.csv instead of data/extracted.csv",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change without writing",
    )
    parser.add_argument(
        "--doi", type=str, default=None, metavar="DOI_O",
        help="Fix only rows with this doi_o value",
    )
    args = parser.parse_args()

    csv_path = DATA_DIR / ("extracted-test.csv" if args.extracted_test else "extracted.csv")
    result = fix_years(csv_path, dry_run=args.dry_run, doi_filter=args.doi)
    print(result)


if __name__ == "__main__":
    main()
