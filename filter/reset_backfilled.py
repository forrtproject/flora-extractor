"""
reset_backfilled.py — Reset Stage 2 screening decisions for abstract-backfilled rows.

Roughly 494k candidates were screened by Stage 2 with an EMPTY abstract (title-only
decisions). An abstract backfill (search/fetch_abstracts.py) is now filling those
abstracts into candidates.csv. But Stage 2's resume index (cache/filtered_index.txt)
makes run_filter skip any row already written to filtered.csv, so the recovered
abstracts never trigger a re-screen. This tool deletes exactly the filtered.csv rows
that (a) were decided with an empty abstract AND (b) now have an abstract in
candidates.csv, then rebuilds the resume index. After that, a normal
`python -m filter.run_filter` reprocesses those rows — this time with the abstract.

Why delete the row rather than just drop its key from the index? run_filter APPENDS
new decisions to filtered.csv; it never rewrites in place. If we removed only the
resume key, the row would be reprocessed and a SECOND decision appended, leaving two
rows for the same paper. Deleting the filtered.csv row (and rebuilding the index from
the surviving rows) keeps exactly one decision per paper.

Three streamed passes, memory-bounded (both CSVs are multi-GB — neither is ever loaded
whole; only resume keys, never abstract text, are held in memory):
  Pass A: scan filtered.csv → S = resume keys of rows with an empty abstract_r.
  Pass B: scan candidates.csv → R = keys in S whose candidates row now has an abstract.
  Pass C (--apply only): rewrite filtered.csv dropping rows whose key ∈ R, atomically
          replace, then rebuild cache/filtered_index.txt from the survivors.

Dry-run by default; pass --apply to write.

WARNING: Do NOT run this while `python -m filter.run_filter` or
`search/fetch_abstracts.py` is writing to filtered.csv / candidates.csv. It rewrites
filtered.csv in place and rebuilds the resume index; a concurrent writer will corrupt
the result.

Usage:
    python -m filter.reset_backfilled            # dry-run report
    python -m filter.reset_backfilled --apply     # rewrite filtered.csv + rebuild index
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, log
from filter.run_filter import (
    _FILTERED_INDEX_PATH,
    _build_filtered_index,
    _row_key,
)

_CHUNK = 50_000


def collect_empty_abstract_keys(filtered_path: Path) -> tuple[set[str], int]:
    """Pass A. Stream filtered.csv; return (S, total_rows_scanned).

    S = resume keys of rows whose abstract_r is blank. Rows with an empty _row_key
    (identifier-less; their resume key is an idx:<pos> fallback that this tool never
    touches) are skipped — they can neither be matched to candidates nor backfilled.
    """
    keys: set[str] = set()
    total = 0
    for chunk in pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig",
                             chunksize=_CHUNK, low_memory=False):
        chunk = chunk.fillna("")
        total += len(chunk)
        for _, row in chunk.iterrows():
            if str(row.get("abstract_r", "")).strip():
                continue
            key = _row_key(row)
            if key:
                keys.add(key)
    return keys, total


def collect_reset_keys(candidates_path: Path, empty_keys: set[str]) -> set[str]:
    """Pass B. Stream candidates.csv; return R = keys in *empty_keys* whose candidates
    row now carries a non-empty abstract_r."""
    reset: set[str] = set()
    for chunk in pd.read_csv(candidates_path, dtype=str, encoding="utf-8-sig",
                             chunksize=_CHUNK, low_memory=False):
        chunk = chunk.fillna("")
        for _, row in chunk.iterrows():
            if not str(row.get("abstract_r", "")).strip():
                continue
            key = _row_key(row)
            if key in empty_keys:
                reset.add(key)
    return reset


def _rewrite_dropping(filtered_path: Path, reset_keys: set[str]) -> int:
    """Pass C write step. Stream filtered.csv → tmp, dropping rows whose key ∈
    *reset_keys*, then atomically replace filtered.csv. Returns rows dropped.

    Column order is taken from the header (pandas preserves it); the header is written
    utf-8-sig (BOM, Excel), subsequent appends utf-8 to avoid embedding a BOM mid-file.
    """
    tmp_path = filtered_path.with_suffix(".reset.tmp")
    dropped = 0
    first_write = True
    for chunk in pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig",
                             chunksize=_CHUNK, low_memory=False):
        chunk = chunk.fillna("")
        keep_mask: list[bool] = []
        for _, row in chunk.iterrows():
            drop = _row_key(row) in reset_keys
            keep_mask.append(not drop)
            if drop:
                dropped += 1
        kept = chunk[keep_mask]
        if not kept.empty:
            kept.to_csv(
                tmp_path,
                mode="w" if first_write else "a",
                index=False,
                encoding="utf-8-sig" if first_write else "utf-8",
                header=first_write,
                quoting=1,
                quotechar='"',
            )
            first_write = False

    if first_write:
        # Every row was dropped (or file was empty apart from header) — still need a
        # valid filtered.csv with just the header.
        pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig", nrows=0).to_csv(
            tmp_path, index=False, encoding="utf-8-sig")
    os.replace(tmp_path, filtered_path)
    return dropped


def reset_backfilled(apply: bool = False) -> dict:
    """Orchestrate the three passes. Returns a summary dict."""
    log.warning(
        "reset_backfilled: do NOT run while filter.run_filter or fetch_abstracts is "
        "writing — it rewrites filtered.csv and rebuilds the resume index."
    )

    filtered_path = DATA_DIR / "filtered.csv"
    candidates_path = DATA_DIR / "candidates.csv"

    if not filtered_path.exists():
        raise FileNotFoundError(
            f"{filtered_path} not found. If it is still zipped as "
            f"{DATA_DIR / 'filtered.zip'}, unzip it first."
        )
    if not candidates_path.exists():
        raise FileNotFoundError(
            f"{candidates_path} not found. If it is still zipped as "
            f"{DATA_DIR / 'candidates.zip'}, unzip it first."
        )

    empty_keys, total_scanned = collect_empty_abstract_keys(filtered_path)
    log.info("Pass A: %d filtered rows scanned, %d with empty abstract",
             total_scanned, len(empty_keys))

    reset_keys = collect_reset_keys(candidates_path, empty_keys)
    log.info("Pass B: %d empty-abstract rows now have a backfilled abstract",
             len(reset_keys))

    summary = {
        "filtered_rows_scanned": total_scanned,
        "empty_abstract_rows": len(empty_keys),
        "backfilled_rows": len(reset_keys),
        "would_drop": len(reset_keys),
        "applied": apply,
    }

    if apply:
        index_before = len(_read_index_lines())
        dropped = _rewrite_dropping(filtered_path, reset_keys)
        log.info("Pass C: dropped %d rows from filtered.csv — rebuilding index", dropped)
        new_index = _build_filtered_index(filtered_path)
        summary["rows_dropped"] = dropped
        summary["index_before"] = index_before
        summary["index_after"] = len(new_index)

    return summary


def _read_index_lines() -> list[str]:
    if not _FILTERED_INDEX_PATH.exists():
        return []
    with open(_FILTERED_INDEX_PATH, "r", encoding="utf-8") as f:
        return [ln for ln in (line.strip() for line in f) if ln]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Reset Stage 2 screening for rows whose abstract was backfilled."
    )
    ap.add_argument("--apply", action="store_true",
                    help="rewrite filtered.csv and rebuild the index (default: dry-run)")
    args = ap.parse_args()

    summary = reset_backfilled(apply=args.apply)

    print(f"\nReset backfilled screening"
          f"{' (APPLIED)' if args.apply else ' (dry-run)'}:")
    print(f"  filtered rows scanned      {summary['filtered_rows_scanned']}")
    print(f"  rows with empty abstract   {summary['empty_abstract_rows']}")
    print(f"  now backfilled (reset set) {summary['backfilled_rows']}")
    print(f"  would-drop                 {summary['would_drop']}")
    if args.apply:
        print(f"  rows dropped               {summary['rows_dropped']}")
        print(f"  index size before → after  {summary['index_before']} → {summary['index_after']}")
        print("\nDone. Next step: run `python -m filter.run_filter` to re-screen the "
              "reset rows (now with abstracts).")
    else:
        print("\nDry-run only — no files changed. Rerun with --apply to rewrite "
              "filtered.csv, then run `python -m filter.run_filter`.")


if __name__ == "__main__":
    main()
