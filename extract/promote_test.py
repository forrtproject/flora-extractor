"""
promote_test.py — Merge rows from extracted-test.csv into extracted.csv.

Usage:
    python -m extract.promote_test --all
    python -m extract.promote_test --doi 10.xxxx/yyyy
    python -m extract.promote_test --all --dry-run
    python -m extract.promote_test --all --force
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, log
from shared.schema import EXTRACTED_COLS
from shared.utils import clean_doi

_TEST_PATH = DATA_DIR / "extracted-test.csv"
_MAIN_PATH = DATA_DIR / "extracted.csv"


def promote_rows(
    dois: "list[str] | None" = None,
    all_rows: bool = False,
    dry_run: bool = False,
    force: bool = False,
    test_path: "Path | None" = None,
    main_path: "Path | None" = None,
) -> dict:
    """
    Merge rows from extracted-test.csv into extracted.csv.

    Returns {"promoted": N, "replaced": N, "skipped": N}.
    test_path / main_path override the module defaults (used in tests and the web API).
    """
    tp = Path(test_path) if test_path else _TEST_PATH
    mp = Path(main_path) if main_path else _MAIN_PATH

    if not tp.exists():
        raise FileNotFoundError(f"extracted-test.csv not found at {tp}")

    test_df = pd.read_csv(tp, dtype=str, encoding="utf-8-sig").fillna("")

    if dois is not None:
        cleaned = {clean_doi(d) for d in dois}
        test_df = test_df[test_df["doi_r"].apply(clean_doi).isin(cleaned)]
    elif not all_rows:
        raise ValueError("Specify dois or all_rows=True")

    if mp.exists():
        main_df = pd.read_csv(mp, dtype=str, encoding="utf-8-sig").fillna("")
    else:
        main_df = pd.DataFrame(columns=EXTRACTED_COLS)

    # Build lookup: doi → link_method for existing production rows
    main_by_doi: dict[str, str] = {
        clean_doi(str(r["doi_r"])): str(r.get("link_method", ""))
        for _, r in main_df.iterrows()
        if r.get("doi_r")
    }

    rows_to_write: list[tuple[str, str, dict]] = []  # (doi, action, row_dict)
    skipped = 0

    for _, test_row in test_df.iterrows():
        doi = clean_doi(str(test_row.get("doi_r", "") or ""))
        if not doi:
            continue

        existing_method = main_by_doi.get(doi)
        if existing_method is not None:
            if existing_method == "target_pending" or force:
                action = "replace"
            else:
                log.info(
                    "[%s] already resolved in extracted.csv — skipping (use --force to overwrite)",
                    doi,
                )
                skipped += 1
                continue
        else:
            action = "append"

        rows_to_write.append((doi, action, test_row.to_dict()))

        if dry_run:
            label = "[REPLACE]" if action == "replace" else "[APPEND ]"
            print(f"  {label} {doi}")

    if dry_run:
        replaced = sum(1 for _, a, _ in rows_to_write if a == "replace")
        promoted = sum(1 for _, a, _ in rows_to_write if a == "append")
        return {"promoted": promoted, "replaced": replaced, "skipped": skipped}

    if rows_to_write:
        replace_dois = {doi for doi, action, _ in rows_to_write if action == "replace"}
        if replace_dois:
            main_df = main_df[~main_df["doi_r"].apply(clean_doi).isin(replace_dois)]

        new_rows_df = pd.DataFrame([row for _, _, row in rows_to_write])
        for col in EXTRACTED_COLS:
            if col not in new_rows_df.columns:
                new_rows_df[col] = ""

        out_df = pd.concat([main_df, new_rows_df[EXTRACTED_COLS]], ignore_index=True)
        out_df.to_csv(mp, index=False, encoding="utf-8-sig")

    promoted = sum(1 for _, a, _ in rows_to_write if a == "append")
    replaced  = sum(1 for _, a, _ in rows_to_write if a == "replace")
    return {"promoted": promoted, "replaced": replaced, "skipped": skipped}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Promote rows from extracted-test.csv to extracted.csv"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true",
        help="Promote all rows in extracted-test.csv",
    )
    group.add_argument(
        "--doi", action="append", metavar="DOI",
        help="Promote a specific DOI (repeatable: --doi X --doi Y)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen; no file writes",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite even already-resolved rows in extracted.csv",
    )
    args = parser.parse_args()

    result = promote_rows(
        dois=args.doi,
        all_rows=args.all,
        dry_run=args.dry_run,
        force=args.force,
    )
    prefix = "(dry run) " if args.dry_run else ""
    print(
        f"{prefix}Done — promoted: {result['promoted']}, "
        f"replaced: {result['replaced']}, skipped: {result['skipped']}"
    )
