"""
Resumable OpenAlex search with per-phrase cursor persistence.

Design
------
- Every (phrase, from_year, to_year) search is a *job*.
- After each page the cursor is checkpointed to
  ``<OA_CACHE_DIR>/<job_key>.cursor.json`` (atomic write).
- Re-running picks up from the last saved cursor; completed jobs are skipped.
- On HTTP 429 the code sleeps ``Retry-After`` seconds and retries in-place —
  it does NOT abort pagination (mirrors the R ``purrr::insistently`` approach).
- ``max_records_per_phrase`` limits rows *returned this call* without advancing
  past the checkpoint, so subsequent runs continue from exactly that page.

Public API
----------
    fetch_openalex_candidates(from_year, to_year, max_records_per_phrase) → pd.DataFrame
"""

import datetime
import json
import time
from pathlib import Path
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

_BASE_URL = "https://api.openalex.org/works"
_PER_PAGE = 200
_SELECT = (
    "id,doi,display_name,publication_year,"
    "authorships,primary_location,abstract_inverted_index"
)
SOURCE_TAG = "openalex"
_CURSOR_START = "*"


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _job_key(phrase: str, from_year: Optional[int], to_year: Optional[int]) -> str:
    """Return a stable hash key identifying a (phrase, year-range) job."""
    return cache_key(f"oa|{phrase}|{from_year or 'any'}|{to_year or 'any'}")


def _cursor_path(phrase: str, from_year: Optional[int], to_year: Optional[int]) -> Path:
    """Return the Path where the cursor checkpoint for this job is stored."""
    return OA_CACHE_DIR / f"{_job_key(phrase, from_year, to_year)}.cursor.json"


def _load_cursor_state(path: Path) -> dict:
    """Load cursor state from *path*, or return a fresh-start state if absent or corrupt."""
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"cursor": _CURSOR_START, "total_fetched": 0, "completed": False}


def _save_cursor_state(
    path: Path, cursor: Optional[str], total: int, completed: bool
) -> None:
    """Atomically write cursor state so a crashed process leaves a valid checkpoint file."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "cursor": cursor,
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


def _year_filter(from_year: Optional[int], to_year: Optional[int]) -> Optional[str]:
    """Build an OpenAlex ``publication_year`` filter fragment, or ``None`` if unrestricted."""
    if from_year is None and to_year is None:
        return None
    return f"publication_year:{from_year or 1000}-{to_year or 9999}"


def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    """Reconstruct plain abstract text from an OpenAlex inverted index.

    OpenAlex represents abstracts as a mapping of ``{word: [positions]}``.
    This reverses the mapping into position order and joins the tokens.
    Returns ``None`` when no abstract data is provided.
    """
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions[pos] = word
    return " ".join(positions[k] for k in sorted(positions)) if positions else None


# ---------------------------------------------------------------------------
# HTTP — one page, cache-first, 429-aware
# ---------------------------------------------------------------------------


def _get_page(params: dict, max_retries: int = 5) -> dict:
    """Fetch one OpenAlex results page, serving from disk cache when available.

    On a cache miss the page is requested from the API.  A 429 response causes
    the function to sleep for ``Retry-After`` seconds then retry in-place; the
    cursor is checkpointed by the caller *before* this call, so an interrupted
    process can resume safely.  Other transient HTTP/network errors use
    exponential back-off up to *max_retries* attempts.

    Parameters
    ----------
    params : dict
        Query parameters for the OpenAlex works endpoint.
    max_retries : int
        Maximum number of retry attempts for transient errors.

    Returns
    -------
    dict
        Parsed JSON response from OpenAlex.

    Raises
    ------
    requests.RequestException
        If the request keeps failing after all retries.
    RuntimeError
        If the retry loop exits without returning a response.
    """
    key = cache_key(str(sorted(params.items())))
    cache_path = OA_CACHE_DIR / f"{key}.json"

    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    for attempt in range(max_retries):
        try:
            resp = requests.get(_BASE_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2**attempt
            log.warning(
                "Network error (%s) — retry %d/%d in %ds",
                exc,
                attempt + 1,
                max_retries,
                wait,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            data = resp.json()
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data

        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 60))
            wait_str = str(datetime.timedelta(seconds=int(wait)))
            log.warning(
                "OpenAlex 429 — sleeping %s then retrying (attempt %d/%d)",
                wait_str,
                attempt + 1,
                max_retries,
            )
            time.sleep(wait)
            continue

        if attempt == max_retries - 1:
            resp.raise_for_status()
        wait = 2**attempt
        log.warning(
            "HTTP %d — retry %d/%d in %ds",
            resp.status_code,
            attempt + 1,
            max_retries,
            wait,
        )
        time.sleep(wait)

    raise RuntimeError("OpenAlex: max retries exceeded")


def _extract_row(work: dict) -> dict:
    """Convert one OpenAlex work record into the shared candidate-row schema."""
    authorships = work.get("authorships") or []
    names = [(a.get("author") or {}).get("display_name") for a in authorships]
    authors = "; ".join(n for n in names if n) or None

    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    open_access = work.get("open_access") or {}

    return {
        "doi_r": clean_doi(work.get("doi") or ""),
        "title_r": work.get("display_name") or work.get("title"),
        "abstract_r": _reconstruct_abstract(work.get("abstract_inverted_index")),
        "year_r": work.get("publication_year"),
        "authors_r": authors,
        "journal_r": source.get("display_name"),
        "url_r": open_access.get("oa_url"),
        "openalex_id_r": work.get("id"),
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
    """Fetch OpenAlex works matching *phrase* with resumable cursor persistence.

    The cursor is checkpointed twice per page: once *before* the request
    (so a crash during the request retries that page on resume) and once
    *after* (advancing the bookmark to the next page).  Completed phrases
    write ``completed: true`` and are skipped on subsequent calls.

    Parameters
    ----------
    phrase : str
        Exact-phrase search string applied against title and abstract.
    from_year, to_year : int, optional
        Publication year bounds (inclusive).  Together with *phrase* these
        form the job identity — a different year range is an independent job
        with its own cursor file.
    max_records : int, optional
        Stop returning rows after this count *for this call* without losing
        the cursor position.  The next call resumes from the same page
        boundary.  ``None`` runs until the phrase result set is exhausted.

    Returns
    -------
    list[dict]
        Candidate rows in the shared schema defined by ``CANDIDATES_COLS``.
    """
    cursor_path = _cursor_path(phrase, from_year, to_year)
    state = _load_cursor_state(cursor_path)

    if state["completed"]:
        log.info("OpenAlex phrase=%r already fully fetched — skipping", phrase)
        return []

    cursor = state["cursor"] or _CURSOR_START
    total_fetched = state["total_fetched"]
    rows: list[dict] = []

    yr_filt = _year_filter(from_year, to_year)
    base_filter = f'title_and_abstract.search:"{phrase}"'
    oa_filter = f"{base_filter},{yr_filt}" if yr_filt else base_filter

    log.info(
        "OpenAlex phrase=%r  years=%s–%s  prev_fetched=%d",
        phrase,
        from_year or "any",
        to_year or "any",
        total_fetched,
    )

    while cursor:
        params = {
            "filter": oa_filter,
            "per-page": _PER_PAGE,
            "cursor": cursor,
            "mailto": RESEARCHER_EMAIL,
            "select": _SELECT,
        }

        # Checkpoint current cursor before the request (crash-safe: the next
        # run retries this page rather than skipping it).
        _save_cursor_state(cursor_path, cursor, total_fetched, completed=False)

        data = _get_page(params)
        results = data.get("results") or []
        if not results:
            cursor = None
            break

        rows.extend(_extract_row(w) for w in results)
        total_fetched += len(results)

        next_cursor = (data.get("meta") or {}).get("next_cursor")
        api_total = data.get("meta", {}).get("count", "?")
        log.info(
            "  phrase=%r  page_rows=%d  run_rows=%d  api_total=%s",
            phrase,
            len(results),
            len(rows),
            api_total,
        )

        cursor = next_cursor  # None → phrase fully exhausted

        # Checkpoint the next cursor, advancing the bookmark past this page.
        _save_cursor_state(cursor_path, cursor, total_fetched, completed=(not cursor))

        if not cursor:
            log.info("  phrase=%r fully exhausted", phrase)
            break

        if max_records is not None and len(rows) >= max_records:
            log.info(
                "  phrase=%r  reached max_records=%d for this run — cursor saved at page boundary",
                phrase,
                max_records,
            )
            break

        time.sleep(OPENALEX_RATE_SEC)

    log.info("Done — %d rows for phrase=%r", len(rows), phrase)
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_openalex_candidates(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    max_records_per_phrase: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch OpenAlex candidates across all ``SEARCH_PHRASES``.

    Each phrase is an independent resumable job backed by a cursor file in
    ``OA_CACHE_DIR``.  Completed jobs are skipped automatically, so this
    function can be called repeatedly to incrementally extend the dataset.

    Parameters
    ----------
    from_year, to_year : int, optional
        Publication year range (inclusive).  The year range is part of the
        job identity, so changing it starts a fresh set of cursor files
        without disturbing jobs run under a different range.
    max_records_per_phrase : int, optional
        Cap on new rows fetched per phrase per call.  The cursor is saved at
        the page boundary so the next call continues from that point.
        ``None`` runs each phrase to full exhaustion.

    Returns
    -------
    pd.DataFrame
        Candidate rows with columns ordered per ``CANDIDATES_COLS``.
        Returns an empty DataFrame (with correct columns) if no results
        are found or all phrase jobs are already complete.
    """
    all_rows: list[dict] = []

    for i, phrase in enumerate(SEARCH_PHRASES, 1):
        log.info("%d/%d  phrase=%r", i, len(SEARCH_PHRASES), phrase)
        all_rows.extend(
            fetch_phrase(phrase, from_year, to_year, max_records=max_records_per_phrase)
        )

    if not all_rows:
        return pd.DataFrame(columns=CANDIDATES_COLS)

    return pd.DataFrame(all_rows, columns=CANDIDATES_COLS)
