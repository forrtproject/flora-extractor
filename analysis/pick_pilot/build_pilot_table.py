"""One row per pilot work: the live pick (data/extracted.csv + set-asides) vs the sandbox
pick under the 2026-09-23 prompt (analysis/pick_pilot/extracted_sandbox.csv + its
set-asides). Writes pilot_150.csv and prints the tallies REPORT.md quotes.

    .venv/bin/python -m analysis.pick_pilot.build_pilot_table
    .venv/bin/python -m analysis.pick_pilot.build_pilot_table \
        --sandbox extracted_sandbox_v2.csv --out pilot_150_v2.csv   # the v2 pilot
"""
from __future__ import annotations

import argparse
import csv
import glob
from collections import Counter
from pathlib import Path

from shared.utils import bare_work_id, clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _rows(paths: list[str]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for p in paths:
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                w = bare_work_id(r.get("oa_work_id_r") or r.get("openalex_id_r") or "")
                if w:
                    r["_file"] = Path(p).name
                    by.setdefault(w, []).append(r)
    return by


def _side(rows: list[dict]) -> dict:
    rows = rows or []
    return {
        "doi_o": " | ".join(sorted({clean_doi(r.get("doi_o") or "") for r in rows} - {""})),
        "title_o": " | ".join(dict.fromkeys((r.get("title_o") or "")[:90] for r in rows if r.get("title_o"))),
        "link_method": " | ".join(sorted({r.get("link_method") or "" for r in rows})),
        "outcome": " | ".join(sorted({r.get("outcome") or "" for r in rows})),
        "file": " | ".join(sorted({r["_file"] for r in rows})),
        "n_rows": len(rows),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", default="extracted_sandbox.csv",
                    help="the sandbox render, relative to this folder; its set-asides "
                         "are read from <stem>-set-aside/")
    ap.add_argument("--out", default="pilot_150.csv")
    args = ap.parse_args()
    sandbox = HERE / args.sandbox
    ids = [w.strip() for w in (HERE / "sample_150.txt").read_text().split(",") if w.strip()]
    live = _rows([str(ROOT / "data/extracted.csv")]
                 + [p for p in glob.glob(str(ROOT / "data/*.csv"))
                    if Path(p).name in {"api_error.csv", "keyed_link_disputed.csv",
                                        "no_evidence.csv", "no_original_found.csv",
                                        "not_a_replication.csv", "prospective_registration.csv",
                                        "search_link_unconfirmed.csv", "target_pending.csv",
                                        "unidentified_original.csv"}])
    sand = _rows([str(sandbox)]
                 + glob.glob(str(sandbox.with_name(sandbox.stem + "-set-aside") / "*.csv")))
    out, tally = [], Counter()
    for w in ids:
        o, n = _side(live.get(w, [])), _side(sand.get(w, []))
        old_set = set(o["doi_o"].split(" | ")) - {""}
        new_set = set(n["doi_o"].split(" | ")) - {""}
        if not n["n_rows"]:
            change = "no sandbox row"
        elif not new_set and old_set:
            change = "declined (no original)"
        elif old_set == new_set:
            change = "same original"
        elif old_set & new_set:
            change = "overlap (added/dropped an original)"
        else:
            change = "different original"
        descended = "llm_references" not in n["link_method"] and "llm_references" in o["link_method"]
        tally[change] += 1
        out.append({
            "work_id": w, "doi_r": (live.get(w) or sand.get(w) or [{}])[0].get("doi_r", ""),
            **{f"old_{k}": v for k, v in o.items()}, **{f"new_{k}": v for k, v in n.items()},
            "match_change": change,
            "link_method_changed": o["link_method"] != n["link_method"],
            "left_llm_references": descended,
            "outcome_changed": o["outcome"] != n["outcome"],
        })
    with open(HERE / args.out, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(out[0]))
        wr.writeheader()
        wr.writerows(out)
    print("works:", len(out), dict(tally))
    print("new link_method:", Counter(r["new_link_method"] for r in out).most_common())
    print("left llm_references:", sum(r["left_llm_references"] for r in out))
    same = [r for r in out if r["match_change"] == "same original"]
    print("outcome changed where the original is the same:",
          sum(r["outcome_changed"] for r in same), "of", len(same))
    print("outcome transitions (same original):",
          Counter((r["old_outcome"], r["new_outcome"]) for r in same if r["outcome_changed"]).most_common())


if __name__ == "__main__":
    main()
