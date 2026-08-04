"""
Stage 1 entry point — the OpenAlex snapshot scan.

Stage 1 does snapshot scanning and nothing else. Every API-harvest source
(phrase search, concept search, Semantic Scholar, the discovery engine) has been
removed: they wrote into ``data/candidates.csv``, which nothing downstream reads.
Stage 2 reads the SURVIVOR POOL, so the pool is what a scan produces.

This module is a thin operator front-end over ``search/snapshot_scan.py`` — it
owns the flags a person types, the scan lives there. Two commands:

    python -m search.run_search --scan                    # full corpus scan
    python -m search.run_search --scan --snapshot-max-files 20
    python -m search.run_search --snapshot-pilot data/pilot.csv --snapshot-max-files 3

``--scan`` is required and deliberately not the default: a bare
``python -m search.run_search`` must never start a 400+ GB, 13-21 hour read.

Progress is reported by a separate read-only command that is safe to run against
a scan in flight: ``python -m search.snapshot_scan --status``.
"""

import argparse
from pathlib import Path

from shared.config import SNAPSHOT_POOL_DIR, log


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 1: scan the OpenAlex parquet snapshot into the survivor pool.",
        epilog="Progress: python -m search.snapshot_scan --status",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--scan",
        action="store_true",
        help="Run the ledger-backed production scan over the full corpus. Resumable: "
             "partitions already marked done in the ledger are skipped.",
    )
    mode.add_argument(
        "--snapshot-pilot",
        metavar="PATH",
        default=None,
        help="Pilot mode: scan a few partitions into PATH (a CSV), keep no ledger, and "
             "print the gate report. The only mode where --from-year/--to-year apply.",
    )
    parser.add_argument(
        "--survivor-pool",
        metavar="PATH",
        default=None,
        help=f"Directory the survivor pool is written to (default: {SNAPSHOT_POOL_DIR}). "
             "This is Stage 2's input — a scan that writes no pool produces nothing.",
    )
    parser.add_argument(
        "--snapshot-max-files",
        type=int,
        default=None,
        metavar="N",
        help="Scan at most N snapshot partitions this run, then stop. Omit for all of them.",
    )
    parser.add_argument(
        "--from-year",
        type=int,
        default=None,
        metavar="YYYY",
        help="Earliest publication year (pilot mode only).",
    )
    parser.add_argument(
        "--to-year",
        type=int,
        default=None,
        metavar="YYYY",
        help="Latest publication year (pilot mode only).",
    )
    args = parser.parse_args()

    from search.snapshot_scan import scan_snapshot

    pool = Path(args.survivor_pool) if args.survivor_pool else SNAPSHOT_POOL_DIR

    # The dashboard's Stage 1 panels read stats.json, not the pool: a request path
    # never scans column data. So the machine that HOLDS the pool is the only one
    # that can write those numbers, and it does so in `finally` — a Ctrl-C or a
    # crash 300 files in still leaves the dashboard showing what was scanned, the
    # same discipline sanity_check follows in Stage 3.
    try:
        if args.snapshot_pilot:
            log.info("Snapshot pilot: scanning into %s ...", args.snapshot_pilot)
            n_rows = scan_snapshot(
                pilot_csv=Path(args.snapshot_pilot),
                max_files=args.snapshot_max_files,
                from_year=args.from_year,
                to_year=args.to_year,
                survivor_pool=pool,
            )
            print(f"Snapshot pilot complete: {n_rows} rows written to {args.snapshot_pilot}")
        else:
            log.info("Stage 1: scanning the OpenAlex parquet snapshot into %s ...", pool)
            n_rows = scan_snapshot(
                max_files=args.snapshot_max_files,
                survivor_pool=pool,
            )
            log.info("Stage 1 complete: %d row(s) admitted into the survivor pool at %s",
                     n_rows, pool)
    finally:
        from shared.dashboard_cache import POOL_STAGE, refresh
        refresh(POOL_STAGE, pool_dir=pool)


if __name__ == "__main__":
    main()
