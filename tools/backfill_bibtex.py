#!/usr/bin/env python3
"""
backfill_bibtex.py — Add bibtex_ref_r and bibtex_ref_o to existing extracted CSVs.

For bibtex_ref_r: built from the r-side columns already in the CSV (doi_r, title_r,
authors_r, year_r, journal_r, url_r). No new API calls needed.

For bibtex_ref_o: built from ref_o metadata. For rows with a doi_o, calls
fetch_openalex_full_metadata (cached) to get full volume/issue/pages. For rows
without a doi_o (or where the API returns nothing), builds a minimal entry from
the title_o/year_o/authors_o columns already in the CSV.

Usage:
    python -m tools.backfill_bibtex                      # updates data/extracted.csv
    python -m tools.backfill_bibtex --extracted-test     # updates data/extracted-test.csv
    python -m tools.backfill_bibtex --dry-run            # preview without writing
    python -m tools.backfill_bibtex --only-missing       # skip rows that already have values
"""
import argparse
import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, log
from shared.dashboard_cache import refresh as _dashboard_refresh
from shared.utils import clean_doi
from extract.run_extract import build_bibtex, _build_bibtex_r, _build_ref_o


def _bibtex_o_from_row(row: pd.Series) -> str:
    """Build bibtex_ref_o from a row that may or may not have a doi_o."""
    doi_o = clean_doi(str(row.get("doi_o") or ""))
    if doi_o:
        try:
            _, _, bibtex = _build_ref_o(
                doi_o,
                str(row.get("authors_o") or ""),
                str(row.get("year_o")    or ""),
                str(row.get("title_o")   or ""),
            )
            if bibtex:
                return bibtex
        except Exception as exc:
            log.debug("[%s] bibtex_ref_o API failed: %s", doi_o, exc)

    # Fallback: build from columns already in the CSV
    authors_raw = str(row.get("authors_o") or "")
    authors = [a.strip() for a in authors_raw.split(";") if a.strip()]
    return build_bibtex(
        authors = authors,
        year    = str(row.get("year_o")    or ""),
        title   = str(row.get("title_o")   or ""),
        doi     = doi_o,
    )


def backfill_bibtex(
    csv_path: Path,
    dry_run: bool = False,
    only_missing: bool = False,
) -> dict:
    if not csv_path.exists():
        log.error("File not found: %s", csv_path)
        return {"error": "File not found"}

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Loaded %d rows from %s", len(df), csv_path.name)

    # Ensure columns exist
    if "bibtex_ref_r" not in df.columns:
        df["bibtex_ref_r"] = ""
    if "bibtex_ref_o" not in df.columns:
        df["bibtex_ref_o"] = ""

    if not dry_run:
        bak = csv_path.with_suffix(".csv.bak")
        shutil.copy2(csv_path, bak)
        log.info("Backup: %s", bak)

    updated_r = updated_o = 0

    for idx, row in df.iterrows():
        do_r = not only_missing or not str(row.get("bibtex_ref_r") or "").strip()
        do_o = not only_missing or not str(row.get("bibtex_ref_o") or "").strip()

        if do_r:
            bibtex_r = _build_bibtex_r(row)
            if not dry_run:
                df.at[idx, "bibtex_ref_r"] = bibtex_r
            updated_r += 1

        if do_o:
            bibtex_o = _bibtex_o_from_row(row)
            if not dry_run:
                df.at[idx, "bibtex_ref_o"] = bibtex_o
            updated_o += 1

    if not dry_run:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=csv_path.parent, prefix=".backfill_bibtex_tmp_", suffix=".csv"
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
        stage = "extracted-test" if "extracted-test" in str(csv_path) else "extracted"
        try:
            _dashboard_refresh(stage)
            log.info("Dashboard cache refreshed for stage=%s", stage)
        except Exception as exc:
            log.warning("Dashboard cache refresh failed: %s", exc)
    else:
        log.info("Dry run — no changes written")

    log.info("Done: %d bibtex_ref_r, %d bibtex_ref_o filled", updated_r, updated_o)
    return {"bibtex_ref_r_updated": updated_r, "bibtex_ref_o_updated": updated_o}


def main():
    parser = argparse.ArgumentParser(
        description="Backfill bibtex_ref_r and bibtex_ref_o columns in extracted CSVs."
    )
    parser.add_argument(
        "--extracted-test", action="store_true",
        help="Update data/extracted-test.csv instead of data/extracted.csv",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show counts without writing",
    )
    parser.add_argument(
        "--only-missing", action="store_true",
        help="Only fill rows where the bibtex column is blank (skip rows already populated)",
    )
    args = parser.parse_args()

    csv_path = DATA_DIR / ("extracted-test.csv" if args.extracted_test else "extracted.csv")
    result = backfill_bibtex(csv_path, dry_run=args.dry_run, only_missing=args.only_missing)
    print(result)


if __name__ == "__main__":
    main()
