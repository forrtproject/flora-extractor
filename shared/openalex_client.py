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
    OA_CACHE_DIR, OPENALEX_API_KEY, OPENALEX_RATE_SEC, CROSSREF_RATE_SEC,
    RESEARCHER_EMAIL, log,
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

# ── strict_bare gate ─────────────────────────────────────────────────────────
# The single_bare pattern ({NAME},?\s+{YEAR}) matches any capitalised >=3-letter
# token before a year, so date/structural phrases ("January 2020", "Study 2019",
# "Between 1966", "COVID 2019") fire as if they were citations.  In Stage 2 a
# single citation match promotes a row from needs_review (LLM review) to a
# high-confidence accept, so these false matches bypass the LLM.  When
# strict_bare=True, a single_bare match whose leading name token is one of these
# words is dropped.  Measured on the full data/filtered.csv (2.3M rows, 16,126
# gate-firing rows) this flips ~15.7% of them from auto-accept to needs_review,
# and eyeballing the removed matches ~96-98% are genuine non-citations (dates,
# months, structural words) with only ~2-4% real citations lost — mostly
# corporate authors like "…Research Group, 1992", which then simply get LLM
# review instead of a rule-based accept, so no record is dropped.  A more
# aggressive rule (requiring a comma before the bare year) flipped ~29% but lost
# ~12% real bare citations, so the blacklist is the best precision/recall
# trade-off (see analysis/citation_gate_analysis.py).  Stage 3 candidate finding
# keeps strict_bare=False, where recall matters more.
import calendar as _calendar

_BARE_LEADING_BLACKLIST: frozenset[str] = frozenset(
    w.lower() for w in (
        {m for m in _calendar.month_name if m}
        | {m for m in _calendar.month_abbr if m}
        | {"Winter", "Spring", "Summer", "Fall", "Autumn"}
        | {  # structural / document words
            "Study", "Studies", "Table", "Figure", "Fig", "Experiment",
            "Experiments", "Session", "Sessions", "Wave", "Waves", "Sample",
            "Samples", "Model", "Models", "Appendix", "Chapter", "Section",
            "Panel", "Phase", "Trial", "Trials", "Cohort", "Group", "Groups",
            "Item", "Items", "Question", "Version", "Round", "Block",
            "Condition", "Column", "Row", "Note", "Equation", "Hypothesis",
            "Day", "Week", "Month", "Year", "Time", "Age", "Quarter", "Volume",
            "Vol", "Issue", "Number", "No", "Page", "Part", "Level", "Step",
            "Set", "Series", "Line", "Site", "Class", "Type", "Grade",
        }
        | {  # disease / entity acronyms
            "COVID", "SARS", "MERS", "HIV", "AIDS", "EU", "US", "USA", "UK",
            "UN", "WHO", "GDP", "AI", "ML", "PCR", "DNA", "RNA",
        }
        | {  # capitalised function words that commonly begin a sentence
            "Since", "During", "Between", "From", "Until", "After", "Before",
            "In", "On", "By", "At", "For", "With", "Within", "Over", "Through",
            "Under", "Around", "About", "Across", "Throughout", "Post", "Pre",
            "Early", "Late", "The", "This", "That", "These", "Those", "Their",
            "Our", "Its", "And", "But", "However", "Thus", "Here", "There",
            "When", "While", "Copyright", "Circa", "Ca", "Fiscal", "Academic",
            "Christmas", "Easter",
        }
    )
)


def _bare_leading_blacklisted(name_group: str) -> bool:
    """True if the leading name token of a single_bare match is a blacklisted
    (non-surname) word, e.g. a month, season, or structural document word."""
    lead = name_group.strip().lower().rstrip(",")
    if not lead:
        return False
    lead_last = lead.split()[-1] if " " in lead else lead
    return lead in _BARE_LEADING_BLACKLIST or lead_last in _BARE_LEADING_BLACKLIST


def extract_author_year_patterns(text: str,
                                  max_year: Optional[int] = None,
                                  strict_bare: bool = False) -> list[dict]:
    """
    Parse author-year citation patterns from *text*.

    Returns a list of dicts:
        surname   – first-author surname (lowercased)
        year      – publication year (int)
        raw       – matched string
        pattern   – pattern name
        start/end – character offsets
    Overlapping matches are deduplicated; years > max_year are excluded.

    strict_bare – when True, drop single_bare matches whose leading token is a
    blacklisted non-surname word (months, seasons, structural words, disease
    acronyms, sentence-initial function words).  Used by the Stage-2 filter,
    where a spurious match wrongly auto-accepts a row past LLM review; left
    False for Stage-3 candidate finding, where recall matters more.
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

            if strict_bare and pat_name == "single_bare" \
                    and _bare_leading_blacklisted(groups[0]):
                continue

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


def format_author_apa(display_name: str) -> str:
    """Convert OpenAlex display_name to APA format.

    'John D. Bransford' → 'Bransford, J. D.'
    'J. Richard Barclay' → 'Barclay, J. R.'
    """
    parts = display_name.strip().split()
    if not parts:
        return display_name
    if len(parts) == 1:
        return parts[0]
    last = parts[-1]
    firsts = parts[:-1]
    initials = " ".join(p if p.endswith(".") else (p[0] + ".") for p in firsts if p)
    return f"{last}, {initials}" if initials else last


def _all_authors_apa(work: dict) -> list[str]:
    """Return APA-formatted names for all authors in an OpenAlex work dict."""
    names: list[str] = []
    for auth in work.get("authorships", []):
        display = (auth.get("author") or {}).get("display_name", "")
        if display:
            names.append(format_author_apa(display))
    return names


def _crossref_author_apa(family: str, given: str) -> str:
    """Format CrossRef family/given pair as APA: 'Family, G. I.'

    CrossRef given names can be full ('John D.') or already initials ('J. D.').
    Each token is converted to an initial if it doesn't already end with a period.
    """
    family = family.strip()
    if not family:
        return given.strip()
    given = given.strip()
    if not given:
        return family
    initials = " ".join(
        p if p.endswith(".") else (p[0] + ".")
        for p in given.split() if p
    )
    return f"{family}, {initials}" if initials else family


def _fetch_crossref_full_meta(doi: str) -> Optional[dict]:
    """Fetch full metadata from CrossRef API for *doi*.

    Returns same shape as fetch_openalex_full_metadata, or None on failure.
    CrossRef is the DOI registry of record and covers works OpenAlex doesn't index
    (book chapters, older papers, DataCite DOIs).
    """
    try:
        r = requests.get(
            f"https://api.crossref.org/works/{doi}",
            headers={"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"},
            timeout=20,
        )
        time.sleep(CROSSREF_RATE_SEC)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        msg = r.json().get("message", {})
    except Exception as exc:
        log.warning("CrossRef full meta %s failed: %s", doi, exc)
        return None

    raw_authors = msg.get("author") or []
    authors = [
        _crossref_author_apa(a.get("family", ""), a.get("given", ""))
        for a in raw_authors
        if a.get("family") or a.get("given")
    ]

    # Year: prefer published-print, then published-online, then issued
    year = None
    for k in ("published-print", "published-online", "issued", "published"):
        parts = (msg.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break

    titles = msg.get("title") or []
    title  = titles[0] if titles else ""

    containers = msg.get("container-title") or []
    journal    = containers[0] if containers else ""

    page = msg.get("page") or ""
    first_page, last_page = ("", "")
    if "-" in page:
        first_page, _, last_page = page.partition("-")
    elif page:
        first_page = page

    return {
        "doi"       : clean_doi(msg.get("DOI", "") or doi),
        "title"     : title,
        "year"      : year,
        "authors"   : authors,
        "journal"   : journal,
        "volume"    : msg.get("volume") or "",
        "issue"     : msg.get("issue") or "",
        "first_page": first_page.strip(),
        "last_page" : last_page.strip(),
    }


def _fetch_doi_org_full_meta(doi: str) -> Optional[dict]:
    """Resolve *doi* via doi.org with CSL-JSON content negotiation.

    Covers DOIs registered with any registrar (DataCite, mEDRA, CrossRef, etc.)
    — not just CrossRef.  Returns same shape as fetch_openalex_full_metadata,
    or None if the DOI doesn't resolve.
    """
    headers = {
        "Accept"    : "application/vnd.citationstyles.csl+json",
        "User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})",
    }
    for delay in (0, 1, 2):
        if delay:
            time.sleep(delay)
        try:
            r = requests.get(f"https://doi.org/{doi}", headers=headers,
                             timeout=20, allow_redirects=True)
            time.sleep(CROSSREF_RATE_SEC)
            if r.status_code == 404:
                return None
            if 400 <= r.status_code < 500 and r.status_code != 429:
                return None
            r.raise_for_status()
            csl = r.json()
            break
        except Exception as exc:
            log.debug("doi.org CSL %s failed: %s", doi, exc)
            csl = None
    else:
        return None

    if not csl:
        return None

    raw_authors = csl.get("author") or []
    authors = [
        _crossref_author_apa(a.get("family", ""), a.get("given", ""))
        for a in raw_authors
        if a.get("family") or a.get("given")
    ]

    year = None
    for k in ("issued", "published-print", "published-online"):
        parts = (csl.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break

    title      = csl.get("title") or ""
    journal    = csl.get("container-title") or ""
    page       = csl.get("page") or ""
    first_page, _, last_page = page.partition("-") if "-" in page else (page, "", "")

    return {
        "doi"       : clean_doi(csl.get("DOI", "") or doi),
        "title"     : title,
        "year"      : year,
        "authors"   : authors,
        "journal"   : journal,
        "volume"    : str(csl.get("volume") or ""),
        "issue"     : str(csl.get("issue") or ""),
        "first_page": first_page.strip(),
        "last_page" : last_page.strip(),
    }


def _jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"\b\w{3,}\b", a.lower()))
    tb = set(re.findall(r"\b\w{3,}\b", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _search_crossref_by_title(title: str, year: str = "") -> Optional[dict]:
    """Search CrossRef by title and return full metadata if a confident hit is found.

    Uses a Jaccard threshold of 0.7 to confirm the top hit matches *title*,
    and requires the year to be within ±2 when *year* is provided.
    Returns same shape as fetch_openalex_full_metadata, or None.
    """
    try:
        r = requests.get(
            "https://api.crossref.org/works",
            params={"query.title": title, "rows": 5, "select": "DOI,title,author,issued,published-print,published-online,container-title,volume,issue,page"},
            headers={"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"},
            timeout=20,
        )
        time.sleep(CROSSREF_RATE_SEC)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items") or []
    except Exception as exc:
        log.debug("CrossRef title search failed: %s", exc)
        return None

    for item in items:
        hit_titles = item.get("title") or []
        hit_title  = hit_titles[0] if hit_titles else ""
        if _jaccard(hit_title, title) < 0.7:
            continue

        # Year check
        if year:
            hit_year = None
            for k in ("published-print", "published-online", "issued"):
                parts = (item.get(k) or {}).get("date-parts") or []
                if parts and parts[0] and parts[0][0]:
                    hit_year = int(parts[0][0])
                    break
            try:
                if hit_year and abs(hit_year - int(float(year))) > 2:
                    continue
            except (ValueError, TypeError):
                pass

        raw_authors = item.get("author") or []
        authors = [
            _crossref_author_apa(a.get("family", ""), a.get("given", ""))
            for a in raw_authors
            if a.get("family") or a.get("given")
        ]
        containers = item.get("container-title") or []
        page = item.get("page") or ""
        first_page, _, last_page = page.partition("-") if "-" in page else (page, "", "")
        hit_year_val = None
        for k in ("published-print", "published-online", "issued"):
            parts = (item.get(k) or {}).get("date-parts") or []
            if parts and parts[0] and parts[0][0]:
                hit_year_val = int(parts[0][0])
                break

        return {
            "doi"       : clean_doi(item.get("DOI", "") or ""),
            "title"     : hit_title,
            "year"      : hit_year_val,
            "authors"   : authors,
            "journal"   : containers[0] if containers else "",
            "volume"    : item.get("volume") or "",
            "issue"     : item.get("issue") or "",
            "first_page": first_page.strip(),
            "last_page" : last_page.strip(),
        }
    return None


def _search_openalex_by_title(title: str, year: str = "") -> Optional[dict]:
    """Search OpenAlex by title and return full metadata if a confident hit is found.

    Jaccard threshold 0.7 against *title*; year ±2 when *year* is provided.
    Returns same shape as fetch_openalex_full_metadata, or None.
    """
    params: dict = {
        "filter" : f"title.search:{title[:200]}",
        "select" : "id,doi,title,publication_year,authorships,primary_location,biblio",
        "per-page": "5",
        "mailto" : RESEARCHER_EMAIL,
    }
    data = _oa_get("https://api.openalex.org/works", params)
    if not data or not data.get("results"):
        return None

    for work in data["results"]:
        hit_title = work.get("title", "") or ""
        if _jaccard(hit_title, title) < 0.7:
            continue

        if year:
            hit_year = work.get("publication_year")
            try:
                if hit_year and abs(hit_year - int(float(year))) > 2:
                    continue
            except (ValueError, TypeError):
                pass

        authors = _all_authors_apa(work)
        loc     = work.get("primary_location") or {}
        src     = loc.get("source") or {}
        biblio  = work.get("biblio") or {}
        return {
            "doi"       : clean_doi(work.get("doi", "") or ""),
            "title"     : hit_title,
            "year"      : work.get("publication_year"),
            "authors"   : authors,
            "journal"   : (src.get("display_name") or "").strip(),
            "volume"    : biblio.get("volume") or "",
            "issue"     : biblio.get("issue") or "",
            "first_page": biblio.get("first_page") or "",
            "last_page" : biblio.get("last_page") or "",
        }
    return None


def fetch_openalex_full_metadata(doi: str) -> Optional[dict]:
    """Fetch full metadata for a DOI: authors (APA-formatted), journal, biblio fields.

    Tries OpenAlex first, then CrossRef as fallback (covers book chapters and
    older works not indexed by OpenAlex).

    Returns a dict with: doi, title, year, authors (list of APA-formatted names),
    journal, volume, issue, first_page, last_page.
    Cached per DOI in OA_CACHE_DIR/doi_full_<hash>.json.
    """
    doi = clean_doi(doi)
    if not doi:
        return None

    cache_file = OA_CACHE_DIR / f"doi_full_{cache_key(doi)}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    # ── Try OpenAlex first ────────────────────────────────────────────────────
    result: Optional[dict] = None
    data = _oa_get(
        "https://api.openalex.org/works",
        {
            "filter" : f"doi:{doi}",
            "select" : "id,doi,title,publication_year,authorships,primary_location,biblio",
            "mailto" : RESEARCHER_EMAIL,
        },
    )
    if data and data.get("results"):
        work    = data["results"][0]
        authors = _all_authors_apa(work)
        loc     = work.get("primary_location") or {}
        src     = loc.get("source") or {}
        biblio  = work.get("biblio") or {}
        result  = {
            "doi"       : clean_doi(work.get("doi", "") or ""),
            "title"     : work.get("title", "") or "",
            "year"      : work.get("publication_year"),
            "authors"   : authors,
            "journal"   : (src.get("display_name") or "").strip(),
            "volume"    : biblio.get("volume") or "",
            "issue"     : biblio.get("issue") or "",
            "first_page": biblio.get("first_page") or "",
            "last_page" : biblio.get("last_page") or "",
        }

    # ── CrossRef fallback ─────────────────────────────────────────────────────
    if result is None:
        log.debug("OpenAlex miss for %s — trying CrossRef", doi)
        result = _fetch_crossref_full_meta(doi)

    # ── doi.org content negotiation (any registrar) ───────────────────────────
    if result is None:
        log.debug("CrossRef miss for %s — trying doi.org CSL-JSON", doi)
        result = _fetch_doi_org_full_meta(doi)

    if result is None:
        return None

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with cache_file.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    return result


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


def resolve_doi_from_url(url: str) -> str:
    """Try to find a DOI for a paper identified only by URL.

    Resolution strategy:
      1. If the URL itself is a doi.org URL, extract the DOI directly.
      2. Query OpenAlex with filter=open_access.oa_url:<url> (open-access PDFs).
      3. Query OpenAlex with filter=primary_location.landing_page_url:<url> (landing pages).

    Returns the cleaned DOI string if found, or "" if not resolvable.
    Caches the result (including negative lookups) so subsequent calls are free.
    """
    import re as _re
    url = (url or "").strip()
    if not url:
        return ""

    cache_file = OA_CACHE_DIR / f"url_doi_{cache_key(url)}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh).get("doi", "")

    def _save(doi: str) -> str:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump({"url": url, "doi": doi}, fh)
        return doi

    # 1. doi.org URL → extract DOI directly
    m = _re.match(r"https?://(?:dx\.)?doi\.org/(.+)", url, _re.IGNORECASE)
    if m:
        return _save(clean_doi(m.group(1)))

    # 2 & 3. Ask OpenAlex
    for oa_filter in (
        f"open_access.oa_url:{url}",
        f"primary_location.landing_page_url:{url}",
    ):
        data = _oa_get(
            "https://api.openalex.org/works",
            {"filter": oa_filter, "select": "doi", "mailto": RESEARCHER_EMAIL},
        )
        results = (data or {}).get("results") or []
        if results:
            doi = clean_doi(results[0].get("doi") or "")
            if doi:
                return _save(doi)

    return _save("")
