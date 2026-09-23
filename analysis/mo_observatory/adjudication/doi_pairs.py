"""Classify the pairs where both sides name the same original paper (by title) under
different DOIs: is the difference an ALTERNATIVE IDENTIFIER of the same work (preprint,
working paper, duplicate registration, reprint), or a MISTAKE (the DOI points to
something that is not the work: an erratum, a reply, a journal issue, a review, another
paper, or nothing at all)?

Per DOI, the registry record is fetched (`fetch_doi_metadata`, cached) and, for journal
articles, the raw Crossref record — its `update-to` relation is how a correction notice
that carries the article's own title is told apart from the article. Raw Crossref
responses are cached in `out/crossref/`.

Writes `doi_pairs.csv`: one row per pair, each side's verdict and the pair's.

    .venv/bin/python -m analysis.mo_observatory.adjudication.doi_pairs
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

import pandas as pd
import requests

from analysis.mo_observatory.adjudication.agreement import load
from shared.config import RESEARCHER_EMAIL
from shared.disambiguation import jaccard_similarity
from shared.doi_verify import fetch_doi_metadata
from shared.utils import cache_key, clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_DOI_URL = r"^https?://(dx\.)?doi\.org/"

# Registrant/type patterns for a DOI that names another VERSION of the same work.
_ALTERNATIVE = [(r"^10\.3386/", "NBER working paper"), (r"^10\.2139/ssrn", "SSRN working paper"),
                (r"^10\.24149/", "working paper"), (r"^10\.1101/", "bioRxiv/medRxiv preprint"),
                (r"^10\.3123[45]/", "OSF preprint"), (r"^10\.48550/", "arXiv preprint"),
                (r"^10\.2307/", "JSTOR duplicate registration"),
                (r"^10\.1037/e\d", "PsycEXTRA conference record"), (r"^10\.4324/", "book reprint"),
                (r"^10\.5771/", "book reprint")]
# What a DOI can point to that is not the work itself.
_NOT_THE_WORK = re.compile(r"^\s*(erratum|errata|correction|corrigendum|addendum|response to|reply to|"
                           r"comment on|author response|faculty opinions|publisher.s note|expression of concern)", re.I)


# Pairs the rules cannot read, decided by looking: (doi_a, doi_b) -> (pair, wrong side(s), note).
_MANUAL = {
    ("10.2307/2393017", "10.1016/0142-694x(82)90089-8"):
        ("mistake", "ab", "both DOIs are book REVIEWS (ASQ 1983, Design Studies 1982) of Hofstede's "
                    "Culture's Consequences (1980), a book with no DOI"),
    ("10.1176/appi.ajp.158.9.1449", "10.1001/archpsyc.57.4.311"):
        ("mistake", "a", "side A names Khan et al. 2000 (antidepressant trials, Arch Gen Psychiatry) by title and "
                    "year but carries the DOI of Khan et al. 2001 (antipsychotic trials, Am J Psychiatry)"),
    ("10.1086/298261", "10.1016/s0272-7757(99)00057-6"):
        ("mistake", "b", "side B names Sicherman's 1991 'Overeducation in the Labor Market' by title and year but "
                    "carries the DOI of Groot & Maassen van den Brink's 2000 meta-analysis"),
}


def _canon(doi: str) -> str:
    return urllib.parse.unquote(doi or "").replace("//", "/").lower()


def _crossref(doi: str) -> dict:
    p = HERE / "out" / "crossref" / f"{cache_key(doi)}.json"
    if p.exists():
        return json.loads(p.read_text())
    r = requests.get(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}",
                     params={"mailto": RESEARCHER_EMAIL}, timeout=30)
    if r.status_code not in (200, 404):
        r.raise_for_status()
    msg = r.json().get("message", {}) if r.status_code == 200 else {}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(msg))
    return msg


def side(doi: str, stated_title: str) -> tuple[str, str, str]:
    """(class, detail, registry title) for one DOI: work | alternative | mistake | missing."""
    if not doi:
        return "missing", "no DOI given", ""
    meta = fetch_doi_metadata(doi)
    if meta is None:
        return "unknown", "registry lookup failed (transient)", ""
    title = str(meta.get("title") or "")
    typ = str(meta.get("type") or "")
    if typ in ("journal-issue", "peer-review"):
        return "mistake", f"DOI points to a {typ.replace('-', ' ')}, not the article", title
    if not meta.get("registered", True) or not title:
        return "mistake", "DOI does not resolve to a registered work (malformed or wrong)", ""
    if _NOT_THE_WORK.match(title):
        return "mistake", f"DOI points to a notice/reply: “{title[:60]}”", title
    if re.match(r"^10\.1016/s0084-", doi):
        return "mistake", "DOI points to a Year Book abstract digest, not the article", title
    if stated_title and jaccard_similarity(title, stated_title) < 0.5:
        return "mistake", f"DOI points to a different work: “{title[:60]}”", title
    alt = next((why for pat, why in _ALTERNATIVE if re.search(pat, doi)), "")
    if alt or typ in ("posted-content", "preprint", "report", "dataset", "book-chapter"):
        return "alternative", alt or typ.replace("-", " "), title
    if typ == "journal-article":
        cr = _crossref(doi)
        if cr.get("update-to"):
            kind = cr["update-to"][0].get("type", "update")
            return "mistake", f"Crossref marks this DOI as a {kind} of another DOI", title
    if title.lower().startswith("retracted"):
        return "work", "the article of record — RETRACTED", title
    return "work", "article of record", title


def classify(doi_a: str, title_a: str, doi_b: str, title_b: str) -> dict:
    if doi_a and doi_b and _canon(doi_a) == _canon(doi_b):
        bad = "a" if ("//" in doi_a or "%" in doi_a) else "b"
        return {"pair": "same DOI, formatting", "a": "work", "b": "work",
                "detail": f"side {bad.upper()} writes the DOI with '//' or URL-encoding", "reg_a": "", "reg_b": ""}
    ca, da, ra = side(doi_a, title_a)
    cb, db, rb = side(doi_b, title_b)
    alt_prefix = any(re.search(pat, d or "") for pat, _ in _ALTERNATIVE for d in (doi_a, doi_b))
    if (doi_a, doi_b) in _MANUAL or (doi_b, doi_a) in _MANUAL:
        pair, wrong, note = _MANUAL.get((doi_a, doi_b)) or _MANUAL[(doi_b, doi_a)]
        return {"pair": pair, "a": "mistake" if "a" in wrong else "work", "b": "mistake" if "b" in wrong else "work",
                "detail": note, "reg_a": ra, "reg_b": rb}
    if ca in ("work", "alternative") and cb in ("work", "alternative") and ra and rb \
            and jaccard_similarity(ra, rb) < 0.8 and not alt_prefix:
        pair = "different works with similar titles — needs judging"
    elif "mistake" in (ca, cb):
        pair = "mistake"
    elif "alternative" in (ca, cb):
        pair = "alternative identifier"
    elif ca == cb == "work":
        pair = "duplicate registration (alternative identifier)"
    else:
        pair = "unresolved"
    return {"pair": pair, "a": ca, "b": cb, "detail": f"A: {da} · B: {db}", "reg_a": ra, "reg_b": rb}


def pairs() -> pd.DataFrame:
    """Both populations: our pipeline vs the Observatory, FLoRA vs the Observatory."""
    # original_title_sim.csv keeps titles cut to 60 characters, so the full titles are
    # re-read here: the registry-title check compares against them.
    sim = pd.read_csv(HERE / "original_title_sim.csv", dtype=str).fillna("")
    ours, mo_all = load()
    rows = []
    for r in sim[sim.sim.astype(float) >= 0.95].itertuples():
        o = ours[(ours.doi_r == r.doi_r) & (ours.doi_o == r.our_doi)]
        m = mo_all[(mo_all.doi_r == r.doi_r) & (mo_all.doi_o == r.mo_doi)]
        rows.append({"population": "pipeline vs Observatory", "doi_r": r.doi_r, "side_a": "pipeline",
                     "doi_a": r.our_doi, "title_a": o.title_o.iloc[0] if len(o) else r.ours,
                     "doi_b": r.mo_doi, "title_b": m.original_title.iloc[0] if len(m) else r.mo})
    diff = pd.read_csv(HERE / "flora_vs_mo_originals.csv", dtype=str)
    mo = pd.read_csv(HERE.parent / "replications_database_2026_09_04_184008.csv", dtype=str).fillna("")
    mo = mo[~mo.source.str.contains("FLoRa", case=False)]
    mo["doi_r"] = mo.replication_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    mo["doi_o"] = mo.original_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    fl = pd.read_csv(ROOT / "data/flora.csv", dtype=str, encoding="utf-8-sig").fillna("")
    fl["doi_r"], fl["doi_o"] = fl.doi_r.map(clean_doi), fl.doi_o.map(clean_doi)
    for w in diff[diff.kind.isin(["same title, other DOI", "same DOI, // variant"])].doi_r:
        f, m = fl[fl.doi_r == w].drop_duplicates("doi_o"), mo[mo.doi_r == w].drop_duplicates("doi_o")
        s, fd, ft, md, mt = max((jaccard_similarity(a, b), fd, a, md, b)
                                for a, fd in zip(f.title_o, f.doi_o) for b, md in zip(m.original_title, m.doi_o))
        rows.append({"population": "FLoRA vs Observatory", "doi_r": w, "side_a": "flora", "doi_a": fd,
                     "title_a": ft, "doi_b": md, "title_b": mt})
    return pd.DataFrame(rows)


def main() -> None:
    p = pairs()
    out = pd.concat([p, pd.DataFrame([classify(r.doi_a, r.title_a, r.doi_b, r.title_b) for r in p.itertuples()])], axis=1)
    out = out.rename(columns={"a": "class_a", "b": "class_b"})
    out.to_csv(HERE / "doi_pairs.csv", index=False, encoding="utf-8-sig")
    print(pd.crosstab(out.pair, out.population, margins=True).to_string())
    pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 110)
    print(out[["population", "doi_a", "class_a", "doi_b", "class_b", "detail"]].to_string())


if __name__ == "__main__":
    main()
