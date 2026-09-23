"""The free ground truth: the Observatory against FLoRA itself, on the replications both
carry. FLoRA's rows are human-curated, so this measures the Observatory's accuracy
directly — except where the Observatory row was IMPORTED from FLoRA, which would agree
by construction; those are excluded (`source` names the FLoRA import).

    .venv/bin/python -m analysis.mo_observatory.adjudication.mo_vs_flora
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.mo_observatory.build_flora_entries import TO_MO
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_DOI_URL = r"^https?://(dx\.)?doi\.org/"


def main() -> None:
    mo = pd.read_csv(HERE.parent / "replications_database_2026_09_04_184008.csv", dtype=str).fillna("")
    mo["doi_r"] = mo.replication_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    mo["doi_o"] = mo.original_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    imported = mo.source.str.contains("FLoRa", case=False)
    fl = pd.read_csv(ROOT / "data/flora.csv", dtype=str, encoding="utf-8-sig").fillna("")
    fl["doi_r"] = fl.doi_r.map(clean_doi)
    fl["doi_o"] = fl.doi_o.map(clean_doi)
    fl = fl[fl.doi_r != ""]
    both = set(mo.doi_r) & set(fl.doi_r) - {""}
    print(f"replication DOIs in both: {len(both):,}; Observatory rows imported from FLoRA: "
          f"{mo[imported & mo.doi_r.isin(both)].doi_r.nunique():,} works")
    indep = mo[~imported & mo.doi_r.isin(both)]
    works = sorted(set(indep.doi_r))
    rows = []
    for w in works:
        m, f = indep[indep.doi_r == w], fl[fl.doi_r == w]
        common = (set(m.doi_o) & set(f.doi_o)) - {""}
        mres = {o: r for o, r in zip(m.doi_o, m.result)}
        fres = {o: TO_MO.get(r, r) for o, r in zip(f.doi_o, f.outcome)}
        out_same = [mres[o] == fres[o] for o in common if fres[o] in ("success", "failure", "inconclusive")]
        rows.append({"doi_r": w, "n_mo": m.doi_o.nunique(), "n_flora": f.doi_o.nunique(),
                     "same_original": bool(common), "comparable": bool(out_same),
                     "same_outcome": bool(out_same) and all(out_same),
                     "mo_validated": "|".join(sorted(set(m.validated))),
                     "mo_source": "|".join(sorted(set(m.source)))[:80]})
    r = pd.DataFrame(rows)
    print(f"independent Observatory works in FLoRA: {len(r):,}")
    print(f"  same original (any in common): {r.same_original.sum():,} ({r.same_original.mean():.0%})")
    c = r[r.comparable]
    print(f"  same outcome, given same original and a comparable FLoRA verdict: "
          f"{c.same_outcome.sum():,} / {len(c):,} ({c.same_outcome.mean():.0%})")
    print("\nby Observatory 'validated':")
    print(r.groupby("mo_validated").agg(n=("doi_r", "size"), same_original=("same_original", "mean")).round(2).to_string())
    print(c.groupby("mo_validated").agg(n=("doi_r", "size"), same_outcome=("same_outcome", "mean")).round(2).to_string())
    r.to_csv(HERE / "mo_vs_flora.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
