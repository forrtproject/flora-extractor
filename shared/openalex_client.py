"""
openalex.py — OpenAlex API helpers + author-year citation pattern extraction.

Public API:
    extract_author_year_patterns(text, max_year) → list[dict]
    fetch_referenced_works_metadata(openalex_id, cache) → list[dict] | None
    find_all_candidates(doi_r, openalex_id_r, study_r, abstract_r,
                        year_r, pattern_str) → list[dict] | None
    fetch_openalex_by_doi(doi) → Optional[dict]
"""
import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Optional

import requests

from .config import (
    OA_CACHE_DIR, OPENALEX_RATE_SEC, CROSSREF_RATE_SEC,
    RESEARCHER_EMAIL, log,
)
from .cache import content_key, read_cache, write_cache, write_json
from .openalex_keys import (
    current_index, headers as oa_headers, is_budget_refusal, quota_message,
    rotate_key,
)
from .rate_limit import throttle
from .utils import bare_work_id, clean_doi, cache_key, non_article_doi

# ── Unicode ranges (chr() avoids \u in compiled regexes for Python < 3.12) ────
_UNI_RANGE  = chr(0x00C0) + "-" + chr(0x024F) + chr(0x1E00) + "-" + chr(0x1EFF)
_UPPER_UNI  = chr(0x00C0) + "-" + chr(0x024F)
_LETTER     = rf"[\w{_UNI_RANGE}]"
_PREFIX     = (r"(?:van\s+der\s+|van\s+|von\s+|de\s+la\s+|de\s+|da\s+|"
               r"del\s+|den\s+|der\s+|du\s+|le\s+|la\s+|el\s+|al\s+)?")
_NAME       = rf"(?:{_PREFIX}[A-Z{_UPPER_UNI}]{_LETTER}{{2,}})"
_YEAR       = r"(?:19|20)\d{2}"
# What may sit inside the citation's parentheses after the year. Papers routinely put
# the venue or the study number there — "Wilson et al. (2017, JPSP)", "Vess (2012, PS,
# Study 1)" — and a pattern that demanded the year alone read those as no citation at
# all, so the ladder searched their titles instead of their authors.
_PAREN_TAIL = r"(?:\s*[,;][^)]{0,60})?"

# Patterns ordered most-specific → least-specific (avoids partial overlaps)
_PATTERNS: list[tuple[str, str]] = [
    ("multi_and_paren",
     rf"({_NAME}(?:,\s*{_NAME}){{1,}},?\s+(?:and|&)\s+{_NAME})\s*'?s?\s*\(({_YEAR}){_PAREN_TAIL}\)"),
    ("multi_and_bare",
     rf"({_NAME}(?:,\s*{_NAME}){{1,}},?\s+(?:and|&)\s+{_NAME}),?\s+({_YEAR})(?!\d)"),
    ("etal_paren",
     rf"({_NAME})\s+et\s+al\.?\s*'?s?\s*\(({_YEAR}){_PAREN_TAIL}\)"),
    ("etal_bare",
     rf"({_NAME})\s+et\s+al\.?\s*,?\s+({_YEAR})(?!\d)"),
    ("two_and_paren",
     rf"({_NAME})\s+(?:and|&)\s+({_NAME})\s*'?s?\s*\(({_YEAR}){_PAREN_TAIL}\)"),
    ("two_and_bare",
     rf"({_NAME})\s+(?:and|&)\s+({_NAME}),?\s+({_YEAR})(?!\d)"),
    ("single_paren",
     rf"({_NAME})\s*'?s?\s*\(({_YEAR}){_PAREN_TAIL}\)"),
    ("single_bare",
     rf"({_NAME}),?\s+({_YEAR})(?!\d)"),
]

_COMPILED = [(name, re.compile(pat)) for name, pat in _PATTERNS]

_MONTHS = {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}
_WEEKDAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}
_NAME_STOPWORDS = _MONTHS | _WEEKDAYS


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

    strict_bare – when True, apply two extra gates (both Stage-2 only, since
    Stage-3 candidate finding is recall-critical and uses the same function):
      1. drop single_bare matches whose leading token is a blacklisted
         non-surname word (months, seasons, structural words, disease acronyms,
         sentence-initial function words);
      2. drop any match where a captured name token is a month/weekday name —
         catches the multi-token case the leading-token check misses, e.g.
         "May and June 2018" in AEA RCT-registry abstracts read as authors
         "May" and "June".
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

            # Unconditional: a month/weekday token is never an author surname in an
            # author-year pattern, so this is wrong in Stage 3 as much as Stage 2.
            name_tokens: list[str] = []
            for g in groups[:-1]:
                name_tokens.extend(re.findall(r"[A-Za-z\-]+", g))
            if any(tok.lower() in _NAME_STOPWORDS for tok in name_tokens):
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


class OpenAlexQuotaExhausted(RuntimeError):
    """OpenAlex refused the request for lack of budget, not for lack of data.

    Raised instead of returning None so callers cannot mistake an unaffordable
    request for a genuine empty result. A swallowed quota 429 degrades every
    downstream resolver at once: find_all_candidates() returns [], the rule-based
    and title-pattern resolvers have nothing to match, and each row falls through
    to PDF acquisition where the LLM is asked to resolve with no candidate list.
    That produces slow, low-confidence, potentially fabricated links. Stopping the
    run and topping up the quota is always the cheaper outcome.
    """


# Free-text `search` queries are billed at 10× a filter query (see the cost table in
# CLAUDE.md), so a run's OpenAlex bill is dominated by how many of them it issues.
# Counted here rather than at each call site because every one of them goes through
# _oa_get, and the count is per logical query, not per retry or key rotation.
_search_queries = 0
_search_lock = threading.Lock()


def search_query_count() -> int:
    """How many free-text OpenAlex `search` queries this process has issued."""
    return _search_queries


def print_search_summary() -> None:
    """Print the run's free-text OpenAlex search count (its dominant OpenAlex cost)."""
    n = search_query_count()
    if not n:
        print("OpenAlex free-text searches: none issued.")
        return
    print(f"OpenAlex free-text searches: {n:,} "
          f"(billed ~10x a filter query — see CLAUDE.md cost table)\n")


def _params_summary(params: "dict | None") -> str:
    """The identifying part of an OpenAlex query, for a log line.

    Only `filter` and `search` — the rest is `select`/`mailto`/`per-page` boilerplate
    that would bury the one thing a reader needs to know: which question failed.
    """
    if not params:
        return ""
    parts = [f"{k}={str(params[k])[:120]}" for k in ("filter", "search") if params.get(k)]
    return f"({' '.join(parts)})" if parts else ""


def _oa_get(url: str, params: dict | None = None) -> Optional[dict]:
    """GET from OpenAlex with rate limiting, 429 retry, and error handling.

    Raises OpenAlexQuotaExhausted when the 429 is a budget refusal — see that
    class. Transient 429s are still retried on the schedule below.
    """
    # Both shapes of free-text query: the `search` param and a `<field>.search:`
    # filter (the title search uses the latter).
    if params and ("search" in params or ".search:" in str(params.get("filter", ""))):
        global _search_queries
        with _search_lock:
            _search_queries += 1
    _RETRY_DELAYS = [5, 15, 30]  # seconds to wait after 1st, 2nd, 3rd 429
    attempt = 0
    # Not a for-loop over attempts: rotating to a fresh key is not a retry of a
    # throttled request, so it must not consume the backoff budget — otherwise a
    # run with more keys than retry slots would give up with keys still unused.
    while attempt <= len(_RETRY_DELAYS):
        # Once per attempt, including the retries: the throttle is a reservation in
        # one shared queue (shared/rate_limit.py), so concurrent callers space
        # themselves at OPENALEX_RATE_SEC between them rather than each sleeping
        # its own interval and firing together.
        throttle("openalex", OPENALEX_RATE_SEC)
        key_idx = current_index()
        try:
            r = requests.get(url, headers=oa_headers(), params=params or {},
                             timeout=30)
            if r.status_code == 429:
                if is_budget_refusal(r):
                    # Try the next key before giving up; only when every key is
                    # drained does this become a hard stop.
                    if rotate_key(key_idx):
                        continue
                    raise OpenAlexQuotaExhausted(quota_message(r))
                if attempt >= len(_RETRY_DELAYS):
                    break
                # Use our own schedule — OpenAlex sometimes sends absurdly large
                # Retry-After values (e.g. 40000+s) that would stall the pipeline.
                delay = _RETRY_DELAYS[attempt]
                log.warning("OpenAlex 429 — waiting %ds before retry %d/%d",
                            delay, attempt + 1, len(_RETRY_DELAYS))
                time.sleep(delay)
                attempt += 1
                continue
            # A 5xx is OpenAlex having a moment, not an answer about the query, and
            # under EXTRACT_WORKERS concurrent callers they arrive in clusters: a
            # 27-work run on 2026-08-07 logged 15 failures whose requests all
            # succeeded when replayed one at a time. Breaking out on the first one
            # spent the row's whole question on a blip, and — now that a title search
            # that finds nothing settles a work as `no_original_found` — would have
            # closed works on it. Retried on the same schedule as a 429.
            if r.status_code >= 500:
                if attempt >= len(_RETRY_DELAYS):
                    log.warning("OpenAlex %s after %d retries: %s %s", r.status_code,
                                attempt, url, _params_summary(params))
                    return None
                delay = _RETRY_DELAYS[attempt]
                log.warning("OpenAlex %s — waiting %ds before retry %d/%d: %s",
                            r.status_code, delay, attempt + 1, len(_RETRY_DELAYS),
                            _params_summary(params))
                time.sleep(delay)
                attempt += 1
                continue
            r.raise_for_status()
            return r.json()
        except OpenAlexQuotaExhausted:
            raise
        except requests.exceptions.HTTPError as e:
            # 4xx other than 429: the query itself is the problem, so a retry asks the
            # same bad question again. The status and the query go in the log — the
            # bare "failed after retries" this used to print was reached by BOTH this
            # path and the 429 path, so a run's failures could not be told apart.
            log.warning("OpenAlex %s (not retried): %s %s",
                        getattr(e.response, "status_code", "?"), url,
                        _params_summary(params))
            return None
        except Exception as e:
            log.warning("OpenAlex request failed: %s — %s %s", url, e,
                        _params_summary(params))
            return None

    log.warning("OpenAlex 429 after %d retries: %s %s", attempt, url,
                _params_summary(params))
    return None


def fetch_referenced_works_metadata(openalex_id: str,
                                    use_cache: bool = True) -> list[dict] | None:
    """
    Return full metadata for every work referenced by *openalex_id*.

    None is no answer — the work lookup or one of the batch requests failed — and
    is never cached; [] is OpenAlex saying this work lists no references. A failed
    batch takes the whole list down with it rather than returning a short one:
    references missing from a list nobody can tell is short is how a resolver
    reports "the target is not cited here" about a paper that cites it.

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
    if work is None:
        log.warning("[%s] reference fetch: the work lookup never answered", bare)
        return None

    # Step 2: batch-fetch metadata (up to 50 IDs per request)
    results: list[dict] = []
    batch_size = 50
    bare_refs  = [re.sub(r"https?://openalex\.org/", "", rid)
                  for rid in work.get("referenced_works", [])]

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
        if data is None:
            log.warning("[%s] reference fetch: batch %d of %d never answered "
                        "— discarding the partial list",
                        bare, i // batch_size + 1, -(-len(bare_refs) // batch_size))
            return None
        results.extend(data.get("results", []))

    if use_cache:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(cache_file, results, indent=2)

    return results


# ── OpenCitations reference fallback ─────────────────────────────────────────
# OpenAlex returns no referenced_works for a sizeable minority of papers, which
# leaves the Stage 4.5 screen with nothing to resolve against. OpenCitations
# (COCI) is built from a different ingest of Crossref open references and covers
# some of those gaps: on five sampled papers with zero OpenAlex references it had
# 1, 9 and 40 references for three of them. It is a supplement, not a
# replacement — where OpenAlex had 46 references OpenCitations had 36 — so it is
# consulted only when OpenAlex comes back empty.
#
# COCI returns bare cited DOIs with no titles, so the DOIs are resolved back
# through OpenAlex in batches of 50 (1 credit per batch) to reach the same shape
# fetch_referenced_works_metadata() returns.
#
# None and [] are different answers here and the caller must keep them apart: []
# is OpenCitations saying this paper has no open references, None is nobody
# saying anything (COCI unreachable, or an OpenAlex resolution batch failed).
# Collapsing the second into the first would let a provider outage be recorded as
# "screened against the full reference list" — and, worse, cached as one. Only a
# complete answer is written to the cache.

_OPENCITATIONS_URL = "https://opencitations.net/index/api/v1/references/{doi}"


def fetch_opencitations_references(doi_r: str) -> list[dict] | None:
    """Referenced-work metadata for *doi_r* via OpenCitations; None = no answer."""
    doi_r = clean_doi(doi_r)
    if not doi_r:
        return []

    cache_file = OA_CACHE_DIR / f"ocrefs_{cache_key(doi_r)}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    try:
        throttle("crossref", CROSSREF_RATE_SEC)
        r = requests.get(_OPENCITATIONS_URL.format(doi=doi_r),
                         headers={"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"},
                         timeout=45)
        r.raise_for_status()
        cited = [c.get("cited", "").strip() for c in r.json()]
    except Exception as exc:
        log.warning("OpenCitations lookup failed for %s: %s", doi_r, exc)
        return None

    dois = [clean_doi(c) for c in cited if c]

    results: list[dict] = []
    batch_size = 50
    for i in range(0, len(dois), batch_size):
        batch = dois[i : i + batch_size]
        data = _oa_get(
            "https://api.openalex.org/works",
            {
                "filter"  : f"doi:{'|'.join(batch)}",
                "per-page": str(batch_size),
                "select"  : "id,doi,title,publication_year,authorships",
                "mailto"  : RESEARCHER_EMAIL,
            },
        )
        # A failed batch makes the list short, not empty, which is the hardest
        # kind of wrong to notice downstream — give up on the whole list instead.
        if data is None:
            log.warning("[%s] OpenCitations: OpenAlex resolution failed for batch "
                        "%d of %d — discarding the partial list",
                        doi_r, i // batch_size + 1, -(-len(dois) // batch_size))
            return None
        results.extend(data.get("results", []))

    log.info("[%s] OpenCitations: %d cited DOIs → %d with metadata",
             doi_r, len(dois), len(results))
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    write_json(cache_file, results, indent=2)
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
                         pattern_str: str = "") -> list[dict] | None:
    """
    Re-fetch all referenced works for *openalex_id_r* and return EVERY work
    that matches any extracted author-year pattern. None if the reference list
    could not be fetched at all — no candidates was not the answer given.

    Cached in OA_CACHE_DIR on every argument the result depends on, not on doi_r
    alone: the three call sites pass different titles and abstracts for the same
    paper (run_extract passes the candidates-file title, link_original the
    filtered-row one), and the extracted author-year patterns — hence the whole
    candidate list — follow from them. pattern_str is not in the key because it is
    not read: the call that used it is commented out below.

    Returns a list of dicts:
        openalex_id, doi, title, year, first_author,
        match_year_exact, cited_pattern
    """

    key = content_key("candidates", doi_r, openalex_id_r, study_r, abstract_r, year_r)
    cache_file = OA_CACHE_DIR / f"{key}.json"
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
    # None is OpenAlex never answering. Returning [] here would tell the caller
    # this paper cites nothing that matches its own citations — and the empty
    # candidate list would be cached under a content key that will never change,
    # pinning the outage on the row forever.
    if refs is None:
        return None
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
    write_json(cache_file, candidates, indent=2)

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
        # Before the request, not after it: the throttle is a reservation for the
        # call about to go out (shared/rate_limit.py). Charging the wait afterwards
        # spaced the caller's NEXT action — which is often no CrossRef call at all —
        # while letting this one fire the instant the previous one returned.
        throttle("crossref", CROSSREF_RATE_SEC)
        r = requests.get(
            f"https://api.crossref.org/works/{doi}",
            headers={"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"},
            timeout=20,
        )
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

    # Year: published-print → published → published-online → issued → created (last resort)
    year = None
    for k in ("published-print", "published", "published-online", "issued"):
        parts = (msg.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    if year is None:
        parts = (msg.get("created") or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])

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
            # Reserved before the request it spaces — see _fetch_crossref_full_meta.
            throttle("crossref", CROSSREF_RATE_SEC)
            r = requests.get(f"https://doi.org/{doi}", headers=headers,
                             timeout=20, allow_redirects=True)
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
    for k in ("published-print", "issued", "published-online", "created"):
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


class TitleSearchUnavailable(Exception):
    """The provider never answered — distinct from a genuine no-match.

    Public because the difference now decides a work's ENDING. A title search that
    finds nothing settles the work `no_original_found`, and there is no point
    re-running a query that will return nothing again; a title search that never
    reached its provider must settle nothing, or an outage closes the work for good.
    Callers that need to tell them apart pass `raise_on_unavailable=True`.
    """


# The private alias the module's own raisers still use.
_TitleSearchUnavailable = TitleSearchUnavailable


_TITLE_SEARCH_SHAPE = "v2-openalex-id"


def _cached_title_search(source: str, title: str, year: str, search,
                         raise_on_unavailable: bool = False) -> Optional[dict]:
    """Disk-cache a title search, misses included.

    The title searches fire three to five times for a single row — the pre-PDF
    resolver, both reference builders and the link guard all ask — and a miss is
    the common answer, so caching only the hits would leave most of the traffic
    uncached. A miss is stored as {"hit": null}, which is a result like any other.
    A transport or API failure is not: caching it would pin the outage forever.
    """
    if not title:
        return None
    # _TITLE_SEARCH_SHAPE is part of the key because the cached value is a dict shape,
    # not just an answer: entries written before openalex_id was returned would still
    # be found and would silently strip the work id a DOI-less original is keyed on.
    # Bump it whenever the returned dict gains or loses a field.
    key = content_key(f"titlesearch_{source}", "", title, year, _TITLE_SEARCH_SHAPE)
    cached = read_cache(OA_CACHE_DIR, key)
    if cached is not None:
        return cached.get("hit")
    try:
        hit = search(title, year)
    except TitleSearchUnavailable:
        # Never cached — caching it would pin the outage forever. Whether it is
        # RAISED is the caller's choice: most callers treat a failed search as a
        # miss and move on, but one that would settle a work on the answer must be
        # able to tell the two apart.
        if raise_on_unavailable:
            raise
        return None
    write_cache(OA_CACHE_DIR, key, {"hit": hit})
    return hit


def _search_crossref_by_title(title: str, year: str = "",
                            raise_on_unavailable: bool = False) -> Optional[dict]:
    """Search CrossRef by title and return full metadata if a confident hit is found.

    Uses a Jaccard threshold of 0.7 to confirm the top hit matches *title*,
    and requires the year to be within ±2 when *year* is provided.
    Returns the fetch_openalex_full_metadata shape plus openalex_id (always "" here
    — CrossRef has no OpenAlex ids), or None. Cached per (title, year) in OA_CACHE_DIR.
    """
    return _cached_title_search("crossref", title, year, _search_crossref_by_title_live,
                                raise_on_unavailable)


def _search_crossref_by_title_live(title: str, year: str = "") -> Optional[dict]:
    try:
        # Reserved before the request it spaces — see _fetch_crossref_full_meta.
        throttle("crossref", CROSSREF_RATE_SEC)
        r = requests.get(
            "https://api.crossref.org/works",
            params={"query.title": title, "rows": 5, "select": "DOI,title,author,issued,published-print,published,published-online,created,container-title,volume,issue,page"},
            headers={"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"},
            timeout=20,
        )
        r.raise_for_status()
        items = r.json().get("message", {}).get("items") or []
    except Exception as exc:
        log.debug("CrossRef title search failed: %s", exc)
        raise _TitleSearchUnavailable(str(exc)) from exc

    for item in items:
        hit_titles = item.get("title") or []
        hit_title  = hit_titles[0] if hit_titles else ""
        if _jaccard(hit_title, title) < 0.7:
            continue

        # Year check
        if year:
            hit_year = None
            for k in ("published-print", "published", "published-online", "issued", "created"):
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
        for k in ("published-print", "published", "published-online", "issued", "created"):
            parts = (item.get(k) or {}).get("date-parts") or []
            if parts and parts[0] and parts[0][0]:
                hit_year_val = int(parts[0][0])
                break

        return {
            "doi"        : clean_doi(item.get("DOI", "") or ""),
            "openalex_id": "",   # CrossRef has none; kept so both title searches share a shape
            "title"      : hit_title,
            "year"       : hit_year_val,
            "authors"    : authors,
            "journal"    : containers[0] if containers else "",
            "volume"     : item.get("volume") or "",
            "issue"      : item.get("issue") or "",
            "first_page" : first_page.strip(),
            "last_page"  : last_page.strip(),
        }
    return None


def _search_openalex_by_title(title: str, year: str = "",
                            raise_on_unavailable: bool = False) -> Optional[dict]:
    """Search OpenAlex by title and return full metadata if a confident hit is found.

    Jaccard threshold 0.7 against *title*; year ±2 when *year* is provided.
    Returns the fetch_openalex_full_metadata shape plus openalex_id (the bare W-id,
    which is the only identity a DOI-less original has), or None. Cached per
    (title, year) in OA_CACHE_DIR.
    """
    return _cached_title_search("openalex", title, year, _search_openalex_by_title_live,
                                raise_on_unavailable)


def _openalex_filter_value(text: str) -> str:
    """*text* as a value an OpenAlex `filter=` can carry.

    A comma SEPARATES FILTERS in OpenAlex's filter syntax, so a value containing one
    is rejected at the API edge with HTTP 400 ("A filter value contains an unescaped
    comma"). There is no escape for it — the character has to go.

    This is not an edge case for the title search: what it is given is however the
    paper referred to the study it replicates, which is routinely "Toya and Skidmore,
    2007, Economic development and..." or "Zhong, Bohns, & Gino (2010) Good lamps...".
    Every one of those was a 400 that `_oa_get` reported as "request failed after
    retries", which reads as an outage and, before this, as no such paper.

    A pipe is stripped for the same reason (it is OpenAlex's OR separator).

    So are `?` and `*`, which are WILDCARDS: a stemmed field rejects them outright —
    "Wildcards (* or ?) require the exact (no-stem) field". Titles ending in a
    question are ordinary ("Are STEM Faculty Biased Against Female Applicants?",
    "Is Eco-Friendly Unmanly?"), and every one of them was an HTTP 400 that read
    downstream as an outage. Measured 2026-08-07: of five 400s in a 100-work run, four
    were a question mark and the fifth was this same title. Parentheses, colons,
    apostrophes, percent signs and ampersands were probed at the same time and are all
    accepted.
    """
    for char in (",", "|", "?", "*"):
        text = text.replace(char, " ")
    return " ".join(text.split())


def _search_openalex_by_title_live(title: str, year: str = "") -> Optional[dict]:
    params: dict = {
        "filter" : f"title.search:{_openalex_filter_value(title)[:200]}",
        "select" : "id,doi,title,publication_year,authorships,primary_location,biblio",
        "per-page": "5",
        "mailto" : RESEARCHER_EMAIL,
    }
    data = _oa_get("https://api.openalex.org/works", params)
    if data is None:
        raise _TitleSearchUnavailable("OpenAlex title search returned no response")
    if not data.get("results"):
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
            "doi"        : clean_doi(work.get("doi", "") or ""),
            "openalex_id": bare_work_id(work.get("id", "") or ""),
            "title"      : hit_title,
            "year"       : work.get("publication_year"),
            "authors"    : authors,
            "journal"    : (src.get("display_name") or "").strip(),
            "volume"     : biblio.get("volume") or "",
            "issue"      : biblio.get("issue") or "",
            "first_page" : biblio.get("first_page") or "",
            "last_page"  : biblio.get("last_page") or "",
        }
    return None


# How many candidates an author-and-year query may hand on to be judged, and how
# large the raw hit list may get before the topic words are added to narrow it.
# Measured against six real campaign targets on 2026-08-07: an author-and-year filter
# alone returns 12 works for "Ramscar 2010" and 154 for "Turri 2015" — a surname that
# collides across fields drowns the right paper, and the top-cited hits for that one
# are 3D-printing papers. Adding the replication's own topic words cut those to 5.
AUTHOR_YEAR_MAX_OFFERED = 10
AUTHOR_YEAR_NARROW_ABOVE = 12
# How many of the citation's authors are ANDed into the query. Three is enough to make
# a shortlist unique and stops a long author list from over-narrowing when the citation
# abbreviates it differently from the record.
AUTHOR_YEAR_MAX_NAMES = 3
# How many topic words the narrowing query carries, tried longest first. OpenAlex ANDs
# every word of a `.search` value, so a whole replication title matches nothing at all:
# "anderson 2012" + the full title "Does Desire for Status Increase Overconfidence? A
# Replication and Extension of Study 5 in Anderson et al. (2012)" returned 0, and the
# narrowing was silently discarded in favour of 15,015 unnarrowed works. The same query
# on three words returned exactly the right paper.
AUTHOR_YEAR_TOPIC_WORDS = (3, 2)
_AUTHOR_YEAR_SHAPE = "v3-topic-words"

# Words that carry no topic: ordinary English, and the vocabulary every replication
# title is built from. Dropping them is what leaves "status overconfidence" behind.
_TOPIC_STOPWORDS = frozenset("""
about above after again against  all also among  and  another  any  are  because
been  before  being  between  both  but  can  conceptual  could  direct  does
during  each  effect  effects  evidence  experiment  experiments  extension
extensions  first  from  further  have  high  higher  how  into  investigation  its
large  low  lower  many  materials  more  most  new  not  novel  one  only  other
over  paper  papers  pre  preregistered  registered  replicate  replicates
replicating  replication  replications  report  reports  research  results  revisited
same  second  several  study  studies  such  test  testing  tests  than  that  the
their  them  then  there  these  they  this  those  three  through  two  under  using
very  was  were  what  when  where  which  while  who  why  will  with  within
without  would  your
""".split())


def _topic_words(text: str, limit: int, exclude: "list[str] | None" = None) -> str:
    """The first *limit* topic-bearing words of *text*, in the order they appear.

    *exclude* is the cited authors' own surnames. They are in the replication's title
    more often than not — "Conceptual Replication (Young et al., 2016, Study 1)" has no
    other word of four letters — and searching an author's name in a title-and-abstract
    field alongside an author filter narrows to the papers that discuss them, which is
    not the same set as the papers they wrote.
    """
    skip = {_fold_accents(str(e or "")).lower() for e in (exclude or [])}
    words: list[str] = []
    for word in re.findall(r"[^\W\d_]{4,}", str(text or "").lower(), re.UNICODE):
        if (word in _TOPIC_STOPWORDS or word in words
                or _fold_accents(word) in skip):
            continue
        words.append(word)
        if len(words) >= limit:
            break
    return " ".join(words)


def _fold_accents(text: str) -> str:
    """*text* without diacritics — "Zárate" as "Zarate".

    An author filter is matched against however OpenAlex spelled the name, and a
    citation's spelling and a record's routinely differ by an accent alone.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or ""))
                   if not unicodedata.combining(c))


def _author_year_query(surnames: list[str], year: int, topic: str) -> Optional[dict]:
    """One OpenAlex works query for a set of author surnames, a year and a topic.

    Every surname is its own filter, so they AND: a work has to carry all of them.
    That is what makes the shortlist usable at all. Measured 2026-08-07 against real
    campaign targets: "jones 1995" matches 8,348 works and the right paper is nowhere
    near the top; "jones AND macken 1995" matches 7 and it is third. "han 2017"
    matches 54,802; "han AND kahn 2017" matches 12.
    """
    parts = [f"publication_year:{year}"] + [
        f"raw_author_name.search:{_openalex_filter_value(_fold_accents(s))[:80]}"
        for s in surnames]
    if topic:
        parts.append(f"title_and_abstract.search:{_openalex_filter_value(topic)[:120]}")
    return _oa_get("https://api.openalex.org/works", {
        "filter":   ",".join(parts),
        "select":   "id,doi,title,publication_year,authorships,primary_location,"
                    "cited_by_count",
        "per-page": str(AUTHOR_YEAR_MAX_OFFERED),
        # Most-cited first, because the paper a replication targets is the one that got
        # noticed. It is a ranking for the shortlist, never a pick: which of these is
        # the original is a judgment about the topic, and an LLM makes it.
        "sort":     "cited_by_count:desc",
        "mailto":   RESEARCHER_EMAIL,
    })


def author_year_candidates(surnames: "str | list[str]", year: int,
                           topic: str = "") -> "tuple[list[dict], int, bool]":
    """(candidates, how many the query matched, unavailable) for an author and a year.

    The resolver for a target the paper named as a bare citation — "Ramscar et al.
    (2010)", "Turri, Buckwalter, & Blouw (2015)". A title search cannot answer that:
    the string contains no title, so both providers return nothing and the work used
    to be closed as though no original existed.

    It is not cheaper per request than the title search it replaces: `raw_author_name`
    and `title_and_abstract` are `.search` filters, which OpenAlex bills as free-text
    queries and `_oa_get` counts as such. What it saves is the two requests the title
    search spent on a question it could not answer — one or two here against CrossRef
    plus OpenAlex there, and unlike those, these can succeed.

    `raw_author_name.search` matches the surname ANYWHERE in the author list, so the
    shortlist includes works the cited author co-wrote rather than led. That is
    deliberate: "Ramscar et al." is a first-author citation but "Van der Werff et al."
    may not be, and the prompt shows the whole author list so the model can judge it.

    Two steps, and the second only when it is needed. A surname that is rare in its
    year returns a handful of works and they all go forward. A surname that collides
    across fields returns hundreds, so a second query adds the replication's own topic
    words.

    The narrowed hits are ADDED to the head of the broad list, never substituted for
    it. Which words of a title carry its topic is a guess, and a wrong guess returns a
    short list that does not contain the paper: measured 2026-08-07, narrowing
    "anderson 2012" on the first three content words of its replication's title
    returned 3 works, none of them the right one, where the unnarrowed list at least
    had it somewhere. Adding cannot hide an answer; replacing can.

    The count is returned so the caller can record it: offering 8 of 154 and getting
    "none of these" is not the same finding as offering all 3 there were, and a row
    that does not say which cannot be read later.

    `unavailable=True` means OpenAlex never answered, which must settle nothing.
    """
    if isinstance(surnames, str):
        surnames = [surnames]
    surnames = [" ".join(str(s or "").split()) for s in surnames]
    surnames = [s for s in surnames if s][:AUTHOR_YEAR_MAX_NAMES]
    if not surnames or not year:
        return [], 0, False

    # Keyed on the values that actually go into the request, after the same cleaning
    # the request applies — a key that normalised the topic differently from the
    # query would file two different questions under one answer. The two constants
    # are in the key because they change the answer without changing any argument:
    # one decides how many candidates come back, the other whether the topic is used
    # at all.
    # Keyed on the values that actually reach the API: the folded surnames and the
    # topic words each narrowing attempt would send, not the raw topic. Keying the raw
    # topic truncated to 120 characters let two long inputs whose first three content
    # words differ share one answer, and left the stopword list and the folding rule
    # out of the key entirely — both change the query without changing an argument.
    narrowings = "|".join(_topic_words(topic, n, surnames)
                          for n in AUTHOR_YEAR_TOPIC_WORDS)
    key = content_key("authoryear", "",
                      "|".join(_openalex_filter_value(_fold_accents(s)).lower()
                               for s in surnames),
                      str(year), narrowings,
                      str(AUTHOR_YEAR_MAX_OFFERED), str(AUTHOR_YEAR_NARROW_ABOVE),
                      str(AUTHOR_YEAR_MAX_NAMES), _AUTHOR_YEAR_SHAPE)
    cached = read_cache(OA_CACHE_DIR, key)
    if cached is not None:
        return cached["candidates"], int(cached["total"]), False

    data = _author_year_query(surnames, year, "")
    if data is None:
        return [], 0, True
    total = int((data.get("meta") or {}).get("count") or 0)
    if not total and len(surnames) > 1:
        # Every co-author has to be indexed under the surname the citation used for
        # the AND to hold, and one of them routinely is not — an initial in the
        # record, a hyphen dropped, an author OpenAlex never attached. Falling back
        # to the first name alone is a longer list, not no list.
        log.info("[author-year] %s %s matched nothing together — retrying on %s",
                 "+".join(surnames), year, surnames[0])
        first = _author_year_query(surnames[:1], year, "")
        if first is None:
            return [], 0, True
        data, total = first, int((first.get("meta") or {}).get("count") or 0)
    narrowed_results: list[dict] = []
    if total > AUTHOR_YEAR_NARROW_ABOVE and topic:
        for limit in AUTHOR_YEAR_TOPIC_WORDS:
            words = _topic_words(topic, limit, surnames)
            if not words:
                break
            narrowed = _author_year_query(surnames, year, words)
            # A narrowing that failed is not an outage of the question: the broad
            # answer is in hand and is the answer, just a longer one. Reporting
            # `unavailable` here wrote api_error over four works in a 100-work run
            # whose broad query had answered perfectly well.
            if narrowed is None:
                log.info("[author-year] narrowing failed for %s %s — keeping the "
                         "unnarrowed shortlist", "+".join(surnames), year)
                break
            if narrowed.get("results"):
                narrowed_results = narrowed["results"]
                break

    candidates = []
    seen_ids: set[str] = set()
    for work in (narrowed_results + (data.get("results") or [])):
        ident = str(work.get("id") or work.get("doi") or "")
        if ident in seen_ids:
            continue
        # A DOI the project already calls not-a-study is not an original either. APA
        # files conference abstracts under 10.1037/e…, and one of those standing in
        # for the paper is the wrong-original class this evaluation could not
        # otherwise reach.
        if non_article_doi(clean_doi(work.get("doi", "") or "")):
            continue
        seen_ids.add(ident)
        if len(candidates) >= AUTHOR_YEAR_MAX_OFFERED:
            break
        loc = work.get("primary_location") or {}
        src = loc.get("source") or {}
        candidates.append({
            "doi":          clean_doi(work.get("doi", "") or ""),
            "openalex_id":  bare_work_id(work.get("id", "") or ""),
            "title":        work.get("title", "") or "",
            "year":         work.get("publication_year"),
            "authors":      _all_authors_apa(work),
            "first_author": (_first_author_surnames(work) or [""])[0],
            "journal":      (src.get("display_name") or "").strip(),
            "cited_by":     int(work.get("cited_by_count") or 0),
        })
    write_cache(OA_CACHE_DIR, key, {"candidates": candidates, "total": total})
    return candidates, total, False


def _fetch_openalex_work(doi: str) -> Optional[dict]:
    """Fetch the raw OpenAlex work for *doi*, cached once per DOI.

    fetch_openalex_by_doi() and fetch_openalex_full_metadata() both need this work
    and used to request it separately under different cache keys, so every doi_o
    was fetched twice. They now share one lookup and derive their own shapes from
    it; the select list is the union of what either needs. A DOI OpenAlex does not
    hold is cached as a miss, so the full-metadata path reaches its CrossRef
    fallback without re-asking.
    """
    doi = clean_doi(doi)
    if not doi:
        return None

    cache_file = OA_CACHE_DIR / f"oa_work_{cache_key(doi)}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh).get("work")

    data = _oa_get(
        "https://api.openalex.org/works",
        {
            "filter" : f"doi:{doi}",
            "select" : "id,doi,title,publication_year,authorships,primary_location,biblio",
            "mailto" : RESEARCHER_EMAIL,
        },
    )
    if data is None:
        return None   # request failed — a failure is not an answer, do not cache it

    work = (data.get("results") or [None])[0]
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    write_json(cache_file, {"work": work}, indent=2)
    return work


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
    work = _fetch_openalex_work(doi)
    if work:
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
    write_json(cache_file, result, indent=2)

    return result


def fetch_openalex_by_doi(doi: str) -> Optional[dict]:
    """
    Fetch OpenAlex metadata for a specific DOI and return a candidate dict
    in the same format as find_all_candidates() entries.

    Used to inject the FLoRA-verified original into the candidate pool for
    validated DOIs. The underlying OpenAlex work is cached by _fetch_openalex_work;
    this shape is derived from it. Returns None if the DOI is not found or the
    request fails.
    """
    work = _fetch_openalex_work(doi)
    if not work:
        return None

    authors = _first_author_surnames(work)
    return {
        "openalex_id"     : work.get("id", ""),
        "doi"             : clean_doi(work.get("doi", "") or ""),
        "title"           : work.get("title", ""),
        "year"            : work.get("publication_year"),
        "first_author"    : authors[0] if authors else "",
        "all_authors"     : authors,
        "match_year_exact": True,
        "cited_pattern"   : "doi_lookup",
    }

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
        write_json(cache_file, {"url": url, "doi": doi})
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
