"""
fetch_abstracts.py — Fetch missing abstracts for no-abstract rows in candidates.csv.

Strategy (waterfall by identifier type):

  1. OpenAlex batch   — rows with openalex_id_r (305K rows → ~6,100 batch calls).
                        Near-zero yield when the corpus was itself discovered via
                        OpenAlex (measured 2026-07-27: 0/200 random sample) — see
                        --skip-openalex.
  2. Europe PMC batch — rows with doi_r, 25 DOIs/call. No API key. Ordered ahead of
                        every other DOI phase: on 960 never-tried missing-abstract
                        DOIs sampled across this corpus's dominant prefixes
                        (2026-07-29) it recovered 47.7%, against Semantic Scholar's
                        8.5% and CrossRef's 0.3% on the same DOIs. The gap is
                        structural — 69% of this corpus's missing abstracts are
                        Elsevier (10.1016) and Springer (10.1007), neither of which
                        deposits abstracts to CrossRef, and OpenAlex's abstract index
                        derives from that same deposit stream. Europe PMC indexes the
                        publisher record instead, so it sees what they do not.
  3. Semantic Scholar batch — rows with doi_r, up to 500 DOIs/call (requires
                        S2_API_KEY in .env). Still worth running after Europe PMC:
                        the two are complementary, not nested — on the sample above
                        S2 added +10 Elsevier and +11 SSRN (10.2139) abstracts Europe
                        PMC missed entirely (SSRN: EPMC 2%, S2 10%). Measured on a
                        full production run over this corpus's entire 494,406-row S2
                        target list (2026-07-27/28), ~49.8 DOIs/sec sustained at a
                        14.5% hit rate (71,900 found), vs CrossRef's ~3/sec at ~0.6%
                        on the rows it was tried on (an earlier ~9,900-row sample had
                        suggested ~31%, which the full run showed was not
                        representative — hit rate varies a lot across the corpus).
  4. CrossRef by DOI  — fallback for rows Phases 2-3 didn't resolve (one DOI/call;
                        CrossRef has no equivalent batch-by-DOI-list endpoint)
  5. Scopus by DOI    — Elsevier Abstract Retrieval API fallback (requires
                        ELSEVIER_API_KEY; ~10k requests/week quota, so a run is
                        capped by --scopus-limit)

Rows whose DOI prefix registers datasets rather than articles (see
_DATASET_PREFIXES) are dropped from the worklist entirely — they have no abstract
to find, so every phase would spend calls confirming that forever.

Results are cached per identifier in cache/abstracts/ — the durable, crash-safe
store (paired with the checkpoint below). Memory is bounded: run() streams
candidates.csv in 50k-row chunks to build a compact worklist of only the
identifier fields for rows still missing an abstract (never the whole 4.7 GB
file), runs the fetch phases against the per-identifier cache, and writes the
recovered abstracts back with a final streamed merge into candidates.csv.tmp
that is atomically renamed. There is no in-memory full DataFrame and no periodic
full-file flush.

Checkpoint (cache/fetch_abstracts_done.txt): one identifier per line (oa:<id as
stored in CSV>, epmc:10.x/y, doi:10.x/y, s2:10.x/y, scopus:10.x/y). Each phase owns
its own namespace, so adding one never invalidates another's progress. On restart,
already-tried
identifiers are skipped — even those that returned no abstract, so we don't re-hit
the API for known misses.

OpenAlex miss recovery
----------------------
An earlier bug in the OpenAlex batch join keyed the result dict on the full URL
form of openalex_id_r ('https://openalex.org/W123') while the response was matched
on the bare id ('W123'), so no batch row ever matched. Every OpenAlex row was
therefore recorded as a miss and checkpointed as done, permanently suppressing
recovery. The join is now normalised to the bare id on both sides. To re-attempt
rows poisoned by the old bug, run with --retry-openalex-misses: it drops every
'oa:' checkpoint entry whose cached abstract is absent or '__none__' and clears
those poisoned cache files, so the fixed batch phase re-fetches them.

Usage
-----
    python -m search.fetch_abstracts                       # full run
    python -m search.fetch_abstracts --limit 1000          # first 1000 missing rows
    python -m search.fetch_abstracts --reset               # clear checkpoint, start fresh
    python -m search.fetch_abstracts --dry-run             # count missing rows, no API calls
    python -m search.fetch_abstracts --retry-openalex-misses  # re-attempt bug-poisoned OA rows
    python -m search.fetch_abstracts --scopus-limit 9000   # cap Scopus calls (weekly quota)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

from shared.config import (
    CACHE_DIR, CROSSREF_RATE_SEC, DATA_DIR, ELSEVIER_API_KEY, ELSEVIER_INSTTOKEN,
    EPMC_BATCH_SIZE, EPMC_RATE_SEC, OA_BATCH_SIZE, OPENALEX_RATE_SEC, RESEARCHER_EMAIL,
    S2_API_KEY, S2_BATCH_RATE_SEC, S2_BATCH_SIZE, S2_RATE_SEC, SCOPUS_DEFAULT_LIMIT,
    SCOPUS_RATE_SEC, log,
)
from shared.openalex_keys import headers as oa_headers, is_budget_refusal, rotate_key
from shared.utils import clean_doi, cache_key
from shared.dashboard_cache import _parquet_path, refresh as _dc_refresh

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

CANDIDATES_PATH    = DATA_DIR / "candidates.csv"
ABSTRACT_CACHE_DIR = CACHE_DIR / "abstracts"
CHECKPOINT_PATH    = CACHE_DIR / "fetch_abstracts_done.txt"
# Sidecar index of identifiers that resolved to a real abstract (mirrors the
# candidates_index.txt / filtered_index.txt pattern). Building a phase's target list
# means checking every row in the worklist (500k+) against results from earlier
# phases; once abstracts/ passed ~500k files, doing that via a handful of per-row
# file stats/reads (_lookup_cached_abstract) took ~2 hours — NTFS lookup cost in one
# huge flat directory, not disk speed. This index lets that check happen in memory.
FOUND_INDEX_PATH   = CACHE_DIR / "fetch_abstracts_found.txt"
ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# DOI prefixes belonging to data repositories, not journals. Their records are
# datasets, so there is no abstract anywhere to recover — 10.7910 (Harvard
# Dataverse) alone accounts for ~24k of this corpus's missing-abstract rows.
# Dropping them from the worklist stops every phase from spending calls to
# rediscover that, and keeps the "still missing" total meaningful.
_DATASET_PREFIXES = {"10.7910", "10.5281"}
_DOI_PREFIX_RE = re.compile(r"(10\.\d{4,9})/")

# Consecutive transient failures (429/5xx/network) that trip the circuit breaker
# and stop a phase gracefully — the host is throttling us; rerun to resume the
# un-checkpointed rows. Mirrors how the Scopus phase stops on quota exhaustion.
TRANSIENT_BREAKER_LIMIT = 25

# One session for every host, deliberately carrying NO Authorization header. The
# OpenAlex key must never leak to CrossRef, which rejects an unknown Bearer token
# with 401 and so loses the entire CrossRef abstract-recovery tier. The OpenAlex
# phase passes its credential per request instead (oa_headers()), which is also
# what lets it pick up a key rotated in by another phase or stage.
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": f"FLoRA-Extractor/1.0 (mailto:{RESEARCHER_EMAIL})"})


# ---------------------------------------------------------------------------
# Abstract cache helpers
# ---------------------------------------------------------------------------

def _cache_path(ident: str) -> Path:
    return ABSTRACT_CACHE_DIR / f"{cache_key(ident)}.json"


def _read_abstract_cache(ident: str) -> Optional[str]:
    p = _cache_path(ident)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("abstract")
        except Exception:
            return None
    return None


def _write_abstract_cache(ident: str, abstract: Optional[str]) -> None:
    _cache_path(ident).write_text(
        json.dumps({"ident": ident, "abstract": abstract}),
        encoding="utf-8",
    )
    if abstract and abstract != "__none__":
        _append_found_index(ident)


def _lookup_cached_abstract(oa_id: str, doi_r: str) -> Optional[str]:
    """Return the recovered abstract for a row from the per-identifier cache.

    Tries the same keys the phases write, in priority order — oa → epmc → doi →
    s2 → scopus — and returns the first non-`__none__`, non-None hit. The `__none__`
    sentinel means "tried, no abstract" and is treated as a miss. This is the
    single source of truth for both the phase "still-missing" checks and the
    final streamed write-back merge, so the write-back looks up exactly the keys
    the phases wrote. Returns None if no key yields an abstract.
    """
    doi = clean_doi(str(doi_r or ""))
    keys: list[str] = []
    if oa_id:
        keys.append(f"oa:{oa_id}")
    if doi:
        keys.extend([f"epmc:{doi}", f"doi:{doi}", f"s2:{doi}", f"scopus:{doi}"])
    for k in keys:
        val = _read_abstract_cache(k)
        if val is not None and val != "__none__":
            return val
    return None


def _append_found_index(ident: str) -> None:
    with open(FOUND_INDEX_PATH, "a", encoding="utf-8") as f:
        f.write(ident + "\n")


def _build_found_index() -> set[str]:
    """One-time migration: scan every cached-abstract file once and record which
    identifiers hold a real (non-`__none__`) abstract, writing FOUND_INDEX_PATH as
    we go. Every later run loads that small file instead of repeating this scan.
    """
    found: set[str] = set()
    if not ABSTRACT_CACHE_DIR.exists():
        return found
    n = 0
    with open(FOUND_INDEX_PATH, "w", encoding="utf-8") as f:
        for p in ABSTRACT_CACHE_DIR.glob("*.json"):
            n += 1
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            abstract = data.get("abstract")
            if abstract and abstract != "__none__":
                ident = data.get("ident", "")
                if ident:
                    found.add(ident)
                    f.write(ident + "\n")
            if n % 50000 == 0:
                log.info("  Building found-index: %d cache files scanned, %d hits.", n, len(found))
    log.info("Found-index built: %d hits from %d cache files.", len(found), n)
    return found


def _load_found_index() -> set[str]:
    if FOUND_INDEX_PATH.exists():
        return {l.strip() for l in FOUND_INDEX_PATH.read_text(encoding="utf-8").splitlines() if l.strip()}
    return _build_found_index()


def _already_resolved(oa_id: str, doi_r: str, found_index: set[str]) -> bool:
    """In-memory equivalent of `_lookup_cached_abstract(...) is not None`, backed by
    the found-index sidecar instead of per-row cache-file reads (see FOUND_INDEX_PATH).
    """
    doi = clean_doi(str(doi_r or ""))
    if oa_id and f"oa:{oa_id}" in found_index:
        return True
    return bool(doi) and any(
        f"{p}:{doi}" in found_index for p in ("epmc", "doi", "s2", "scopus"))


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> set[str]:
    if not CHECKPOINT_PATH.exists():
        return set()
    return {l.strip() for l in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines() if l.strip()}


def _append_checkpoint(ident: str) -> None:
    with open(CHECKPOINT_PATH, "a", encoding="utf-8") as f:
        f.write(ident + "\n")


# ---------------------------------------------------------------------------
# OpenAlex inverted-index decoder
# ---------------------------------------------------------------------------

def _reconstruct_abstract(inverted_index: Optional[dict]) -> Optional[str]:
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, pos_list in inverted_index.items():
        for pos in pos_list:
            positions[pos] = word
    return " ".join(positions[k] for k in sorted(positions)) if positions else None


# ---------------------------------------------------------------------------
# Source 1: OpenAlex batch (phase 1)
# ---------------------------------------------------------------------------

def _fetch_openalex_batch(oa_ids: list[str]) -> Optional[dict[str, Optional[str]]]:
    """Fetch abstracts for up to OA_BATCH_SIZE OpenAlex IDs in one call.

    *oa_ids* may be full URLs ('https://openalex.org/W123') or bare ids ('W123');
    the openalex_id_r column stores the URL form. The returned dict is keyed by the
    exact strings passed in, so the caller can join results back to its CSV values
    and cache keys. Both the query filter and the response are matched on the bare
    'W…' id — mismatching the two forms is what previously made every row a miss.

    Returns None on a whole-batch HTTP failure (exception / non-200) so the caller
    can decline to checkpoint any id in the batch — one transient batch failure must
    not poison up to OA_BATCH_SIZE rows as permanent misses. A successful batch that
    simply lacks a given id returns that id mapped to None (a definitive miss).
    """
    bare_to_input: dict[str, str] = {}
    for oid in oa_ids:
        bare = oid.replace("https://openalex.org/", "").strip()
        bare_to_input[bare] = oid
    pipe_ids = "|".join(bare_to_input.keys())
    url = (
        "https://api.openalex.org/works"
        f"?filter=ids.openalex:{pipe_ids}"
        "&select=id,abstract_inverted_index"
        f"&per-page={OA_BATCH_SIZE}"
    )
    result: dict[str, Optional[str]] = {oid: None for oid in oa_ids}
    while True:
        try:
            resp = _SESSION.get(url, headers=oa_headers(), timeout=30)
            # A budget refusal means this key is spent, not that the batch failed:
            # the next key can still serve it.
            if resp.status_code == 429 and is_budget_refusal(resp) and rotate_key():
                continue
            resp.raise_for_status()
            for work in resp.json().get("results", []):
                wid = work.get("id", "").replace("https://openalex.org/", "").strip()
                abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
                input_key = bare_to_input.get(wid)
                if input_key is not None:
                    result[input_key] = abstract
        except Exception as exc:
            log.warning("OpenAlex batch error (batch not checkpointed): %s", exc)
            return None
        return result


# ---------------------------------------------------------------------------
# Shared response helpers (used by every DOI-keyed source below)
# ---------------------------------------------------------------------------

# Publisher abstracts arrive wrapped in JATS markup on several of these APIs.
_JATS_RE = re.compile(r"<[^>]+>")


def _retry_after_seconds(resp) -> float:
    """Parse a Retry-After header (integer seconds) into a float; 0 if absent/unparseable."""
    val = resp.headers.get("Retry-After", "").strip()
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _request_with_retry(label: str, send, *, backoff=None, stop_on=None,
                        attempts: int = 3) -> "tuple[Optional[requests.Response], str]":
    """Send a request, retrying transient failures. Returns (response, "ok") or
    (None, "transient").

    Transient means a network exception, a 429, a 5xx, or a 2xx whose body is not
    JSON (seen live from CrossRef after ~12k good calls, and a real anomaly rather
    than a miss). Each is retried up to *attempts* times, waiting the longer of the
    server's Retry-After header and *backoff(attempt)* — 1s/2s/4s unless a caller
    passes its own schedule.

    Everything else, 4xx included, comes back as (response, "ok"): only the caller
    knows whether a 404 means "no abstract here" or "this request failed".
    *stop_on* is checked first and short-circuits the retries when a response
    already settles the matter (Scopus's spent-quota 429).

    (None, "transient") is the contract every phase must honour by NOT
    checkpointing the identifiers in this request. A throttled host must never
    turn a row into a permanent miss.
    """
    for attempt in range(attempts):
        try:
            resp = send()
        except Exception as exc:
            log.warning("%s network error (attempt %d/%d): %s", label, attempt + 1, attempts, exc)
            time.sleep(2 ** attempt)
            continue

        if stop_on is not None and stop_on(resp):
            return resp, "ok"

        if resp.status_code == 429 or resp.status_code >= 500:
            wait = backoff(attempt) if backoff else 2 ** attempt
            log.warning("%s HTTP %s (attempt %d/%d) — backing off.",
                        label, resp.status_code, attempt + 1, attempts)
            time.sleep(max(_retry_after_seconds(resp), wait))
            continue

        if resp.status_code < 400:
            try:
                resp.json()
            except ValueError:
                log.warning("%s returned a non-JSON body (attempt %d/%d) — backing off.",
                            label, attempt + 1, attempts)
                time.sleep(2 ** attempt)
                continue

        return resp, "ok"

    return None, "transient"


# ---------------------------------------------------------------------------
# Source 2: Europe PMC by DOI, batched (phase 2)
# ---------------------------------------------------------------------------

def _fetch_epmc_batch(dois: list[str]) -> Optional[dict[str, Optional[str]]]:
    """Fetch abstracts for up to EPMC_BATCH_SIZE DOIs in one Europe PMC search.

    Europe PMC has no id-list endpoint, so the batch is expressed as a boolean
    query: 'DOI:"a" OR DOI:"b" ...'. Results come back unordered and a DOI may
    match more than one record (a preprint and its published version), so the
    join is by DOI rather than by position — the first record carrying an
    abstract wins.

    resultType=core is REQUIRED: the lighter 'lite' view omits abstractText
    entirely, so with it every DOI would look like a miss.

    Returns None on a whole-batch failure (retried 3x with backoff, honouring
    Retry-After on 429) so the caller does not checkpoint any DOI in the batch —
    the same contract as _fetch_openalex_batch / _fetch_s2_batch. A DOI absent
    from a successful response is a definitive miss, mapped to None.
    """
    query = " OR ".join(f'DOI:"{d}"' for d in dois)
    params = {
        "query": query,
        "format": "json",
        "resultType": "core",
        # A DOI can match several records; ask for headroom so a duplicate
        # cannot push a distinct DOI's only record off the first page.
        "pageSize": min(len(dois) * 2, 100),
    }
    resp, status = _request_with_retry(
        "EuropePMC batch",
        lambda: _SESSION.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params=params, timeout=40,
        ),
        backoff=lambda attempt: EPMC_RATE_SEC * (attempt + 1),
    )
    if status == "transient":
        return None
    if resp.status_code >= 400:
        log.warning("EuropePMC batch error (batch not checkpointed): HTTP %d — %s",
                    resp.status_code, resp.text[:200])
        return None

    result: dict[str, Optional[str]] = {d: None for d in dois}
    for record in ((resp.json().get("resultList") or {}).get("result") or []):
        doi = str(record.get("doi") or "").strip().lower()
        abstract = record.get("abstractText") or None
        if doi in result and abstract and not result[doi]:
            result[doi] = _JATS_RE.sub("", abstract).strip() or None
    return result


# ---------------------------------------------------------------------------
# Source 4: CrossRef by DOI (phase 4)
# ---------------------------------------------------------------------------

def _fetch_crossref_abstract(doi: str) -> tuple[Optional[str], str]:
    """Fetch an abstract from CrossRef by DOI.

    Returns (abstract, status) where status is:
      "ok"        — an abstract was found
      "empty"     — HTTP 200/404 but no abstract (a DEFINITIVE miss to checkpoint)
      "transient" — 429/5xx/network failure that persisted through all retries
                    (must NOT be checkpointed, so a later run retries the DOI)

    The polite-pool ?mailto= param earns better rate limits. Transient failures
    retry 3× with 1s/2s/4s backoff, honouring a 429 Retry-After header when present.
    """
    url = f"https://api.crossref.org/works/{doi}?mailto={RESEARCHER_EMAIL}"
    resp, status = _request_with_retry(
        f"CrossRef {doi}", lambda: _SESSION.get(url, timeout=20))
    if status == "transient":
        return None, "transient"
    if resp.status_code >= 400:
        return None, "empty"
    raw = resp.json().get("message", {}).get("abstract", "")
    cleaned = _JATS_RE.sub("", raw).strip() if raw else ""
    return (cleaned, "ok") if cleaned else (None, "empty")


# ---------------------------------------------------------------------------
# Source 3: Semantic Scholar by DOI (phase 3)
# ---------------------------------------------------------------------------

def _fetch_s2_abstract(doi: str, s2_key: str) -> tuple[Optional[str], str]:
    """Fetch an abstract from Semantic Scholar by DOI.

    Returns (abstract, status) with the same contract as _fetch_crossref_abstract:
    "ok" / "empty" (definitive miss) / "transient" (429/5xx/network, retried 3×).
    A 429 was previously treated as a clean miss and checkpointed — that permanently
    suppressed the row. It is now transient so a later run retries it.
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
    headers = {"x-api-key": s2_key} if s2_key else {}
    resp, status = _request_with_retry(
        f"S2 {doi}", lambda: _SESSION.get(url, timeout=20, headers=headers))
    if status == "transient":
        return None, "transient"
    if resp.status_code >= 400:
        return None, "empty"
    abstract = resp.json().get("abstract") or None
    return (abstract, "ok") if abstract else (None, "empty")


def _fetch_s2_batch(dois: list[str], s2_key: str) -> Optional[dict[str, Optional[str]]]:
    """Fetch abstracts for up to S2_BATCH_SIZE DOIs in one call to S2's batch endpoint.

    The response is a JSON array in the SAME ORDER as the request's ids list, with
    null for any id S2 doesn't have — unlike OpenAlex's batch endpoint, no id-based
    join is needed, just zip(dois, response).

    Returns None on a whole-batch failure (retried 3x with backoff, honouring
    Retry-After on 429) so the caller does not checkpoint any id in the batch — one
    transient batch failure must not poison up to S2_BATCH_SIZE rows as permanent
    misses. A successful batch's null entry for a given DOI is a definitive miss.
    """
    url = "https://api.semanticscholar.org/graph/v1/paper/batch"
    headers = {"x-api-key": s2_key} if s2_key else {}
    payload = {"ids": [f"DOI:{d}" for d in dois]}
    resp, status = _request_with_retry(
        "S2 batch",
        lambda: _SESSION.post(url, params={"fields": "abstract"}, json=payload,
                              headers=headers, timeout=60),
        backoff=lambda attempt: S2_BATCH_RATE_SEC * (attempt + 1),
    )
    if status == "transient":
        return None
    if resp.status_code >= 400:
        log.warning("S2 batch error (batch not checkpointed): HTTP %d — %s",
                    resp.status_code, resp.text[:200])
        return None
    return {
        doi: ((entry or {}).get("abstract") or None)
        for doi, entry in zip(dois, resp.json())
    }


# ---------------------------------------------------------------------------
# Source 5: Elsevier Scopus by DOI (phase 5)
# ---------------------------------------------------------------------------

def _parse_scopus_abstract(payload: dict) -> Optional[str]:
    """Pull the abstract out of a Scopus Abstract Retrieval JSON response.

    Abstract text lives at abstracts-retrieval-response → coredata → dc:description.
    """
    coredata = (
        (payload or {})
        .get("abstracts-retrieval-response", {})
        .get("coredata", {})
    )
    desc = coredata.get("dc:description")
    if not desc:
        return None
    return _JATS_RE.sub("", str(desc)).strip() or None


def _fetch_scopus_abstract(doi: str, api_key: str) -> tuple[Optional[str], bool]:
    """Fetch an abstract from Elsevier Scopus by DOI.

    Returns (abstract_or_none, quota_exhausted). On a 429 whose
    X-RateLimit-Remaining header is "0" — or after 3 backed-off retries still
    hitting 429 — the ~10k/week quota is treated as spent and quota_exhausted is
    True so the caller stops the phase gracefully. Transient errors retry 3× with
    1s/2s/4s backoff per repo convention.
    """
    url = f"https://api.elsevier.com/content/abstract/doi/{doi}"
    headers = {"X-ELS-APIKey": api_key, "Accept": "application/json"}
    if ELSEVIER_INSTTOKEN:
        headers["X-ELS-Insttoken"] = ELSEVIER_INSTTOKEN
    # view=META_ABS is REQUIRED: the endpoint's default view (META) omits
    # dc:description entirely, so without this the tier returns HTTP 200 and no
    # abstract for every DOI — silently recovering nothing.
    params = {"view": "META_ABS"}

    def _quota_spent(resp) -> bool:
        return (resp.status_code == 429
                and resp.headers.get("X-RateLimit-Remaining", "").strip() == "0")

    resp, status = _request_with_retry(
        f"Scopus {doi}",
        lambda: _SESSION.get(url, timeout=20, headers=headers, params=params),
        stop_on=_quota_spent,
    )
    # Retries exhausted on 429/5xx — assume the weekly quota is gone rather than
    # keep spending calls to find out.
    if status == "transient" or _quota_spent(resp):
        return None, True
    if resp.status_code in (400, 404):
        return None, False
    if resp.status_code in (401, 403):
        # AUTHORIZATION_ERROR — the key is valid but not entitled to the abstract
        # view. Retrying cannot help, and it is NOT a spent quota.
        log.warning(
            "Scopus not entitled to the abstract view for %s (HTTP %d). "
            "Elsevier entitlement is IP-bound: run from the subscribing "
            "network/VPN, or set ELSEVIER_INSTTOKEN.", doi, resp.status_code)
        return None, False
    if resp.status_code >= 400:
        return None, True
    return _parse_scopus_abstract(resp.json()), False


# ---------------------------------------------------------------------------
# OpenAlex miss recovery (undo the pre-bugfix poisoned checkpoint/cache)
# ---------------------------------------------------------------------------

def _drop_openalex_misses() -> int:
    """Drop OpenAlex-phase miss entries from the checkpoint so they re-run.

    A miss is an 'oa:' checkpoint line whose cached abstract is absent or the
    '__none__' sentinel. Its poisoned cache file is deleted too, so the fixed
    batch phase actually re-fetches it instead of reading the stale miss. Rows
    that genuinely recovered an abstract are kept. Returns the number dropped.
    """
    if not CHECKPOINT_PATH.exists():
        return 0
    lines = [l.strip() for l in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if line.startswith("oa:"):
            val = _read_abstract_cache(line)   # checkpoint line == abstract-cache ident
            if val is None or val == "__none__":
                p = _cache_path(line)
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
                dropped += 1
                continue
        kept.append(line)
    CHECKPOINT_PATH.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return dropped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def enrich_abstracts(df: "pd.DataFrame") -> "pd.DataFrame":
    """Fill missing abstracts in *df* in-place using Europe PMC, then CrossRef, then S2.

    Called by run_search._merge_into_candidates_csv before writing new rows
    so every candidate arrives with the best available abstract.

    Europe PMC goes first for the same reason it is Phase 2 in run(): it is the only
    source that covers the Elsevier/Springer records the other two structurally lack,
    and it needs no API key. Rows are batched EPMC_BATCH_SIZE at a time before the
    per-row CrossRef/S2 loop, so a merge of new candidates costs a handful of calls
    rather than one per row.

    Modifies df in-place and returns it. Uses the same cache as the standalone
    fetch_abstracts run command, so results are shared across both code paths.
    """
    import pandas as pd

    s2_key = S2_API_KEY
    missing_mask = df["abstract_r"].fillna("").str.strip() == ""
    if not missing_mask.any():
        return df

    n_missing = missing_mask.sum()
    log.debug("enrich_abstracts: %d rows have no abstract — trying Europe PMC + CrossRef + S2",
              n_missing)

    n_found = 0

    def _fetchable_doi(doi_r) -> str:
        """Cleaned DOI for a row an abstract could actually exist for, else "".

        Both passes below must apply the dataset-prefix rule identically — filtering
        in only one of them silently sends dataset DOIs to the other.
        """
        doi = clean_doi(str(doi_r or ""))
        return "" if doi.split("/")[0] in _DATASET_PREFIXES else doi

    # Europe PMC pass — batched.
    epmc_rows: dict = {}
    for idx, row in df[missing_mask].iterrows():
        doi = _fetchable_doi(row.get("doi_r", ""))
        if doi:
            epmc_rows[idx] = doi

    pending = [d for d in dict.fromkeys(epmc_rows.values())
               if _read_abstract_cache(f"epmc:{d}") is None]
    for start in range(0, len(pending), EPMC_BATCH_SIZE):
        batch = pending[start : start + EPMC_BATCH_SIZE]
        time.sleep(EPMC_RATE_SEC)
        fetched = _fetch_epmc_batch(batch)
        if fetched is None:
            continue   # transient — leave uncached so a later run retries
        for doi, abstract in fetched.items():
            _write_abstract_cache(f"epmc:{doi}", abstract if abstract else "__none__")
    for idx, doi in epmc_rows.items():
        cached = _read_abstract_cache(f"epmc:{doi}")
        if cached and cached != "__none__":
            df.at[idx, "abstract_r"] = cached
            n_found += 1

    missing_mask = df["abstract_r"].fillna("").str.strip() == ""
    for idx, row in df[missing_mask].iterrows():
        doi = _fetchable_doi(row.get("doi_r", ""))
        if not doi:
            continue

        # CrossRef — only cache definitive results; a transient failure is left
        # uncached so a later run retries the DOI.
        cached = _read_abstract_cache(f"doi:{doi}")
        if cached is None:
            time.sleep(CROSSREF_RATE_SEC)
            cr_abstract, cr_status = _fetch_crossref_abstract(doi)
            if cr_status != "transient":
                _write_abstract_cache(f"doi:{doi}", cr_abstract if cr_abstract else "__none__")
            cached = cr_abstract if cr_abstract else "__none__"
        abstract = cached if cached and cached != "__none__" else None

        # S2 fallback
        if not abstract and s2_key:
            s2_cached = _read_abstract_cache(f"s2:{doi}")
            if s2_cached is None:
                time.sleep(S2_RATE_SEC)
                s2_abstract, s2_status = _fetch_s2_abstract(doi, s2_key)
                if s2_status != "transient":
                    _write_abstract_cache(f"s2:{doi}", s2_abstract if s2_abstract else "__none__")
                s2_cached = s2_abstract if s2_abstract else "__none__"
            abstract = s2_cached if s2_cached and s2_cached != "__none__" else None

        if abstract:
            df.at[idx, "abstract_r"] = abstract
            n_found += 1

    log.info("enrich_abstracts: recovered %d / %d missing abstracts", n_found, n_missing)
    return df


def _build_worklist(dry_run: bool, limit: Optional[int]):
    """Stream candidates to a compact worklist of rows still missing an abstract.

    Returns (worklist, total_missing, has_oa, has_doi, n_dataset). The worklist is a
    list of {"oa": stripped openalex_id_r, "doi_r": raw doi_r} dicts holding ONLY the
    two identifier fields the phases need — never the full 10-column rows — so ~536k
    missing rows stay well under a few hundred MB instead of the 4.7 GB whole-file
    load. Under dry_run the worklist is left empty (counts only). The Parquet mirror
    (already column-pruned and smaller) is a fast path; the CSV path MUST stream in
    50k-row chunks to bound memory.

    Rows whose DOI prefix is in _DATASET_PREFIXES are counted in total_missing (they
    really are missing an abstract) but excluded from the worklist and from
    has_oa/has_doi, which describe what is actually actionable.
    """
    import pandas as pd

    needed = ["abstract_r", "doi_r", "openalex_id_r"]
    pq_path = _parquet_path("candidates")
    if pq_path.exists():
        import pyarrow.parquet as pq
        log.info("Building worklist from Parquet (streamed in row-group batches): %s", pq_path)
        # A single pq.read_table(...).to_pandas() materialises every row at once — on
        # this repo's 2.5 GB / 2.58M-row mirror that is a multi-hundred-MB-to-GB spike
        # that OOM'd in practice (ArrowMemoryError), defeating the whole point of this
        # function per its own docstring. iter_batches() yields one row-group (~50k
        # rows here) at a time, so peak memory matches the CSV chunked path below.
        chunks = (
            batch.to_pandas()
            for batch in pq.ParquetFile(pq_path).iter_batches(batch_size=50_000, columns=needed)
        )
    else:
        log.info("Streaming candidates.csv (50k-row chunks) to build worklist...")
        chunks = pd.read_csv(
            CANDIDATES_PATH, dtype=str, encoding="utf-8-sig",
            low_memory=False, usecols=needed, chunksize=50_000,
        )

    worklist: list[dict[str, str]] = []
    total_missing = has_oa = has_doi = n_dataset = 0
    limit_reached = False
    for chunk in chunks:
        chunk = chunk.fillna("")
        m = chunk[chunk["abstract_r"].str.strip() == ""]
        if m.empty:
            continue
        total_missing += len(m)

        is_dataset = m["doi_r"].str.extract(_DOI_PREFIX_RE, expand=False).isin(_DATASET_PREFIXES)
        n_dataset += int(is_dataset.sum())
        m = m[~is_dataset]
        if m.empty:
            continue

        oa_col  = m["openalex_id_r"].str.strip()
        doi_col = m["doi_r"].str.strip()
        has_oa  += int((oa_col != "").sum())
        has_doi += int((doi_col != "").sum())
        if dry_run:
            continue
        for oa, doi_r in zip(oa_col.tolist(), m["doi_r"].tolist()):
            worklist.append({"oa": oa, "doi_r": doi_r})
            if limit and len(worklist) >= limit:
                limit_reached = True
                break
        if limit_reached:
            break

    return worklist, total_missing, has_oa, has_doi, n_dataset


def _merge_abstracts_into_csv():
    """Stream candidates.csv → candidates.csv.tmp, filling empty abstract_r cells
    from the per-identifier cache, then atomically replace the original.

    Reads in 50k-row chunks so the full 4.7 GB file is never held whole. For each
    row whose abstract_r is empty, the abstract is looked up in the cache
    (oa → doi → s2 → scopus priority). Column order and the original header are
    preserved; the header is written utf-8-sig (BOM) on the first chunk and each
    later chunk is appended utf-8 to avoid a mid-file BOM. Returns (filled,
    still_missing).
    """
    import pandas as pd

    tmp_path = CANDIDATES_PATH.parent / (CANDIDATES_PATH.name + ".tmp")
    filled = still_missing = 0
    first = True
    for chunk in pd.read_csv(
        CANDIDATES_PATH, dtype=str, encoding="utf-8-sig",
        low_memory=False, chunksize=50_000,
    ):
        chunk = chunk.fillna("")
        empty_mask = chunk["abstract_r"].str.strip() == ""
        for idx in chunk.index[empty_mask.values]:
            abstract = _lookup_cached_abstract(
                chunk.at[idx, "openalex_id_r"].strip(), chunk.at[idx, "doi_r"]
            )
            if abstract is not None:
                chunk.at[idx, "abstract_r"] = abstract
                filled += 1
            else:
                still_missing += 1
        chunk.to_csv(
            tmp_path, index=False,
            encoding="utf-8-sig" if first else "utf-8",
            header=first, mode="w" if first else "a",
        )
        first = False

    os.replace(tmp_path, CANDIDATES_PATH)
    return filled, still_missing


def _load_scopus_priority(path: Path) -> dict[str, int]:
    """Map cleaned DOI → rank (file line order) from a priority-DOI file.

    Lines are one DOI each; earlier lines get the Scopus quota first. Blank
    lines and '#' comments are skipped.
    """
    ranks: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        doi = clean_doi(line)
        if doi and doi not in ranks:
            ranks[doi] = len(ranks)
    return ranks


def run(dry_run: bool = False, limit: Optional[int] = None,
        scopus_limit: int = SCOPUS_DEFAULT_LIMIT,
        scopus_priority: Optional[Path] = None,
        skip_openalex: bool = False) -> None:

    if not CANDIDATES_PATH.exists():
        sys.exit(f"ERROR: {CANDIDATES_PATH} not found.")

    s2_key = S2_API_KEY
    elsevier_key = ELSEVIER_API_KEY

    # ------------------------------------------------------------------
    # Build the worklist by streaming — never load the whole file (issue #65)
    # ------------------------------------------------------------------
    worklist, total_missing, has_oa, has_doi, n_dataset = _build_worklist(dry_run, limit)
    log.info("Rows missing abstract: %d", total_missing)
    if n_dataset:
        log.info("  dataset DOIs (%s): %d  — no abstract exists, excluded from every phase",
                 "/".join(sorted(_DATASET_PREFIXES)), n_dataset)

    if dry_run:
        log.info("  with openalex_id_r: %d  (OpenAlex batch)", has_oa)
        log.info("  with doi_r:         %d  (Europe PMC / S2 / CrossRef / Scopus)", has_doi)
        log.info("  no openalex_id_r:   %d", total_missing - n_dataset - has_oa)
        log.info("DRY RUN — no API calls. Re-run without --dry-run to fetch.")
        return

    if total_missing == 0:
        log.info("No rows missing an abstract — nothing to do.")
        return

    # ------------------------------------------------------------------
    # Checkpoint — skip already-tried identifiers
    # ------------------------------------------------------------------
    done = _load_checkpoint()
    if done:
        log.info("Checkpoint: %d identifiers already tried — skipping.", len(done))
    found_index = _load_found_index()
    if limit:
        log.info("--limit %d: processing first %d missing rows.", limit, len(worklist))

    # Recovered abstracts land in the per-identifier cache (the durable store);
    # the streamed write-back merge below reads them back into candidates.csv.
    # No in-memory full DataFrame, no periodic full-file flushes.
    n_found = 0

    # ------------------------------------------------------------------
    # Phase 1: OpenAlex batch (rows with openalex_id_r)
    # ------------------------------------------------------------------
    if skip_openalex:
        # Rows found here were themselves DISCOVERED via OpenAlex (source =
        # openalex/openalex_concept), so if OpenAlex never had an abstract at harvest
        # time it essentially never gains one later — re-asking is asking the same well
        # twice. Measured 2026-07-27: 0/200 random sample, 0/185,000 in a live run.
        # --skip-openalex jumps straight to CrossRef/S2/Scopus, independent sources
        # that can have text OpenAlex lacks. Nothing is stranded by skipping: Phase 2's
        # target list is every row with a doi_r regardless of Phase 1's outcome (it
        # checkpoints under a separate oa: namespace) — only oa-only, DOI-less rows
        # (already near-0% recoverable here) go untried, and a later plain run still
        # picks up exactly where Phase 1's checkpoint left off.
        log.info("Phase 1 — OpenAlex batch: skipped (--skip-openalex).")
    else:
        oa_ids = [r["oa"] for r in worklist if r["oa"] and f"oa:{r['oa']}" not in done]
        log.info("Phase 1 — OpenAlex batch: %d rows to try.", len(oa_ids))

        for batch_start in range(0, len(oa_ids), OA_BATCH_SIZE):
            batch_ids = oa_ids[batch_start : batch_start + OA_BATCH_SIZE]

            # Check cache first — skip call if all cached
            results: dict[str, Optional[str]] = {}
            uncached_ids: list[str] = []
            for oid in batch_ids:
                cached = _read_abstract_cache(f"oa:{oid}")
                if cached is not None:
                    results[oid] = cached if cached != "__none__" else None
                else:
                    uncached_ids.append(oid)

            # A whole-batch HTTP failure returns None: no id in the batch is a
            # definitive miss, so leave the uncached ids un-cached and un-checkpointed
            # for a later run to retry. A successful batch missing a specific id IS a
            # definitive miss for that id (cached + checkpointed below).
            batch_transient = False
            if uncached_ids:
                time.sleep(OPENALEX_RATE_SEC)
                fetched = _fetch_openalex_batch(uncached_ids)
                if fetched is None:
                    batch_transient = True
                    log.warning("Phase 1 — OpenAlex batch failed; %d ids left for retry.",
                                len(uncached_ids))
                else:
                    for oid, abstract in fetched.items():
                        _write_abstract_cache(f"oa:{oid}", abstract if abstract else "__none__")
                        results[oid] = abstract

            # Checkpoint each resolved id. On a transient batch, skip the ids that were
            # not resolved (no cache, no checkpoint) so they retry next run.
            for oid in batch_ids:
                if batch_transient and oid in uncached_ids:
                    continue
                _append_checkpoint(f"oa:{oid}")
                if results.get(oid):
                    n_found += 1
                    found_index.add(f"oa:{oid}")

            done_so_far = batch_start + len(batch_ids)
            if done_so_far % 5000 == 0:
                log.info("  OpenAlex progress: %d / %d  (found: %d)", done_so_far, len(oa_ids), n_found)

        log.info("Phase 1 complete. Abstracts found: %d", n_found)

    # ------------------------------------------------------------------
    # Phase 2: Europe PMC, BATCHED (rows still missing after Phase 1)
    # ------------------------------------------------------------------
    # First among the DOI phases, and unconditional — it needs no API key. On 960
    # never-tried missing-abstract DOIs (2026-07-29) it recovered 47.7% where S2 got
    # 8.5% and CrossRef 0.3%, because this corpus's gap is dominated by Elsevier and
    # Springer, who do not deposit abstracts to CrossRef at all. Running it first
    # leaves the keyed, slower, quota-bound phases a much smaller residual.
    epmc_dois = [
        clean_doi(str(r["doi_r"] or "")) for r in worklist
        if clean_doi(str(r["doi_r"] or ""))
        and f"epmc:{clean_doi(r['doi_r'])}" not in done
        and not _already_resolved(r["oa"], r["doi_r"], found_index)
    ]
    log.info("Phase 2 — Europe PMC batch: %d rows to try.", len(epmc_dois))

    phase2_found = 0
    consecutive_transient = 0
    for batch_start in range(0, len(epmc_dois), EPMC_BATCH_SIZE):
        batch_dois = epmc_dois[batch_start : batch_start + EPMC_BATCH_SIZE]

        results: dict[str, Optional[str]] = {}
        uncached_dois: list[str] = []
        for doi in batch_dois:
            cached = _read_abstract_cache(f"epmc:{doi}")
            if cached is not None:
                results[doi] = cached if cached != "__none__" else None
            else:
                uncached_dois.append(doi)

        # Same whole-batch-failure contract as Phases 1 and 3: a failed batch leaves
        # its DOIs un-cached/un-checkpointed for a later run; a DOI absent from a
        # successful response is a definitive miss (cached + checkpointed).
        batch_transient = False
        if uncached_dois:
            time.sleep(EPMC_RATE_SEC)
            fetched = _fetch_epmc_batch(uncached_dois)
            if fetched is None:
                batch_transient = True
                consecutive_transient += 1
                log.warning("Phase 2 — Europe PMC batch failed; %d DOIs left for retry.",
                            len(uncached_dois))
                if consecutive_transient >= TRANSIENT_BREAKER_LIMIT:
                    log.warning("Europe PMC throttling — stopping phase; rerun to resume. "
                                "(%d consecutive transient batches)", consecutive_transient)
                    break
            else:
                consecutive_transient = 0
                for doi, abstract in fetched.items():
                    _write_abstract_cache(f"epmc:{doi}", abstract if abstract else "__none__")
                    results[doi] = abstract

        for doi in batch_dois:
            if batch_transient and doi in uncached_dois:
                continue
            _append_checkpoint(f"epmc:{doi}")
            if results.get(doi):
                phase2_found += 1
                n_found += 1
                found_index.add(f"epmc:{doi}")

        done_so_far = batch_start + len(batch_dois)
        if done_so_far % 5000 < EPMC_BATCH_SIZE:
            log.info("  Europe PMC progress: %d / %d  (found: %d)",
                     done_so_far, len(epmc_dois), phase2_found)

    log.info("Phase 2 complete. Abstracts found: %d", phase2_found)

    # ------------------------------------------------------------------
    # Phase 3: Semantic Scholar, BATCHED (rows still missing after Phase 2)
    # ------------------------------------------------------------------
    # Ordered ahead of CrossRef: a full production run (2026-07-27/28, 494,406 rows)
    # measured S2's batch endpoint at ~49.8 DOIs/sec sustained, 14.5% hit rate, vs
    # CrossRef's one-DOI-at-a-time ~3/sec at ~0.6% on the same corpus. Running S2
    # first clears most of the corpus in a fraction of the time, leaving CrossRef
    # (Phase 4) a much smaller residual to pick over. It stays worth running after
    # Europe PMC because the two overlap only partly — S2 is the only source that
    # sees SSRN (10.2139), which Europe PMC does not index.
    if not s2_key:
        log.info("Phase 3 — S2: skipped (S2_API_KEY not set in .env).")
    else:
        s2_dois = [
            clean_doi(str(r["doi_r"] or "")) for r in worklist
            if clean_doi(str(r["doi_r"] or ""))
            and f"s2:{clean_doi(r['doi_r'])}" not in done
            and not _already_resolved(r["oa"], r["doi_r"], found_index)
        ]
        log.info("Phase 3 — Semantic Scholar batch: %d rows to try.", len(s2_dois))

        phase3_found = 0
        consecutive_transient = 0
        for batch_start in range(0, len(s2_dois), S2_BATCH_SIZE):
            batch_dois = s2_dois[batch_start : batch_start + S2_BATCH_SIZE]

            results: dict[str, Optional[str]] = {}
            uncached_dois: list[str] = []
            for doi in batch_dois:
                cached = _read_abstract_cache(f"s2:{doi}")
                if cached is not None:
                    results[doi] = cached if cached != "__none__" else None
                else:
                    uncached_dois.append(doi)

            # Same whole-batch-failure contract as Phase 1: a failed batch leaves its
            # ids un-cached/un-checkpointed for a later run; a successful batch's
            # null entry for a DOI is a definitive miss (cached + checkpointed).
            batch_transient = False
            if uncached_dois:
                time.sleep(S2_BATCH_RATE_SEC)
                fetched = _fetch_s2_batch(uncached_dois, s2_key)
                if fetched is None:
                    batch_transient = True
                    consecutive_transient += 1
                    log.warning("Phase 3 — S2 batch failed; %d DOIs left for retry.",
                                len(uncached_dois))
                    if consecutive_transient >= TRANSIENT_BREAKER_LIMIT:
                        log.warning("Semantic Scholar throttling — stopping phase; "
                                    "rerun to resume. (%d consecutive transient batches)",
                                    consecutive_transient)
                        break
                else:
                    consecutive_transient = 0
                    for doi, abstract in fetched.items():
                        _write_abstract_cache(f"s2:{doi}", abstract if abstract else "__none__")
                        results[doi] = abstract

            for doi in batch_dois:
                if batch_transient and doi in uncached_dois:
                    continue
                _append_checkpoint(f"s2:{doi}")
                if results.get(doi):
                    phase3_found += 1
                    n_found += 1
                    found_index.add(f"s2:{doi}")

            done_so_far = batch_start + len(batch_dois)
            if done_so_far % 5000 < S2_BATCH_SIZE:
                log.info("  S2 progress: %d / %d  (found: %d)",
                         done_so_far, len(s2_dois), phase3_found)

        log.info("Phase 3 complete. Abstracts found: %d", phase3_found)

    # ------------------------------------------------------------------
    # Phase 4: CrossRef by DOI (fallback for rows Phases 2-3 didn't resolve)
    # ------------------------------------------------------------------
    crossref_targets = [
        r["doi_r"] for r in worklist
        if clean_doi(str(r["doi_r"] or ""))
        and f"doi:{clean_doi(r['doi_r'])}" not in done
        and not _already_resolved(r["oa"], r["doi_r"], found_index)
    ]
    log.info("Phase 4 — CrossRef: %d rows to try.", len(crossref_targets))

    phase4_found = 0
    consecutive_transient = 0
    for i, doi_r in enumerate(crossref_targets, 1):
        doi = clean_doi(str(doi_r or ""))
        if not doi:
            continue

        cached = _read_abstract_cache(f"doi:{doi}")
        if cached is not None:
            abstract = cached if cached != "__none__" else None
        else:
            time.sleep(CROSSREF_RATE_SEC)
            abstract, status = _fetch_crossref_abstract(doi)
            if status == "transient":
                # Do NOT cache or checkpoint — a later run must retry this DOI.
                consecutive_transient += 1
                log.warning("CrossRef transient failure for %s (not checkpointed).", doi)
                if consecutive_transient >= TRANSIENT_BREAKER_LIMIT:
                    log.warning("CrossRef throttling — stopping phase; rerun to resume. "
                                "(%d consecutive transient failures)", consecutive_transient)
                    break
                continue
            _write_abstract_cache(f"doi:{doi}", abstract if abstract else "__none__")

        consecutive_transient = 0
        _append_checkpoint(f"doi:{doi}")

        if abstract:
            phase4_found += 1
            n_found += 1
            found_index.add(f"doi:{doi}")

        if i % 2000 == 0:
            log.info("  CrossRef progress: %d / %d  (found: %d)", i, len(crossref_targets), phase4_found)

    log.info("Phase 4 complete. Abstracts found: %d", phase4_found)

    # ------------------------------------------------------------------
    # Phase 5: Elsevier Scopus (fallback for rows still missing a DOI abstract)
    # ------------------------------------------------------------------
    if not elsevier_key:
        log.info("Phase 5 — Scopus: skipped (ELSEVIER_API_KEY not set in .env).")
    else:
        scopus_targets = [
            r["doi_r"] for r in worklist
            if clean_doi(str(r["doi_r"] or ""))
            and f"scopus:{clean_doi(r['doi_r'])}" not in done
            and not _already_resolved(r["oa"], r["doi_r"], found_index)
        ]
        if scopus_priority is not None:
            # The weekly quota (~10k) is far smaller than the missing-abstract pool,
            # so which rows get it matters. DOIs in the priority file (in file order)
            # are tried first; everything else keeps worklist order after them
            # (list.sort is stable, so equal-rank rows are not reshuffled).
            ranks = _load_scopus_priority(scopus_priority)
            scopus_targets.sort(key=lambda d: ranks.get(clean_doi(str(d)), len(ranks)))
            log.info("Phase 5 — Scopus priority: %d DOIs in %s, %d matched in queue.",
                     len(ranks), scopus_priority,
                     sum(1 for d in scopus_targets if clean_doi(str(d)) in ranks))
        if scopus_limit and scopus_limit > 0:
            scopus_targets = scopus_targets[:scopus_limit]
        log.info("Phase 5 — Scopus: %d rows to try (weekly-quota cap: %s).",
                 len(scopus_targets), scopus_limit)

        phase5_found = 0
        for i, doi_r in enumerate(scopus_targets, 1):
            doi = clean_doi(str(doi_r or ""))
            if not doi:
                continue

            cached = _read_abstract_cache(f"scopus:{doi}")
            if cached is not None:
                abstract = cached if cached != "__none__" else None
            else:
                time.sleep(SCOPUS_RATE_SEC)
                abstract, quota_exhausted = _fetch_scopus_abstract(doi, elsevier_key)
                if quota_exhausted:
                    log.warning("Phase 5 — Scopus quota exhausted; stopping phase "
                                "(%d rows done, %d found).", i - 1, phase5_found)
                    break
                _write_abstract_cache(f"scopus:{doi}", abstract if abstract else "__none__")

            _append_checkpoint(f"scopus:{doi}")

            if abstract:
                phase5_found += 1
                n_found += 1

            if i % 500 == 0:
                log.info("  Scopus progress: %d / %d  (found: %d)", i, len(scopus_targets), phase5_found)

        log.info("Phase 5 complete. Abstracts found: %d", phase5_found)

    # ------------------------------------------------------------------
    # Final write-back: streamed merge cache → candidates.csv, then Parquet mirror
    # ------------------------------------------------------------------
    filled, still_missing_final = _merge_abstracts_into_csv()
    _dc_refresh("candidates")

    log.info("=" * 60)
    log.info("FETCH ABSTRACTS COMPLETE")
    log.info("=" * 60)
    log.info("Abstracts recovered:  %d", n_found)
    log.info("Rows filled from cache: %d", filled)
    log.info("Still missing:        %d", still_missing_final)
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch missing abstracts for candidates.csv. Resumable."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Count missing rows by identifier type — no API calls.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process only the first N missing rows (testing).")
    parser.add_argument("--reset", action="store_true",
                        help="Clear the checkpoint and start fresh.")
    parser.add_argument("--retry-openalex-misses", action="store_true",
                        help="Drop OpenAlex-phase miss entries (and their poisoned "
                             "cache files) from the checkpoint so the fixed batch "
                             "phase re-attempts them, then continue the run.")
    parser.add_argument("--scopus-limit", type=int, default=SCOPUS_DEFAULT_LIMIT, metavar="N",
                        help="Max Scopus calls this run (weekly quota ~10k; "
                             f"default {SCOPUS_DEFAULT_LIMIT}). 0 disables the cap.")
    parser.add_argument("--scopus-priority", type=Path, default=None, metavar="FILE",
                        help="File of DOIs (one per line, priority order) tried first "
                             "in the Scopus phase, so the weekly quota goes to the rows "
                             "that matter most.")
    parser.add_argument("--skip-openalex", action="store_true",
                        help="Skip Phase 1 (OpenAlex batch) and go straight to "
                             "Europe PMC/S2/CrossRef/Scopus. Rows missing an abstract were "
                             "themselves discovered via OpenAlex, so Phase 1 has near-0%% "
                             "yield on this corpus (measured) — use this to spend time on "
                             "the phases that actually find new abstracts. Safe: a later "
                             "run without this flag resumes Phase 1 from its checkpoint.")
    args = parser.parse_args()

    if args.reset:
        if CHECKPOINT_PATH.exists():
            CHECKPOINT_PATH.unlink()
            print(f"Checkpoint cleared: {CHECKPOINT_PATH}")
        sys.exit(0)

    if args.retry_openalex_misses:
        n = _drop_openalex_misses()
        print(f"Dropped {n} OpenAlex miss entries from checkpoint — they will be retried.")

    run(dry_run=args.dry_run, limit=args.limit, scopus_limit=args.scopus_limit,
        scopus_priority=args.scopus_priority, skip_openalex=args.skip_openalex)
