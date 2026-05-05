"""
Utilities for querying OpenAlex for replication-related papers.

This module searches OpenAlex works for a fixed phrase and returns results in the
standard candidate-paper schema used elsewhere in the project.

Public API:
    fetch_openalex_candidates() -> pd.DataFrame
"""

import datetime
import json
import time
from typing import Optional

import pandas as pd
import requests

from shared.config import OA_CACHE_DIR, OPENALEX_RATE_SEC, RESEARCHER_EMAIL, log
from shared.schema import CANDIDATES_COLS
from shared.utils import cache_key, clean_doi


SEARCH_PHRASES = [
    "replication of",
    "direct replication",
    "close replication",
    "conceptual replication",
    "replication study",
    "reproduction study",
    "we replicated",
    "attempts to replicate",
    "registered replication report",
    "pre-registered replication",
]

# OpenAlex works endpoint and query defaults.
_BASE_URL = "https://api.openalex.org/works"
# Maximum number of records returned by OpenAlex per page.
_PER_PAGE = 200
# Fields we actually need from each work. Keeping this small reduces payload size.
_SELECT = (
    "id,doi,display_name,publication_year,"
    "authorships,primary_location,abstract_inverted_index"
)

SOURCE_TAG = "openalex"

# Prototype safeguard: keep API use modest during development.
MAX_PAGES_PER_PHRASE = 1


def _year_filter(from_year: Optional[int], to_year: Optional[int]) -> Optional[str]:
    """Build the OpenAlex publication_year filter fragment, or None if unrestricted."""
    if from_year is None and to_year is None:
        return None
    lo = from_year or 1000
    hi = to_year   or 9999
    return f"publication_year:{lo}-{hi}"


def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    """Rebuild plain abstract text from an OpenAlex inverted index.

    OpenAlex returns abstracts as a mapping of words to one or more token
    positions. This function reverses that structure into the original
    word order and joins the tokens into a single string.

    Parameters
    ----------
    inverted_index
        OpenAlex ``abstract_inverted_index`` mapping words to token positions,
        or ``None`` if no abstract is available.

    Returns
    -------
    Optional[str]
        The reconstructed abstract text, or ``None`` if no abstract data was
        provided.
    """
    if not inverted_index:
        # Some works have no abstract at all; we standardise on None in that case
        return None

    # OpenAlex gives positions -> multiple words; we invert that to positions -> single word.
    positions: dict[int, str] = {}
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions[pos] = word

    # Positions are integer offsets into the token sequence; sort to restore order.
    return " ".join(positions[k] for k in sorted(positions)) if positions else None


def _get_page(params: dict) -> dict:
    """Fetch a single OpenAlex results page, using the on-disk cache when possible.

    The request is cached by the full parameter set so repeated runs can reuse
    previous responses and avoid unnecessary API calls. Transient request errors
    are retried with exponential backoff.

    Parameters
    ----------
    params
        Query parameters to send to the OpenAlex works endpoint.

    Returns
    -------
    dict
        Parsed JSON response from OpenAlex.

    Raises
    ------
    StopIteration
        If OpenAlex returns HTTP 429 (rate limited). The caller uses this to
        stop pagination cleanly and return partial results collected so far.
    requests.RequestException
        If the request keeps failing after all retries.
    RuntimeError
        If retry logic exits unexpectedly without returning a response.
    """
    # Cache key is derived from the full parameter set so we reuse results
    # across runs with exactly the same query.
    key = cache_key(str(sorted(params.items())))
    cache_path = OA_CACHE_DIR / f"{key}.json"

    # 1) Cache hit: serve the response from disk, no network call.
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    # 2) Cache miss: call the API, with retry and backoff on transient errors.
    for attempt in range(5):
        try:
            resp = requests.get(_BASE_URL, params=params, timeout=30)

            if resp.status_code == 200:
                # Happy path: cache successful responses for future runs.
                data = resp.json()
                with open(cache_path, "w") as f:
                    json.dump(data, f)
                return data

            if resp.status_code == 429:
                # OpenAlex is rate limiting us; the Retry-After header tells us how long
                # they would like us to wait. We surface this as StopIteration so the
                # caller can stop paging but still return partial data.
                wait = float(resp.headers.get("Retry-After", 10))
                wait_hms = str(datetime.timedelta(seconds=wait))
                log.warning(f"Rate limited — need to wait {wait_hms}")
                raise StopIteration(
                    "OpenAlex rate limit hit — returning rows collected so far"
                )

            # For any other HTTP error, raise to enter the retry block below.
            resp.raise_for_status()

            # If raise_for_status() did not raise, we treat this as a valid response
            # (e.g., 2xx other than 200), cache it, and return.
            data = resp.json()
            with open(cache_path, "w") as f:
                json.dump(data, f)
            return data

        except requests.RequestException as exc:
            # Network errors / server errors: log and retry with exponential backoff.
            if attempt == 4:
                # Exhausted all retries — bubble the last exception up to the caller.
                raise

            log.warning(f"Request error ({exc}), retry {attempt + 1}/5")
            # Backoff schedule: 1, 2, 4, 8 seconds.
            time.sleep(2**attempt)

    # We should never reach this line, but it guards against logic errors.
    raise RuntimeError("OpenAlex: max retries exceeded")


def _extract_row(work: dict) -> dict:
    """Convert one OpenAlex work record into the project candidate-row schema.

    Parameters
    ----------
    work
        A single work object returned by the OpenAlex API.

    Returns
    -------
    dict
        Dictionary matching the fields expected by ``CANDIDATES_COLS``.
    """
    # Authorships is a list of dicts; we pull out author.display_name and join.
    authorships = work.get("authorships") or []
    names = [(a.get("author") or {}).get("display_name") for a in authorships]
    authors = "; ".join(n for n in names if n) or None

    # Primary location gives us journal/source name and landing/pdf URLs.
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    open_access = work.get("open_access") or {}

    # Normalise field names to the *_r convention used by the candidates schema.
    return {
        # DOI is cleaned to a canonical form for downstream matching.
        "doi_r": clean_doi(work.get("doi") or ""),
        "title_r": work.get("display_name") or work.get("title"),
        "abstract_r": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "year_r": work.get("publication_year"),
        "authors_r": authors,
        "journal_r": source.get("display_name"),
        "url_r": open_access.get("oa_url"),
        "openalex_id_r": work.get("id"),
        # Provenance marker: useful as we merge candidates from multiple sources.
        "source": SOURCE_TAG,
    }


def fetch_phrase(
    phrase: str,
    from_year: Optional[int] = None,
    to_year:   Optional[int] = None,
) -> list[dict]:
    """
    Fetch OpenAlex works matching phrase.

    Results are requested page by page using cursor pagination and converted
    into the standard candidate-paper schema defined by ``CANDIDATES_COLS``.
    The function respects the configured inter-request delay and stops cleanly
    if OpenAlex rate-limits the search, returning any rows collected so far.

    Parameters
    ----------
    phrase : str
        Search phrase.
    from_year : int, optional
        Earliest publication year (inclusive). None = no lower bound.
    to_year : int, optional
        Latest publication year (inclusive). None = no upper bound.

    Returns
    -------
    list of dictionaries with CANDIDATES_COLS schema.
    Stops cleanly on rate-limit, returning whatever was collected
    """
    rows: list[dict] = []
    # OpenAlex cursor pagination starts at "*".
    cursor = "*"
    page = 0

    yr_filt = _year_filter(from_year, to_year)
    base_filter = f'title_and_abstract.search:"{phrase}"'
    oa_filter   = f"{base_filter},{yr_filt}" if yr_filt else base_filter

    log.info("OpenAlex search: %r  years=%s–%s",
             phrase,
             from_year or "any",
             to_year   or "any")

    # Keep requesting pages until there is no next cursor or the API tells us to stop.
    while cursor and page < MAX_PAGES_PER_PHRASE:
        # The filter searches both title and abstract text. We also include a
        # mailto parameter as recommended in the OpenAlex API docs so they
        # can reach out in case of abusive traffic.
        params = {
            "filter": oa_filter,
            "per-page": _PER_PAGE,
            "cursor": cursor,
            "mailto": RESEARCHER_EMAIL,
            "select": _SELECT,
        }

        try:
            data = _get_page(params)
        except StopIteration as e:
            # Rate limit reached mid-run. We keep whatever we have and log how
            # far we got for reproducibility/monitoring.
            log.warning(f"  {e} ({len(rows):,} rows collected)")
            break

        # An empty results list means we've consumed all pages.
        results = data.get("results") or []
        if not results:
            break

        # Convert each work to our internal schema and append to the running list.
        rows.extend(_extract_row(work) for work in results)

        page += 1
        cursor = (data.get("meta") or {}).get("next_cursor")
        total = data.get("meta", {}).get("count", "?")

        log.info(
            "OpenAlex phrase=%r page=%d rows=%d total=%s",
            phrase,
            page,
            len(rows),
            total,
        )

        # Throttle between requests to avoid hitting OpenAlex rate limits.
        if cursor and page < MAX_PAGES_PER_PHRASE:
            time.sleep(OPENALEX_RATE_SEC)

    log.info(f"Done — {len(rows):,} rows")

    return rows


def fetch_openalex_candidates(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
) -> pd.DataFrame:
    all_rows: list[dict] = []

    for i, phrase in enumerate(SEARCH_PHRASES, 1):
        log.info("%d/%d Searching OpenAlex phrase %r", i, len(SEARCH_PHRASES), phrase)
        all_rows.extend(fetch_phrase(phrase, from_year, to_year))

    return pd.DataFrame(all_rows, columns=CANDIDATES_COLS)
