"""Attach Stage 2's verdict to every case, in one streaming pass over filtered.csv.

Two things the eval cannot be read without:

  * `filter_status == "false_positive"` rows never reach Stage 3 (run_extract skips
    them), so counting them as negatives the pre-screen "saved" inflates the benefit.
    They are marked here and excluded by the scorer.
  * Stage 2 already emits a deterministic high-confidence replication verdict
    (a replication phrase AND a same-sentence author-year cite — filter/rule_filter.py),
    and curated-source rows bypass keyword filtering entirely. Both are candidate
    overrides in place of a hand-written regex, so the eval has to be able to see them.

Run from anywhere:  python3 analysis/prescreen_eval/enrich_casesets.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path("/Users/lukaswallrich/Documents/Coding/flora-extractor")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from shared.utils import clean_doi  # noqa: E402

csv.field_size_limit(10_000_000)

CASE_FILES = ("cases_goldpos_flora.json", "cases_goldpos_repro.json",
              "cases_goldneg_curated.json", "cases_goldneg_screen.json")


def main() -> None:
    cases: dict[str, list[dict]] = {f: json.loads((HERE / f).read_text()) for f in CASE_FILES}
    wanted = {c["doi"] for cs in cases.values() for c in cs if c.get("doi")}
    print(f"{sum(len(c) for c in cases.values())} cases, {len(wanted)} distinct DOIs")

    stage2: dict[str, dict] = {}
    n = 0
    with open(REPO / "data/filtered.csv", newline="", encoding="utf-8-sig",
              errors="replace") as fh:
        for row in csv.DictReader(fh):
            n += 1
            if n % 500_000 == 0:
                print(f"  … {n:,} rows, {len(stage2):,} matched", flush=True)
            raw = (row.get("doi_r") or "").strip()
            if not raw:
                continue
            d = clean_doi(raw)
            if d in wanted and d not in stage2:
                stage2[d] = {
                    "filter_status": (row.get("filter_status") or "").strip(),
                    "filter_confidence": (row.get("filter_confidence") or "").strip(),
                    "filter_evidence": (row.get("filter_evidence") or "").strip()[:300],
                    "source": (row.get("source") or "").strip(),
                }
    print(f"scanned {n:,} rows; matched {len(stage2):,}/{len(wanted)} DOIs")

    for fname, cs in cases.items():
        hit = 0
        for c in cs:
            s = stage2.get(c.get("doi", ""))
            if s:
                hit += 1
                c.update(s)
            else:
                c.setdefault("filter_status", "")
        (HERE / fname).write_text(json.dumps(cs, indent=1, ensure_ascii=False))
        statuses: dict[str, int] = {}
        for c in cs:
            statuses[c.get("filter_status", "")] = statuses.get(c.get("filter_status", ""), 0) + 1
        print(f"{fname}: {hit}/{len(cs)} matched  filter_status={statuses}")


if __name__ == "__main__":
    main()
