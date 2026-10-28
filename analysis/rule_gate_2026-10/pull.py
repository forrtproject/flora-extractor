"""Freeze the inputs for the rule-gate measurement: routing, screen votes, endings,
validator judgments.

Read-only: the routing store is opened read_only, Supabase (the engine tables and the
validation `unvalidated` table) is only read, and the exported CSVs are read where
`--data` points. Validator names and notes are not kept: one row per work, with how
many validators judged its type and whether any or all said `not_validation`. Writes one parquet per input under
`out/`, so `measure.py` can be re-run offline.

    .venv/bin/python -m analysis.rule_gate_2026-10.pull --store <engine.duckdb> \
        --data <dir holding extracted.csv and the set-asides> --release <id>
"""

import argparse
import csv
import json
from pathlib import Path

import duckdb
import pandas as pd

from filter.engine.claims import ClaimsClient
from filter.engine.handoff import decisions
from filter.engine.workids import load_aliases, resolve, work_id
from shared import supabase_client

OUT = Path(__file__).parent / "out"
# Each file's rows end the work the way the file is named; extracted.csv ships.
ENDING_FILES = ("extracted", "not_a_replication", "no_original_found",
                "prospective_registration", "no_evidence", "unidentified_original",
                "keyed_link_disputed", "search_link_unconfirmed", "target_pending",
                "api_error")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--release", required=True)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    con = duckdb.connect(args.store, read_only=True)
    release = args.release
    routing = con.execute(
        "SELECT work_id, pile, rule_id, matched_rules FROM routing "
        "WHERE release_id LIKE ? || '%' AND pile IN ('screen_expensive', 'screen_cheap')",
        [release]).df()
    routing["matched_rules"] = routing["matched_rules"].map(list).map(json.dumps)
    routing.to_parquet(OUT / "routing.parquet")

    drop, screen = decisions(ClaimsClient())
    rows = []
    for work, d in screen.items():
        rows.append({"work_id": work, "outcome": d["outcome"],
                     "record_type": d.get("record_type", ""),
                     "votes": json.dumps([{k: v.get(k) for k in
                                           ("model", "classification", "confident")}
                                          for v in d.get("votes") or []])})
    pd.DataFrame(rows).to_parquet(OUT / "screen.parquet")

    aliases = load_aliases(Path("filter/spec/aliases.json"))
    endings = []
    for name in ENDING_FILES:
        path = Path(args.data) / f"{name}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    wid = resolve(work_id(str(row.get("openalex_id_r") or "")), aliases)
                except ValueError:
                    continue
                endings.append({"work_id": wid, "file": name,
                                "outcome": row.get("outcome", ""),
                                "link_method": row.get("link_method", "")})
    pd.DataFrame(endings).to_parquet(OUT / "endings.parquet")

    judged = []
    for rec in supabase_client._get("unvalidated", {
            "select": "oa_work_id_r,validator_1,validator_2"}):
        slots = [v for v in (rec.get("validator_1"), rec.get("validator_2"))
                 if isinstance(v, dict) and v.get("type_check") in ("correct", "incorrect")]
        try:
            wid = resolve(work_id(str(rec.get("oa_work_id_r") or "")), aliases)
        except ValueError:
            continue
        if slots:
            nar = [v["type_check"] == "incorrect"
                   and v.get("corrected_type") == "not_validation" for v in slots]
            judged.append({"work_id": wid, "judged": len(slots),
                           "nar_any": any(nar), "nar_all": all(nar)})
    (pd.DataFrame(judged).groupby("work_id")
     .agg(judged=("judged", "sum"), nar_any=("nar_any", "max"),
          nar_all=("nar_all", "min")).reset_index()
     .to_parquet(OUT / "validated.parquet"))
    (OUT / "manifest.json").write_text(json.dumps(
        {"release_id": release, "routing_rows": len(routing),
         "screen_decisions": len(screen), "screen_discards": len(drop),
         "ending_rows": len(endings), "data_dir": str(args.data)}, indent=1))
    print(json.loads((OUT / "manifest.json").read_text()))


if __name__ == "__main__":
    main()
