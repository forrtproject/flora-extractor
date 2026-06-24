"""
backfill_authors.py — Retroactively update authors_o and ref_o in extracted.csv.

Fetches full author lists and APA-style references from OpenAlex for every row
that has a doi_o.  All OpenAlex responses are cached so re-runs are fast.

Usage:
    python -m extract.backfill_authors                   # dry-run: print changes
    python -m extract.backfill_authors --apply           # write changes to extracted.csv
    python -m extract.backfill_authors --extracted-test  # target extracted-test.csv
    python -m extract.backfill_authors --doi 10.xxx/y   # single doi_o only
"""
from __future__ import annotations

import argparse

import pandas as pd

from shared.config import DATA_DIR, log
from shared.utils import clean_doi
from extract.run_extract import _build_ref_o


def backfill(csv_path, apply: bool = False, target_doi: str = "") -> None:
    if not csv_path.exists():
        log.error("%s does not exist", csv_path)
        return

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Loaded %d rows from %s", len(df), csv_path.name)

    changes: list[tuple] = []

    for idx, row in df.iterrows():
        doi_o   = clean_doi(str(row.get("doi_o",   "") or ""))
        title_o = str(row.get("title_o", "") or "").strip()

        # Skip rows with neither a DOI nor a title to search by
        if not doi_o and not title_o:
            continue
        # Skip the one no_original_found row (no title, nothing to look up)
        if row.get("link_method") == "no_original_found":
            continue
        if target_doi and clean_doi(target_doi) != doi_o:
            continue

        old_authors = str(row.get("authors_o", "") or "")
        old_ref     = str(row.get("ref_o",     "") or "")
        fallback_author = old_authors.split(";")[0].strip() if old_authors else ""

        try:
            new_ref, new_authors = _build_ref_o(
                doi_o, fallback_author,
                str(row.get("year_o", "") or ""),
                title_o,
            )
        except Exception as exc:
            log.warning("[%s] backfill failed: %s", doi_o, exc)
            continue

        if new_authors != old_authors or new_ref != old_ref:
            changes.append((idx, new_authors, new_ref, old_authors, old_ref, doi_o))
            if apply:
                df.at[idx, "authors_o"] = new_authors
                df.at[idx, "ref_o"]     = new_ref

    print(f"\nBackfill: {len(changes)} rows changed out of {len(df)} total")
    shown = changes[:20]
    for idx, new_auth, new_ref, old_auth, old_ref, doi_o in shown:
        print(f"\n  doi_o    : {doi_o}")
        print(f"  authors_o: {old_auth!r}")
        print(f"           -> {new_auth!r}")
        print(f"  ref_o    : {old_ref!r}")
        print(f"           -> {new_ref[:120]!r}")
    if len(changes) > 20:
        print(f"\n  … and {len(changes) - 20} more rows")

    if apply and changes:
        df.to_csv(csv_path, index=False, encoding="utf-8-sig",
                  quoting=1, quotechar='"')
        print(f"\nWrote {len(changes)} updated rows to {csv_path}")
        log.info("backfill_authors: wrote %d rows to %s", len(changes), csv_path)
    elif not apply:
        print("\nDry-run — no changes written. Pass --apply to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill authors_o and ref_o in extracted.csv with full OpenAlex data."
    )
    parser.add_argument("--apply",          action="store_true",
                        help="Write changes (default: dry-run).")
    parser.add_argument("--extracted-test", action="store_true",
                        help="Target extracted-test.csv instead of extracted.csv.")
    parser.add_argument("--doi",            type=str, default="",
                        help="Only update rows with this doi_o.")
    args = parser.parse_args()

    path = DATA_DIR / ("extracted-test.csv" if args.extracted_test else "extracted.csv")
    backfill(path, apply=args.apply, target_doi=args.doi)
