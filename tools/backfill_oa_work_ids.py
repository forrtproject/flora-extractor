"""
backfill_oa_work_ids.py — Populate oa_work_id_r / oa_work_id_o on an existing extracted.csv.

Issue #69 added two bare OpenAlex work-ID columns to the Stage 3 schema. New rows get
them for free from run_extract._fill_work_ids, but rows written before that need a
one-off backfill. This is that backfill.

The r-side is free for almost every row: Stage 1 already stores openalex_id_r as
"https://openalex.org/W..." and this just strips it to "W...". Only rows missing that,
plus every o-side, cost an API call — and fetch_openalex_by_doi caches per DOI, so a
re-run after an interruption is nearly free.

Dry-run by default, like tools/migrate_link_methods.py — pass --apply to write.

Usage:
    python -m tools.backfill_oa_work_ids                                   # dry-run
    python -m tools.backfill_oa_work_ids --apply                           # write
    python -m tools.backfill_oa_work_ids --input data/extracted-test.csv --apply
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from shared.openalex_client import fetch_openalex_by_doi
from shared.schema import EXTRACTED_COLS
from shared.utils import bare_work_id, clean_doi, csv_lock

_COLS = ("oa_work_id_r", "oa_work_id_o")


def _lookup(doi: str) -> str:
    """Bare OpenAlex work ID for *doi*, or "" if the DOI is blank or unindexed."""
    doi = clean_doi(doi)
    if not doi:
        return ""
    return bare_work_id((fetch_openalex_by_doi(doi) or {}).get("openalex_id", ""))


def backfill_file(csv_path: Path, apply: bool = False, limit: int | None = None) -> dict:
    """Fill the two work-ID columns in *csv_path*.

    Returns counts of what was filled. With apply=False nothing is written, but the
    lookups still run (and populate the cache), so the dry-run reports real numbers
    rather than an estimate.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found")

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    for col in _COLS:
        if col not in df.columns:
            df[col] = ""

    stats = {"total": len(df), "r_from_url": 0, "r_from_api": 0,
             "o_from_api": 0, "r_missing": 0, "o_missing": 0}

    rows = df.index if limit is None else df.index[:limit]
    for n, i in enumerate(rows, 1):
        if not df.at[i, "oa_work_id_r"]:
            wid = bare_work_id(df.at[i, "openalex_id_r"])
            if wid:
                stats["r_from_url"] += 1
            else:
                wid = _lookup(df.at[i, "doi_r"])
                stats["r_from_api" if wid else "r_missing"] += 1
            df.at[i, "oa_work_id_r"] = wid

        if not df.at[i, "oa_work_id_o"]:
            wid = _lookup(df.at[i, "doi_o"])
            stats["o_from_api" if wid else "o_missing"] += 1
            df.at[i, "oa_work_id_o"] = wid

        if n % 100 == 0:
            print(f"  {n}/{len(rows)} rows …", flush=True)

    print(f"\n{csv_path}: {stats['total']} rows")
    print(f"  oa_work_id_r: {stats['r_from_url']} from openalex_id_r, "
          f"{stats['r_from_api']} via API, {stats['r_missing']} unresolved")
    print(f"  oa_work_id_o: {stats['o_from_api']} via API, {stats['o_missing']} unresolved")

    if not apply:
        print("[dry-run] nothing written (pass --apply to write)")
        return {**stats, "written": False}

    # Same lock run_extract's appender takes — a backfill during a streaming run would
    # otherwise drop every row appended between this read and this write.
    ordered = [c for c in EXTRACTED_COLS if c in df.columns]
    df = df[ordered + [c for c in df.columns if c not in ordered]]
    with csv_lock(csv_path):
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"Wrote {stats['total']} rows → {csv_path}")
    return {**stats, "written": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill oa_work_id_r / oa_work_id_o on an extracted CSV.")
    parser.add_argument("--input", type=Path, default=Path("data/extracted.csv"),
                        help="CSV to backfill (default: data/extracted.csv)")
    parser.add_argument("--apply", action="store_true",
                        help="Write the result in place. Omit for a dry-run.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N rows (for a quick check).")
    args = parser.parse_args()

    backfill_file(args.input, apply=args.apply, limit=args.limit)
