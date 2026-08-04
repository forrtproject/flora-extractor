"""
fetch_abstracts.py — The six abstract sources, their order, and the contract
that keeps a transient failure from being recorded as a definitive miss.

This module is a library of phase runners, not a command. Its one consumer is
`filter.engine.backfill`, which supplies the worklist (from the routing table)
and decides where the recovered text lands (an overlay chunk). Everything about
HOW an abstract is fetched lives here so there is exactly one copy of it.

Waterfall by identifier type, in the order a run should spend calls:

  1. OpenAlex batch   — rows with an OpenAlex id, OA_BATCH_SIZE ids per call.
                        Near-zero yield when the corpus was itself discovered via
                        OpenAlex (measured 2026-07-27: 0/200 random sample).
  2. Europe PMC batch — rows with a DOI, 25 DOIs/call. No API key. Ordered ahead of
                        every other DOI phase: on 960 never-tried missing-abstract
                        DOIs sampled across this corpus's dominant prefixes
                        (2026-07-29) it recovered 47.7%, against Semantic Scholar's
                        8.5% and CrossRef's 0.3% on the same DOIs. The gap is
                        structural — 69% of this corpus's missing abstracts are
                        Elsevier (10.1016) and Springer (10.1007), neither of which
                        deposits abstracts to CrossRef, and OpenAlex's abstract index
                        derives from that same deposit stream. Europe PMC indexes the
                        publisher record instead, so it sees what they do not.
  3. Semantic Scholar batch — rows with a DOI, up to 500 DOIs/call (requires
                        S2_API_KEY in .env). Still worth running after Europe PMC:
                        the two are complementary, not nested — on the sample above
                        S2 added +10 Elsevier and +11 SSRN (10.2139) abstracts Europe
                        PMC missed entirely (SSRN: EPMC 2%, S2 10%). Measured over a
                        494,406-row target list (2026-07-27/28), ~49.8 DOIs/sec
                        sustained at a 14.5% hit rate, vs CrossRef's ~3/sec at ~0.6%.
  4. CrossRef by DOI  — fallback for rows Phases 2-3 didn't resolve (one DOI/call;
                        CrossRef has no equivalent batch-by-DOI-list endpoint)
  5. Scopus by DOI    — Elsevier Abstract Retrieval API fallback (requires
                        ELSEVIER_API_KEY; ~10k requests/week quota, so a caller
                        should cap its Scopus phase)
  6. OSF registrations — rows on the OSF registrant (10.17605) ONLY, one call per
                        DOI, keyless. Not an abstract source in the ordinary
                        sense: these records HAVE no abstract, they have a
                        registration template and a responses form, and the
                        template is what says whether the record reports a
                        completed replication or announces a planned one. It runs
                        FIRST because it is free, because nothing else holds text
                        for these rows, and because its text must be the text the
                        overlay records — the template name leads it, and two
                        specs in `filter/spec/` read that first line.

Rows whose DOI prefix registers datasets rather than articles (_DATASET_PREFIXES)
should be dropped from a worklist entirely — they have no abstract to find, so
every phase would spend calls confirming that forever.

Results are cached per identifier in cache/abstracts/ — the durable, crash-safe
store, paired with the checkpoint below. The two are shared across callers on
purpose: a DOI one run already asked Europe PMC about is answered for free next
time, and a miss recorded once is a miss nobody re-buys.

Checkpoint (cache/fetch_abstracts_done.txt): one identifier per line (oa:<id>,
epmc:10.x/y, doi:10.x/y, s2:10.x/y, scopus:10.x/y). Each phase owns its own
namespace, so adding one never invalidates another's progress. On restart,
already-tried identifiers are skipped — even those that returned no abstract, so
we don't re-hit the API for known misses. A TRANSIENT failure is never
checkpointed: only a definitive answer (text, or a confirmed absence) is.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import requests

from shared.config import (
    CACHE_DIR, ELSEVIER_INSTTOKEN, EPMC_BATCH_SIZE, EPMC_RATE_SEC, OA_BATCH_SIZE,
    OSF_TOKEN, RESEARCHER_EMAIL, S2_BATCH_RATE_SEC, S2_BATCH_SIZE, log,
)
from shared.openalex_keys import headers as oa_headers, is_budget_refusal, rotate_key
from shared.utils import clean_doi, cache_key, reconstruct_abstract

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

ABSTRACT_CACHE_DIR = CACHE_DIR / "abstracts"
CHECKPOINT_PATH    = CACHE_DIR / "fetch_abstracts_done.txt"
# Sidecar index of identifiers that resolved to a real abstract (mirrors the
# candidates_index.txt / filtered_index.txt pattern). Building a phase's target list
# means checking every row in the worklist (500k+) against results from earlier
# phases; once abstracts/ passed ~500k files, doing that via a handful of per-row
# file stats/reads took ~2 hours — NTFS lookup cost in one
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
    """True when some earlier phase already recovered an abstract for this row.

    Backed by the found-index sidecar rather than per-row cache-file reads (see
    FOUND_INDEX_PATH): the same key order the phases write under, checked in memory.
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
                abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
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
# Source 6: OSF registrations by DOI (registrant 10.17605 only)
# ---------------------------------------------------------------------------

# The registrant OSF mints its registration and project DOIs on.
OSF_REGISTRANT = "10.17605"
# The template name goes at the START of the recovered text, because that is
# what decides the record: `filter/spec/osf-registration-completed.json` and its
# discard twin match this exact prefix and read the template off the first line.
# Changing this string is a spec change, not a formatting change.
OSF_TEMPLATE_PREFIX = "OSF registration template: "
# The named-but-unset case: a registration whose template field is blank still
# gets a template line, so the two OSF specs partition every recovered row.
OSF_TEMPLATE_UNSPECIFIED = "unspecified"

_OSF_API = "https://api.osf.io/v2/registrations/{guid}/"


def _osf_guid(doi: str) -> Optional[str]:
    """The OSF GUID in *doi* (`10.17605/OSF.IO/AB12D` → `ab12d`), or None."""
    parts = clean_doi(doi).split("/")
    return parts[-1].lower() if len(parts) >= 2 and parts[-1] else None


def _osf_registration_text(attributes: dict) -> str:
    """The registration's text, template name first.

    OSF leaves `description` empty on most registrations and keeps the substance
    in `registration_responses` — a question-key → answer map holding a median
    5,268 characters (measured 2026-08-04 over 34 sampled registrations). Both
    are joined into one abstract-shaped string so a screen can read the record
    the way it reads any other row.
    """
    template = (attributes.get("registration_supplement")
                or OSF_TEMPLATE_UNSPECIFIED).strip()
    blocks = [OSF_TEMPLATE_PREFIX + template]
    description = (attributes.get("description") or "").strip()
    if description:
        blocks.append(description)
    for question, answer in (attributes.get("registration_responses") or {}).items():
        if isinstance(answer, (list, tuple)):
            answer = "; ".join(str(a) for a in answer if a)
        text = str(answer or "").strip()
        if text:
            blocks.append(f"{question}: {text}")
    return "\n\n".join(blocks)


def _fetch_osf_registration(doi: str) -> tuple[Optional[str], str]:
    """Fetch an OSF registration's template and responses by DOI.

    Same (abstract, status) contract as the other per-item sources. A 404 is a
    definitive `empty`: the DOI is an OSF project or component rather than a
    registration, and no later run should re-buy that answer.
    """
    guid = _osf_guid(doi)
    if not guid:
        return None, "empty"
    headers = {"Authorization": f"Bearer {OSF_TOKEN}"} if OSF_TOKEN else {}
    resp, status = _request_with_retry(
        f"OSF {doi}",
        lambda: _SESSION.get(_OSF_API.format(guid=guid), timeout=30, headers=headers))
    if status == "transient":
        return None, "transient"
    if resp.status_code >= 400:
        return None, "empty"
    attributes = (resp.json().get("data") or {}).get("attributes") or {}
    if not attributes:
        return None, "empty"
    return _osf_registration_text(attributes), "ok"


# ---------------------------------------------------------------------------
# Phase runners — one contract for all six sources
# ---------------------------------------------------------------------------

def _phase_targets(worklist: list[dict], namespace: str, done: set[str],
                   found_index: set[str]) -> list[str]:
    """Cleaned DOIs a DOI-keyed phase still has to try.

    A DOI drops out if this phase has already tried it (its own checkpoint
    namespace) or if any earlier phase already recovered an abstract for the row.
    """
    targets: list[str] = []
    for r in worklist:
        doi = clean_doi(str(r["doi_r"] or ""))
        if not doi or f"{namespace}:{doi}" in done:
            continue
        if _already_resolved(r["oa"], r["doi_r"], found_index):
            continue
        targets.append(doi)
    return targets


def _run_batch_phase(label: str, namespace: str, ids: list[str], batch_size: int,
                     rate_sec: float, fetch, found_index: set[str]) -> int:
    """Run one batched phase over *ids*; return how many abstracts it recovered.

    *fetch* takes the uncached ids of one batch and returns {id: abstract | None},
    or None for a whole-batch failure. That distinction is the contract: a failed
    batch leaves its ids un-cached and un-checkpointed so a later run retries them,
    while an id absent from a SUCCESSFUL response is a definitive miss, cached as
    ``__none__`` and checkpointed so no later run pays for it again.
    TRANSIENT_BREAKER_LIMIT consecutive whole-batch failures stop the phase — the
    host is throttling us, and a rerun resumes from the checkpoint.
    """
    log.info("%s: %d rows to try.", label, len(ids))
    found = 0
    consecutive_transient = 0

    for batch_start in range(0, len(ids), batch_size):
        batch = ids[batch_start : batch_start + batch_size]

        results: dict[str, Optional[str]] = {}
        uncached: list[str] = []
        for ident in batch:
            cached = _read_abstract_cache(f"{namespace}:{ident}")
            if cached is not None:
                results[ident] = cached if cached != "__none__" else None
            else:
                uncached.append(ident)

        batch_transient = False
        if uncached:
            time.sleep(rate_sec)
            fetched = fetch(uncached)
            if fetched is None:
                batch_transient = True
                consecutive_transient += 1
                log.warning("%s: batch failed; %d ids left for retry.", label, len(uncached))
                if consecutive_transient >= TRANSIENT_BREAKER_LIMIT:
                    log.warning("%s: throttled — stopping phase; rerun to resume. "
                                "(%d consecutive transient batches)",
                                label, consecutive_transient)
                    break
            else:
                consecutive_transient = 0
                for ident, abstract in fetched.items():
                    _write_abstract_cache(f"{namespace}:{ident}", abstract or "__none__")
                    results[ident] = abstract

        for ident in batch:
            if batch_transient and ident in uncached:
                continue
            _append_checkpoint(f"{namespace}:{ident}")
            if results.get(ident):
                found += 1
                found_index.add(f"{namespace}:{ident}")

        done_so_far = batch_start + len(batch)
        if done_so_far % 5000 < batch_size:
            log.info("  %s progress: %d / %d  (found: %d)",
                     label, done_so_far, len(ids), found)

    log.info("%s complete. Abstracts found: %d", label, found)
    return found


def _run_item_phase(label: str, namespace: str, dois: list[str], rate_sec: float,
                    fetch, found_index: set[str], progress_every: int) -> int:
    """Run one per-DOI phase; return how many abstracts it recovered.

    *fetch* returns (abstract, status) with status ``ok`` / ``empty`` (a definitive
    miss, cached and checkpointed) / ``transient`` (neither, so a later run retries
    the DOI) / ``stop`` (end the phase now — Scopus's spent weekly quota).
    TRANSIENT_BREAKER_LIMIT transient failures in a row stop the phase too.
    """
    log.info("%s: %d rows to try.", label, len(dois))
    found = 0
    consecutive_transient = 0

    for i, doi in enumerate(dois, 1):
        cached = _read_abstract_cache(f"{namespace}:{doi}")
        if cached is not None:
            abstract = cached if cached != "__none__" else None
        else:
            time.sleep(rate_sec)
            abstract, status = fetch(doi)
            if status == "stop":
                log.warning("%s: stopping phase (%d rows done, %d found).",
                            label, i - 1, found)
                break
            if status == "transient":
                consecutive_transient += 1
                log.warning("%s: transient failure for %s (not checkpointed).", label, doi)
                if consecutive_transient >= TRANSIENT_BREAKER_LIMIT:
                    log.warning("%s: throttled — stopping phase; rerun to resume. "
                                "(%d consecutive transient failures)",
                                label, consecutive_transient)
                    break
                continue
            _write_abstract_cache(f"{namespace}:{doi}", abstract or "__none__")

        consecutive_transient = 0
        _append_checkpoint(f"{namespace}:{doi}")
        if abstract:
            found += 1
            found_index.add(f"{namespace}:{doi}")

        if i % progress_every == 0:
            log.info("  %s progress: %d / %d  (found: %d)", label, i, len(dois), found)

    log.info("%s complete. Abstracts found: %d", label, found)
    return found
