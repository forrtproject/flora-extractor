"""
Resumable Semantic Scholar search with per-phrase offset persistence.

Design mirrors openalex_search.py as closely as S2's API allows:
- Every (phrase, from_year, to_year) search is a *job* with a
  ``<S2_CACHE_DIR>/<job_key>.offset.json`` checkpoint file.
- The current offset is saved before each request; re-running resumes
  from the last saved offset rather than restarting.
- Completed jobs (offset exhausted or hard cap reached) are skipped.
- Without S2_API_KEY: first 429 stops the phrase immediately — the shared
  unauthenticated pool is too saturated to retry against.
- With S2_API_KEY:   exponential back-off on 429 up to _MAX_RETRIES.
- S2 relevance search hard cap: offset + limit ≤ 1,000, so max 1,000
  results per query regardless of how large the result set is.

Public API
----------
    fetch_semantic_scholar_candidates(from_year, to_year, max_records_per_phrase) → pd.DataFrame
"""

import datetime
import json
import os
import random
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from shared.config import OA_CACHE_DIR, log
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

S2_API_KEY = os.getenv("S2_API_KEY", "")
S2_CACHE_DIR = OA_CACHE_DIR.parent / "semantic_scholar"
S2_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_PER_PAGE = 100
_MAX_OFFSET = 900  # S2 enforces offset + limit ≤ 1,000
_FIELDS = "paperId,externalIds,title,abstract,year,authors,journal,openAccessPdf"
_RATE_SEC = 3.0 if not S2_API_KEY else 1.1
_MAX_RETRIES = 6
_BACKOFF_CAP = 120

SOURCE_TAG = "semantic_scholar"


# ---------------------------------------------------------------------------
# Offset / progress helpers
# ---------------------------------------------------------------------------


def _job_key(phrase: str, from_year: Optional[int], to_year: Optional[int]) -> str:
    return cache_key(f"s2|{phrase}|{from_year or 'any'}|{to_year or 'any'}")


def _offset_path(phrase: str, from_year: Optional[int], to_year: Optional[int]) -> Path:
    return S2_CACHE_DIR / f"{_job_key(phrase, from_year, to_year)}.offset.json"


def _load_offset_state(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"offset": 0, "total_fetched": 0, "completed": False}


def _save_offset_state(path: Path, offset: int, total: int, completed: bool) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "offset": offset,
                "total_fetched": total,
                "completed": completed,
                "last_updated": datetime.datetime.now().isoformat(timespec="seconds"),
            },
            f,
        )
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _year_param(from_year: Optional[int], to_year: Optional[int]) -> Optional[str]:
    """Build the S2 'year' query parameter, or None if unrestricted."""
    if from_year is None and to_year is None:
        return None
    if from_year and to_year:
        return f"{from_year}-{to_year}"
    if from_year:
        return f"{from_year}-"
    return f"-{to_year}"


def _backoff_sleep(attempt: int) -> None:
    base = min(2.0**attempt, _BACKOFF_CAP)
    jitter = base * random.uniform(-0.1, 0.1)
    wait = base + jitter
    log.warning(
        "S2 rate limited — backoff %.1fs (attempt %d/%d)",
        wait,
        attempt + 1,
        _MAX_RETRIES,
    )
    time.sleep(wait)


# ---------------------------------------------------------------------------
# HTTP — one page, cache-first, 429-aware
# ---------------------------------------------------------------------------


def _get_page(params: dict) -> dict:
    """
    Fetch one S2 results page (cache-first).

    Without API key: first 429 raises StopIteration immediately — the shared
    unauthenticated pool cannot be reliably retried against.
    With API key:    exponential back-off up to _MAX_RETRIES, then StopIteration.
    """
    key = cache_key(str(sorted(params.items())))
    path = S2_CACHE_DIR / f"{key}.json"

    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else {}
    resp = requests.get(_BASE_URL, params=params, headers=headers, timeout=30)

    # Without a key: no point retrying — bail out immediately
    if resp.status_code == 429 and not S2_API_KEY:
        raise StopIteration(
            "S2 rate limited (unauthenticated shared pool). "
            "Get a free API key: https://www.semanticscholar.org/product/api#api-key-form"
        )

    # With a key: exponential back-off retry loop
    for attempt in range(_MAX_RETRIES):
        if resp.status_code == 200:
            data = resp.json()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data
        if resp.status_code == 429:
            _backoff_sleep(attempt)
            resp = requests.get(_BASE_URL, params=params, headers=headers, timeout=30)
        else:
            resp.raise_for_status()

    raise StopIteration(
        f"S2 still returning 429 after {_MAX_RETRIES} retries. "
        "Check your API key or try again later."
    )


def _extract_row(paper: dict) -> dict:
    authors_list = paper.get("authors") or []
    authors = (
        "; ".join(a.get("name", "") for a in authors_list if a.get("name")) or None
    )
    ext_ids = paper.get("externalIds") or {}
    return {
        "doi_r": clean_doi(ext_ids.get("DOI") or ""),
        "title_r": paper.get("title"),
        "abstract_r": paper.get("abstract"),
        "year_r": paper.get("year"),
        "authors_r": authors,
        "journal_r": (paper.get("journal") or {}).get("name"),
        "url_r": (paper.get("openAccessPdf") or {}).get("url"),
        "openalex_id_r": None,
        "source": SOURCE_TAG,
    }


# ---------------------------------------------------------------------------
# Resumable per-phrase paginator
# ---------------------------------------------------------------------------


def fetch_phrase(
    phrase: str,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    max_records: Optional[int] = None,
) -> list[dict]:
    """
    Fetch S2 works for one phrase with resumable offset persistence.

    The offset is checkpointed before each request so an interrupted run
    retries the in-progress page rather than losing it.  Completed phrases
    (offset exhausted or S2 hard cap reached) are skipped on re-run.

    Parameters
    ----------
    phrase : str
    from_year, to_year : int, optional
    max_records : int, optional
        Stop after this many rows *this call* without losing offset position.
        Omit for a full run up to S2's 1,000-result hard cap.
    """
    offset_path = _offset_path(phrase, from_year, to_year)
    state = _load_offset_state(offset_path)

    if state["completed"]:
        log.info("S2 phrase=%r already fully fetched — skipping", phrase)
        return []

    offset = state["offset"]
    total_fetched = state["total_fetched"]
    rows: list[dict] = []

    yr = _year_param(from_year, to_year)
    log.info(
        "S2 phrase=%r  years=%s–%s  resuming from offset=%d (fetched so far: %d)",
        phrase,
        from_year or "any",
        to_year or "any",
        offset,
        total_fetched,
    )

    while offset <= _MAX_OFFSET:
        params = {
            "query": phrase,
            "fields": _FIELDS,
            "offset": offset,
            "limit": _PER_PAGE,
        }
        if yr:
            params["year"] = yr

        # Checkpoint current offset BEFORE the request (crash-safe)
        _save_offset_state(offset_path, offset, total_fetched, completed=False)

        try:
            data = _get_page(params)
        except StopIteration as exc:
            log.warning(
                "  Stopping phrase=%r: %s (%d rows kept)", phrase, exc, len(rows)
            )
            break

        items = data.get("data") or []
        if not items:
            # Offset exhausted — mark complete
            _save_offset_state(offset_path, offset, total_fetched, completed=True)
            log.info("  S2 phrase=%r fully exhausted at offset=%d", phrase, offset)
            break

        rows.extend(_extract_row(p) for p in items)
        total_fetched += len(items)
        log.info(
            "  [%r] offset %5d | run_rows=%d  api_total=%s",
            phrase,
            offset,
            len(rows),
            data.get("total", "?"),
        )

        next_offset = offset + _PER_PAGE
        last_page = len(items) < _PER_PAGE

        if last_page or next_offset > _MAX_OFFSET:
            _save_offset_state(offset_path, next_offset, total_fetched, completed=True)
            log.info("  S2 phrase=%r done (last page or hard cap reached)", phrase)
            break

        offset = next_offset
        # Checkpoint the NEXT offset (advances bookmark past this page)
        _save_offset_state(offset_path, offset, total_fetched, completed=False)

        if max_records is not None and len(rows) >= max_records:
            log.info(
                "  S2 phrase=%r hit max_records=%d for this run — offset saved",
                phrase,
                max_records,
            )
            break

        time.sleep(_RATE_SEC)

    log.info("  [%r] done — %d rows", phrase, len(rows))
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_semantic_scholar_candidates(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    max_records_per_phrase: Optional[int] = None,
) -> pd.DataFrame:
    """
    Search Semantic Scholar for papers matching replication phrases.

    Each phrase job is individually resumable via an offset file in
    S2_CACHE_DIR.  Completed phrase jobs are skipped automatically.

    Parameters
    ----------
    from_year, to_year : int, optional
        Year range.  Part of the job identity — a different range creates
        independent checkpoint files.
    max_records_per_phrase : int, optional
        Cap new rows per phrase per run without losing offset position.
        Omit for a full run (up to S2's 1,000-result hard cap per query).

    Notes
    -----
    Set S2_API_KEY env var for reliable throughput (free key at
    https://www.semanticscholar.org/product/api#api-key-form).
    Without a key the unauthenticated shared pool is used; the first 429
    stops the current phrase and moves on to the next.
    """
    if S2_API_KEY:
        log.info("Semantic Scholar: authenticated (dedicated 1 req/s)")
    else:
        log.info(
            "Semantic Scholar: unauthenticated (%.1fs between pages) — "
            "get a free API key for reliable throughput",
            _RATE_SEC,
        )
    log.info(
        "Semantic Scholar search  years=%s–%s", from_year or "any", to_year or "any"
    )

    yr = _year_param(from_year, to_year)
    all_rows: list[dict] = []

    for i, phrase in enumerate(SEARCH_PHRASES, 1):
        log.info("[%d/%d] Searching S2: %r", i, len(SEARCH_PHRASES), phrase)
        all_rows.extend(
            fetch_phrase(phrase, from_year, to_year, max_records=max_records_per_phrase)
        )

    if not all_rows:
        log.warning("Semantic Scholar returned no results.")
        return pd.DataFrame(columns=CANDIDATES_COLS)

    df = pd.DataFrame(all_rows, columns=CANDIDATES_COLS)
    log.info("Semantic Scholar done — %d rows (pre-dedup)", len(df))
    return df
