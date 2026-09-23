"""Our Stage 3 rows against the Observatory, per WORK, from the raw Observatory file.

Supersedes the numbers of `in_pool_agreement.py`, which joined against `priority.csv` —
one Observatory row per replication DOI. The Observatory stores one row per effect or
per lab (the multi-lab ego-depletion replication has 24), so that file kept one
arbitrary original and one arbitrary result per paper: 214 of the 1,303 works list
more than one original there, and 115 original pairs carry more than one result.

Here each side is a SET: our originals for the work, theirs; and for each original both
name, our outcome against the set of results they record for it.

    .venv/bin/python -m analysis.mo_observatory.adjudication.agreement
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.mo_observatory.build_flora_entries import TO_MO
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
_DOI_URL = r"^https?://(dx\.)?doi\.org/"
_MO_VERDICTS = {"success", "failure", "inconclusive", "reversal"}


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    ours = pd.read_csv(HERE.parent / "in_pool_agreement.csv", dtype=str).fillna("")
    ours = ours.drop_duplicates(["doi_r", "doi_o"])[
        ["doi_r", "title_r", "abstract_r", "year_r", "authors_r", "journal_r", "doi_o", "title_o",
         "authors_o", "year_o", "outcome", "outcome_phrase", "outcome_reasoning", "out_quote_source",
         "type", "link_method", "link_confidence", "link_evidence", "pdf_source"]]
    mo = pd.read_csv(HERE.parent / "replications_database_2026_09_04_184008.csv", dtype=str).fillna("")
    mo["doi_r"] = mo.replication_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    mo["doi_o"] = mo.original_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    mo = mo[mo.doi_r.isin(set(ours.doi_r))]
    return ours, mo


def outcome_kind(ours: str, our_type: str, theirs: set[str]) -> str:
    """How our outcome for one shared original compares with the Observatory's results."""
    if our_type == "reproduction" or "," in ours:
        return "reproduction (two-axis, unmapped)"
    if ours in ("cannot_be_determined", "uninformative", "descriptive only"):
        return f"ours: {ours}"
    theirs = theirs & _MO_VERDICTS
    if not theirs:
        return "theirs: no result"
    if len(theirs) > 1:
        return "theirs: several results (per effect/lab)"
    t = next(iter(theirs))
    o = TO_MO.get(ours, ours)
    if o == t:
        return "agree"
    pair = {o, t}
    if pair == {"success", "failure"}:
        return "contradiction (success vs failure)"
    if "reversal" in pair:
        return "reversal involved"
    return f"inconclusive vs {(pair - {'inconclusive'}).pop()}"


def main() -> None:
    ours, mo = load()
    works, pairs = [], []
    for w, o in ours.groupby("doi_r"):
        m = mo[mo.doi_r == w]
        o_set = set(o.doi_o) - {""}
        m_set = set(m.doi_o) - {""}
        common = o_set & m_set
        if common:
            orig = "same (identical sets)" if o_set == m_set else "overlap (some in common)"
        elif not m_set:
            orig = "theirs: no original DOI"
        elif len(o_set) == 1 and len(m_set) == 1:
            orig = "different (one each)"
        else:
            orig = "different (several)"
        works.append({"doi_r": w, "original": orig, "n_ours": len(o_set), "n_mo": len(m_set)})
        for d in common:
            r = o[o.doi_o == d].iloc[0]
            res = set(m[m.doi_o == d].result)
            pairs.append({"doi_r": w, "doi_o": d, "our_outcome": r.outcome, "mo_results": "|".join(sorted(res)),
                          "kind": outcome_kind(r.outcome, r.type, res)})
    wk, pr = pd.DataFrame(works), pd.DataFrame(pairs)
    n = len(wk)
    print(f"works: {n:,}")
    print((wk.original.value_counts().to_frame("works")
           .assign(share=lambda x: (x.works / n).round(3))).to_string())
    print(f"\nshared originals: {len(pr):,}")
    print(pr.kind.value_counts().to_string())
    comp = pr[pr.kind.isin(["agree"]) | pr.kind.str.startswith(("contradiction", "inconclusive", "reversal"))]
    print(f"\nsame outcome where both give one comparable verdict: "
          f"{(comp.kind == 'agree').sum():,} / {len(comp):,} ({(comp.kind == 'agree').mean():.0%})")
    wk.to_csv(HERE / "works.csv", index=False, encoding="utf-8-sig")
    pr.to_csv(HERE / "pairs.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
