"""Materialise `data/validated_skip.csv` from the Supabase validation tables.

`record_metadata` holds ~1,770 records seeded from the prior FLoRA pipeline. Those
works have been through validation already and must never reach Stage 3 again. The
set is frozen — everything validated from now on flows through this pipeline and is
held out by extracted.csv's resume keys — so the query runs ONCE, here, and the
answer is committed as a static CSV that Stage 3 reads on every run
(`shared/flora_skip.load_validated_skip()`). Stage 3 therefore needs no Supabase
credentials to know what it has already done, and the skip list is reviewable in
the diff rather than changing under a run.

It lives in `analysis/` rather than under a `scratch_` name because the artifact is
git-tracked: if the frozen set is ever re-cut, this is the tool that has to be
re-run, and it must still be there.

    python -m analysis.build_validated_skip            # dry run, the default
    python -m analysis.build_validated_skip --apply
"""

import argparse
import csv
import sys
from pathlib import Path

import shared.config  # noqa: F401  — loads .env before supabase_client reads it
from shared.config import DATA_DIR
from shared.flora_skip import VALIDATED_SKIP_NAME, validated_work_id
from shared.supabase_client import SUPABASE_URL, _get
from shared.utils import clean_doi

COLUMNS = ["work_id", "doi", "record_id"]


def skip_rows(records: list[dict],
              dois_by_record: dict[str, str]) -> "tuple[list[dict], dict[str, int]]":
    """`(rows to write, counts)` from `record_metadata` rows.

    The DOI is not in `record_metadata` — it lives on the `unvalidated` row of the
    same `record_id`, which is why *dois_by_record* is passed in. Both identifiers
    are written whenever they exist: a record with a work id can still match a
    Stage 3 row whose `openalex_id_r` was never resolved.

    Every record is written, including the ones that identify no work at all: the
    file is then a faithful picture of the legacy set, and the counts say honestly
    how much of it can actually be matched.
    """
    rows: list[dict] = []
    counts = {"total": len(records), "with_work_id": 0, "doi_only": 0, "neither": 0}
    for record in records:
        record_id = str(record.get("record_id") or "")
        wid = validated_work_id(record.get("work_id"))
        doi = clean_doi(str(dois_by_record.get(record_id) or ""))
        if wid is not None:
            counts["with_work_id"] += 1
        elif doi:
            counts["doi_only"] += 1
        else:
            counts["neither"] += 1
        rows.append({"work_id": "" if wid is None else str(wid),
                     "doi": doi,
                     "record_id": record_id})
    return rows, counts


def write_skip_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the file. Without it, nothing is touched.")
    parser.add_argument("--dry-run", action="store_true",
                        help="The default; accepted so it can be said out loud.")
    parser.add_argument("--out", type=Path, default=DATA_DIR / VALIDATED_SKIP_NAME)
    args = parser.parse_args()

    if not SUPABASE_URL:
        raise SystemExit("SUPABASE_URL is unset — without the validation tables "
                         "there is nothing to build the skip list from.")
    records = _get("record_metadata", {"select": "record_id,work_id"})
    dois_by_record = {str(r.get("record_id") or ""): str(r.get("doi_r") or "")
                      for r in _get("unvalidated", {"select": "record_id,doi_r"})}
    rows, counts = skip_rows(records, dois_by_record)

    print(f"record_metadata: {counts['total']:,} record(s)")
    print(f"  with a work id:     {counts['with_work_id']:,}")
    print(f"  DOI only:           {counts['doi_only']:,}")
    print(f"  neither:            {counts['neither']:,}  "
          "— these identify no work and can never be matched, so they will not "
          "skip anything")
    print(f"distinct work id(s): {len({r['work_id'] for r in rows} - {''}):,}, "
          f"distinct DOI(s): {len({r['doi'] for r in rows} - {''}):,}")

    if not args.apply:
        print(f"\ndry run — nothing written. Re-run with --apply to write {args.out}.")
        return 0

    write_skip_csv(args.out, rows)
    print(f"\nwrote {len(rows):,} row(s) -> {args.out}")
    print("Commit it: Stage 3 reads this file, not Supabase.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
