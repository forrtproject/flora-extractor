"""
openalex.py — OpenAlex API helpers + author-year citation pattern extraction.

Public API:
    extract_author_year_patterns(text, max_year) → list[dict]
    fetch_referenced_works_metadata(openalex_id, cache) → list[dict]
    find_all_candidates(doi_r, openalex_id_r, study_r, abstract_r,
                        year_r, pattern_str) → list[dict]
    fetch_openalex_by_doi(doi) → Optional[dict]
"""
import json
import re
import time
from pathlib import Path
from typing import Optional

import requests

from .config import (
    OA_CACHE_DIR, OPENALEX_API_KEY, OPENALEX_RATE_SEC, RESEARCHER_EMAIL, log,
)
from .utils import clean_doi, cache_key

# ── Unicode ranges (chr() avoids \u in compiled regexes for Python < 3.12) ────
_UNI_RANGE  = chr(0x00C0) + "-" + chr(0x024F) + chr(0x1E00) + "-" + chr(0x1EFF)
_UPPER_UNI  = chr(0x00C0) + "-" + chr(0x024F)
_LETTER     = rf"[\w{_UNI_RANGE}]"
_PREFIX     = (r"(?:van\s+der\s+|van\s+|von\s+|de\s+la\s+|de\s+|da\s+|"
               r"del\s+|den\s+|der\s+|du\s+|le\s+|la\s+|el\s+|al\s+)?")
_NAME       = rf"(?:{_PREFIX}[A-Z{_UPPER_UNI}]{_LETTER}{{2,}})"
_YEAR       = r"(?:19|20)\d{2}"

# Patterns ordered most-specific → least-specific (avoids partial overlaps)
_PATTERNS: list[tuple[str, str]] = [
    ("multi_and_paren",
     rf"({_NAME}(?:,\s*{_NAME}){{1,}},?\s+(?:and|&)\s+{_NAME})\s*'?s?\s*\(({_YEAR})\)"),
    ("multi_and_bare",
     rf"({_NAME}(?:,\s*{_NAME}){{1,}},?\s+(?:and|&)\s+{_NAME}),?\s+({_YEAR})(?!\d)"),
    ("etal_paren",
     rf"({_NAME})\s+et\s+al\.?\s*'?s?\s*\(({_YEAR})\)"),
    ("etal_bare",
     rf"({_NAME})\s+et\s+al\.?\s*,?\s+({_YEAR})(?!\d)"),
    ("two_and_paren",
     rf"({_NAME})\s+(?:and|&)\s+({_NAME})\s*'?s?\s*\(({_YEAR})\)"),
    ("two_and_bare",
     rf"({_NAME})\s+(?:and|&)\s+({_NAME}),?\s+({_YEAR})(?!\d)"),
    ("single_paren",
     rf"({_NAME})\s*'?s?\s*\(({_YEAR})\)"),
    ("single_bare",
     rf"({_NAME}),?\s+({_YEAR})(?!\d)"),
]

_COMPILED = [(name, re.compile(pat)) for name, pat in _PATTERNS]


def extract_author_year_patterns(text: str,
                                  max_year: Optional[int] = None) -> list[dict]:
    """
    Parse author-year citation patterns from *text*.

    Returns a list of dicts:
        surname   – first-author surname (lowercased)
        year      – publication year (int)
        raw       – matched string
        pattern   – pattern name
        start/end – character offsets
    Overlapping matches are deduplicated; years > max_year are excluded.
    """
    if not text:
        return []

    results: list[dict] = []
    covered: list[tuple[int, int]] = []

    for pat_name, rx in _COMPILED:
        for m in rx.finditer(text):
            start, end = m.start(), m.end()
            if any(s < end and start < e for s, e in covered):
                continue

            groups   = m.groups()
            year_str = groups[-1]
            surname  = re.sub(r"[\s']", "", groups[0])
            surname  = surname.split()[-1] if " " in surname else surname

            try:
                year = int(year_str)
            except ValueError:
                continue

            if year < 1900 or year > 2099:
                continue
            if max_year is not None and year > max_year:
                continue

            results.append({
                "surname": surname.lower(),
                "year"   : year,
                "raw"    : m.group(0),
                "pattern": pat_name,
                "start"  : start,
                "end"    : end,
            })
            covered.append((start, end))

    return results


# ── OpenAlex API ──────────────────────────────────────────────────────────────

_OA_HEADERS: dict[str, str] = {
    "User-Agent": (
        f"FLoRA-DisambiguationPipeline/1.0 (mailto:{RESEARCHER_EMAIL})"
    )
}
if OPENALEX_API_KEY:
    _OA_HEADERS["Authorization"] = OPENALEX_API_KEY
_oa_last_call = 0.0


def _oa_get(url: str, params: dict | None = None) -> Optional[dict]:
    """GET from OpenAlex with rate limiting, 429 retry, and error handling."""
    global _oa_last_call
    wait = OPENALEX_RATE_SEC - (time.time() - _oa_last_call)
    if wait > 0:
        time.sleep(wait)
    _oa_last_call = time.time()

    _RETRY_DELAYS = [5, 15, 30]  # seconds to wait after 1st, 2nd, 3rd 429
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            r = requests.get(url, headers=_OA_HEADERS, params=params or {},
                             timeout=30)
            if r.status_code == 429:
                if attempt >= len(_RETRY_DELAYS):
                    break
                # Use our own schedule — OpenAlex sometimes sends absurdly large
                # Retry-After values (e.g. 40000+s) that would stall the pipeline.
                delay = _RETRY_DELAYS[attempt]
                log.warning("OpenAlex 429 — waiting %ds before retry %d/%d",
                            delay, attempt + 1, len(_RETRY_DELAYS))
                time.sleep(delay)
                _oa_last_call = time.time()
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError:
            break
        except Exception as e:
            log.warning("OpenAlex request failed: %s — %s", url, e)
            return None

    log.warning("OpenAlex request failed after retries: %s", url)
    return None


def fetch_referenced_works_metadata(openalex_id: str,
                                    use_cache: bool = True) -> list[dict]:
    """
    Return full metadata for every work referenced by *openalex_id*.

    Cached as JSON in OA_CACHE_DIR / refs_<bare_id>.json.
    Each item has: id, doi, title, publication_year, authorships.
    """
    bare       = re.sub(r"https?://openalex\.org/", "", openalex_id).strip()
    cache_file = OA_CACHE_DIR / f"refs_{bare}.json"

    if use_cache and cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    # Step 1: fetch the work to get its referenced_works list
    work = _oa_get(
        f"https://api.openalex.org/works/{bare}",
        {"mailto": RESEARCHER_EMAIL},
    )
    if not work:
        return []

    ref_ids = work.get("referenced_works", [])
    if not ref_ids:
        return []

    # Step 2: batch-fetch metadata (up to 50 IDs per request)
    results: list[dict] = []
    batch_size = 50
    bare_refs  = [re.sub(r"https?://openalex\.org/", "", rid) for rid in ref_ids]

    for i in range(0, len(bare_refs), batch_size):
        batch = bare_refs[i : i + batch_size]
        data  = _oa_get(
            "https://api.openalex.org/works",
            {
                "filter"  : f"openalex_id:{'|'.join(batch)}",
                "per-page": str(batch_size),
                "select"  : "id,doi,title,publication_year,authorships",
                "mailto"  : RESEARCHER_EMAIL,
            },
        )
        if data and "results" in data:
            results.extend(data["results"])

    if use_cache:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)

    return results


# ── Author matching ───────────────────────────────────────────────────────────

def _first_author_surnames(work: dict) -> list[str]:
    """Extract all author surnames from an OpenAlex work dict."""
    surnames: list[str] = []
    for auth in work.get("authorships", []):
        display = auth.get("author", {}).get("display_name", "")
        if display:
            parts = display.replace(",", "").split()
            if parts:
                surnames.append(parts[-1])
    return surnames


def author_matches(cited_surname: str,
                   ref_authors: list[str],
                   min_prefix: int = 3) -> bool:
    """
    Return True if *cited_surname* plausibly matches any name in *ref_authors*.

    Matching modes:
      1. Exact case-insensitive
      2. Prefix match either direction (≥ min_prefix chars)
      3. Near-prefix: allow 1-char difference at the end
    """
    cited = cited_surname.strip().lower()
    if not cited:
        return False

    for raw in ref_authors:
        ref = raw.strip().lower()
        if not ref:
            continue

        if cited == ref:
            return True

        shorter, longer = (cited, ref) if len(cited) <= len(ref) else (ref, cited)
        if len(shorter) >= min_prefix and longer.startswith(shorter):
            return True

        if len(shorter) >= min_prefix + 1:
            if longer[: len(shorter) - 1] == shorter[: len(shorter) - 1]:
                return True

    return False


# ── Candidate recovery ────────────────────────────────────────────────────────

def find_all_candidates(doi_r: str,
                         openalex_id_r: str,
                         study_r: str,
                         abstract_r: str,
                         year_r: int,
                         pattern_str: str = "") -> list[dict]:
    """
    Re-fetch all referenced works for *openalex_id_r* and return EVERY work
    that matches any extracted author-year pattern.

    Cached per doi_r in OA_CACHE_DIR / candidates_<hash>.json.

    Returns a list of dicts:
        openalex_id, doi, title, year, first_author,
        match_year_exact, cited_pattern
    """

    cache_file = OA_CACHE_DIR / f"candidates_{cache_key(doi_r)}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    if not openalex_id_r:
        log.warning("[%s] find_all_candidates: no openalex_id_r — returning empty candidates", doi_r)
        return []

    # Extract author-year patterns from title and abstract. 
    # extract_author_year_patterns() always returns a list, so we can concatenate results immediately.
    patterns = extract_author_year_patterns(study_r, max_year=year_r) \
        + extract_author_year_patterns(abstract_r, max_year=year_r)
        # + extract_author_year_patterns(pattern_str, max_year=year_r)

    if not patterns:
        return []

    refs = fetch_referenced_works_metadata(openalex_id_r)
    if not refs:
        return []

    candidates: list[dict] = []
    seen_ids:   set[str]   = set()

    for pat in patterns:
        for yr_delta in (0, 1, -1):
            target_year = pat["year"] + yr_delta
            year_exact  = (yr_delta == 0)

            for ref in refs:
                if ref.get("publication_year") != target_year:
                    continue

                ref_id  = ref.get("id", "")
                ref_doi = clean_doi(ref.get("doi", "") or "")

                # Skip self-match
                if ref_doi and ref_doi == clean_doi(doi_r):
                    continue

                ref_authors = _first_author_surnames(ref)
                if author_matches(pat["surname"], ref_authors):
                    if ref_id not in seen_ids:
                        seen_ids.add(ref_id)
                        candidates.append({
                            "openalex_id"     : ref_id,
                            "doi"             : ref_doi,
                            "title"           : ref.get("title", ""),
                            "year"            : target_year,
                            "first_author"    : ref_authors[0] if ref_authors else "",
                            "all_authors"     : ref_authors,
                            "match_year_exact": year_exact,
                            "cited_pattern"   : pat["raw"],
                        })

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with cache_file.open("w", encoding="utf-8") as fh:
        json.dump(candidates, fh, ensure_ascii=False, indent=2)

    return candidates


def fetch_openalex_by_doi(doi: str) -> Optional[dict]:
    """
    Fetch OpenAlex metadata for a specific DOI and return a candidate dict
    in the same format as find_all_candidates() entries.

    Used to inject the FLoRA-verified original into the candidate pool for
    validated DOIs. Cached per DOI in OA_CACHE_DIR/doi_lookup_<hash>.json.
    Returns None if the DOI is not found or the request fails.
    """
    doi = clean_doi(doi)
    if not doi:
        return None

    cache_file = OA_CACHE_DIR / f"doi_lookup_{cache_key(doi)}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    data = _oa_get(
        "https://api.openalex.org/works",
        {
            "filter" : f"doi:{doi}",
            "select" : "id,doi,title,publication_year,authorships",
            "mailto" : RESEARCHER_EMAIL,
        },
    )
    if not data or not data.get("results"):
        return None

    work    = data["results"][0]
    authors = _first_author_surnames(work)
    result  = {
        "openalex_id"     : work.get("id", ""),
        "doi"             : clean_doi(work.get("doi", "") or ""),
        "title"           : work.get("title", ""),
        "year"            : work.get("publication_year"),
        "first_author"    : authors[0] if authors else "",
        "all_authors"     : authors,
        "match_year_exact": True,
        "cited_pattern"   : "flora_anchor",
    }

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with cache_file.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    return result
