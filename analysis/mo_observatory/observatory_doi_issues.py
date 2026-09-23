"""The Metascience Observatory's own DOI problems, as a table for its maintainer.

Two sources, merged into one row per (field, identifier as given):

1. `adjudication/doi_pairs.csv` — the same-title pairs where the Observatory's side
   (side B) was classified a MISTAKE: an erratum/addendum, a reply, a Faculty Opinions
   record, a journal-issue DOI, a DOI that does not resolve, a DOI for another paper.
2. A scan of every `original_url` / `replication_url` in the raw Observatory file:
   each distinct DOI is looked up in Crossref (raw record, cached in
   `adjudication/out/crossref_scan/`), a Crossref 404 is re-checked against the doi.org
   handle API (DataCite DOIs are not in Crossref), and the record is tested for the same
   shapes — plus identifiers that are not a DOI at all (Semantic Scholar URLs, strings
   with junk appended). Semantic Scholar ids are resolved through the S2 Graph API
   (cached) to suggest the DOI.

A "DOI for another paper" is flagged by the scan when the registry title and the
Observatory's stated title share almost no words (Jaccard < `_OTHER_PAPER_SIM`, set by
reading the distribution: below it every hit read was another paper; just above it are
truncated titles and translations), or when a pair was confirmed by hand
(`_REVIEWED_OTHER`).

Row identifiers are 1-based record numbers in the export file
(`replications_database_2026_09_04_184008.csv`; record 1 = the first data row, so the
spreadsheet row is record + 1), with the replication identifier alongside because the
file has no id column of its own.

    .venv/bin/python -m analysis.mo_observatory.observatory_doi_issues --fetch   # network, cached
    .venv/bin/python -m analysis.mo_observatory.observatory_doi_issues          # build the CSV
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

from shared.config import RESEARCHER_EMAIL, S2_API_KEY
from shared.disambiguation import jaccard_similarity
from shared.utils import cache_key, clean_doi

HERE = Path(__file__).resolve().parent
RAW = HERE / "replications_database_2026_09_04_184008.csv"
PAIRS = HERE / "adjudication" / "doi_pairs.csv"
CACHE = HERE / "adjudication" / "out" / "crossref_scan"
S2_CACHE = HERE / "adjudication" / "out" / "s2"
SEARCH_CACHE = HERE / "adjudication" / "out" / "crossref_search"
_YEAR = {"original_url": "original_year", "replication_url": "replication_year"}
_AUTH = {"original_url": "original_authors", "replication_url": "replication_authors"}
OUT = HERE / "observatory_doi_issues.csv"
FIELDS = {"original_url": "original_title", "replication_url": "replication_title"}
_DOI_URL = re.compile(r"^https?://(dx\.)?doi\.org/", re.I)
_NOT_THE_WORK = re.compile(r"^\s*(erratum|errata|correction|corrigendum|addendum|response to|reply to|"
                           r"comment on|author response|faculty opinions|publisher.s note|expression of concern)", re.I)
_OTHER_PAPER_SIM = 0.10

_lock = threading.Lock()
_last = [0.0]


def _throttle(interval: float = 0.12) -> None:
    with _lock:
        wait = _last[0] + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def crossref(doi: str) -> dict:
    """{"status": 200|404, "msg": trimmed record, "handle": doi.org responseCode on a 404}."""
    p = CACHE / f"{cache_key(doi)}.json"
    if p.exists():
        return json.loads(p.read_text())
    for attempt in range(4):
        _throttle()
        try:
            r = requests.get(f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}",
                             params={"mailto": RESEARCHER_EMAIL}, timeout=30)
            if r.status_code in (200, 404):
                break
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    else:
        return {}                                  # transient: never cached
    out: dict = {"status": r.status_code}
    if r.status_code == 200:
        m = r.json().get("message", {})
        out["msg"] = {k: m.get(k) for k in ("title", "type", "update-to", "relation", "container-title",
                                           "issued", "DOI")}
        out["msg"]["author"] = (m.get("author") or [])[:1]
    else:
        try:
            h = requests.get(f"https://doi.org/api/handles/{urllib.parse.quote(doi, safe='/')}", timeout=30)
            out["handle"] = h.json().get("responseCode") if h.status_code in (200, 404) else None
        except (requests.RequestException, ValueError):
            return {}
        if out["handle"] is None:
            return {}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out))
    return out


def s2(paper_id: str) -> dict:
    p = S2_CACHE / f"{paper_id}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def s2_fetch(ids: list[str]) -> None:
    """One batch call for every uncached Semantic Scholar id (the per-paper endpoint is
    throttled hard without a key)."""
    todo = [i for i in dict.fromkeys(ids) if not (S2_CACHE / f"{i}.json").exists()]
    if not todo:
        return
    headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else {}
    for attempt in range(6):
        r = requests.post("https://api.semanticscholar.org/graph/v1/paper/batch",
                          params={"fields": "title,year,externalIds"}, json={"ids": todo},
                          headers=headers, timeout=60)
        if r.status_code == 200:
            break
        time.sleep(5 * 2 ** attempt)
    else:
        print(f"S2 batch failed: HTTP {r.status_code}")
        return
    S2_CACHE.mkdir(parents=True, exist_ok=True)
    for i, rec in zip(todo, r.json()):
        (S2_CACHE / f"{i}.json").write_text(json.dumps(rec or {"missing": True}))


def load_raw() -> pd.DataFrame:
    d = pd.read_csv(RAW, dtype=str, keep_default_na=False)
    d["record"] = range(1, len(d) + 1)
    return d


def raw_dois(d: pd.DataFrame) -> list[str]:
    got = set()
    for f in FIELDS:
        for v in d[f]:
            if _DOI_URL.match(v):
                doi = clean_doi(v)
                if re.match(r"^10\.\d{4,9}/\S+$", doi):
                    got.add(doi)
    return sorted(got)


def fetch() -> None:
    d = load_raw()
    dois = raw_dois(d)
    todo = [x for x in dois if not (CACHE / f"{cache_key(x)}.json").exists()]
    print(f"{len(dois)} distinct DOIs, {len(todo)} to fetch")
    done = [0]

    def one(x: str) -> None:
        crossref(x)
        done[0] += 1
        if done[0] % 500 == 0:
            print(f"  {done[0]}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(3) as ex:
        list(ex.map(one, todo))
    s2_fetch([v.rstrip("/").split("/")[-1]
              for v in d.original_url[d.original_url.str.contains("semanticscholar.org/paper/")]])


def _title(msg: dict) -> str:
    return ((msg.get("title") or [""]) or [""])[0] or ""


def _cr_title(doi: str) -> str:
    c = crossref(doi)
    return _title(c.get("msg") or {}) if c.get("status") == 200 else ""


# Scan hits on "another paper" that were read and confirmed (DOI -> note). A low-overlap
# title alone is not enough; see the module docstring.
_REVIEWED_OTHER: dict[str, str] = {}
# Low title overlap that is the same paper under its original-language title (read 2026-09-23).
_TRANSLATED = {"10.1016/j.zefq.2017.09.001", "10.1590/s0034-89102007000500014", "10.3724/sp.j.1041.2021.00128",
               "10.2130/jjesp.2008"}


def _cr_search(title: str, year: str, author: str) -> str:
    """The DOI Crossref's bibliographic search returns for a stated title, accepted only on a
    near-identical title and a year within one. Cached; "" when nothing qualifies."""
    if not title:
        return ""
    key = cache_key(f"{title}|{year}|{author}")
    p = SEARCH_CACHE / f"{key}.json"
    if p.exists():
        items = json.loads(p.read_text())
    else:
        _throttle(0.3)
        r = requests.get("https://api.crossref.org/works",
                         params={"query.bibliographic": f"{title} {author} {year}".strip(), "rows": 5,
                                 "select": "DOI,title,issued,type", "mailto": RESEARCHER_EMAIL}, timeout=60)
        if r.status_code != 200:
            return ""
        items = r.json().get("message", {}).get("items", [])
        SEARCH_CACHE.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items))
    for it in items:
        t = (it.get("title") or [""])[0]
        y = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        if jaccard_similarity(t, title) >= 0.9 and (not year or not y or abs(int(float(year)) - int(y)) <= 1) \
                and it.get("type") not in ("peer-review", "journal-issue") and not _NOT_THE_WORK.match(t):
            return clean_doi(it["DOI"])
    return ""


def _repair(doi: str) -> str:
    """Mechanical repairs of a broken DOI string, kept only when the result resolves."""
    for cand in (re.sub(r"^(10\.1023/a)[/_]", r"\1:", doi), re.sub(r"^(10\.\d+/)2f", r"\1", doi)):
        if cand != doi and crossref(cand).get("status") == 200:
            return cand
    return ""


def classify(doi: str, stated: str) -> tuple[str, str, str, str] | None:
    """(category, registry title, suggested DOI, evidence) or None when the DOI is fine."""
    c = crossref(doi)
    if not c:
        return None                                        # transient; not a finding
    if c["status"] == 404:
        if c.get("handle") == 1:
            return None                                    # DataCite / other registrar
        return ("unresolvable DOI", "", "", "Crossref 404 and doi.org reports the handle does not exist")
    m = c["msg"]
    title = _title(m)
    typ = m.get("type") or ""
    if typ == "journal-issue":
        return ("journal-issue DOI", title or "(journal issue)", "", "Crossref type journal-issue")
    if "faculty opinions" in title.lower() or doi.startswith("10.3410/f."):
        rel = [r.get("id") for r in (m.get("relation") or {}).get("is-review-of", [])]
        return ("Faculty Opinions record", title, clean_doi(rel[0]) if rel else "",
                "Crossref: a Faculty Opinions recommendation of the article, not the article")
    if typ == "peer-review" or re.search(r"\.sa\d+$", doi):
        return ("author response / peer review", title, re.sub(r"\.sa\d+$", "", doi) if re.search(r"\.sa\d+$", doi) else "",
                f"Crossref type {typ}")
    # A notice carries update-to pointing at ANOTHER DOI. new_version/new_edition (F1000 v2,
    # PLOS versioning) is the work itself, and a retracted article marked on its own DOI is
    # still the article — neither is a DOI error.
    upd = [u for u in (m.get("update-to") or [])
           if clean_doi(u.get("DOI", "")) != doi and u.get("type") not in ("new_version", "new_edition")]
    if upd:
        u = upd[0]
        kind = "retraction notice" if "retraction" in (u.get("type") or "") else "erratum / correction notice"
        return (kind, title, clean_doi(u.get("DOI", "")),
                f"Crossref registers this DOI as a {u.get('type')} of {clean_doi(u.get('DOI', ''))}")
    if _NOT_THE_WORK.match(title):
        if re.match(r"^\s*author response", title, re.I):
            return ("author response / peer review", title, "", "registry title is an author response to reviewers")
        if re.match(r"^\s*(response|reply|comment)", title, re.I):
            if _NOT_THE_WORK.match(stated or ""):
                return None                                # a reply/comment listed as such: a scope call, not a DOI error
            return ("reply / comment instead of the article", title, "", "registry title is a reply or comment")
        return ("erratum / correction notice", title, "", "registry title is a correction notice")
    if (stated or "").strip().upper() == "WITHDRAWN":
        return ("withdrawn preprint", title, "", "the Observatory's title reads 'WITHDRAWN' while the DOI's registry "
                "record carries the title shown — the preprint was likely withdrawn; worth checking whether to keep the row")
    if doi in _REVIEWED_OTHER:
        return ("DOI for another paper", title, "", _REVIEWED_OTHER[doi])
    if stated and title and jaccard_similarity(title, stated) < _OTHER_PAPER_SIM and doi not in _TRANSLATED:
        return ("DOI for another paper", title, "",
                f"registry title shares {jaccard_similarity(title, stated):.0%} of its words with the title given")
    return None


def not_a_doi(value: str) -> tuple[str, str, str, str] | None:
    """Identifiers in the URL fields that are not a clean doi.org DOI."""
    if not value:
        return None
    if "semanticscholar.org" in value:
        pid = value.rstrip("/").split("/")[-1]
        info = s2(pid) if "/paper/" in value else {}
        doi = clean_doi((info.get("externalIds") or {}).get("DOI", ""))
        ev = f"S2 record: {info.get('title', '')} ({info.get('year', '')})" if info.get("title") else \
            "Semantic Scholar PDF link" if "pdfs." in value else "S2 record not found"
        return ("Semantic Scholar URL, not a DOI", info.get("title", ""), doi, ev)
    if not _DOI_URL.match(value):
        return None                                        # other landing URL: not our business
    doi = clean_doi(value)
    if re.match(r"^10\.\d{4,9}/\S+$", doi) and "%" not in doi and not doi.startswith("http"):
        return None
    fixed = urllib.parse.unquote(doi)
    fixed = re.sub(r"^https?://(dx\.)?doi\.org/", "", fixed)
    fixed = re.split(r"\s+|;", fixed)[0] if re.match(r"^10\.", fixed) else ""
    if fixed and fixed != doi:
        return ("malformed DOI string", _cr_title(fixed), fixed,
                "junk appended, URL-encoding or a doubled doi.org prefix; the leading DOI resolves"
                if _cr_title(fixed) else "junk appended or URL-encoded")
    return ("malformed DOI string", "", "", "not a DOI")


def from_pairs() -> pd.DataFrame:
    p = pd.read_csv(PAIRS, dtype=str).fillna("")
    p = p[p.class_b == "mistake"]
    rows = []
    for r in p.itertuples():
        suggest = r.doi_a if r.class_a == "work" else ""
        rows.append({"doi_r": r.doi_r, "given": r.doi_b, "pair_detail": r.detail,
                     "pair_suggest": suggest, "pair_population": r.population})
    return pd.DataFrame(rows)


def build() -> pd.DataFrame:
    d = load_raw()
    pairs = from_pairs()
    d["doi_r"] = d.replication_url.map(lambda v: clean_doi(v) if _DOI_URL.match(v) else v)
    found: dict[tuple[str, str], dict] = {}
    for f, tcol in FIELDS.items():
        for v, grp in d.groupby(f):
            if not v:
                continue
            doi = clean_doi(v) if _DOI_URL.match(v) else ""
            hit = not_a_doi(v)
            if hit is None and doi:
                hit = classify(doi, grp[tcol].iloc[0])
            pair = pairs[(pairs.given == doi)] if doi else pairs.iloc[0:0]
            if hit is None and pair.empty:
                continue
            cat, reg, sugg, ev = hit or ("", "", "", "")
            if not pair.empty:
                pr = pair.iloc[0]
                sugg = sugg or pr.pair_suggest
                ev = "; ".join(x for x in (ev, f"same-title comparison ({pr.pair_population}): {pr.pair_detail}") if x)
                if not cat:
                    cat = "DOI for another paper" if "different work" in pr.pair_detail or "carries the DOI" in pr.pair_detail \
                        else "book review, not the work" if "REVIEWS" in pr.pair_detail else "other"
                    reg = reg or _cr_title(doi)
            if cat == "unresolvable DOI" and not sugg:
                sugg = _repair(doi)
                if sugg:
                    ev += f"; {sugg} (the same string repaired) resolves to “{_cr_title(sugg)}”"
            if not sugg and cat in ("unresolvable DOI", "Semantic Scholar URL, not a DOI", "DOI for another paper",
                                    "journal-issue DOI", "erratum / correction notice"):
                g0 = grp.iloc[0]
                first = re.split(r"[,;&]| and ", g0[_AUTH[f]])[0].strip()
                sugg = _cr_search(g0[tcol] or reg, g0[_YEAR[f]], first)
                if sugg:
                    t = (crossref(sugg).get("msg") or {}).get("type") or ""
                    ev += f"; Crossref search on the stated title finds {sugg} (“{_cr_title(sugg)}”)" + \
                        (f" — a {t.replace('-', ' ')}, not necessarily the version of record" if t != "journal-article" else "")
            found[(f, v)] = {
                "category": cat, "field": f, "value_as_given": v,
                "observatory_records": ", ".join(map(str, grp.record)),
                "n_records": len(grp),
                "replication_ids": " | ".join(sorted(set(grp.replication_url)))[:500],
                "stated_title": grp[tcol].iloc[0],
                "registry_title": reg,
                "suggested_doi": sugg,
                "evidence": ev,
            }
    out = pd.DataFrame(found.values()).sort_values(["category", "field", "value_as_given"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        fetch()
        return
    out = build()
    out.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(out.category.value_counts().to_string())
    print(f"{len(out)} issues, {out.n_records.sum()} Observatory records -> {OUT}")


if __name__ == "__main__":
    main()
