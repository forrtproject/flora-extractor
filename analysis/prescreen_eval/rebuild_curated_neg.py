"""Rebuild the curated-negative bucket from rows that actually reach Stage 3.

The first build capped at 400 before checking Stage 2's verdict, and 90% of those were
already `false_positive` — Stage 3 never sees them, so scoring them as negatives the
pre-screen "saved" measures nothing. This takes the same source pool (old-pipeline
`validation_status == false_positive`) and keeps the first 400 by DOI that Stage 2
passes through, so the negative side of the eval is the population the pre-screen would
really be gating.
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
CAP = 400


def main() -> None:
    with open(REPO / "data/all_replications.csv", newline="", encoding="utf-8-sig",
              errors="replace") as fh:
        pool = {clean_doi((r.get("doi_r") or "").strip()): r
                for r in csv.DictReader(fh)
                if r.get("validation_status") == "false_positive"
                and (r.get("doi_r") or "").strip().upper() not in {"", "NA", "NAN"}}
    pool.pop("", None)

    # keep the other buckets disjoint from this one
    taken = {c["doi"] for f in ("cases_goldpos_flora.json", "cases_goldpos_repro.json",
                                "cases_goldneg_screen.json")
             for c in json.loads((HERE / f).read_text()) if c.get("doi")}
    wanted = set(pool) - taken
    print(f"curated-negative pool: {len(pool)}, after disjointness: {len(wanted)}")

    hits: dict[str, dict] = {}
    n = 0
    with open(REPO / "data/filtered.csv", newline="", encoding="utf-8-sig",
              errors="replace") as fh:
        for row in csv.DictReader(fh):
            n += 1
            if n % 500_000 == 0:
                print(f"  … {n:,} rows, {len(hits):,} live hits", flush=True)
            raw = (row.get("doi_r") or "").strip()
            if not raw:
                continue
            d = clean_doi(raw)
            if d not in wanted or d in hits:
                continue
            if (row.get("filter_status") or "").strip() == "false_positive":
                continue
            title = (row.get("title_r") or "").strip()
            abstract = (row.get("abstract_r") or "").strip()
            if not title or not abstract:
                continue
            hits[d] = {"doi": d, "title": title, "abstract": abstract,
                       "bucket": "goldneg_curated",
                       "filter_status": (row.get("filter_status") or "").strip(),
                       "filter_confidence": (row.get("filter_confidence") or "").strip(),
                       "filter_evidence": (row.get("filter_evidence") or "").strip()[:300],
                       "source": (row.get("source") or "").strip(),
                       "note": "old-pipeline false_positive; " +
                               (pool[d].get("prep_notes") or "").strip()[:600]}
    print(f"scanned {n:,} rows; {len(hits)} curated negatives reach Stage 3")

    cases = [dict(hits[d], id=f"NC{i:03d}") for i, d in enumerate(sorted(hits)[:CAP], 1)]
    (HERE / "cases_live_goldneg_curated.json").write_text(
        json.dumps(cases, indent=1, ensure_ascii=False))
    print(f"wrote cases_live_goldneg_curated.json: {len(cases)}")


if __name__ == "__main__":
    main()
