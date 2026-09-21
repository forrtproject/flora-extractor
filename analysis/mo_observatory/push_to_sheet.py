"""Append the agreed Observatory candidates to the FLoRA entry sheet, via `gws`.

The only thing in this directory that writes outside the repo. It appends rows to the
"replication list" tab of the FLoRA entry sheet and does nothing else — no update, no
delete, no formatting. Append is the one reversible Sheets write: undoing it is deleting
the rows it added, and it prints the exact range it wrote so that is possible.

**The live sheet is the contract, not the CSV export in `data/`.** That export has 20
columns; the live tab has 21 — an `id` column of literal UUIDs in front, which the
export drops. Writing the CSV as-is would put every value one column left of where it
belongs, silently. So the header is READ and checked before anything is written, and the
run aborts if it is not what this script was written against.

Duplicate protection is by `(doi_o, doi_r)` pair and by `doi_r` alone: the sheet is
keyed on the pair, but a replication already present under a different original is a
merge question for a human rather than a second row to add.

    .venv/bin/python -m analysis.mo_observatory.push_to_sheet            # dry run
    .venv/bin/python -m analysis.mo_observatory.push_to_sheet --apply
    .venv/bin/python -m analysis.mo_observatory.push_to_sheet --apply --limit 5

Needs `gws` authenticated; this box resolves credentials through
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE, which `_gws()` sets if it is not already set.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

csv.field_size_limit(10 ** 9)

HERE = Path(__file__).resolve().parent
ROWS = HERE / "flora_entry_rows.csv"

SPREADSHEET = "1yxdCTETI-xa1HwblWtmo9S8RszKe_9i14RBjXz0uTFE"
TAB = "replication list"
CREDS = Path.home() / ".config" / "gws" / "gws_creds.json"

# The live header, in order, as of 2026-09-21. `id` is ours to generate: the existing
# rows carry literal UUID4s, not a formula.
EXPECTED = ["id", "ref_o", "doi_o", "url_o", "ref_r", "doi_r", "url_r", "abstract_r",
            "outcome", "outcome_quote", "out_quote_source", "prep_notes",
            "quote validated", "year", "target_match_liberal", "Coder",
            "validation_status", "validator", "validator_notes",
            "alt_identifier_o", "alt_identifier_r"]

# Sheets appends up to 10 MB per call; these rows carry whole abstracts, so they go in
# batches both to stay well inside that and to fail small.
BATCH = 25


def _gws(*args: str, params: Optional[dict] = None, body: Optional[dict] = None) -> dict:
    env = dict(os.environ)
    env.setdefault("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", str(CREDS))
    cmd = ["gws", *args]
    if params is not None:
        cmd += ["--params", json.dumps(params)]
    if body is not None:
        cmd += ["--json", json.dumps(body)]
    done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    out = "\n".join(line for line in done.stdout.splitlines()
                    if not line.startswith("Using keyring"))
    if done.returncode != 0:
        raise SystemExit(f"gws failed: {done.stderr.strip() or out.strip()}")
    return json.loads(out) if out.strip() else {}


def _clean(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return re.sub(r"^doi:", "", value).strip().rstrip("/")


def live_header() -> list[str]:
    got = _gws("sheets", "spreadsheets", "values", "get",
               params={"spreadsheetId": SPREADSHEET, "range": f"{TAB}!A1:U1"})
    return (got.get("values") or [[]])[0]


def existing() -> tuple[set[tuple[str, str]], set[str], int]:
    """((doi_o, doi_r) pairs, doi_r values, data row count) currently in the tab."""
    got = _gws("sheets", "spreadsheets", "values", "get",
               params={"spreadsheetId": SPREADSHEET, "range": f"{TAB}!C1:F20000"})
    pairs, reps, count = set(), set(), 0
    for row in (got.get("values") or [])[1:]:
        doi_o = _clean(row[0] if len(row) > 0 else "")
        doi_r = _clean(row[3] if len(row) > 3 else "")
        if not (doi_o or doi_r):
            continue
        count += 1
        pairs.add((doi_o, doi_r))
        if doi_r:
            reps.add(doi_r)
    return pairs, reps, count


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="actually write")
    parser.add_argument("--limit", type=int, default=None,
                        help="append only the first N rows (a cautious first pass)")
    args = parser.parse_args(argv)

    header = live_header()
    if header != EXPECTED:
        raise SystemExit(
            "The sheet's header is not what this script was written against, so the "
            "columns it would write are not the columns it thinks.\n"
            f"  live:     {header}\n  expected: {EXPECTED}\n"
            "Nothing was written. Update EXPECTED after checking what moved.")
    print(f"header matches ({len(header)} columns)")

    pairs, reps, count = existing()
    print(f"sheet holds {count:,} data rows")

    with ROWS.open(newline="", encoding="utf-8-sig") as handle:
        mine = list(csv.DictReader(handle))
    fresh, skipped = [], []
    for row in mine:
        key = (_clean(row["doi_o"]), _clean(row["doi_r"]))
        if key in pairs or _clean(row["doi_r"]) in reps:
            skipped.append(row)
            continue
        fresh.append([str(uuid.uuid4())] + [row.get(c, "") for c in EXPECTED[1:]])
    print(f"to append: {len(fresh)}   already present: {len(skipped)}")
    if args.limit:
        fresh = fresh[:args.limit]
        print(f"  --limit {args.limit}: writing {len(fresh)}")
    if not fresh:
        print("Nothing to do.")
        return 0
    if not args.apply:
        print("\nDry run. --apply writes them.")
        return 0

    written = 0
    for start in range(0, len(fresh), BATCH):
        batch = fresh[start:start + BATCH]
        # RAW: an abstract beginning "=" or "+" must stay text, not become a formula.
        got = _gws("sheets", "spreadsheets", "values", "append",
                   params={"spreadsheetId": SPREADSHEET, "range": f"{TAB}!A1",
                           "valueInputOption": "RAW",
                           "insertDataOption": "INSERT_ROWS"},
                   body={"values": batch})
        updates = got.get("updates") or {}
        written += int(updates.get("updatedRows") or 0)
        print(f"  appended {updates.get('updatedRows')} row(s) -> "
              f"{updates.get('updatedRange')}")
    print(f"\nwrote {written} row(s) to '{TAB}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
