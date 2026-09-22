"""How the in-pool Observatory works came out of Stage 3, against the Observatory's own
verdicts — the counterpart of `build_flora_entries.py` for the works the curated rule
admitted rather than the 710 outside the pool.

Reads the rendered export and every set-aside CSV, restricts to works the
`curated-observatory` rule admitted, and compares per row: the same original DOI first,
then the same outcome, in the Observatory's four-value vocabulary (`TO_MO`).

    .venv/bin/python -m analysis.mo_observatory.in_pool_agreement
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.build_flora_entries import TO_MO
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main() -> None:
    listed = {clean_doi(d) for d in json.load(open(ROOT / "filter/spec/curated-observatory.json"))
              ["match"]["doi_in"]}
    mo = pd.read_csv(HERE / "priority.csv", dtype=str).fillna("")
    mo["doi_r"] = mo.doi_r.map(clean_doi)
    mo["mo_doi_o"] = mo.original_url.str.replace(r"^https?://(dx\.)?doi\.org/", "", regex=True).map(clean_doi)
    mo = mo.drop_duplicates(["doi_r", "mo_doi_o"])

    files = [ROOT / "data/extracted.csv"] + [Path(p) for p in glob.glob(str(ROOT / "data/*.csv"))
                                              if Path(p).name not in ("extracted.csv", "flora.csv")
                                              and not Path(p).name.startswith("extracted-test")]
    frames = []
    for f in files:
        try:
            d = pd.read_csv(f, dtype=str, encoding="utf-8-sig").fillna("")
        except Exception:
            continue
        if "doi_r" in d.columns and "link_method" in d.columns:
            frames.append(d.assign(_file=f.name))
    ours = pd.concat(frames)
    ours["doi_r"] = ours.doi_r.map(clean_doi)
    ours = ours[ours.doi_r.isin(listed)]
    old = pd.read_csv(ROOT / "data/extracted.csv", dtype=str, encoding="utf-8-sig").fillna("")

    print(f"Observatory works in the pool that Stage 3 wrote a row for: {ours.doi_r.nunique():,} works, {len(ours):,} rows")
    print(ours.groupby("_file").doi_r.nunique().sort_values(ascending=False).to_string())
    main_rows = ours[ours._file == "extracted.csv"].copy()
    main_rows["doi_o"] = main_rows.doi_o.map(clean_doi)
    j = main_rows.merge(mo, on="doi_r", how="inner", suffixes=("", "_mo"))
    j["same_original"] = (j.doi_o != "") & (j.doi_o == j.mo_doi_o)
    j["our_mo_outcome"] = j.outcome.map(TO_MO).fillna("")
    same = j[j.same_original]
    agree = same[same.our_mo_outcome == same.result]
    print(f"\nRows in extracted.csv with an Observatory counterpart: {len(j):,} ({j.doi_r.nunique():,} works)")
    print(f"  same original DOI: {len(same):,} rows ({len(same)/len(j):.0%})")
    print(f"  same original AND same outcome: {len(agree):,} ({len(agree)/max(len(same),1):.0%} of same-original)")
    print("  outcome pairs on same-original rows (ours → theirs):")
    print(pd.crosstab(same.our_mo_outcome, same.result).to_string())
    j.to_csv(HERE / "in_pool_agreement.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
