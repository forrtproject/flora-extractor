"""Count the Observatory rows whose replication carries no DOI, by kind of identifier,
and how many of them FLoRA already holds. Read-only; see `no_replication_doi.md`.

    .venv/bin/python -m analysis.mo_observatory.no_replication_doi
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
_THESIS = r"helda\.helsinki|purl\.utwente|academicworks|thekeep\.eiu|hdl\.handle\.net|escholarship"


def kind(url: str) -> str:
    if "osf.io" in url:
        return "OSF"
    if "web.archive.org" in url:
        return "web.archive.org"
    if "datacolada" in url:
        return "datacolada"
    if re.search(_THESIS, url):
        return "thesis / institutional repository"
    return "other"


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def main() -> None:
    mo = pd.read_csv(HERE / "replications_database_2026_09_04_184008.csv", dtype=str, keep_default_na=False)
    nod = mo[~mo.replication_url.str.match(r"^https?://(dx\.)?doi\.org/10\.")].copy()
    nod["kind"] = nod.replication_url.map(kind)
    fl = pd.read_csv(ROOT / "data/flora.csv", dtype=str, encoding="utf-8-sig", keep_default_na=False)
    ids = " ".join((fl.doi_r + " " + fl.url_r + " " + fl.alt_identifier_r + " " + fl.oa_url_r).str.lower())
    titles = set(fl.title_r.map(_norm)) - {""}

    works = nod.drop_duplicates("replication_url").copy()
    guid = works.replication_url.str.extract(r"osf\.io/(?:preprints/\w+/)?([a-z0-9]{5})", flags=re.I)[0].str.lower()
    works["in_flora_by_osf_id"] = [isinstance(g, str) and re.search(rf"osf\.io/{g}(?![a-z0-9])", ids) is not None
                                   for g in guid]
    works["in_flora_by_title"] = works.replication_title.map(_norm).isin(titles)
    works["in_flora"] = works.in_flora_by_osf_id | works.in_flora_by_title
    works["fred"] = works.source.str.contains("FReD", case=False)

    print(f"rows without a replication DOI: {len(nod)}   distinct works (replication_url): {len(works)}\n")
    t = pd.DataFrame({"rows": nod.kind.value_counts(), "works": works.kind.value_counts(),
                      "from FReD": works.groupby("kind").fred.sum(),
                      "in FLoRA (OSF id)": works.groupby("kind").in_flora_by_osf_id.sum(),
                      "in FLoRA (id or exact title)": works.groupby("kind").in_flora.sum()})
    t.loc["total"] = t.sum()
    print(t.astype(int).to_string())
    print("\nOSF works not in FLoRA:")
    print(works[(works.kind == "OSF") & ~works.in_flora][["replication_url", "source"]].to_string(index=False))
    print("\n'other':")
    print(works[works.kind == "other"][["replication_url", "source"]].to_string(index=False))


if __name__ == "__main__":
    main()
