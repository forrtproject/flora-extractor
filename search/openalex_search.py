"""
openalex_search.py — Query OpenAlex for replication papers.

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


SEARCH_PHRASE = "registered replication report"   # ~3,700 results — good test size

_BASE_URL = "https://api.openalex.org/works"
_PER_PAGE = 200
_SELECT = (
    "id,doi,display_name,publication_year,"
    "authorships,primary_location,abstract_inverted_index"
)


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
    """Fetch one page (cache-first). Raises on HTTP errors including 429."""
    key = cache_key(str(sorted(params.items())))
    cache_path = OA_CACHE_DIR / f"{key}.json"

    if cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)

    resp = requests.get(_BASE_URL, params=params, timeout=30)

    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "unknown")
        raise RuntimeError(
            f"OpenAlex rate limit hit. Retry-After: {retry_after}s  "
            f"(~{int(retry_after)//3600}h {(int(retry_after)%3600)//60}m). "
            f"Wait before re-running, or delete cache/openalex/ to start fresh."
            if retry_after.isdigit() else
            f"OpenAlex rate limit hit. Retry-After header: {retry_after!r}"
        )

    resp.raise_for_status()
    data = resp.json()
    with open(cache_path, "w") as f:
        json.dump(data, f)
    return data


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


# ---------------------------------------------------------------------------

def fetch_openalex_candidates() -> pd.DataFrame:
    """
    Fetch all OpenAlex works matching SEARCH_PHRASE.
    Returns a DataFrame with CANDIDATES_COLS schema.
    """
    rows: list[dict] = []
    cursor = "*"
    page = 0

    log.info(f"OpenAlex search: {SEARCH_PHRASE!r}")

    while cursor:
        params = {
            "filter":   f'title_and_abstract.search:"{SEARCH_PHRASE}"',
            "per-page": _PER_PAGE,
            "cursor":   cursor,
            "mailto":   RESEARCHER_EMAIL,
            "select":   _SELECT,
        }
        data    = _get_page(params)   # raises clearly on 429
        results = data.get("results") or []
        if not results:
            break

        rows.extend(_extract_row(w) for w in results)
        page  += 1
        cursor = (data.get("meta") or {}).get("next_cursor")

        total = data.get("meta", {}).get("count", "?")
        log.info(f"  page {page:>3} | {len(rows):>5,} / {total:,} fetched")
        time.sleep(OPENALEX_RATE_SEC)

    log.info(f"Done — {len(rows):,} rows")
    return pd.DataFrame(rows, columns=CANDIDATES_COLS)
