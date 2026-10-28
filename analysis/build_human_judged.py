"""Materialise `data/human_judged.csv`: the works a person has judged in validation.

A strict admission gate defers a screen proceed (`filter/engine/handoff.py`), and a
deferred work stops rendering — which retires its validation record. A record a
validator or an admin has already judged is exempt: the judgment is the stronger
evidence about the work than two screen votes, and retiring it would discard the
work done on it. This file is that exemption list.

It is a snapshot, not a live read, for the reason `validated_skip.csv` is: the
extractor must not need the validation tables to write its own artifact, and the
list is reviewable in the diff. Unlike that list it GROWS, so re-run this before a
live `extract.export` whose deferrals matter. A record judged after the snapshot
is still safe on the validation side — `csv_to_db.py --retire` only flags, never
removes, a record anyone has touched — it just shows up as a flagged retirement.

"Judged" means a validator slot holds a submitted judgment, or the record has left
the plain queue for a status only a person sets (`validated`, `need_review`,
`consensus_reached`, `rejected`). `validation_inprogress` alone is not a judgment.

    .venv/bin/python -m analysis.build_human_judged            # dry run, the default
    .venv/bin/python -m analysis.build_human_judged --apply
"""

import argparse
import csv
import sys
from pathlib import Path

import shared.config  # noqa: F401  — loads .env before supabase_client reads it
from filter.engine.handoff import HUMAN_JUDGED_CSV
from filter.engine.workids import load_aliases, resolve, work_id
from shared.supabase_client import SUPABASE_URL, _get

COLUMNS = ["work_id", "record_id", "validation_status"]
JUDGED_STATUSES = frozenset({"validated", "need_review", "consensus_reached",
                             "rejected"})
_ALIASES = Path(__file__).resolve().parent.parent / "filter" / "spec" / "aliases.json"


def judged_rows(records: list[dict], aliases: dict[int, int]
                ) -> "tuple[list[dict], dict[str, int]]":
    """`(rows to write, counts)` from `unvalidated` rows; work ids alias-resolved."""
    rows: list[dict] = []
    counts = {"judged": 0, "no_work_id": 0}
    for record in records:
        slots = any(isinstance(record.get(k), dict) and record[k]
                    for k in ("validator_1", "validator_2"))
        status = str(record.get("validation_status") or "")
        if not slots and status not in JUDGED_STATUSES:
            continue
        counts["judged"] += 1
        try:
            wid = resolve(work_id(str(record.get("oa_work_id_r") or "")), aliases)
        except ValueError:
            counts["no_work_id"] += 1
            continue
        rows.append({"work_id": str(wid), "record_id": str(record.get("record_id")),
                     "validation_status": status})
    return sorted(rows, key=lambda r: (int(r["work_id"]), r["record_id"])), counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the file. Without it, nothing is touched.")
    parser.add_argument("--out", type=Path, default=HUMAN_JUDGED_CSV)
    args = parser.parse_args()

    if not SUPABASE_URL:
        raise SystemExit("SUPABASE_URL is unset — without the validation tables "
                         "there is nothing to build the list from.")
    rows, counts = judged_rows(
        _get("unvalidated", {"select": "record_id,oa_work_id_r,validation_status,"
                                       "validator_1,validator_2"}),
        load_aliases(_ALIASES))
    print(f"judged record(s): {counts['judged']:,}  (no work id, unmatchable: "
          f"{counts['no_work_id']:,})")
    print(f"distinct work id(s): {len({r['work_id'] for r in rows}):,}")
    if not args.apply:
        print(f"\ndry run — nothing written. Re-run with --apply to write {args.out}.")
        return 0
    with args.out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nwrote {len(rows):,} row(s) -> {args.out}")
    print("Commit it: the admission gate reads this file, not Supabase.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
