"""
fetch_abstracts.py — The six abstract sources, their order, and the contract
that keeps a transient failure from being recorded as a definitive miss.

This module is a library of phase runners, not a command. Its one consumer is
`filter.engine.backfill`, which supplies the worklist (from the routing table)
and decides where the recovered text lands (an overlay chunk). Everything about
HOW an abstract is fetched lives here so there is exactly one copy of it.

**Two pathways, because the six sources do not cost the same thing.** Two of them
answer about many identifiers per request and are neither keyed nor quota'd, so
they can be run over a whole corpus up front; the other four are one request per
identifier, or gated by a key, an entitlement or a weekly quota, so they are worth
spending only on the rows that are still missing text AND that some caller
actually needs. `filter.engine.backfill` runs them as two phases in that order —
the runners here are the same either way; the pathway is which worklist they see.

CHEAP BULK — run first, over every missing-abstract row:

  1. OpenAlex batch   — rows with an OpenAlex id, OA_BATCH_SIZE ids per call.
                        Single-entity and filter queries are the free/1× end of
                        OpenAlex's price list. Near-zero yield when the corpus was
                        itself discovered via OpenAlex (measured 2026-07-27: 0/200
                        random sample), but it costs almost nothing to ask.
  2. Europe PMC batch — rows with a DOI, EPMC_BATCH_SIZE DOIs per call, no API key
                        and no quota; the one source that can be pointed at
                        hundreds of thousands of DOIs without a budget
                        conversation. Ordered ahead of every other DOI phase: on
                        960 never-tried missing-abstract
                        DOIs sampled across this corpus's dominant prefixes
                        (2026-07-29) it recovered 47.7%, against Semantic Scholar's
                        8.5% and CrossRef's 0.3% on the same DOIs. The gap is
                        structural — 69% of this corpus's missing abstracts are
                        Elsevier (10.1016) and Springer (10.1007), neither of which
                        deposits abstracts to CrossRef, and OpenAlex's abstract index
                        derives from that same deposit stream. Europe PMC indexes the
                        publisher record instead, so it sees what they do not.

EXPENSIVE TARGETED — run only over the rows the bulk pathway left without text,
and only for a worklist that matters:

  3. OSF registrations — rows that identify an OSF record ONLY (a DOI on the OSF
                        registrant 10.17605, or — for a row with no DOI at all —
                        an osf.io URL), one call each, keyless. Not an abstract
                        source in the ordinary
                        sense: these records HAVE no abstract, they have a
                        registration template and a responses form, and the
                        template is what says whether the record reports a
                        completed replication or announces a planned one. Because
                        that template line must be the text the overlay records —
                        two specs in `filter/spec/` read the first line — the OSF
                        phase is the one targeted phase whose targets are NOT
                        narrowed by what the bulk pathway found: a Europe PMC
                        abstract for an OSF DOI is not a substitute for it. It is
                        first in the targeted order because it is free and its
                        target set is one registrant wide.
  4. Semantic Scholar batch — rows with a DOI, up to 500 DOIs/call, but requires
                        S2_API_KEY, which is why it is not in the bulk pathway.
                        Still worth running after Europe PMC:
                        the two are complementary, not nested — on the sample above
                        S2 added +10 Elsevier and +11 SSRN (10.2139) abstracts Europe
                        PMC missed entirely (SSRN: EPMC 2%, S2 10%). Measured over a
                        494,406-row target list (2026-07-27/28), ~49.8 DOIs/sec
                        sustained at a 14.5% hit rate, vs CrossRef's ~3/sec at ~0.6%.
  5. CrossRef by DOI  — one DOI per call; CrossRef has no batch-by-DOI-list
                        endpoint, so its cost is linear in the worklist.
  6. Scopus by DOI    — Elsevier Abstract Retrieval API, last because it is the
                        most gated of all: an ELSEVIER_API_KEY, an IP-bound
                        entitlement, and a ~10k requests/week quota, so a caller
                        must cap its Scopus phase.

The order above is the order calls are SPENT. It is not the order a recovered
abstract is attributed in — `filter.engine.backfill.SOURCE_ORDER` keeps OSF first
for that, so an OSF registration's template line wins over any abstract another
source happened to hold for the same DOI.

Rows whose DOI prefix registers datasets rather than articles (_DATASET_PREFIXES)
should be dropped from a worklist entirely — they have no abstract to find, so
every phase would spend calls confirming that forever.

Results go to `shared/abstract_store.py` — one SQLite row per identifier
(oa:<id>, epmc:10.x/y, doi:10.x/y, s2:10.x/y, scopus:10.x/y, osf:<id>). Each phase
owns its own namespace, so adding one never invalidates another's progress, and
the store is shared across callers on purpose: a DOI one run already asked Europe
PMC about is answered for free next time, and a miss recorded once is a miss
nobody re-buys.

**The row IS the checkpoint.** It exists exactly when a phase got a definitive
answer, so "already tried" and "what came back" can no longer disagree — they were
two files that had to be kept in step, and the third file, a sidecar index of which
identifiers resolved, existed only because asking that of ~500k cache files took
about two hours. A TRANSIENT failure is recorded by nobody: only a definitive
answer (text, or a confirmed absence) becomes a row.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import requests

from shared import abstract_store
from shared.config import (
    ELSEVIER_INSTTOKEN, EPMC_BATCH_SIZE, EPMC_RATE_SEC, OA_BATCH_SIZE,
    OSF_TOKEN, RESEARCHER_EMAIL, S2_BATCH_RATE_SEC, S2_BATCH_SIZE, log,
)
from shared.openalex_keys import headers as oa_headers, is_budget_refusal, rotate_key
from shared.pdf_sources import osf_registration_guid
from shared.utils import clean_doi, reconstruct_abstract

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------


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

def _read_abstract_cache(ident: str) -> Optional[str]:
    """The cached answer for *ident*: text, `__none__` for a recorded miss, or
    None when no phase has tried it yet.

    The three-way answer is the phase runners' contract and predates the store;
    `abstract_store.lookup()` returns it as `(tried, abstract)` and this keeps the
    old shape so the runners' logic is unchanged.
    """
    tried, abstract = abstract_store.lookup(ident)
    if not tried:
        return None
    return abstract if abstract else "__none__"


def _write_abstract_cache(ident: str, abstract: Optional[str]) -> None:
    """Record a DEFINITIVE answer — text, or a miss. This is also the checkpoint:
    the row's existence is what "already tried" means now."""
    abstract_store.record(ident, None if abstract == "__none__" else abstract)


def _load_found_index() -> set[str]:
    """Identifiers that resolved to real text.

    One indexed query. This used to be a 92 KB sidecar file that existed only
    because answering the same question from ~500k cache files took about two
    hours, and that file could drift from the cache it described.
    """
    return abstract_store.found_idents()


def _already_resolved(oa_id: str, doi_r: str, found_index: set[str]) -> bool:
    """True when some earlier phase already recovered an abstract for this row."""
    doi = clean_doi(str(doi_r or ""))
    if oa_id and f"oa:{oa_id}" in found_index:
        return True
    return bool(doi) and any(
        f"{p}:{doi}" in found_index for p in ("epmc", "doi", "s2", "scopus", "osf"))


# ---------------------------------------------------------------------------
# Checkpoint — the store IS the checkpoint
# ---------------------------------------------------------------------------

def _load_checkpoint() -> set[str]:
    """Every identifier already answered. A row exists exactly when a phase got a
    definitive answer, so the checkpoint can no longer disagree with the cache."""
    return abstract_store.tried_idents()


def _checkpoint_batch(entries: list[tuple[str, Optional[str]]]) -> None:
    """Record a whole batch's definitive answers in one transaction."""
    abstract_store.record_many(
        [(ident, None if abstract == "__none__" else abstract)
         for ident, abstract in entries])


# ---------------------------------------------------------------------------
# Source 1: OpenAlex batch — bulk pathway
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
# Source 2: Europe PMC by DOI, batched — the bulk pathway's workhorse
# ---------------------------------------------------------------------------

# Europe PMC's search page ceiling. A batch asks for headroom on top of its own
# size because one DOI can match several records; the batch is refused rather
# than truncated if even that is not enough (see below).
_EPMC_MAX_PAGE_SIZE = 1000
_EPMC_SEARCH_POST = "https://www.ebi.ac.uk/europepmc/webservices/rest/searchPOST"


def _fetch_epmc_batch(dois: list[str]) -> Optional[dict[str, Optional[str]]]:
    """Fetch abstracts for up to EPMC_BATCH_SIZE DOIs in one Europe PMC search.

    Europe PMC has no id-list endpoint, so the batch is expressed as a boolean
    query: 'DOI:"a" OR DOI:"b" ...'. That IS its batch API, and the reason this
    source carries the bulk pathway: it is keyless, unquota'd, and one request
    answers about a whole chunk of the corpus. The request goes to `searchPOST`,
    the form-encoded twin of `/search`, so the query has no URL-length ceiling
    and EPMC_BATCH_SIZE can be raised without the URL silently truncating
    (verified live 2026-08-05: a 500-DOI OR query, 12.9k characters, answered
    HTTP 200).

    Results come back unordered and a DOI may match more than one record (a
    preprint and its published version), so the join is by DOI rather than by
    position — the first record carrying an abstract wins.

    resultType=core is REQUIRED: the lighter 'lite' view omits abstractText
    entirely, so with it every DOI would look like a miss.

    Returns None on a whole-batch failure (retried 3x with backoff, honouring
    Retry-After on 429) so the caller does not checkpoint any DOI in the batch —
    the same contract as _fetch_openalex_batch / _fetch_s2_batch. A DOI absent
    from a successful response is a definitive miss, mapped to None.
    """
    query = " OR ".join(f'DOI:"{d}"' for d in dois)
    data = {
        "query": query,
        "format": "json",
        "resultType": "core",
        # A DOI can match several records; ask for headroom so a duplicate
        # cannot push a distinct DOI's only record off the first page.
        "pageSize": min(len(dois) * 3, _EPMC_MAX_PAGE_SIZE),
    }
    resp, status = _request_with_retry(
        "EuropePMC batch",
        lambda: _SESSION.post(_EPMC_SEARCH_POST, data=data, timeout=60),
        backoff=lambda attempt: EPMC_RATE_SEC * (attempt + 1),
    )
    if status == "transient":
        return None
    if resp.status_code >= 400:
        log.warning("EuropePMC batch error (batch not checkpointed): HTTP %d — %s",
                    resp.status_code, resp.text[:200])
        return None

    payload = resp.json()
    records = ((payload.get("resultList") or {}).get("result") or [])
    # A truncated page is not an answer about the DOIs it left out, and recording
    # them as misses would be permanent. Refuse the whole batch instead and say
    # what to change — the same "never checkpoint what the source did not say"
    # rule the per-item phases run on.
    if int(payload.get("hitCount") or 0) > len(records):
        log.warning("EuropePMC batch returned %d of %d matches (page truncated; batch "
                    "not checkpointed). Lower EPMC_BATCH_SIZE.",
                    len(records), payload.get("hitCount"))
        return None

    result: dict[str, Optional[str]] = {d: None for d in dois}
    for record in records:
        doi = str(record.get("doi") or "").strip().lower()
        abstract = record.get("abstractText") or None
        if doi in result and abstract and not result[doi]:
            result[doi] = _JATS_RE.sub("", abstract).strip() or None
    return result


# ---------------------------------------------------------------------------
# Source 5: CrossRef by DOI — targeted pathway, one call per DOI
# ---------------------------------------------------------------------------

def _fetch_crossref_abstract(doi: str) -> tuple[Optional[str], str]:
    """Fetch an abstract from CrossRef by DOI.

    Returns (abstract, status) where status is:
      "ok"        — an abstract was found
      "empty"     — a response that POSITIVELY ESTABLISHES ABSENCE: 404 (CrossRef
                    does not have this DOI) or 200 with no abstract field. Only
                    these are checkpointed.
      "transient" — 429/5xx/network failure that persisted through all retries
                    (must NOT be checkpointed, so a later run retries the DOI)
      "stop"      — 401/403: the request was refused, not answered

    **A 401/403 is "stop", never "empty"** — the same rule the Scopus phase runs on.
    A refused request establishes nothing about whether CrossRef holds an abstract
    for this DOI, and every 4xx used to map to "empty": one misconfigured polite-pool
    header or a WAF block would have written `__none__` for every DOI the phase
    touched, permanently, and no later run would look again. Auth is host-wide rather
    than per-record, so the first one ends the phase.

    The polite-pool ?mailto= param earns better rate limits. Transient failures
    retry 3× with 1s/2s/4s backoff, honouring a 429 Retry-After header when present.
    """
    url = f"https://api.crossref.org/works/{doi}?mailto={RESEARCHER_EMAIL}"
    resp, status = _request_with_retry(
        f"CrossRef {doi}", lambda: _SESSION.get(url, timeout=20))
    if status == "transient":
        return None, "transient"
    if resp.status_code in (401, 403):
        log.warning(
            "CrossRef refused the request for %s (HTTP %d) — stopping the phase "
            "rather than recording a miss it cannot establish. Check RESEARCHER_EMAIL "
            "and whether this IP is being blocked.", doi, resp.status_code)
        return None, "stop"
    if resp.status_code == 404 or resp.status_code == 400:
        return None, "empty"
    if resp.status_code >= 400:
        # Any other 4xx: unrecognised, so not evidence of absence either.
        return None, "transient"
    raw = resp.json().get("message", {}).get("abstract", "")
    cleaned = _JATS_RE.sub("", raw).strip() if raw else ""
    return (cleaned, "ok") if cleaned else (None, "empty")


# ---------------------------------------------------------------------------
# Source 4: Semantic Scholar by DOI — targeted pathway (needs S2_API_KEY)
# ---------------------------------------------------------------------------

def _fetch_s2_abstract(doi: str, s2_key: str) -> tuple[Optional[str], str]:
    """Fetch an abstract from Semantic Scholar by DOI.

    Returns (abstract, status) with the same contract as _fetch_crossref_abstract:
    "ok" / "empty" (definitive miss) / "transient" (429/5xx/network, retried 3×) /
    "stop" (401/403 — a rejected key is not evidence that S2 has no abstract, and it
    will reject every remaining DOI too).
    A 429 was previously treated as a clean miss and checkpointed — that permanently
    suppressed the row. It is now transient so a later run retries it.
    """
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=abstract"
    headers = {"x-api-key": s2_key} if s2_key else {}
    resp, status = _request_with_retry(
        f"S2 {doi}", lambda: _SESSION.get(url, timeout=20, headers=headers))
    if status == "transient":
        return None, "transient"
    if resp.status_code in (401, 403):
        log.warning("Semantic Scholar refused the request for %s (HTTP %d) — stopping "
                    "the phase; check S2_API_KEY.", doi, resp.status_code)
        return None, "stop"
    if resp.status_code == 404:
        return None, "empty"
    if resp.status_code >= 400:
        return None, "transient"
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
    misses. A successful batch's null entry for a given DOI is a definitive miss — but
    only when the array is as long as the request, which is checked below.
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
    entries = resp.json()
    # The join is positional, so a response of the wrong length is not an answer about
    # the ids it left out — and `zip` would silently drop them, checkpointing them as
    # definitive misses for good. Refuse the whole batch, as the EPMC phase does with
    # a truncated page.
    if not isinstance(entries, list) or len(entries) != len(dois):
        log.warning("S2 batch returned %s entr(ies) for %d DOI(s) (batch not "
                    "checkpointed): the positional join would attribute answers to the "
                    "wrong ids or record the rest as misses.",
                    len(entries) if isinstance(entries, list) else type(entries).__name__,
                    len(dois))
        return None
    return {
        doi: ((entry or {}).get("abstract") or None)
        for doi, entry in zip(dois, entries)
    }


# ---------------------------------------------------------------------------
# Source 6: Elsevier Scopus by DOI — targeted pathway, last (weekly quota)
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


def _fetch_scopus_abstract(doi: str, api_key: str) -> tuple[Optional[str], str]:
    """Fetch an abstract from Elsevier Scopus by DOI.

    Returns (abstract_or_none, status) on the same contract as the sibling
    fetchers: "ok" / "empty" (a DEFINITIVE miss, cached and checkpointed) /
    "stop" (end the phase now). Transient errors retry 3× with 1s/2s/4s backoff
    per repo convention; on a 429 whose X-RateLimit-Remaining header is "0" — or
    after 3 backed-off retries still hitting 429 — the ~10k/week quota is treated
    as spent and the phase stops gracefully.

    **A 401/403 is "stop", never "empty".** Elsevier answers an unentitled request
    the same way it answers one for a record it has: with no abstract. Recording
    that as a definitive miss would write `__none__` and a checkpoint line for
    every DOI the phase touched, so a machine that later GAINS the entitlement
    would skip them all and never find out. Entitlement is account- and IP-wide
    rather than per-record, so the first one is enough to end the phase.
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
        return None, "stop"
    if resp.status_code in (400, 404):
        return None, "empty"
    if resp.status_code in (401, 403):
        # AUTHORIZATION_ERROR — the key is valid but not entitled to the abstract
        # view. Retrying cannot help, and it is NOT a spent quota; what it is NOT
        # either is evidence that Scopus holds no abstract for this DOI.
        log.warning(
            "Scopus not entitled to the abstract view for %s (HTTP %d) — stopping the "
            "phase rather than recording a miss it cannot establish. Elsevier "
            "entitlement is IP-bound: run from the subscribing network/VPN, or set "
            "ELSEVIER_INSTTOKEN.", doi, resp.status_code)
        return None, "stop"
    if resp.status_code >= 400:
        return None, "stop"
    abstract = _parse_scopus_abstract(resp.json())
    return (abstract, "ok") if abstract else (None, "empty")


# ---------------------------------------------------------------------------
# Source 3: OSF registrations — targeted pathway (rows that identify an OSF record)
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
# The projects/components endpoint, tried when the registrations one says 404.
_OSF_NODE_API = "https://api.osf.io/v2/nodes/{guid}/"


def _osf_guid(identifier: str) -> Optional[str]:
    """The OSF GUID in *identifier*, or None.

    Both forms the phase is given reach the same GUID: `10.17605/OSF.IO/AB12D`
    and `osf.io/ab12d` → `ab12d`. The derivation is imported rather than
    re-written because Stage 3's PDF waterfall already had to answer this exact
    question for the same rows (`osf_registration_guid()` in
    `shared/pdf_sources.py`), and two regexes for one identifier drift.
    """
    return osf_registration_guid(identifier) or None


def osf_identifier(doi: str, url: str = "") -> Optional[str]:
    """What the OSF phase asks about for a row, and checkpoints under — or None.

    Two forms, deliberately, because the checkpoint namespace has to stay
    coherent across a change that widened the phase:

    - A DOI on the OSF registrant keeps the cleaned DOI as its identifier,
      unchanged. That is what the 878 `osf:10.17605/...` checkpoint entries
      already on disk are keyed by; re-keying them to the GUID would re-buy
      every one of those answered calls.
    - A row with NO DOI and an osf.io URL is keyed `osf.io/<guid>`. It is
      canonical, so `https://osf.io/ab12d`, `http://api.osf.io/v2/nodes/ab12d/`
      and `.../v2/registrations/ab12d/` are one call and one key rather than
      three; and it cannot collide with the DOI form, which always begins
      `10.17605/`.

    The URL only counts when the row has no DOI. A published article's OA link
    is sometimes an OSF copy (`10.1037/xhp0000556` → `https://osf.io/ebv4q` in
    the current export), and because OSF leads `backfill.SOURCE_ORDER`, asking
    about it would replace that article's abstract with a registration template
    line — handing a real paper to the `osf-registration-protocol` discard.
    """
    doi = clean_doi(str(doi or ""))
    if doi:
        return doi if doi.split("/", 1)[0] == OSF_REGISTRANT else None
    guid = _osf_guid(str(url or ""))
    return f"osf.io/{guid}" if guid else None


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


def _fetch_osf_registration(identifier: str) -> tuple[Optional[str], str]:
    """Fetch an OSF registration's template and responses.

    *identifier* is whatever `osf_identifier()` produced for the row — a
    registrant DOI or a canonical `osf.io/<guid>`; both carry the GUID the
    endpoint answers about.

    Same (abstract, status) contract as the other per-item sources. A 404 is a
    definitive `empty`: the DOI is an OSF project or component rather than a
    registration, and no later run should re-buy that answer.

    A CREDENTIAL failure stops the phase: with no token every private registration
    answers exactly like a non-existent one, so writing a miss per DOI would record
    "no such registration" for a whole corpus on one missing setting. That is 401,
    and 403 when no token was sent.

    A 403 with a token sent is a different fact — OSF accepted the credential and
    refused this ONE record, which is private or embargoed to us. Stopping there
    ends the phase on its first private registration: measured 2026-08-08, the run
    covered 10 of 878 rows before one 403 (`10.17605/osf.io/tg6sp`) halted it, and
    the 868 rows behind it kept the routing rule that reads their template line
    inert. It is reported as transient rather than empty because the answer is
    about our access, not about the record: a later token with more scope should
    ask again rather than read a checkpointed miss.
    """
    guid = _osf_guid(identifier)
    if not guid:
        return None, "empty"
    headers = {"Authorization": f"Bearer {OSF_TOKEN}"} if OSF_TOKEN else {}
    resp, status = _request_with_retry(
        f"OSF {identifier}",
        lambda: _SESSION.get(_OSF_API.format(guid=guid), timeout=30, headers=headers))
    if status == "transient":
        return None, "transient"
    if resp.status_code == 401 or (resp.status_code == 403 and not OSF_TOKEN):
        log.warning("OSF refused the credential for %s (HTTP %d) — stopping the "
                    "phase; check OSF_TOKEN.", identifier, resp.status_code)
        return None, "stop"
    if resp.status_code == 403:
        log.info("OSF registration %s is private or embargoed to this token — "
                 "skipped, not checkpointed", identifier)
        return None, "transient"
    if resp.status_code == 410:
        return None, "empty"
    if resp.status_code == 404:
        # Not a registration. 1,696 of the OSF identifiers in the 2026-08-13 worklist
        # answer this way: they are PROJECTS and components, which the registrations
        # endpoint does not serve and which can never have a template line.
        return _fetch_osf_node(guid, headers)
    if resp.status_code >= 400:
        return None, "transient"
    attributes = (resp.json().get("data") or {}).get("attributes") or {}
    if not attributes:
        return None, "empty"
    return _osf_registration_text(attributes), "ok"


def _fetch_osf_node(guid: str, headers: dict) -> tuple[Optional[str], str]:
    """The OSF PROJECT's own description, for a guid that is not a registration.

    Its description is the only text these records have — measured 2026-08-13 over 30
    admitted OSF misses, 26 carry one (median 252 chars) and it is the record's own
    account: "This study is a replication attempt of the first experiment of ... the
    chameleon effect ...". That names the original, which is what both the screen and
    the linking rungs need and neither can get from a title.

    No template line, deliberately: a project HAS no template, and prefixing one would
    make `osf-registration-protocol` discard a record whose own words nobody read. What
    this returns is an ordinary abstract, and is written only where the row has none —
    the caller's job, because a 252-char description must not displace a real one
    (`_write_overlay` in filter/engine/backfill.py).
    """
    resp, status = _request_with_retry(
        f"OSF node {guid}",
        lambda: _SESSION.get(_OSF_NODE_API.format(guid=guid), timeout=30,
                             headers=headers))
    if status == "transient":
        return None, "transient"
    if resp.status_code in (401, 403):
        # Private to this token, not absent: the same reading the registration arm
        # gives a 403, and for the same reason.
        return None, "transient"
    if resp.status_code >= 400:
        return None, "empty"
    description = str(((resp.json().get("data") or {}).get("attributes")
                       or {}).get("description") or "").strip()
    return (description, "ok") if description else (None, "empty")


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
                    results[ident] = abstract

        # One transaction for the batch's definitive answers. Every id the request
        # covered is recorded, not only the ones the response mentioned: an id
        # absent from a SUCCESSFUL response is the source saying it has nothing,
        # which is exactly the miss nobody should re-buy. Ids left unanswered by a
        # FAILED batch are recorded by nobody, so a later run retries them.
        _checkpoint_batch([(f"{namespace}:{ident}", results.get(ident))
                           for ident in batch
                           if not (batch_transient and ident in uncached)])
        for ident in batch:
            if batch_transient and ident in uncached:
                continue
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
    the DOI) / ``stop`` (end the phase now — a spent weekly quota, or an auth
    refusal that will refuse every remaining DOI just as blankly).
    TRANSIENT_BREAKER_LIMIT transient failures in a row stop the phase too.

    Only ``empty`` is ever written. ``empty`` therefore has one meaning across all
    six sources: the source ANSWERED, and its answer was that it holds no abstract
    for this identifier. A response that was refused rather than answered — 401,
    403, a rate limit, a network failure — is never it.
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
            # The write IS the checkpoint now: the row's existence is what "already
            # tried" means, so a miss and its checkpoint cannot drift apart.
            _write_abstract_cache(f"{namespace}:{doi}", abstract or "__none__")

        consecutive_transient = 0
        if abstract:
            found += 1
            found_index.add(f"{namespace}:{doi}")

        if i % progress_every == 0:
            log.info("  %s progress: %d / %d  (found: %d)", label, i, len(dois), found)

    log.info("%s complete. Abstracts found: %d", label, found)
    return found
