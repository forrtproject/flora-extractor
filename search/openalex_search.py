"""
openalex_search.py — Query OpenAlex API for replication/reproduction papers.

Strategy:
    Query OpenAlex with specific quoted phrases rather than the broad term
    "replication" (which returns ~200k results, mostly biology).
    Each phrase query is small (hundreds to a few thousand results), so the
    full set of queries completes in a few minutes.

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
# Search phrases
# These are sent as exact quoted-phrase queries to OpenAlex's
# title_and_abstract.search filter.  Each returns a small, precise result set.
# ---------------------------------------------------------------------------

SEARCH_PHRASES = [
    "direct replication",
    "conceptual replication",
    "close replication",
    "registered replication report",
    "replication study",
    "replication studies",
    "we replicated",
    "we conducted a replication",
    "we performed a replication",
    "attempt to replicate",
    "attempts to replicate",
    "set out to replicate",
    "aim to replicate",
    "aims to replicate",
    "failed to replicate",
    "fail to replicate",
    "did not replicate",
    "successfully replicated",
    "replication and extension",
    "pre-registered replication",
    "preregistered replication",
    "many-labs replication",
    "multi-site replication",
    "multi-lab replication",
    "reproduction study",
    "reproduction studies",
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
# Helpers
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions[pos] = word
    return " ".join(positions[k] for k in sorted(positions)) if positions else None


def _get_page(params: dict) -> dict:
    """Return cached page or fetch from OpenAlex; retry up to 5× on failure."""
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
                wait = float(resp.headers.get("Retry-After", 60))
                log.warning(f"Rate limited — sleeping {min(wait, 120):.0f}s")
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
    authorships = work.get("authorships") or []
    names   = [(a.get("author") or {}).get("display_name") for a in authorships]
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


def _count_phrase(phrase: str) -> int:
    """Quick HEAD-equivalent: fetch one result to read meta.count."""
    params = {
        "filter":   f'title_and_abstract.search:"{phrase}"',
        "per-page": 1,
        "mailto":   RESEARCHER_EMAIL,
        "select":   "id",
    }
    try:
        resp = requests.get(_BASE_URL, params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("meta", {}).get("count", 0)
    except Exception:
        pass
    return 0


def _fetch_phrase(phrase: str) -> list[dict]:
    """Paginate through all OpenAlex results for one quoted phrase."""
    rows: list[dict] = []
    cursor = "*"
    page = 0

    while cursor:
        params = {
            "filter":   f'title_and_abstract.search:"{phrase}"',
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
        page  += 1
        cursor = (data.get("meta") or {}).get("next_cursor")
        time.sleep(OPENALEX_RATE_SEC)

    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_openalex_candidates() -> pd.DataFrame:
    """
    Search OpenAlex for papers matching explicit replication phrases.
    Returns a DataFrame with CANDIDATES_COLS schema.
    """
    # Print estimated size before committing to the full fetch
    log.info("Counting results per phrase...")
    total_est = 0
    for phrase in SEARCH_PHRASES:
        n = _count_phrase(phrase)
        total_est += n
        log.info(f"  {phrase!r:45s} → {n:>6,}")
        time.sleep(OPENALEX_RATE_SEC)
    log.info(f"Estimated total (with overlap): ~{total_est:,}")

    all_rows: list[dict] = []
    for i, phrase in enumerate(SEARCH_PHRASES, 1):
        log.info(f"[{i}/{len(SEARCH_PHRASES)}] Fetching {phrase!r}...")
        rows = _fetch_phrase(phrase)
        all_rows.extend(rows)
        log.info(f"  → {len(rows):,} rows  (running total: {len(all_rows):,})")

    if not all_rows:
        log.warning("OpenAlex returned no results.")
        return pd.DataFrame(columns=CANDIDATES_COLS)

    df = pd.DataFrame(all_rows, columns=CANDIDATES_COLS)
    log.info(f"OpenAlex: {len(df):,} rows before deduplication")
    return df
