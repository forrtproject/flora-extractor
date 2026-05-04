"""
openalex_search.py — Query OpenAlex API for replication/reproduction papers.

Public API:
    fetch_openalex_candidates() → pd.DataFrame  (CANDIDATES_COLS schema)
"""
import json
import time
from typing import Optional

import pandas as pd
import requests

from shared.config import OA_CACHE_DIR, OPENALEX_RATE_SEC, RESEARCHER_EMAIL, log
from shared.schema import CANDIDATES_COLS
from shared.utils import cache_key, clean_doi


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------

REPLICATION_KEYWORDS = [
    "replication",
    "direct replication",
    "close replication",
    "conceptual replication",
    "reproduction study",
    "replication study",
    "we replicated",
    "attempts to replicate",
    "registered replication report",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.openalex.org/works"
_PER_PAGE = 200
_SELECT = (
    "id,doi,display_name,publication_year,"
    "authorships,primary_location,abstract_inverted_index"
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    """Rebuild plain text from an OpenAlex inverted index."""
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions[pos] = word
    return " ".join(positions[k] for k in sorted(positions)) if positions else None


def _get_page(params: dict) -> dict:
    """
    Fetch one paginated response from OpenAlex.
    Checks the on-disk cache first; writes the response to cache on success.
    Retries up to 5 times with exponential backoff on transient errors.
    """
    key = cache_key(str(sorted(params.items())))
    cache_path = OA_CACHE_DIR / f"{key}.json"

    if cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)

    for attempt in range(5):
        try:
            resp = requests.get(_BASE_URL, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                with open(cache_path, "w") as f:
                    json.dump(data, f)
                return data
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 10))
                log.warning(f"Rate limited — sleeping {wait:.0f}s")
                time.sleep(min(wait, 120))
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            if attempt == 4:
                raise
            log.warning(f"Request error ({exc}), retry {attempt + 1}/5")
            time.sleep(2 ** attempt)

    raise RuntimeError("OpenAlex: max retries exceeded")


def _extract_row(work: dict) -> dict:
    """Pull CANDIDATES_COLS fields from a raw OpenAlex work dict."""
    authorships = work.get("authorships") or []
    names = [(a.get("author") or {}).get("display_name") for a in authorships]
    authors = "; ".join(n for n in names if n) or None

    location = work.get("primary_location") or {}
    source   = location.get("source") or {}

    return {
        "doi_r":         clean_doi(work.get("doi") or ""),
        "title_r":       work.get("display_name"),
        "abstract_r":    _reconstruct_abstract(work.get("abstract_inverted_index")),
        "year_r":        work.get("publication_year"),
        "authors_r":     authors,
        "journal_r":     source.get("display_name"),
        "url_r":         location.get("landing_page_url") or location.get("pdf_url"),
        "openalex_id_r": work.get("id"),
        "source":        "openalex",
    }


def _fetch_keyword(keyword: str) -> list[dict]:
    """Paginate through all OpenAlex results for one keyword phrase."""
    rows: list[dict] = []
    cursor = "*"
    page = 0

    while cursor:
        params = {
            "filter":   f'title_and_abstract.search:"{keyword}"',
            "per-page": _PER_PAGE,
            "cursor":   cursor,
            "mailto":   RESEARCHER_EMAIL,
            "select":   _SELECT,
        }
        data    = _get_page(params)
        results = data.get("results") or []
        if not results:
            break

        rows.extend(_extract_row(w) for w in results)
        page += 1
        if page % 10 == 0:
            log.info(f"  [{keyword!r}] page {page} — {len(rows):,} rows so far")

        time.sleep(OPENALEX_RATE_SEC)
        cursor = (data.get("meta") or {}).get("next_cursor")

    log.info(f"  [{keyword!r}] done — {len(rows):,} rows")
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_openalex_candidates() -> pd.DataFrame:
    """
    Search OpenAlex for papers matching replication keywords.
    Returns a DataFrame with CANDIDATES_COLS schema.
    """
    all_rows: list[dict] = []

    for keyword in REPLICATION_KEYWORDS:
        log.info(f"OpenAlex search: {keyword!r}")
        all_rows.extend(_fetch_keyword(keyword))

    if not all_rows:
        log.warning("OpenAlex returned no results.")
        return pd.DataFrame(columns=CANDIDATES_COLS)

    df = pd.DataFrame(all_rows, columns=CANDIDATES_COLS)
    log.info(f"OpenAlex: {len(df):,} rows (before deduplication)")
    return df
