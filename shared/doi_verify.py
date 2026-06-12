"""
doi_verify.py — Verify that a resolved doi_o actually points to the resolved
original study.

LLM resolution can hallucinate DOIs: title/author correct but the DOI pointing
to a different (yet perfectly registered) paper. A doi.org resolution check
cannot catch this, so we fetch the metadata the DOI points to (CrossRef, the
registry of record; OpenAlex fallback) and compare title/year. On mismatch we
re-resolve the DOI from title+author via CrossRef bibliographic search, with an
OpenAlex fallback because OpenAlex also indexes DOI-less works (old papers,
book chapters).
"""
from __future__ import annotations

import re
import time
from typing import Optional

import requests

from shared.cache import read_cache, write_cache
from shared.config import CROSSREF_RATE_SEC, DOI_VERIFY_CACHE_DIR, RESEARCHER_EMAIL, log
from shared.disambiguation import jaccard_similarity
from shared.openalex_client import author_matches
from shared.utils import cache_key, clean_doi

# Tunable thresholds — starting points, not yet empirically validated.
VERIFY_TITLE_JACCARD  = 0.5   # DOI metadata vs resolved title → "verified"
RESOLVE_TITLE_JACCARD = 0.7   # search hit vs resolved title → safe to auto-correct
YEAR_TOLERANCE        = 1     # print vs online publication year offset

_HEADERS      = {"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"}
_RETRY_DELAYS = [0, 1, 2, 4]  # initial attempt + 3 retries per CLAUDE.md


def _get_json(url: str, params: dict) -> "tuple[Optional[dict], bool]":
    """GET with retries. Returns (json, not_found).

    (None, True)  → HTTP 404 (a definitive answer, not retried)
    (None, False) → hard failure after all retries
    """
    for delay in _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=20)
            if r.status_code == 404:
                return None, True
            # other 4xx (except rate-limit 429) are definitive — retrying won't help
            if 400 <= r.status_code < 500 and r.status_code != 429:
                log.warning("doi_verify GET %s: HTTP %d (not retried)", url, r.status_code)
                return None, False
            r.raise_for_status()
            time.sleep(CROSSREF_RATE_SEC)
            return r.json(), False
        except Exception as exc:
            log.warning("doi_verify GET %s failed: %s", url, exc)
    return None, False


def _to_year(year) -> Optional[int]:
    try:
        return int(float(str(year)))
    except (ValueError, TypeError):
        return None


def _crossref_year(msg: dict) -> Optional[int]:
    for k in ("published-print", "published-online", "issued"):
        parts = (msg.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    return None


def _surname(author: str) -> str:
    """Best-effort surname from 'Surname', 'Surname, First' or 'First Surname'."""
    author = (author or "").strip()
    if not author:
        return ""
    if "," in author:
        return author.split(",")[0].strip()
    return author.split()[-1]


def fetch_doi_metadata(doi: str) -> Optional[dict]:
    """Return the metadata *doi* currently points to, or None on api_error.

    Shape: {"registered": bool, "title": str, "first_author_surname": str,
            "year": int|None, "source": "crossref"|"openalex"}
    """
    doi = clean_doi(doi)
    key = cache_key(doi + "_doimeta")
    cached = read_cache(DOI_VERIFY_CACHE_DIR, key)
    if cached is not None:
        return cached

    data, not_found = _get_json(f"https://api.crossref.org/works/{doi}",
                                {"mailto": RESEARCHER_EMAIL})
    if data:
        msg     = data.get("message") or {}
        titles  = msg.get("title") or []
        authors = msg.get("author") or []
        meta = {
            "registered": True,
            "title": titles[0] if titles else "",
            "first_author_surname": (authors[0].get("family", "") if authors else ""),
            "year": _crossref_year(msg),
            "source": "crossref",
        }
        write_cache(DOI_VERIFY_CACHE_DIR, key, meta)
        return meta
    if not_found:
        meta = {"registered": False, "title": "", "first_author_surname": "",
                "year": None, "source": "crossref"}
        write_cache(DOI_VERIFY_CACHE_DIR, key, meta)
        return meta

    # CrossRef hard-failed → OpenAlex fallback
    data, _ = _get_json("https://api.openalex.org/works",
                        {"filter": f"doi:{doi}",
                         "select": "title,authorships,publication_year",
                         "mailto": RESEARCHER_EMAIL})
    if data and data.get("results"):
        w = data["results"][0]
        first = ((w.get("authorships") or [{}])[0].get("author") or {}).get("display_name", "")
        meta = {
            "registered": True,
            "title": w.get("title") or "",
            "first_author_surname": _surname(first),
            "year": w.get("publication_year"),
            "source": "openalex",
        }
        write_cache(DOI_VERIFY_CACHE_DIR, key, meta)
        return meta

    # Both unavailable (or OpenAlex has no record while CrossRef is down):
    # cannot distinguish wrong-DOI from outage → api_error, not cached.
    return None


def metadata_matches(meta: Optional[dict], title: str, author: str, year) -> bool:
    """True when *meta* plausibly describes (title, year). Author is not
    required here — surname formats vary too much across registries; the
    title Jaccard + year window carries the decision."""
    if not meta or not meta.get("registered") or not (title or "").strip():
        return False
    if jaccard_similarity(meta.get("title", ""), title) < VERIFY_TITLE_JACCARD:
        return False
    y_meta, y = meta.get("year"), _to_year(year)
    if y_meta and y and abs(y_meta - y) > YEAR_TOLERANCE:
        return False
    return True


def _score_hit(hit_title: str, hit_year, hit_surnames: list[str],
               title: str, author: str, year) -> bool:
    """Strict acceptance: a wrong auto-correction is worse than a flag."""
    if jaccard_similarity(hit_title, title) < RESOLVE_TITLE_JACCARD:
        return False
    y_hit, y = _to_year(hit_year), _to_year(year)
    if y_hit and y and abs(y_hit - y) > YEAR_TOLERANCE:
        return False
    surname = _surname(author)
    if surname and hit_surnames and not author_matches(surname, hit_surnames):
        return False
    return True


def _sanitize_search(text: str) -> str:
    """OpenAlex's search param returns HTTP 400 for some punctuation (e.g. '?').
    Keep word characters, spaces and hyphens; collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\-]", " ", text)).strip()


def resolve_doi_by_metadata(title: str, author: str, year,
                            exclude_doi: str = "") -> Optional[dict]:
    """Find the DOI for (title, author, year) via CrossRef bibliographic
    search, OpenAlex fallback.

    exclude_doi: the replication's own DOI — replication titles often echo the
    original's title, so the search can return the replication paper itself.

    Returns {"doi", "title", "year", "openalex_id", "source"} or None.
    "doi" is "" when the work exists only in OpenAlex without a DOI.
    """
    title = (title or "").strip()
    if not title:
        return None
    exclude = clean_doi(exclude_doi or "")
    key = cache_key(f"{title}|{author}|{year}|{exclude}_doisearch")
    cached = read_cache(DOI_VERIFY_CACHE_DIR, key)
    if cached is not None:
        return cached if cached.get("found") else None

    data, _ = _get_json("https://api.crossref.org/works",
                        {"query.bibliographic": f"{title} {author}".strip(),
                         "rows": 5, "mailto": RESEARCHER_EMAIL})
    crossref_failed = data is None
    for item in ((data or {}).get("message") or {}).get("items", []):
        hit_doi      = clean_doi(item.get("DOI", ""))
        hit_title    = (item.get("title") or [""])[0]
        hit_year     = _crossref_year(item)
        hit_surnames = [a.get("family", "") for a in (item.get("author") or []) if a.get("family")]
        if exclude and hit_doi == exclude:
            continue
        if _score_hit(hit_title, hit_year, hit_surnames, title, author, year):
            hit = {"found": True, "doi": hit_doi,
                   "title": hit_title, "year": hit_year,
                   "openalex_id": "", "source": "crossref"}
            write_cache(DOI_VERIFY_CACHE_DIR, key, hit)
            return hit

    data, _ = _get_json("https://api.openalex.org/works",
                        {"search": _sanitize_search(title), "per-page": 5,
                         "select": "id,doi,title,authorships,publication_year",
                         "mailto": RESEARCHER_EMAIL})
    openalex_failed = data is None
    for w in (data or {}).get("results", []):
        hit_doi      = clean_doi(w.get("doi") or "")
        hit_title    = w.get("title") or ""
        hit_year     = w.get("publication_year")
        hit_surnames = [_surname(((a.get("author") or {}).get("display_name", "")))
                        for a in (w.get("authorships") or [])]
        hit_surnames = [s for s in hit_surnames if s]
        if exclude and hit_doi == exclude:
            continue
        if _score_hit(hit_title, hit_year, hit_surnames, title, author, year):
            hit = {"found": True, "doi": hit_doi,
                   "title": hit_title, "year": hit_year,
                   "openalex_id": w.get("id", ""), "source": "openalex"}
            write_cache(DOI_VERIFY_CACHE_DIR, key, hit)
            return hit

    if not (crossref_failed and openalex_failed):
        write_cache(DOI_VERIFY_CACHE_DIR, key, {"found": False})
    return None


def verify_and_correct(doi_o, title_o, author_o, year_o,
                       exclude_doi: str = "") -> dict:
    """Verify (and if needed correct) doi_o against the resolved metadata.

    exclude_doi: the replication's own doi_r, never a valid correction target.

    Returns {"doi_o_verification": <status>, "doi_o": <possibly new doi>,
             "evidence_note": <text to append to link_evidence, may be "">}.
    """
    doi   = clean_doi(doi_o or "")
    title = (title_o or "").strip()

    if not doi and not title:
        return {"doi_o_verification": "skipped", "doi_o": doi, "evidence_note": ""}

    if doi:
        meta = fetch_doi_metadata(doi)
        if meta is None:
            return {"doi_o_verification": "api_error", "doi_o": doi,
                    "evidence_note": "DOI verification failed: CrossRef and OpenAlex unavailable"}
        if metadata_matches(meta, title, author_o, year_o):
            return {"doi_o_verification": "verified", "doi_o": doi, "evidence_note": ""}

        repl = resolve_doi_by_metadata(title, author_o, year_o, exclude_doi=exclude_doi)
        if repl and repl.get("doi"):
            reason = "pointed to a different work" if meta["registered"] else "is not registered"
            return {"doi_o_verification": "corrected", "doi_o": repl["doi"],
                    "evidence_note": (f"DOI corrected: {doi} {reason}; "
                                      f"replaced with {repl['doi']} (\"{repl['title']}\")")}
        if repl:
            return {"doi_o_verification": "no_doi", "doi_o": "",
                    "evidence_note": (f"DOI removed: {doi} did not match; original exists "
                                      f"without a DOI ({repl.get('openalex_id', '')})")}
        if meta["registered"]:
            return {"doi_o_verification": "mismatch", "doi_o": doi,
                    "evidence_note": (f"DOI mismatch: {doi} points to "
                                      f"\"{meta['title']}\" ({meta['year']}), not \"{title}\"")}
        return {"doi_o_verification": "no_metadata", "doi_o": doi,
                "evidence_note": f"DOI {doi} is not registered and no replacement was found"}

    # Blank DOI, title available — try to fill it.
    repl = resolve_doi_by_metadata(title, author_o, year_o, exclude_doi=exclude_doi)
    if repl and repl.get("doi"):
        return {"doi_o_verification": "corrected", "doi_o": repl["doi"],
                "evidence_note": f"DOI filled from metadata search: {repl['doi']}"}
    if repl:
        return {"doi_o_verification": "no_doi", "doi_o": "",
                "evidence_note": (f"Original has no registered DOI "
                                  f"({repl.get('openalex_id', '')})")}
    return {"doi_o_verification": "not_found", "doi_o": "",
            "evidence_note": "No DOI found for resolved title/author"}
