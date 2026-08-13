"""Drop the recorded Europe PMC retry delay from every acquisition retry log.

The Europe PMC tier built its PDF URL on `backend/ptpmcrender.fcgi`, which now
breaks the HTTP/2 stream instead of serving the file. Every row that reached the
tier recorded a failure for it, and a recorded failure suppresses the tier for
PDF_RETRY_AFTER_DAYS (14). The URL is fixed and the tier now tries the JATS full
text first, so leaving those stamps in place would hold the fixed tier shut for two
more weeks on rows that have no document at all.

The record is one file per row — `cache/pdfs/retry_<md5 of doi_r or url_r>.json` —
holding `{tier: iso timestamp}` for every tier that came back empty. So the purge is
per-KEY, not per-file: the `europepmc` entry is removed and the other tiers' stamps
are left alone. A file left with no entries is deleted, because an empty log and an
absent one mean the same thing to `_read_retry_log()`.

`cache/openalex_xml/retry_*.json` is a different log (slot `content_free`, for the
OpenAlex XML tier) and is not touched.

Dry run by default. `--apply` writes.

    .venv/bin/python -m analysis.purge_epmc_retries
    .venv/bin/python -m analysis.purge_epmc_retries --apply
"""

import argparse
import json
import sys
from pathlib import Path

from shared.config import PDF_CACHE_DIR

TIER = "europepmc"


def logs(pdf_dir: Path) -> "list[tuple[Path, dict]]":
    """Every readable `retry_*.json` in *pdf_dir*, with its entries.

    An unreadable log is skipped rather than deleted: `_read_retry_log()` already
    degrades it to "probe everything", which is the outcome this run is after.
    """
    found = []
    for path in sorted(pdf_dir.glob("retry_*.json")):
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"  ! unreadable, skipped: {path.name} ({exc})")
            continue
        if isinstance(entries, dict):
            found.append((path, entries))
    return found


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analysis.purge_epmc_retries",
        description=f"Remove the {TIER} entry from every PDF acquisition retry log, "
                    "so the fixed tier is re-probed on the next run.")
    parser.add_argument("--apply", action="store_true",
                        help="Write. Without it, print what would change.")
    parser.add_argument("--pdf-dir", type=Path, default=PDF_CACHE_DIR)
    parser.add_argument("--verbose", action="store_true",
                        help="Print every file, not just the counts.")
    args = parser.parse_args(argv)

    records = logs(args.pdf_dir)
    print(f"{len(records):,} retry log(s) in {args.pdf_dir}")

    carrying = 0
    emptied = 0
    for path, entries in records:
        if TIER not in entries:
            continue
        carrying += 1
        remaining = {k: v for k, v in entries.items() if k != TIER}
        if not remaining:
            emptied += 1
        if args.verbose:
            verb = "delete" if not remaining else "rewrite"
            print(f"  {verb if args.apply else 'would ' + verb} {path.name} "
                  f"({len(remaining)} other tier(s) kept)")
        if not args.apply:
            continue
        try:
            if remaining:
                path.write_text(json.dumps(remaining, ensure_ascii=False),
                                encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"  ! write failed: {path} ({exc})")

    verb = "purged" if args.apply else "would purge"
    print(f"  {verb}: {carrying:,} log(s) carrying a {TIER} stamp "
          f"— {emptied:,} of them held nothing else and are removed whole")
    if not args.apply:
        print("\n  dry run — nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
