"""The corrections to shipped FLoRA's original-study DOIs, for the data owner:
doi_r, current_doi_o, correct_main_doi_o, alt_identifier_o, note.

Sources, all from this directory: the judged FLoRA-vs-Observatory items where both
judges named the Observatory's original (`replace`), the version fixes (FLoRA cites a
working paper, meeting abstract or secondary record where a journal article exists),
the DOI classes in `doi_pairs.csv` where FLoRA's side is the alternative identifier or is
misformatted, and FLoRA rows with no DOI where the Observatory's DOI verifies against
FLoRA's own title. Every proposed DOI is checked against its registry record
(`doi_pairs.side`); one that fails is not proposed. `alt_identifier_o` carries a preprint
or working-paper DOI only — not a JSTOR copy, a conference abstract or a secondary record.

    .venv/bin/python -m analysis.mo_observatory.adjudication.flora_corrections
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication.build_decisions import _mo_independent
from analysis.mo_observatory.adjudication.doi_pairs import side
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_PREPRINT_OR_WP = re.compile(r"^10\.(3386/|2139/ssrn|24149/|1101/|3123[45]/|48550/)")

# Judged items whose fix is a VERSION swap, not a different paper: item -> (journal DOI, note).
_VERSION_FIX = {
    "flor-005": ("10.1038/s41380-020-0754-0", "FLoRA cites the 2018 meeting abstract; the study is Han et al. 2021, Mol Psychiatry"),
    "flor-010": ("10.1073/pnas.1905779116", "FLoRA cites the SSRN working paper; the article is Breda & Napp 2019, PNAS"),
    "flor-017": ("10.1126/science.1083968", "FLoRA's DOI is a secondary (Russian) record of Caspi et al. 2003, Science"),
    "flor-020": ("10.1016/s0047-2727(99)00036-5", "FLoRA cites the NBER working paper; the article is Goolsbee & Maydew 2000, J Public Econ"),
}

# Proposed DOIs that pass the title check but are REVIEWS of a book carrying the book's
# title (checked by hand against the Crossref record's container).
_BOOK_REVIEWS = {"10.1176/ajp.140.5.633-a", "10.5860/choice.34-2458", "10.2307/3032069"}

# The replace items whose true original is a book with no DOI: item -> note.
_NO_DOI_ORIGINAL = {
    "flor-009": "wrong original: FLoRA lists Burger (2009); the paper re-tests Milgram (1974), Obedience to "
                "Authority, Experiment 2 — a book with no DOI",
}


def main() -> None:
    fl = pd.read_csv(ROOT / "data/flora.csv", dtype=str, encoding="utf-8-sig").fillna("")
    fl["doi_r_c"], fl["doi_o_c"] = fl.doi_r.map(clean_doi), fl.doi_o.map(clean_doi)
    mo = _mo_independent()
    d = pd.read_csv(HERE / "decisions.csv", dtype=str).fillna("")
    d = d[d.population == "FLoRA vs Observatory"]
    rows, refused = [], []

    def add(doi_r: str, current: str, correct: str, alt: str, note: str, title_for_check: str) -> None:
        if correct in _BOOK_REVIEWS:
            refused.append((doi_r, correct, "a review of a book, not the work"))
            return
        if correct:
            cls, detail, _ = side(correct, title_for_check)
            if cls not in ("work",):
                refused.append((doi_r, correct, detail))
                return
        existing = fl[(fl.doi_r_c == doi_r) & (fl.doi_o_c == clean_doi(current))].alt_identifier_o
        alt = alt or (existing.iloc[0] if len(existing) else "")
        if clean_doi(alt) == clean_doi(correct):
            alt = ""
        rows.append({"doi_r": doi_r, "current_doi_o": current, "correct_main_doi_o": correct,
                     "alt_identifier_o": alt, "note": note})

    for r in d[d.kind == "different paper"].itertuples():
        f = fl[fl.doi_r_c == r.doi_r]
        m = mo[mo.doi_r == r.doi_r].drop_duplicates("doi_o")
        if r.item in _VERSION_FIX:
            correct, note = _VERSION_FIX[r.item]
            cur = f.doi_o.iloc[0]
            alt = cur if _PREPRINT_OR_WP.match(clean_doi(cur)) else ""
            add(r.doi_r, cur, correct, alt, note, m[m.doi_o == correct].original_title.iloc[0] if (m.doi_o == correct).any() else "")
        elif r.item in _NO_DOI_ORIGINAL:
            add(r.doi_r, f.doi_o.iloc[0], "", "", _NO_DOI_ORIGINAL[r.item], "")
        elif r.action == "replace":
            wrong = f[~f.doi_o_c.isin(set(m.doi_o))]
            for w in wrong.itertuples():
                for x in m.itertuples():
                    add(r.doi_r, w.doi_o, x.doi_o, "", f"wrong original: FLoRA lists “{w.title_o[:70]}”; the paper "
                        f"re-tests “{x.original_title[:70]}” ({x.original_year})", x.original_title)
    for r in pd.read_csv(HERE / "doi_pairs.csv", dtype=str).fillna("").itertuples():
        if r.population != "FLoRA vs Observatory":
            continue
        if r.pair == "same DOI, formatting" and "side A" in r.detail:
            add(r.doi_r, r.doi_a, clean_doi(urllib.parse.unquote(r.doi_a).replace("//", "/")), "",
                "same DOI, misformatted ('//' or URL-encoded)", "")
        elif r.class_a == "alternative" and r.class_b == "work":
            alt = r.doi_a if _PREPRINT_OR_WP.match(r.doi_a) else ""
            add(r.doi_r, r.doi_a, r.doi_b, alt, f"FLoRA cites a non-journal version ({r.detail.split(' · ')[0][3:]}); "
                "use the article of record", r.title_b)
    nd = pd.read_csv(HERE / "flora_vs_mo_originals.csv", dtype=str)
    for w in nd[nd.kind == "FLoRA: no original DOI"].doi_r:
        f = fl[fl.doi_r_c == w].iloc[0]
        for x in mo[mo.doi_r == w].drop_duplicates("doi_o").itertuples():
            if x.doi_o:
                add(w, "", x.doi_o, "", f"FLoRA has no DOI for “{f.title_o[:70]}”; this DOI verifies against that title",
                    f.title_o)
    out = pd.DataFrame(rows).drop_duplicates()
    out.to_csv(HERE / "flora_corrections.csv", index=False, encoding="utf-8-sig")
    print(f"{len(out)} corrections over {out.doi_r.nunique()} replications")
    print(out.to_string())
    for x in refused:
        print("NOT PROPOSED (registry check failed):", x)


if __name__ == "__main__":
    main()
