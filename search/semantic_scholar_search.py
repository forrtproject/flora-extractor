"""
semantic_scholar_search.py — Query Semantic Scholar for replication papers.

API docs: https://api.semanticscholar.org/api-docs/graph#tag/Paper-Data/operation/get_graph_get_paper_search

Key differences from OpenAlex:
  - Abstract returned directly (no inverted index reconstruction)
  - Offset-based pagination (not cursor-based), max 10,000 results per query
  - 100 results per page max
  - Optional API key via S2_API_KEY env var (raises rate limit significantly)

Public API:
    fetch_semantic_scholar_candidates() → pd.DataFrame  (CANDIDATES_COLS schema)
"""
import json
import os
import time
from typing import Optional

import pandas as pd
import requests

from shared.config import OA_CACHE_DIR, OPENALEX_RATE_SEC, log
from shared.schema import CANDIDATES_COLS
from shared.utils import cache_key, clean_doi


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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

S2_API_KEY   = os.getenv("S2_API_KEY", "")          # optional — raises rate limit
S2_CACHE_DIR = OA_CACHE_DIR.parent / "semantic_scholar"
S2_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_BASE_URL  = "https://api.semanticscholar.org/graph/v1/paper/search"
_PER_PAGE  = 100    # S2 max
_MAX_OFFSET = 9_900  # S2 hard cap: offset + limit <= 10,000
_FIELDS    = "paperId,externalIds,title,abstract,year,authors,journal,openAccessPdf"
_RATE_SEC  = 1.1 if not S2_API_KEY else 0.1   # ~1 req/s unauthenticated, 10/s with key

SOURCE_TAG = "semantic_scholar"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_page(params: dict) -> dict:
    """Return cached page or fetch from S2; retries on 429."""
    key  = cache_key(str(sorted(params.items())))
    path = S2_CACHE_DIR / f"{key}.json"

    if path.exists():
        with open(path) as f:
            return json.load(f)

    headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else {}

    resp = requests.get(_BASE_URL, params=params, headers=headers, timeout=30)
    if resp.status_code == 429:
        raise StopIteration("S2 rate limit hit — returning rows collected so far")
    resp.raise_for_status()
    data = resp.json()
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def _extract_row(paper: dict) -> dict:
    """Map a Semantic Scholar paper dict → CANDIDATES_COLS row."""
    authors_list = paper.get("authors") or []
    authors = "; ".join(a.get("name", "") for a in authors_list if a.get("name")) or None

    ext_ids = paper.get("externalIds") or {}
    doi     = clean_doi(ext_ids.get("DOI") or "")

    journal = (paper.get("journal") or {}).get("name")
    url     = (paper.get("openAccessPdf") or {}).get("url")

    return {
        "doi_r":         doi,
        "title_r":       paper.get("title"),
        "abstract_r":    paper.get("abstract"),
        "year_r":        paper.get("year"),
        "authors_r":     authors,
        "journal_r":     journal,
        "url_r":         url,
        "openalex_id_r": None,   # not an OA record
        "source":        SOURCE_TAG,
    }


def _fetch_phrase(phrase: str) -> list[dict]:
    """Page through all S2 results for one search phrase (max 10k)."""
    rows   = []
    offset = 0

    while offset <= _MAX_OFFSET:
        params = {
            "query":  phrase,
            "fields": _FIELDS,
            "offset": offset,
            "limit":  _PER_PAGE,
        }
        try:
            data = _get_page(params)
        except StopIteration as e:
            log.warning(f"  {e} ({len(rows):,} rows collected)")
            break
        items = data.get("data") or []
        if not items:
            break

        rows.extend(_extract_row(p) for p in items)
        total = data.get("total", "?")

        log.info(
            f"  [{phrase!r}] offset {offset:>5} "
            f"| {len(rows):>5,} / {total} fetched"
        )

        if len(items) < _PER_PAGE:
            break                        # last page
        offset += _PER_PAGE
        time.sleep(_RATE_SEC)

    log.info(f"  [{phrase!r}] done — {len(rows):,} rows")
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_semantic_scholar() -> pd.DataFrame:
    """
    Search Semantic Scholar for papers matching replication phrases.
    Returns a DataFrame with CANDIDATES_COLS schema.

    Tip: set S2_API_KEY env var for a higher rate limit (free registration at
    https://www.semanticscholar.org/product/api).
    """
    if S2_API_KEY:
        log.info("Semantic Scholar: using API key")
    else:
        log.info("Semantic Scholar: no API key — rate limited to ~1 req/s (set S2_API_KEY to speed up)")

    all_rows: list[dict] = []
    for i, phrase in enumerate(SEARCH_PHRASES, 1):
        log.info(f"[{i}/{len(SEARCH_PHRASES)}] Searching S2: {phrase!r}")
        all_rows.extend(_fetch_phrase(phrase))

    if not all_rows:
        log.warning("Semantic Scholar returned no results.")
        return pd.DataFrame(columns=CANDIDATES_COLS)

    df = pd.DataFrame(all_rows, columns=CANDIDATES_COLS)
    log.info(f"Semantic Scholar: {len(df):,} rows before deduplication")
    return df
