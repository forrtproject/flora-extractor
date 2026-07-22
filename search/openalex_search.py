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
import functools
import json
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from shared.config import OA_CACHE_DIR, OPENALEX_API_KEY, OPENALEX_RATE_SEC, RESEARCHER_EMAIL, log
from shared.schema import CANDIDATES_COLS
from shared.utils import cache_key, clean_doi


SEARCH_PHRASES = [
    # Original high-precision phrases
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
    # Added from hackathon spec/search-keywords.yaml — high precision tier
    "failed to replicate",
    "did not replicate",
    "we replicate",
    "replicating the findings",
    "could not reproduce",
    "successfully replicated",
    "reproducibility of",
    "replication and extension",
    "replicability of",
    "attempt to replicate",
    "failure to replicate",
    "non-replication",
    "reproducibility study",
    "reproduce the findings",
    # Fix 3: abstract-only phrases — confirmed replications that only use
    # replication language inside the abstract, not in the title.
    "our results replicate",
    "our findings replicate",
    "results replicate the",
    "confirm and replicate",
    "replication across",
    "cross-cultural replication",
    "independent replication",
    "partial replication",
    "multi-site replication",
    "multisite replication",
    "preregistered replication",
    "exact replication",
    "systematic replication",
]

# Concept-based search — catches papers classified by OpenAlex's own ML as
# being about replication/reproducibility, even when the abstract is absent or
# uses atypical wording.  IDs verified 2026-06-23 via --list-concepts.
# To add/remove concepts run:  python -m search.run_search --list-concepts "replication"
CONCEPT_IDS = [
    "C12590798",   # Replication (statistics) — 263k works
    "C9893847",    # Reproducibility — 121k works
]

_BASE_URL = "https://api.openalex.org/works"
_PER_PAGE = 200
_SELECT = (
    "id,doi,display_name,publication_year,"
    "authorships,primary_location,abstract_inverted_index"
)
SOURCE_TAG = "openalex"
SOURCE_TAG_CONCEPT = "openalex_concept"
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


@functools.lru_cache(maxsize=1)
def _job_key_index() -> "tuple[list[str], dict[str, str]]":
    """(labels, {job_key: label}) for every plausible (phrase, year-range) job.

    Cursor filenames are hashes, so attribution is done by hashing every phrase
    against every year combination the pipeline could have used and matching the
    result to the files on disk.
    """
    import datetime

    years: list = [None, *range(1900, datetime.date.today().year + 3)]
    labels = [*SEARCH_PHRASES, *(f"concept:{c}" for c in CONCEPT_IDS)]
    return labels, {
        _job_key(label, a, b): label
        for label in labels
        for a in years for b in years
        if a is None or b is None or a <= b
    }


def phrase_yield() -> dict:
    """How many records each search phrase pulled from OpenAlex.

    Reconstructed from the cursor checkpoints in OA_CACHE_DIR, because
    candidates.csv has no phrase column. Counts are records FETCHED, before
    deduplication — a paper matching three phrases is counted three times, so
    they do not sum to the candidate total.

    Returns {"rows": [...], "total_fetched": int, "unattributed_files": int}.
    Every count is 0 when the cache directory is absent (e.g. on a deployment
    that ships only the CSVs), which is why the result is persisted to
    stats.json rather than computed per request.
    """
    labels, key_to_label = _job_key_index()
    totals = {label: 0 for label in labels}
    jobs   = {label: 0 for label in labels}
    unattributed = 0

    for path in OA_CACHE_DIR.glob("*.cursor.json"):
        label = key_to_label.get(path.name.replace(".cursor.json", ""))
        if label is None:
            unattributed += 1
            continue
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            continue
        totals[label] += int(state.get("total_fetched") or 0)
        jobs[label]   += 1

    rows = [
        {"phrase": label, "fetched": totals[label], "jobs": jobs[label],
         "source": "concept" if label.startswith("concept:") else "phrase"}
        for label in labels
    ]
    rows.sort(key=lambda r: -r["fetched"])
    return {"rows": rows,
            "total_fetched": sum(totals.values()),
            "unattributed_files": unattributed}


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
    tmp.replace(path)


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


def _build_ref(authors_r: "str | None", year_r: "int | None", journal_r: "str | None") -> str:
    """Build a FLoRA-style reference string: 'Surname · Year · Journal'.

    Uses only the last-name component of the first author. Returns a partial
    string (e.g. 'Smith · 2020') when journal is unavailable.
    """
    if not authors_r:
        surname = ""
    else:
        first_author = str(authors_r).split(";")[0].strip()
        parts = first_author.split()
        surname = parts[-1] if parts else ""
    segments = [s for s in [surname, str(year_r) if year_r else "", journal_r or ""] if s]
    return " · ".join(segments)


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
        try:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.warning("Corrupt cache file %s — deleting and re-fetching", cache_path.name)
            cache_path.unlink()

    headers: dict = {}
    if OPENALEX_API_KEY:
        headers["Authorization"] = f"Bearer {OPENALEX_API_KEY}"
    elif RESEARCHER_EMAIL:
        headers["User-Agent"] = f"mailto:{RESEARCHER_EMAIL}"

    for attempt in range(max_retries):
        try:
            resp = requests.get(_BASE_URL, params=params, headers=headers, timeout=30)
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
            if wait > 600:
                # Retry-After > 10 minutes means the daily quota is exhausted.
                # The cursor is already saved — the next run resumes from here.
                reset_str = str(datetime.timedelta(seconds=int(wait)))
                log.warning(
                    "OpenAlex daily quota exhausted (Retry-After=%s). "
                    "Stopping this phrase — cursor saved, next run resumes here.",
                    reset_str,
                )
                raise StopIteration("OpenAlex daily quota exhausted")
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
    journal = source.get("display_name")
    year    = work.get("publication_year")

    return {
        "doi_r":         clean_doi(work.get("doi") or ""),
        "title_r":       work.get("display_name") or work.get("title"),
        "abstract_r":    _reconstruct_abstract(work.get("abstract_inverted_index")),
        "year_r":        year,
        "authors_r":     authors,
        "journal_r":     journal,
        "url_r":         open_access.get("oa_url") or location.get("landing_page_url"),
        "openalex_id_r": work.get("id"),
        "source":        SOURCE_TAG,
        "ref_r":         _build_ref(authors, year, journal),
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

        try:
            data = _get_page(params)
        except StopIteration as exc:
            log.warning("  Stopping phrase=%r: %s (%d rows kept)", phrase, exc, len(rows))
            break

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
    if OPENALEX_API_KEY:
        log.info("OpenAlex: authenticated (Bearer token — keyed budget active)")
    else:
        log.info("OpenAlex: unauthenticated — add OPENALEX_API_KEY to .env for higher rate limits")

    all_rows: list[dict] = []

    for i, phrase in enumerate(SEARCH_PHRASES, 1):
        log.info("%d/%d  phrase=%r", i, len(SEARCH_PHRASES), phrase)
        all_rows.extend(
            fetch_phrase(phrase, from_year, to_year, max_records=max_records_per_phrase)
        )

    if not all_rows:
        return pd.DataFrame(columns=CANDIDATES_COLS)

    return pd.DataFrame(all_rows, columns=CANDIDATES_COLS)


# ---------------------------------------------------------------------------
# Concept-based search (Fix 2)
# ---------------------------------------------------------------------------


def fetch_concept(
    concept_id: str,
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    max_records: Optional[int] = None,
) -> list[dict]:
    """Fetch OpenAlex works tagged with *concept_id* with resumable cursor.

    Uses the same cursor-checkpoint pattern as ``fetch_phrase`` but filters by
    ``concepts.id`` instead of ``title_and_abstract.search``.  This catches
    papers classified as being about replication/reproducibility by OpenAlex's
    own ML even when the paper has no abstract stored or uses atypical wording.

    Parameters
    ----------
    concept_id : str
        OpenAlex concept ID, e.g. ``"C2911965"``.  The full URL form
        ``"https://openalex.org/C2911965"`` is also accepted.
    from_year, to_year : int, optional
        Publication year bounds (inclusive).
    max_records : int, optional
        Stop after this many rows for this call; cursor is saved at the page
        boundary so the next call continues from there.
    """
    # Normalise to bare ID so the cursor path is stable regardless of format.
    cid = concept_id.replace("https://openalex.org/", "").strip()
    cursor_path = _cursor_path(f"concept:{cid}", from_year, to_year)
    state = _load_cursor_state(cursor_path)

    if state["completed"]:
        log.info("OpenAlex concept=%r already fully fetched — skipping", cid)
        return []

    cursor = state["cursor"] or _CURSOR_START
    total_fetched = state["total_fetched"]
    rows: list[dict] = []

    yr_filt = _year_filter(from_year, to_year)
    base_filter = f"concepts.id:{cid}"
    oa_filter = f"{base_filter},{yr_filt}" if yr_filt else base_filter

    log.info(
        "OpenAlex concept=%r  years=%s–%s  prev_fetched=%d",
        cid, from_year or "any", to_year or "any", total_fetched,
    )

    while cursor:
        params = {
            "filter": oa_filter,
            "per-page": _PER_PAGE,
            "cursor": cursor,
            "mailto": RESEARCHER_EMAIL,
            "select": _SELECT,
        }
        _save_cursor_state(cursor_path, cursor, total_fetched, completed=False)

        try:
            data = _get_page(params)
        except StopIteration as exc:
            log.warning("  Stopping concept=%r: %s (%d rows kept)", cid, exc, len(rows))
            break

        results = data.get("results") or []
        if not results:
            cursor = None
            break

        for w in results:
            row = _extract_row(w)
            row["source"] = SOURCE_TAG_CONCEPT  # distinguish from phrase-search rows
            rows.append(row)
        total_fetched += len(results)

        next_cursor = (data.get("meta") or {}).get("next_cursor")
        api_total = data.get("meta", {}).get("count", "?")
        log.info(
            "  concept=%r  page_rows=%d  run_rows=%d  api_total=%s",
            cid, len(results), len(rows), api_total,
        )

        cursor = next_cursor
        _save_cursor_state(cursor_path, cursor, total_fetched, completed=(not cursor))

        if not cursor:
            log.info("  concept=%r fully exhausted", cid)
            break

        if max_records is not None and len(rows) >= max_records:
            log.info(
                "  concept=%r  reached max_records=%d — cursor saved at page boundary",
                cid, max_records,
            )
            break

        time.sleep(OPENALEX_RATE_SEC)

    log.info("Done — %d rows for concept=%r", len(rows), cid)
    return rows


def fetch_openalex_concept_candidates(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    max_records_per_concept: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch OpenAlex candidates across all ``CONCEPT_IDS``.

    Each concept is an independent resumable job.  Completed concepts are
    skipped automatically.
    """
    if OPENALEX_API_KEY:
        log.info("OpenAlex concept search: authenticated")
    else:
        log.info("OpenAlex concept search: unauthenticated")

    all_rows: list[dict] = []
    for i, cid in enumerate(CONCEPT_IDS, 1):
        log.info("%d/%d  concept=%r", i, len(CONCEPT_IDS), cid)
        all_rows.extend(fetch_concept(cid, from_year, to_year, max_records=max_records_per_concept))

    if not all_rows:
        return pd.DataFrame(columns=CANDIDATES_COLS)
    return pd.DataFrame(all_rows, columns=CANDIDATES_COLS)


def list_oa_concepts(query: str) -> list[dict]:
    """Query the OpenAlex concepts endpoint and return matches.

    Use this to find the correct IDs for ``CONCEPT_IDS``:
        python -m search.run_search --list-concepts "replication"

    Returns a list of dicts with keys: id, name, works_count.
    """
    headers: dict = {}
    if OPENALEX_API_KEY:
        headers["Authorization"] = f"Bearer {OPENALEX_API_KEY}"
    elif RESEARCHER_EMAIL:
        headers["User-Agent"] = f"mailto:{RESEARCHER_EMAIL}"

    resp = requests.get(
        "https://api.openalex.org/concepts",
        params={"search": query, "per-page": 15, "mailto": RESEARCHER_EMAIL},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return [
        {
            "id":         c.get("id", "").replace("https://openalex.org/", ""),
            "name":       c.get("display_name", ""),
            "works":      c.get("works_count", 0),
            "level":      c.get("level"),
        }
        for c in resp.json().get("results", [])
    ]
