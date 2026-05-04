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


# Fixed search phrase used to retrieve candidate replication papers from OpenAlex.
SEARCH_PHRASE = "registered replication report"

# OpenAlex works endpoint and query defaults.
_BASE_URL = "https://api.openalex.org/works"
# Maximum number of records returned by OpenAlex per page.
_PER_PAGE = 200
# Fields we actually need from each work. Keeping this small reduces payload size.
_SELECT = (
    "id,doi,display_name,publication_year,"
    "authorships,primary_location,abstract_inverted_index"
)


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
        with open(cache_path) as f:
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

    # Normalise field names to the *_r convention used by the candidates schema.
    return {
        # DOI is cleaned to a canonical form for downstream matching.
        "doi_r": clean_doi(work.get("doi") or ""),
        "title_r": work.get("display_name"),
        "abstract_r": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "year_r": work.get("publication_year"),
        "authors_r": authors,
        "journal_r": source.get("display_name"),
        # Prefer a human-readable landing page URL; fall back to direct PDF.
        "url_r": location.get("landing_page_url") or location.get("pdf_url"),
        "openalex_id_r": work.get("id"),
        # Provenance marker: useful as we merge candidates from multiple sources.
        "source": "openalex",
    }


def fetch_openalex_candidates() -> pd.DataFrame:
    """Fetch OpenAlex works matching ``SEARCH_PHRASE``.

    Results are requested page by page using cursor pagination and converted
    into the standard candidate-paper schema defined by ``CANDIDATES_COLS``.
    The function respects the configured inter-request delay and stops cleanly
    if OpenAlex rate-limits the search, returning any rows collected so far.

    Returns
    -------
    pd.DataFrame
        DataFrame with columns ordered according to ``CANDIDATES_COLS``.
    """
    rows: list[dict] = []
    # OpenAlex cursor pagination starts at "*".
    cursor = "*"
    page = 0

    log.info(f"OpenAlex search: {SEARCH_PHRASE!r}")

    # Keep requesting pages until there is no next cursor or the API tells us to stop.
    while cursor:
        # The filter searches both title and abstract text. We also include a
        # mailto parameter as recommended in the OpenAlex API docs so they
        # can reach out in case of abusive traffic.
        params = {
            "filter": f'title_and_abstract.search:"{SEARCH_PHRASE}"',
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

        log.info(f"  page {page:>3} | {len(rows):>5,} / {total} fetched")

        # Throttle between requests to avoid hitting OpenAlex rate limits.
        time.sleep(OPENALEX_RATE_SEC)

    log.info(f"Done — {len(rows):,} rows")

    # We always construct the DataFrame with the canonical column order, even
    # if some columns are entirely missing/null for this particular query.
    return pd.DataFrame(rows, columns=CANDIDATES_COLS)
