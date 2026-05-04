"""
Utilities for querying Semantic Scholar for replication-related papers.

This module searches the Semantic Scholar paper search endpoint using a small
set of replication-related phrases and converts the results into the standard
candidate-paper schema used elsewhere in the project.

Key differences from OpenAlex:
- Abstract text is returned directly, so no inverted-index reconstruction is needed.
- Relevance search uses offset-based pagination rather than cursor pagination.
- Relevance search returns at most 100 results per page.
- Relevance search currently returns up to 1,000 results in total for a query.
- An API key can be supplied via the ``S2_API_KEY`` environment variable.

Public API:
    fetch_semantic_scholar() -> pd.DataFrame
"""

import json
import os
import time

import pandas as pd
import requests

from shared.config import OA_CACHE_DIR, log
from shared.schema import CANDIDATES_COLS
from shared.utils import cache_key, clean_doi


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# A small set of phrases intended to capture replication papers expressed in
# different ways. We search each phrase independently and combine the results.
SEARCH_PHRASES = [
    "registered replication report",
    "direct replication",
    "conceptual replication",
    "replication study",
    "failed to replicate",
    "did not replicate",
    "we replicated",
    "attempt to replicate",
]

# Optional API key. Semantic Scholar recommends using a key when available.
# Keeping this in an environment variable avoids hard-coding secrets in code.
S2_API_KEY = os.getenv("S2_API_KEY", "")

# Cache responses on disk so repeated runs do not re-download identical pages.
# Reusing OA_CACHE_DIR.parent keeps all source caches grouped together.
S2_CACHE_DIR = OA_CACHE_DIR.parent / "semantic_scholar"
S2_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

# Maximum number of records returned per page by relevance search.
_PER_PAGE = 100

# Relevance search currently enforces offset + limit <= 1,000.
# With limit=100, the final usable offset is 900.
_MAX_OFFSET = 900

# Request only the fields we need for the downstream candidate schema.
_FIELDS = "paperId,externalIds,title,abstract,year,authors,journal,openAccessPdf"

# Conservative throttle settings. Unauthenticated access may be throttled, and
# authenticated access depends on your assigned rate limit, so these values aim
# for reliability rather than maximum throughput.
_RATE_SEC = 1.1 if not S2_API_KEY else 0.1

# Source tag written into the output so downstream merged data can retain provenance.
SOURCE_TAG = "semantic_scholar"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_page(params: dict) -> dict:
    """Fetch one Semantic Scholar results page, using the cache when available.

    The cache key is based on the full request parameter set, so identical
    searches can reuse prior responses from disk. If the API returns HTTP 429,
    the function raises ``StopIteration`` so the caller can stop paging cleanly
    and keep any rows already collected.

    Parameters
    ----------
    params
        Query parameters for the Semantic Scholar paper search endpoint.

    Returns
    -------
    dict
        Parsed JSON response body from Semantic Scholar.

    Raises
    ------
    StopIteration
        If the API responds with HTTP 429 (rate limited).
    requests.RequestException
        If the request fails for any other HTTP/network reason.
    """
    # Include all query parameters in the cache key so different phrases, fields,
    # offsets, or limits do not collide.
    key = cache_key(str(sorted(params.items())))
    path = S2_CACHE_DIR / f"{key}.json"

    # Cache hit: return saved JSON directly and avoid a network request.
    if path.exists():
        with open(path) as f:
            return json.load(f)

    # Only send the x-api-key header when a key is actually available.
    headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else {}

    resp = requests.get(_BASE_URL, params=params, headers=headers, timeout=30)

    if resp.status_code == 429:
        # The caller treats this as a signal to stop pagination and return the
        # partial dataset collected so far.
        raise StopIteration("S2 rate limit hit — returning rows collected so far")

    resp.raise_for_status()

    # Cache successful responses so reruns are faster and gentler on the API.
    data = resp.json()
    with open(path, "w") as f:
        json.dump(data, f)

    return data


def _extract_row(paper: dict) -> dict:
    """Convert one Semantic Scholar paper record into the candidate-row schema.

    Parameters
    ----------
    paper
        A single paper object from the Semantic Scholar API response.

    Returns
    -------
    dict
        Dictionary matching the fields expected by ``CANDIDATES_COLS``.
    """
    # Authors arrive as a list of objects. Join available names into the same
    # semicolon-delimited format used by other source adapters.
    authors_list = paper.get("authors") or []
    authors = (
        "; ".join(a.get("name", "") for a in authors_list if a.get("name")) or None
    )

    # Semantic Scholar stores DOI inside externalIds rather than as a top-level field.
    ext_ids = paper.get("externalIds") or {}
    doi = clean_doi(ext_ids.get("DOI") or "")

    # Journal name and OA PDF URL are nested structures and may be missing.
    journal = (paper.get("journal") or {}).get("name")
    url = (paper.get("openAccessPdf") or {}).get("url")

    # Map source-specific fields into the shared candidate schema.
    return {
        "doi_r": doi,
        "title_r": paper.get("title"),
        "abstract_r": paper.get("abstract"),
        "year_r": paper.get("year"),
        "authors_r": authors,
        "journal_r": journal,
        "url_r": url,
        "openalex_id_r": None,  # Semantic Scholar records do not carry OpenAlex IDs.
        "source": SOURCE_TAG,
    }


def _fetch_phrase(phrase: str) -> list[dict]:
    """Fetch all available relevance-search results for one search phrase.

    Semantic Scholar relevance search uses offset-based pagination. This helper
    walks through the available pages for a single phrase, converts each paper
    into the shared row schema, and stops when there are no more items, the last
    page is shorter than the page size, or the API rate-limits the request.

    Parameters
    ----------
    phrase
        Search phrase to send to Semantic Scholar.

    Returns
    -------
    list[dict]
        Candidate rows extracted from all fetched pages for the phrase.
    """
    rows: list[dict] = []
    offset = 0

    # Keep requesting pages until we hit the API's maximum supported offset.
    while offset <= _MAX_OFFSET:
        params = {
            "query": phrase,
            "fields": _FIELDS,
            "offset": offset,
            "limit": _PER_PAGE,
        }

        try:
            data = _get_page(params)
        except StopIteration as e:
            # Stop early but keep the rows we have already accumulated.
            log.warning(f"  {e} ({len(rows):,} rows collected)")
            break

        # Semantic Scholar returns papers under the "data" key.
        items = data.get("data") or []
        if not items:
            # No items means we have exhausted available results for this phrase.
            break

        # Convert page results into the shared internal schema.
        rows.extend(_extract_row(paper) for paper in items)
        total = data.get("total", "?")

        log.info(
            f"  [{phrase!r}] offset {offset:>4} | {len(rows):>5,} / {total} fetched"
        )

        # If the API returned fewer than the requested page size, this is the last page.
        if len(items) < _PER_PAGE:
            break

        # Advance to the next page and pause to avoid hammering the API.
        offset += _PER_PAGE
        time.sleep(_RATE_SEC)

    log.info(f"  [{phrase!r}] done — {len(rows):,} rows")
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_semantic_scholar() -> pd.DataFrame:
    """Search Semantic Scholar for replication-related papers.

    Each phrase in ``SEARCH_PHRASES`` is queried independently. Results are
    concatenated into a single DataFrame using the standard candidate-paper
    schema defined by ``CANDIDATES_COLS``.

    Returns
    -------
    pd.DataFrame
        DataFrame with columns ordered according to ``CANDIDATES_COLS``.

    Notes
    -----
    Setting the ``S2_API_KEY`` environment variable may improve reliability and
    rate limits for repeated or larger searches.
    """
    if S2_API_KEY:
        log.info("Semantic Scholar: using API key")
    else:
        log.info(
            "Semantic Scholar: no API key — using unauthenticated access; requests may be throttled"
        )

    all_rows: list[dict] = []

    # Search phrases one by one so logs clearly show progress and failures can be
    # attributed to a specific query phrase.
    for i, phrase in enumerate(SEARCH_PHRASES, 1):
        log.info(f"[{i}/{len(SEARCH_PHRASES)}] Searching S2: {phrase!r}")
        all_rows.extend(_fetch_phrase(phrase))

    if not all_rows:
        # Return an empty DataFrame with the canonical schema rather than an
        # empty frame with unpredictable columns.
        log.warning("Semantic Scholar returned no results.")
        return pd.DataFrame(columns=CANDIDATES_COLS)

    df = pd.DataFrame(all_rows, columns=CANDIDATES_COLS)
    log.info(f"Semantic Scholar: {len(df):,} rows before deduplication")
    return df
