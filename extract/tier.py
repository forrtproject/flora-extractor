"""The `extract` tier: Stage 3's ladder, run as a claimed, budget-gated engine tier.

Stage 3 already decides everything a row needs — which original, what outcome, how
sure. What it has never had is what the two screen tiers have: a CLAIM before it
spends, a permanent verdict row that outlives the CSV, and a checkpoint the server
holds rather than a file on one machine. This module supplies those by registering
the ladder as a `TierSpec` and running it through `filter/engine/tiers.py:run_tier()`,
the same generic spine the screens use. Nothing about the extraction itself moves:
`judge` calls `run_extract._process_row`, the one per-row pipeline there has ever
been.

This is the ONLY front door. The CSV runner that used to sit beside it is gone
(parked on `wip/csv-runner`), so `data/extracted.csv` has one writer —
`python -m extract.export`, rendering the verdict rows this tier stores — and one
checkpoint, the verdict row itself. A run resumes by rebuilding its worklist: a work
whose latest current-generation result row settles it is not offered again, and
`target_pending`/`api_error` do not settle. `--redo` re-extracts named works and
supersedes their previous result row; editing a prompt or a model mints a new
generation, which reopens every work at once.

**Two kinds of verdict row, told apart by the `verdict` column.**

  * `verdict = "evidence"` — one per LLM call the row actually made. Model,
    prompt hash and a request/response summary in the blob. They are evidence, in
    the §4 sense: written before the result row that summarises them, never read
    back to make a decision.
  * everything else — the RESULT row, exactly one per work per run, its `verdict`
    the row's ending: `resolved` · `provisional` · `not_a_replication` ·
    `no_original_found` · `target_pending` · `api_error`. Its `prompt_hash` is the
    extract GENERATION and its `payload` is the whole answer.

**The self-sufficiency rule, which is what makes the payload worth storing.**
A result row's payload must rebuild that work's `EXTRACTED_COLS` rows with NO
network, NO cache, NO routing store and NO pool. Not as an optimisation — as the
definition of what the state authority holds. A payload that needed the pool to be
readable would make the verdict a pointer to a 4 GB artifact that is re-scanned,
re-fingerprinted and re-released on its own schedule; a payload that needed the
cache would make it a pointer to a directory that is explicitly expendable
(`shared/cache_sync.py`). Either way the permanent row would stop being permanent
evidence and become a bookmark. So the payload carries:

    input   — the FILTERED_COLS + SCREEN_COLS snapshot exactly as the judge read it
    link    — what the RUN did: which rung answered, whether it accepted a single
              link, how many targets it named, what stopped it
    targets — one entry per original PAPER, post-guard, post-collapse, post-renumber:
              every EXTRACTED_COLS value that is not already in `input` unchanged

`targets` is derived from the finished row dicts rather than from semi-structured
fields, so `extract/export.py` is a near-identity render and the round trip is exact
by construction. The alternative — storing the ladder's intermediate answers and
re-deriving rows at export — puts two copies of the row-building rules in the
codebase and makes the export a place a row can silently change shape.

**What is NOT recomputed at export, and why.** `pair_id`, `oa_work_id_r`,
`bibtex_ref_r` and `screen_categories` all look derivable from `input`, and three of
them usually are. None is derivable in general: `make_pair_id()`'s multi-original
fallback hashes the TARGET RECORD's OpenAlex id, which is not on the row;
`oa_work_id_r` falls back to an OpenAlex lookup when `openalex_id_r` is blank;
`bibtex_ref_r` is built from FILTERED_COLS values the row may have rewritten
(`_base_row` fills a blank `title_r` from `study_r`); and `screen_categories` is
blank rather than copied on a row that reached no screen dict. They are stored.
What IS copied from `input` at export is every FILTERED_COLS value the row did not
change, which is the bulk of it.

**Claims and the lease.** One claim per batch of `EXTRACT_CLAIM_BATCH` works, with
a `EXTRACT_CLAIM_TTL_MINUTES` lease renewed by a daemon heartbeat every third of it.
A row here is minutes of PDF download and full-text LLM, not seconds of abstract
screening, so the lease is short and renewed rather than long and unattended: a host
lost mid-batch frees its works in under an hour. `ClaimLeaseLost` from the heartbeat
means another runner may already hold these works, so the batch loop stops after the
current `run_tier` call rather than claiming the next batch.
"""

import argparse
import hashlib
import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from filter.engine.claims import (ClaimLeaseLost, ClaimsClient, ClaimsError,
                                  ClaimsNotConfigured)
from filter.engine.export import (ALIASES_FILENAME, SPEC_DIR, StaleBundleError,
                                  check_release_binding, iter_export_rows,
                                  load_conventions)
from filter.engine.handoff import HANDOFF_PILES, screen_columns, decisions
from filter.engine.release import read_release
from filter.engine.spec import load_specs
from filter.engine.store import (DEFAULT_STORE_PATH, StoreUnavailable, open_store,
                                 releases, resolve_release)
from filter.engine.tiers import TierSpec, Work, register_tier, run_tier
from filter.engine.workids import load_aliases
from shared import token_counter
from filter.engine.overlay import chunk_paths
from shared.config import (DATA_DIR, EXTRACT_WORKERS, LINKING_EFFORT,
                           LINKING_MODEL, OUTCOME_EFFORT, OUTCOME_MODEL,
                           OVERLAY_DIR, PDF_PARSE_MODEL, SNAPSHOT_POOL_DIR, log)
from shared.flora_skip import (VALIDATED_SKIP_NAME,
                               default_flora_skip_dois as _flora_skip_dois,
                               load_validated_skip as _load_validated_skip,
                               validated_work_id as _validated_work_id)
from shared.llm_client import cache_model_id
from shared.schema import (EXTRACTED_COLS, FILTERED_COLS,
                           PROVISIONAL_LINK_METHODS, RESOLVED_LINK_METHODS,
                           SCREEN_COLS)
from shared.utils import clean_doi

TIER_EXTRACT = "extract"

# The result-row vocabulary. `verdict` on a result row is the ROW'S ENDING, not a
# gate outcome: the extract tier admits nothing and discards nothing, it resolves.
EVIDENCE = "evidence"
RESOLVED = "resolved"
PROVISIONAL = "provisional"
NOT_A_REPLICATION = "not_a_replication"
NO_ORIGINAL_FOUND = "no_original_found"
TARGET_PENDING = "target_pending"
API_ERROR = "api_error"
RESULT_VERDICTS = (RESOLVED, PROVISIONAL, NOT_A_REPLICATION, NO_ORIGINAL_FOUND,
                   TARGET_PENDING, API_ERROR)

# The two endings a re-run is MEANT to redo, mirroring `REOPENED_SET_ASIDE_FILES` in
# `shared/schema.py` for the same reason: `target_pending` means "a re-run decides"
# by construction, and `api_error` records a transient provider failure. Neither
# settles the work, so neither takes it out of the worklist. Every other ending does.
UNSETTLING_VERDICTS = frozenset({TARGET_PENDING, API_ERROR})

# ── Claims and the lease ─────────────────────────────────────────────────────
# Works per claim. Small compared with a screen batch (which claims a whole pile)
# because a work here costs minutes rather than seconds: a batch is what one runner
# holds while it works through it, and a batch of thousands would hold works nobody
# reaches for hours. 200 at ~4 workers is roughly an hour of runway.
EXTRACT_CLAIM_BATCH = 200
# How long a claim holds its works before it stops blocking them. 45 minutes rather
# than the client's six-hour default: the heartbeat below renews it, so the number
# is not "how long could this batch take" but "how long after a host dies do we wait
# for its works". A lease is only safe to make short if something renews it.
EXTRACT_CLAIM_TTL_MINUTES = 45


def _ttl_seconds() -> int:
    return EXTRACT_CLAIM_TTL_MINUTES * 60


# ── The generation ───────────────────────────────────────────────────────────
# What a result row was produced BY, in one hash: the ladder's shape, the prompts the
# three LLM rungs and the two standalone coders ask, and the models that answer them
# at the efforts their call sites send. Same contract as `screening_generation()` in
# `filter/engine/tiers.py` — a verdict only answers the question the code asks today
# if the code still asks that question — and read at CALL time so a changed constant
# is seen without a re-import.
#
# What is deliberately NOT in it: the pool, the release, the rule book. A verdict
# follows the WORK. A re-route mints a new release id without changing a single
# thing about how a paper's original is found.
_GENERATION_PROMPTS = ("build_target_outcome_prompt",
                       "build_repro_target_outcome_prompt",
                       "build_outcome_prompt",
                       "build_repro_outcome_prompt")


def generation_inputs() -> dict:
    """Everything `extract_generation()` hashes, as a plain dict.

    Public so the pinning test can assert the INPUTS rather than the digest: a bump
    should read as a diff of what changed, not as one opaque hex string becoming
    another.
    """
    from extract.link_original import EXTRACT_LADDER_VERSION
    from shared.prompts import prompt_version

    return {
        "ladder": EXTRACT_LADDER_VERSION,
        "prompts": {name: prompt_version(name) for name in _GENERATION_PROMPTS},
        "models": {
            "linking": cache_model_id(LINKING_MODEL, LINKING_EFFORT),
            "outcome": cache_model_id(OUTCOME_MODEL, OUTCOME_EFFORT),
            "pdf_parse": PDF_PARSE_MODEL,
        },
    }


def extract_generation() -> str:
    """Fingerprint of the ladder, prompts and models this tier extracts with now."""
    payload = json.dumps(generation_inputs(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _accepts_legacy(rows: list[dict]) -> bool:
    """No pre-generation extract rows exist, so there is no legacy to grandfather.

    The screens grandfather rows written before `meta.generation` existed, on the
    strength of the model pair they recorded. This tier has no such history — its
    first verdict row is written by this commit — so a claim with no generation is a
    claim from a future nobody has written yet, and it counts for nothing.
    """
    return False


# ---------------------------------------------------------------------------
# The unit of work
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractWork(Work):
    """One work, with the whole handoff row the judge will extract from.

    `Work` carries the three fields a screen reads (doi, title, abstract); the ladder
    reads twenty more — the authors, the journal, the year, the OpenAlex id, and every
    `SCREEN_COLS` value the front door decided. Rather than widen `Work` for one tier,
    the row travels beside it: `run_tier` only ever touches `work_id` and `pile`, so
    the generic contract is unchanged and `spec.judge` gets the whole row.
    """
    row: dict = field(default_factory=dict)


def _work_from_row(work_id: int, pile: str, row: dict) -> ExtractWork:
    return ExtractWork(work_id=work_id, doi=clean_doi(str(row.get("doi_r") or "")),
                       title=str(row.get("title_r") or ""),
                       abstract=str(row.get("abstract_r") or ""),
                       pile=pile, source=str(row.get("source") or ""), row=row)


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


def _judge(work: Work) -> tuple[str, list[dict]]:
    """One work through the whole Stage 3 pipeline; the rows it wrote, as a payload.

    `_process_row` is called with the handoff row as a `pd.Series` — the same object
    it gets from the chunked CSV read, so nothing below it can tell the difference —
    and each row it returns goes through `_finalise_row`, which is where DOI
    verification happens. Verification runs INSIDE the judge, once, and its answer is
    stored: it costs up to three OpenAlex free-text searches at 10× a filter query,
    and an export that re-verified would pay that bill again on every render.

    The ending is read off the rows rather than predicted: `provisional` when any row
    is an `llm_title_search`, `resolved` when any row carries a resolved link_method,
    and otherwise whichever unresolved ending the rows agree on.
    """
    from extract import run_extract as rx

    assert isinstance(work, ExtractWork), (
        f"the extract tier was handed a plain Work for {work.work_id}: its judge "
        "needs the handoff row, not just the abstract")

    token_counter.set_stage("extract_tier")
    row = pd.Series(work.row)
    # A pool row admitted on its OpenAlex
    # id may carry a repository URL instead of a DOI, and everything below keys
    # on the DOI. The resolved value lands in the row, so the payload's input
    # snapshot records what the ladder actually ran with.
    row, doi_r = rx._resolve_missing_doi(row, work.doi)
    observed: dict = {}
    rows = rx._process_row(row, doi_r, no_llm=False, no_pdf=False,
                           no_reproductions=False, resolved_only=False,
                           observed=observed)
    final = [rx._finalise_row(r) for r in rows]

    payload = result_payload(row.to_dict(), doi_r, final, observed)
    verdict = _verdict_for(final, observed)
    result_row = {
        "verdict": verdict,
        "model": "",
        "prompt_hash": extract_generation(),
        "confidence": _one(final, "original_match_confidence"),
        # The primary link_evidence, capped: the column is a paragraph on a
        # multi-target row and the verdict table is lean by design. The whole value
        # is in the payload.
        "quote": _one(final, "link_evidence")[:200],
        "cost": _cost(observed, final),
        "payload": payload,
        "blob": {"tier": TIER_EXTRACT, "work_id": work.work_id,
                 "verdict": verdict, "observed": observed},
    }
    return verdict, _evidence_rows(work, final, observed) + [result_row]


def _one(rows: list[dict], column: str) -> str:
    """The first non-empty value of *column* across a work's rows."""
    for row in rows:
        value = str(row.get(column, "") or "")
        if value:
            return value
    return ""


def _verdict_for(rows: list[dict], observed: dict) -> str:
    """The work's ending, read off the rows it produced.

    Order matters and it is the order of what the run ACHIEVED: a paper with one
    resolved original and one provisional title-search row resolved something, and
    filing it as provisional would understate it. `api_error` last of the unresolved
    endings for the opposite reason — it is the only ending that says nothing about
    the paper, and any row that reached a real conclusion outranks it.
    """
    if not rows:
        # --resolved-only drops rows, but the tier never passes it; an empty list here
        # means the pipeline wrote nothing at all, which only a flag can cause.
        return API_ERROR if observed.get("error") else TARGET_PENDING
    methods = [str(r.get("link_method", "") or "") for r in rows]
    if any(m in RESOLVED_LINK_METHODS for m in methods):
        return RESOLVED
    if any(m in PROVISIONAL_LINK_METHODS for m in methods):
        return PROVISIONAL
    for ending in (NOT_A_REPLICATION, NO_ORIGINAL_FOUND, TARGET_PENDING, API_ERROR):
        if ending in methods:
            return ending
    return TARGET_PENDING


def _cost(observed: dict, rows: list[dict]) -> dict:
    """What this work cost, from the meters that already exist.

    Deliberately thin. `shared/token_usage.py` records tokens per day/provider/model
    and `search_query_count()` counts OpenAlex free-text searches per PROCESS — both
    are run-level, and neither can be attributed to one work from a worker thread
    without building a second metering system. What is cheaply and honestly
    observable per work is which rung answered and which acquisition tier supplied
    the document, so that is what the row carries; the money is read off the run
    report and `cache/token_usage.json` as it always has been.
    """
    return {
        "rung": observed.get("target_stage", ""),
        "pdf_tier": observed.get("pdf_source", ""),
        "parse_method": observed.get("parse_method", ""),
        "rows": len(rows),
        "n_targets": int(observed.get("n_targets") or 0),
        "unidentified": int(observed.get("unidentified_count") or 0),
    }


def _evidence_rows(work: Work, rows: list[dict], observed: dict) -> list[dict]:
    """One `verdict = "evidence"` row per LLM call this work is known to have made.

    Known to have made, not known to have been billed for: the ladder reports which
    rung answered and which model answered it, and a cached answer is the same
    evidence about the same question. What this is NOT is a token ledger — that is
    `cache/token_usage.json`, which counts what a provider was actually asked.

    Two calls can be attributed from a finished row without instrumenting the ladder:
    the rung that named the target (`link_llm_model`, at the target prompt's version)
    and the coder that settled the outcome (`outcome_llm_model`). A row that named
    neither made neither.
    """
    from shared.prompts import prompt_version

    calls: list[tuple[str, str, str]] = []
    link_model = observed.get("link_llm_model") or _one(rows, "link_llm_model")
    stage = str(observed.get("target_stage", "") or "")
    if link_model:
        repro = _one(rows, "type") == "reproduction"
        calls.append(("target", link_model, prompt_version(
            "build_repro_target_outcome_prompt" if repro
            else "build_target_outcome_prompt")))
    outcome_model = _one(rows, "outcome_llm_model")
    if outcome_model and outcome_model != "keyword":
        repro = _one(rows, "type") == "reproduction"
        calls.append(("outcome", outcome_model, prompt_version(
            "build_repro_outcome_prompt" if repro else "build_outcome_prompt")))

    return [{
        "verdict": EVIDENCE,
        "model": model,
        "prompt_hash": version,
        "quote": _one(rows, "link_evidence")[:200] if call == "target"
                 else _one(rows, "outcome_phrase")[:200],
        "blob": {"tier": TIER_EXTRACT, "work_id": work.work_id, "kind": "evidence",
                 "call": call, "rung": stage, "model": model,
                 "prompt_version": version, "doi_r": work.doi},
        "payload": {"kind": "evidence", "call": call, "rung": stage},
    } for call, model, version in calls]


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------

# The input snapshot: what the judge read, and the only columns the export copies
# through unchanged. FILTERED_COLS is the Stage 2 contract; SCREEN_COLS is the
# verdict the front door travelled on and is what makes a stored payload replayable
# without the handoff CSV that carried it.
INPUT_COLS = tuple(FILTERED_COLS) + tuple(SCREEN_COLS)


def result_payload(source_row: dict, doi_r: str, rows: list[dict],
                   observed: dict) -> dict:
    """The stored answer for one work: input, what the run did, and the rows.

    A target entry holds every `EXTRACTED_COLS` value that is not an unchanged copy
    of the input's — so `pair_id`, the outcome block, the link columns and the
    verification verdict are all stored, while `authors_r`, `journal_r` and the rest
    of FILTERED_COLS are stored once, in `input`. A FILTERED_COLS value the row DID
    change (a `filter_status` the screen retyped, a normalised `year_r`) is stored on
    the target, because the difference is the row's and it must survive.

    Takes the input row rather than the `ExtractWork` so that the round-trip test can
    build a payload out of rows that already exist on disk, without a claims client,
    a pool or a judge.
    """
    input_row = {col: str(source_row.get(col, "") or "") for col in INPUT_COLS}
    targets = []
    for row in rows:
        entry = {}
        for col in EXTRACTED_COLS:
            value = row.get(col, "")
            value = "" if value is None else value
            if col in input_row and str(value) == input_row[col]:
                continue
            entry[col] = value
        targets.append(entry)
    return {
        "kind": "result",
        "generation": extract_generation(),
        "doi_r": doi_r,
        "input": input_row,
        "link": {
            "link_method": observed.get("link_method", ""),
            "target_stage": observed.get("target_stage", ""),
            "resolved": bool(observed.get("resolved", False)),
            "n_targets": int(observed.get("n_targets") or 0),
            "stated_count": observed.get("stated_count"),
            "unidentified_count": int(observed.get("unidentified_count") or 0),
            "link_llm_model": observed.get("link_llm_model", ""),
            "pdf_source": observed.get("pdf_source", ""),
            "parse_method": observed.get("parse_method", ""),
            "link_evidence": observed.get("link_evidence", ""),
            "grobid_discussion": bool(observed.get("grobid_discussion", False)),
            "discussion_provenance": observed.get("discussion_provenance", ""),
            "error": observed.get("error", ""),
        },
        "targets": targets,
    }


def render_payload(payload: dict) -> list[dict]:
    """A stored result payload back as its `EXTRACTED_COLS` rows.

    The inverse of `_result_payload`, and the only place a stored verdict becomes a
    CSV row. Pure: no network, no cache, no store, no pool — which is the property
    the payload shape exists to make true (see the module docstring).
    """
    input_row = payload.get("input") or {}
    base = {col: str(input_row.get(col, "") or "") for col in INPUT_COLS
            if col in EXTRACTED_COLS}
    rows = []
    for target in payload.get("targets") or []:
        row = {col: "" for col in EXTRACTED_COLS}
        row.update(base)
        row.update({k: v for k, v in target.items() if k in EXTRACTED_COLS})
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# The checkpoint: which works this tier has settled
# ---------------------------------------------------------------------------


def _result_rows(rows: list[dict]) -> list[dict]:
    """One work's RESULT rows, newest last. Evidence rows are not decisions."""
    return sorted((r for r in rows if str(r.get("verdict") or "") in RESULT_VERDICTS),
                  key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))


def _decide(rows: list[dict]) -> dict:
    """This tier's decision for one work: the LATEST result row wins.

    A tier-specific reader rather than the screens' `decided_work_ids`, because that
    one's semantics is wrong here in both directions. It calls a work decided when
    ANY current-generation verdict row adds up to a decision — but an extract work
    writes evidence rows too, and a work with two evidence rows and no result row
    would read as decided and never be extracted. And it has no notion of an ending
    that does not settle: `target_pending` and `api_error` are permanent evidence of
    a run that happened and must NOT take the work out of the worklist, or a
    five-minute provider outage becomes a permanent hole in the corpus.

    The latest row wins rather than the rows being pooled, because a result row is a
    whole answer about the work and two of them are two runs, not two voters. Same
    ordering rule as the screens' `_recorded_at`: `created_at` first, the row id as
    the tiebreaker, since the primary key is a uuid and id order is not time order.
    """
    results = _result_rows(rows)
    if not results:
        return {"outcome": "", "settles": False, "row": None, "record_type": "",
                "votes": []}
    latest = results[-1]
    verdict = str(latest.get("verdict") or "")
    return {"outcome": verdict, "settles": verdict not in UNSETTLING_VERDICTS,
            "row": latest, "record_type": "", "votes": []}


def settled_work_ids(client: ClaimsClient, mode: str = "live") -> set[int]:
    """Works whose latest current-generation *mode* result row SETTLES them.

    `target_pending` and `api_error` are excluded on purpose: they are the two
    endings a re-run is meant to redo (`UNSETTLING_VERDICTS`).

    The mode filter is what makes the sandbox promotable: a validation-mode
    shadow verdict must NOT settle the live worklist, or the work never gets the
    live verdict the export reads and silently vanishes from `extracted.csv`.
    Re-running it live is the promotion, and it is near-free on cached calls —
    but only if the worklist still offers the work.
    """
    from filter.engine.tiers import _by_work

    claims = client.claims(tier=TIER_EXTRACT)
    generations = {c["id"]: (c.get("meta") or {}).get("generation") for c in claims}
    in_mode = {c["id"] for c in claims
               if ((c.get("meta") or {}).get("mode") or "live") == mode}
    rows = [r for r in client.verdicts(TIER_EXTRACT) if r.get("claim_id") in in_mode]
    return {work for work, rows_ in _by_work(TIER_EXTRACT, rows, generations).items()
            if _decide(rows_).get("settles")}


# ---------------------------------------------------------------------------
# The worklist
# ---------------------------------------------------------------------------


def extract_works(con, client: Optional[ClaimsClient], pool_dir: Path,
                  release_id: str, *, only: Optional[Iterable[int]] = None,
                  limit: Optional[int] = None,
                  mode: str = "live",
                  spec_dir: Path = SPEC_DIR,
                  overlay_dir: Optional[Path] = None,
                  aliases: Optional[dict[int, int]] = None,
                  record: Optional[dict] = None,
                  data_dir: Path = DATA_DIR,
                  redo: Optional[Iterable[int]] = None,
                  attempted: Optional[Iterable[int]] = None) -> list[ExtractWork]:
    """Works this tier should extract next, each carrying its whole handoff row.

    The subtraction, in the order the cost of asking rises:

      1. every work a live, current-generation `screen_expensive` PROCEED verdict
         admits (`handoff.decisions()`), minus the ones it discarded. The screen's
         answer is the admission — routing says "this deserves an LLM's attention",
         only the validated pair says "this reaches Stage 3";
      2. minus works this tier has already SETTLED in the current generation;
      3. minus works held by another runner's unexpired extract claim;
      4. minus the two skip lists Stage 3 has always honoured — already in published
         FLoRA, and already in the Supabase validation tables.

    The rows come from `iter_export_rows` + `screen_columns`, in process: the same
    two functions `filter/engine/handoff.py` writes `data/filtered.csv` with, so a
    work extracted through this tier reads exactly the row it would have read from
    that file. Building a CSV first and parsing it back would be a third
    representation of the same thing, and a place for the two to drift.

    *redo* re-admits works the checkpoint would have subtracted (`--redo`).

    *attempted* is what the CURRENT run has already judged, and it is subtracted
    whatever the verdict was. The checkpoint at (2) subtracts only works that
    SETTLED, and `target_pending` deliberately does not settle — so without this the
    rebuild between batches hands every unsettled work straight back and the run
    never ends. Observed 2026-08-07: a 100-work sandbox run judged the same 51
    unsettled works eight times in twenty minutes before it was killed. A re-run is
    how those works get another chance; the same run is not.
    """
    check_release_binding(spec_dir, release_id,
                          (record or {}).get("bundle_hash"),
                          (record or {}).get("alias_release"),
                          overlay_dir, (record or {}).get("overlay_hash"))

    wanted = {int(w) for w in only} if only is not None else None
    reopen = {int(w) for w in (redo or ())}
    done = {int(w) for w in (attempted or ())}

    drop: set[int] = set()
    screen: dict[int, dict] = {}
    held: set[int] = set()
    if client is not None:
        drop, screen = decisions(client)
        held = (client.claimed_work_ids(release_id, TIER_EXTRACT)
                | (settled_work_ids(client, mode) - reopen))

    flora = _flora_skip_dois(data_dir)
    validated_ids, validated_dois = _load_validated_skip(data_dir / VALIDATED_SKIP_NAME)
    conventions = load_conventions()
    specs = load_specs(spec_dir)

    works: list[ExtractWork] = []
    for pile, work, row in iter_export_rows(
            con, pool_dir, list(HANDOFF_PILES), release_id, conventions=conventions,
            specs=specs, aliases=aliases, spec_dir=spec_dir, overlay_dir=overlay_dir):
        if wanted is not None and work not in wanted:
            continue
        if work in drop or work in held or work in done:
            continue
        decision = screen.get(work)
        if client is not None and not decision:
            # Never screened, or screened without settling. A screened-only handoff
            # holds these back; so does this.
            continue
        if decision:
            row.update(screen_columns(row, decision))
            if decision.get("record_type"):
                row["filter_status"] = decision["record_type"]
                row["filter_method"] = "screen"
        if _skipped(row, flora, validated_ids, validated_dois):
            continue
        works.append(_work_from_row(work, pile, row))
        if limit is not None and len(works) >= limit:
            break
    return works


def _skipped(row: dict, flora: set, validated_ids: set, validated_dois: set) -> bool:
    """Whether a skip list already covers this row — the two `_should_skip` checks
    that are about the WORK rather than about a previous run's output CSV."""
    doi = clean_doi(str(row.get("doi_r") or ""))
    if doi and doi in flora:
        return True
    work = _validated_work_id(row.get("openalex_id_r"))
    return bool((work is not None and work in validated_ids)
                or (doi and doi in validated_dois))


# ---------------------------------------------------------------------------
# Pricing (issue #146 §6)
# ---------------------------------------------------------------------------
# What a row costs depends almost entirely on how far down the ladder it goes, and
# the ladder returns at its first success — so an estimate is a weighted sum over
# rungs, not a per-row constant like the screens'.
#
# REACH is measured, not assumed: the shares below are the link_method distribution
# of `data/extracted.csv` on 2026-08-06, counted off the file (285 rows —
# llm_references 81, llm_cited_candidates 51, llm_fulltext 9, and 144 rows a
# deterministic rule resolved: author_year_match_legacy 115,
# single_candidate_after_requery 28, same_author_year_title_overlap 1). They are a
# starting point and nothing more: that file is the output of runs made under an
# older ladder, and the numbers to replace them with are the rung shares in
# `cache/engine/runs/extract-*.json` once this tier has run.
EXTRACT_RUNG_REACH = {
    "deterministic": 0.505,   # 144/285 — resolved before any LLM rung
    "abstract":      0.179,   # 51/285  — llm_cited_candidates
    "references":    0.284,   # 81/285  — llm_references
    "fulltext":      0.032,   # 9/285   — the PDF was acquired, parsed and read
}
# Tokens one row sends and receives AT the rung it stops at, including everything
# above it that was already paid for. The combined prompt asks target and outcome in
# one call, so there is one call per rung reached, not two.
EXTRACT_RUNG_TOKENS = {
    "deterministic": (0, 0),
    "abstract":      (3_000, 700),
    "references":    (9_000, 700),
    "fulltext":      (40_000, 900),
}
# Rough list prices per 1,000 tokens for LINKING_MODEL / OUTCOME_MODEL, which are the
# same id today. Same status as the screens' table in `filter/engine/tiers.py`: they
# answer "is this run $30 or $3,000" before it starts and are not a billing record.
# Update them in the same commit as a price or a model change.
EXTRACT_PRICE_PER_1K_IN = 0.00025
EXTRACT_PRICE_PER_1K_OUT = 0.00200
# The PDF parse call, charged once per row that acquires a document. A whole PDF goes
# to PDF_PARSE_MODEL, so it is priced separately and it dominates a fulltext row.
EXTRACT_PDF_PARSE_USD = 0.0120

# OpenAlex credits, in the units CLAUDE.md's table gives (a filter query is 1).
# Reported as CREDITS and never converted: OpenAlex bills against a daily credit
# budget that resets at midnight UTC, so a dollar figure would answer a question
# nobody is asked at the point of spending.
EXTRACT_OA_CREDITS = {"filter": 1, "search": 10, "content": 100}
# Per row: the candidate/reference fetches (filter queries), and the DOI verification
# searches. `_verify_row` issues up to three free-text searches at 10× each, and only
# on a row that resolved something — hence the multiplication by the resolved share.
EXTRACT_OA_FILTER_PER_ROW = 3
EXTRACT_OA_SEARCH_PER_RESOLVED_ROW = 2
# What share of rows resolve an original at all, and therefore reach verification.
# 285 of the 285 rows in data/extracted.csv carry a resolved link_method, because
# sanity_check moves every unresolved row OUT of that file — so it cannot be read off
# it, and this is the one number here that is a judgement rather than a measurement.
# The ten set-aside CSVs beside it hold 1,176 rows against those 285, which is where
# 0.20 comes from: 285/(285+1176) = 0.195.
EXTRACT_RESOLVED_SHARE = 0.20


def estimate(works: list[Work]) -> dict:
    """What extracting *works* would cost, by rung, before anything is claimed."""
    rows = len(works)
    by_rung = {}
    usd = 0.0
    for rung, share in EXTRACT_RUNG_REACH.items():
        n = rows * share
        tokens_in, tokens_out = EXTRACT_RUNG_TOKENS[rung]
        cost = n * (tokens_in / 1000 * EXTRACT_PRICE_PER_1K_IN
                    + tokens_out / 1000 * EXTRACT_PRICE_PER_1K_OUT)
        if rung == "fulltext":
            cost += n * EXTRACT_PDF_PARSE_USD
        by_rung[rung] = {"rows": round(n, 1), "usd": round(cost, 4)}
        usd += cost

    credits = (rows * EXTRACT_OA_FILTER_PER_ROW * EXTRACT_OA_CREDITS["filter"]
               + rows * EXTRACT_RESOLVED_SHARE * EXTRACT_OA_SEARCH_PER_RESOLVED_ROW
               * EXTRACT_OA_CREDITS["search"])
    return {
        "tier": TIER_EXTRACT,
        "rows": rows,
        "usd": round(usd, 4),
        "usd_per_100k": round(usd / rows * 100_000, 2) if rows else 0.0,
        "oa_credits": int(round(credits)),
        "by_rung": by_rung,
    }


def _usd(amount: float) -> str:
    """Money, at a precision that does not round a real cost to nothing."""
    return f"${amount:,.2f}" if amount >= 1 else f"${amount:.4f}"


def render_estimate(est: dict) -> str:
    lines = [
        "DRY RUN — nothing claimed, nothing fetched, nothing spent.",
        f"  tier          {est['tier']}  ({LINKING_MODEL} + {PDF_PARSE_MODEL})",
        f"  rows          {est['rows']:,}",
        f"  generation    {extract_generation()}",
        "  by rung (reach measured off data/extracted.csv; re-measure from run reports):",
    ]
    for rung in EXTRACT_RUNG_REACH:
        detail = est["by_rung"][rung]
        lines.append(f"    {rung:<14} {detail['rows']:>8,.1f} row(s)  "
                     f"{_usd(detail['usd']):>10}")
    lines += [
        f"  {est['rows']:,} row(s) → tier {est['tier']} ≈ {_usd(est['usd'])}",
        f"  scale check: {_usd(est['usd_per_100k'])} per 100,000 rows",
        # Its own line, in credits: OpenAlex bills a daily credit budget that resets
        # at midnight UTC, and converting it to dollars would answer a question
        # nobody asks at the point of spending (CLAUDE.md, API Budgets).
        f"  OpenAlex      {est['oa_credits']:,} credit(s)  "
        f"({EXTRACT_OA_FILTER_PER_ROW} filter quer(ies)/row + up to "
        f"{EXTRACT_OA_SEARCH_PER_RESOLVED_ROW} verification search(es) at "
        f"{EXTRACT_OA_CREDITS['search']}× on a resolved row)",
        "  Re-run with --run to claim and spend.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------

EXTRACT = register_tier(TierSpec(
    name=TIER_EXTRACT,
    judge=_judge,
    decide=_decide,
    generation=extract_generation,
    accepts_legacy=_accepts_legacy,
    estimate=estimate,
    render_estimate=render_estimate,
    # Stage 3's own concurrency, not the engine tiers': a work here is a PDF
    # download and a full-text call, and `EXTRACT_WORKERS` is the number that has
    # been tuned against those.
    workers=EXTRACT_WORKERS,
    ttl_seconds=EXTRACT_CLAIM_TTL_MINUTES * 60,
))


# ---------------------------------------------------------------------------
# The heartbeat
# ---------------------------------------------------------------------------


class _Heartbeat:
    """Renew one claim's lease until the batch ends, on a daemon thread.

    The lease is 45 minutes and a batch of 200 works is longer than that, so
    something has to renew it or the batch would finish holding nothing. Renewing
    every third of the TTL means two consecutive failures still leave a renewal
    before expiry.

    `ClaimLeaseLost` is the one failure that is not transient: the lease is already
    gone, the works are re-claimable and another runner may hold them. The flag is
    raised and the CALLER stops after the current `run_tier` — the batch already in
    flight finishes and records what it paid for, and no further batch is claimed.
    Verdicts already written stand: expiry frees works, it never retracts evidence.
    """

    def __init__(self, client: ClaimsClient, claim_id: str,
                 ttl_seconds: Optional[int] = None) -> None:
        self._client = client
        self._claim_id = claim_id
        self._ttl = ttl_seconds or _ttl_seconds()
        self._interval = max(1.0, self._ttl / 3)
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.lost = False

    def __enter__(self) -> "_Heartbeat":
        self._thread = threading.Thread(target=self._beat, name="extract-heartbeat",
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _beat(self) -> None:
        while not self._done.wait(self._interval):
            try:
                self._client.renew_claim(self._claim_id, self._ttl)
            except ClaimLeaseLost:
                log.error("extract claim %s lost its lease — another runner may hold "
                          "these works. Finishing the batch in flight and stopping.",
                          self._claim_id[:12])
                self.lost = True
                return
            except ClaimsError as exc:
                # A transport failure is not a lost lease. The next beat tries again,
                # and there are three of them before the lease could actually expire.
                log.warning("extract claim renewal failed (%s) — retrying in %.0fs",
                            exc, self._interval)


# ---------------------------------------------------------------------------
# The run loop
# ---------------------------------------------------------------------------


def run_extract_tier(con, client: Optional[ClaimsClient], release_id: str, *,
                     mode: str = "live", batch_label: str = "",
                     limit: Optional[int] = None,
                     only: Optional[Iterable[int]] = None,
                     redo: Optional[Iterable[int]] = None,
                     pool_dir: Path = SNAPSHOT_POOL_DIR,
                     spec_dir: Path = SPEC_DIR,
                     overlay_dir: Optional[Path] = None,
                     aliases: Optional[dict[int, int]] = None,
                     record: Optional[dict] = None,
                     run: bool = False,
                     cache_dir: Optional[Path] = None,
                     batch_size: int = EXTRACT_CLAIM_BATCH) -> dict:
    """Claim and extract the worklist, `batch_size` works at a time.

    The outer loop is what makes a long campaign resumable at batch granularity: each
    batch is claimed, run and released on its own, and the worklist is REBUILT
    between batches so that verdicts another runner wrote in the meantime are
    subtracted before the next claim rather than fought over in the RPC.

    A dry run needs no state authority and claims nothing — it prints one estimate
    over the whole worklist, because the question a dry run answers is what the
    campaign costs, not what its first 200 rows cost.
    """
    works = extract_works(con, client, pool_dir, release_id, only=only, limit=limit,
                          mode=mode, spec_dir=spec_dir, overlay_dir=overlay_dir,
                          aliases=aliases, record=record, redo=redo)
    if not run:
        print(render_estimate(estimate(works)))
        return {"tier": TIER_EXTRACT, "mode": mode, "dry_run": True,
                "estimate": estimate(works), "batches": 0, "decided": 0,
                "outcomes": {}}

    if client is None:
        raise SystemExit("the extract tier claims before it spends, so --run needs a "
                         "state authority: set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
    if not works:
        raise SystemExit("nothing to extract: every admitted work is already settled, "
                         "claimed or skipped.")

    total = {"tier": TIER_EXTRACT, "mode": mode, "dry_run": False, "batches": 0,
             "decided": 0, "outcomes": {}, "verdicts": 0, "claims": [],
             "generation": extract_generation(), "release_id": release_id}
    done = 0
    attempted: set[int] = set()
    superseded = _supersedable(client, redo, mode) if redo else {}
    reopened = {int(w) for w in (redo or ())}
    while works:
        batch = works[:batch_size]
        report = _run_batch(EXTRACT, client, release_id, batch, mode=mode,
                            batch_label=batch_label, cache_dir=cache_dir)
        total["batches"] += 1
        total["decided"] += report["decided"]
        total["verdicts"] += report.get("verdicts", 0)
        total["claims"].append(report["claim_id"])
        for outcome, count in report["outcomes"].items():
            total["outcomes"][outcome] = total["outcomes"].get(outcome, 0) + count
        done += len(batch)
        attempted |= {w.work_id for w in batch}
        if report.get("lease_lost"):
            total["stopped"] = "claim lease lost"
            break
        if limit is not None and done >= limit:
            break
        remaining = None if limit is None else limit - done
        # A redone work must stop being re-admitted once it HAS been redone, or the
        # rebuild hands it back every batch and the run never ends: `redo` is exactly
        # the set the settled-work subtraction is told to ignore. Observed 2026-08-07,
        # `--redo` over 29 works re-extracted them nine times in ten minutes before the
        # run was killed. Works named in --redo that this batch has not reached yet
        # stay admitted, so a redo set larger than one batch still completes.
        reopened -= {w.work_id for w in batch}
        works = extract_works(con, client, pool_dir, release_id, only=only,
                              limit=remaining, mode=mode, spec_dir=spec_dir,
                              overlay_dir=overlay_dir, aliases=aliases,
                              record=record, redo=reopened, attempted=attempted)

    if superseded:
        total["superseded"] = _supersede(client, superseded)
    return total


def _run_batch(spec: TierSpec, client: ClaimsClient, release_id: str,
               works: list[ExtractWork], *, mode: str, batch_label: str,
               cache_dir: Optional[Path]) -> dict:
    """One claimed batch, with its lease held open by a heartbeat.

    `run_tier` takes the claim, so the id is not knowable here until it returns —
    which is after the batch, and hours too late to start renewing. `on_claim` is
    the hook that fixes the ordering: it fires the moment the claim is taken and
    before the first work is judged.
    """
    beat: dict = {}

    def started(claim_id: str) -> None:
        beat["heartbeat"] = _Heartbeat(client, claim_id, _ttl_seconds())
        beat["heartbeat"].__enter__()

    try:
        report = run_tier(spec, client, release_id, works, mode=mode,
                          batch_label=batch_label, run=True, cache_dir=cache_dir,
                          on_claim=started)
    finally:
        heartbeat = beat.get("heartbeat")
        if heartbeat is not None:
            heartbeat.__exit__(None, None, None)
    report["lease_lost"] = bool(beat.get("heartbeat") and beat["heartbeat"].lost)
    return report


def _supersedable(client: ClaimsClient, redo: Iterable[int],
                  mode: str = "live") -> dict[int, str]:
    """The *mode* result row id of each work `--redo` is about to re-extract.

    Read BEFORE the re-run, because afterwards the "previous" row is whichever of
    two rows sorts earlier and the query cannot tell them apart by anything the
    caller asked for.

    Restricted to the run's own mode, for the reason `settled_work_ids` is: a
    sandbox re-run must not touch live state. Without the filter a
    `--mode validation --redo` marked the work's LIVE result row superseded, and the
    export — which reads live rows — silently lost it.
    """
    from filter.engine.tiers import _by_work

    wanted = {int(w) for w in redo}
    claims = client.claims(tier=TIER_EXTRACT)
    generations = {c["id"]: (c.get("meta") or {}).get("generation") for c in claims}
    in_mode = {c["id"] for c in claims
               if ((c.get("meta") or {}).get("mode") or "live") == mode}
    rows = [r for r in client.verdicts(TIER_EXTRACT) if r.get("claim_id") in in_mode]
    out: dict[int, str] = {}
    for work, work_rows in _by_work(TIER_EXTRACT, rows, generations).items():
        row = (_decide(work_rows) or {}).get("row") or {}
        if work in wanted and row.get("id"):
            out[work] = str(row["id"])
    return out


def _supersede(client: ClaimsClient, previous: dict[int, str]) -> int:
    """Point each re-extracted work's OLD result row at its new one.

    The old row stays — `engine_verdicts` is append-only evidence and it is what was
    believed when it was spent on — but it stops being read: `ClaimsClient.verdicts()`
    filters `superseded_by is null`, so the export and the checkpoint see one answer
    per work.
    """
    from filter.engine.tiers import checkpoint_decisions

    n = 0
    current = checkpoint_decisions(client, TIER_EXTRACT)
    for work, old_id in previous.items():
        new_row = (current.get(work) or {}).get("row") or {}
        new_id = str(new_row.get("id") or "")
        if new_id and new_id != old_id:
            client.supersede_verdict(old_id, new_id)
            n += 1
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _ids(value: Optional[str]) -> Optional[list[int]]:
    if not value:
        return None
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def _overlay_dir(args) -> Optional[Path]:
    """`--overlay` > the standard dir when it holds chunks > none.

    Same rule as the engine CLI's `_overlay()`: an absent flag means "use the
    overlay if there is one", never "run bare" — a release routed with an overlay
    refuses to bind against a bare read, which is exactly the mismatch the check
    exists to catch. `--no-overlay` asks for a bare-pool run out loud.
    """
    if args.no_overlay:
        return None
    if args.overlay:
        return args.overlay
    return OVERLAY_DIR if chunk_paths(OVERLAY_DIR) else None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m extract.tier",
        description="Run Stage 3's resolution ladder as a claimed engine tier. "
                    "Dry run by default: nothing is claimed and nothing is spent "
                    "without --run.")
    parser.add_argument("--run", action="store_true",
                        help="Claim the works and spend. Without it, print an estimate.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N works.")
    parser.add_argument("--batch-label", default="",
                        help="Recorded in each claim's meta, to name a campaign.")
    parser.add_argument("--mode", choices=("live", "validation"), default="live",
                        help="validation records verdicts the live export ignores.")
    parser.add_argument("--only", default=None,
                        help="Comma-separated work ids: extract only these.")
    parser.add_argument("--redo", default=None,
                        help="Comma-separated work ids: re-extract despite the "
                             "checkpoint, and supersede each work's previous result row.")
    parser.add_argument("--release", default=None,
                        help="Routing release (default: the store's only one).")
    parser.add_argument("--overlay", type=Path, default=None,
                        help="Overlay chunk dir (default: the standard dir when "
                             "it holds chunks).")
    parser.add_argument("--no-overlay", action="store_true",
                        help="Read the bare pool even when an overlay exists.")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    parser.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    parser.add_argument("--spec-dir", type=Path, default=SPEC_DIR)
    parser.add_argument("--batch-size", type=int, default=EXTRACT_CLAIM_BATCH,
                        help=f"Works per claim (default {EXTRACT_CLAIM_BATCH}).")
    args = parser.parse_args(argv)

    # An operator condition with two different fixes ("route first" vs "wait for the
    # writer"), so the message is the whole answer — not a traceback. Same handling
    # as `filter/engine/cli.py`.
    try:
        con = open_store(args.store, read_only=True)
    except StoreUnavailable as exc:
        raise SystemExit(str(exc))
    if args.release:
        release_id = resolve_release(con, args.release)
    else:
        present = releases(con)
        if len(present) != 1:
            raise SystemExit(f"the store holds {len(present)} releases — name one "
                             "with --release")
        release_id = present[0]
    try:
        record = read_release(release_id, cache_dir=args.store.parent)
    except (FileNotFoundError, OSError):
        record = {}

    client: Optional[ClaimsClient] = None
    try:
        client = ClaimsClient()
    except ClaimsNotConfigured as exc:
        if args.run:
            raise SystemExit(f"{exc}. Set SUPABASE_URL and SUPABASE_SERVICE_KEY, or "
                             "drop --run for a dry run.")
        print(f"no state authority configured ({exc}) — estimating over the whole "
              "admitted pile, with no verdicts or claims subtracted.")

    try:
        report = run_extract_tier(
            con, client, release_id, mode=args.mode, batch_label=args.batch_label,
            limit=args.limit, only=_ids(args.only), redo=_ids(args.redo),
            pool_dir=args.pool, spec_dir=args.spec_dir,
            overlay_dir=_overlay_dir(args),
            aliases=load_aliases(args.spec_dir / ALIASES_FILENAME),
            record=record, run=args.run, cache_dir=args.store.parent,
            batch_size=args.batch_size)
    except StaleBundleError as exc:
        raise SystemExit(str(exc))
    finally:
        con.close()

    if report.get("dry_run"):
        return 0
    print(f"tier {report['tier']}  mode {report['mode']}  "
          f"generation {report['generation']}")
    print(f"  {report['batches']:,} batch(es), {report['decided']:,} work(s) "
          f"extracted, {report['verdicts']:,} verdict row(s)")
    for outcome, count in sorted(report["outcomes"].items()):
        print(f"  {outcome:<20} {count:,}")
    if report.get("superseded"):
        print(f"  superseded {report['superseded']:,} previous result row(s)")
    if report.get("stopped"):
        print(f"  STOPPED: {report['stopped']}")
    print("  Render the CSV with: python -m extract.export")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# Retroactive corrections
# ---------------------------------------------------------------------------


def supersede_targets(client: ClaimsClient, patches: dict[str, dict], *,
                      batch_label: str, mode: str = "live") -> dict:
    """Correct stored rows by writing a NEW result verdict that supersedes the old one.

    *patches* is `pair_id → {column: corrected value}`. The pair id is the row
    identity every stored target carries, so a correction names the row it is about
    rather than a position in a file.

    This is how the two retroactive tools (`extract/audit_dois.py`,
    `extract/backfill_authors.py`) write. They used to edit `data/extracted.csv` in
    place, which stopped meaning anything the moment the export became that file's
    only writer: the next render rebuilds every row from the stored payload, so an
    edit to the file would simply disappear. The correction goes where the row comes
    from.

    The shape is the tier's own, for the tier's reasons: CLAIM before writing, so two
    correction runs cannot both rewrite one work; one new result row per work,
    carrying the previous row's verdict, generation and confidence, because a
    corrected field is not a re-extraction and must not read as one; then
    `supersede_verdict`, so the old row stays as evidence of what was believed and
    stops being read.

    The work id is read off the verdict rows themselves — never looked up in
    OpenAlex. A stored payload is the only thing that knows which work a row came
    from, and asking a registry would be a second, guessable answer.

    Returns `{"works": n, "rows": n, "unmatched": [pair_id, …], "claim": id}`.
    """
    from extract.export import latest_results

    results, _ = latest_results(client, mode=mode)
    # pair_id → (work_id, target index). A work's targets are its payload's rows.
    located: dict[str, tuple[int, int]] = {}
    for work, result in results.items():
        for index, target in enumerate((result.get("payload") or {}).get("targets") or []):
            pair = str(target.get("pair_id") or "")
            if pair:
                located.setdefault(pair, (int(work), index))

    by_work: dict[int, list[tuple[int, dict]]] = {}
    unmatched: list[str] = []
    for pair, changes in patches.items():
        where = located.get(str(pair))
        if where is None:
            unmatched.append(str(pair))
            continue
        by_work.setdefault(where[0], []).append((where[1], changes))
    if not by_work:
        return {"works": 0, "rows": 0, "unmatched": unmatched, "claim": ""}

    # The release the works were extracted under, read off the claim that holds each
    # old result row: a correction belongs to the same release as the answer it
    # corrects.
    claim_release = {claim["id"]: claim["release_id"]
                     for claim in client.claims(tier=TIER_EXTRACT)}
    spanned = {claim_release.get(results[work].get("claim_id")) for work in by_work}
    spanned.discard(None)
    if len(spanned) != 1:
        raise SystemExit(
            f"the works to correct span {len(spanned)} release(s); correct them one "
            "release at a time, so each claim names the release its rows came from.")
    release_id = spanned.pop()

    # An empty pile: `engine_claim_items.pile` records the ROUTING pile a work sat in
    # at claim time, and a correction claims works for a reason routing knows nothing
    # about. Inventing a pile name here would put a value in that column that no rule
    # book ever produced.
    claim_id = client.claim(release_id, TIER_EXTRACT,
                            [(work, "") for work in sorted(by_work)],
                            meta={"mode": mode, "batch_label": batch_label,
                                  "kind": "correction",
                                  "generation": extract_generation()},
                            ttl_seconds=_ttl_seconds())
    corrected_rows = 0
    try:
        for work, changes in sorted(by_work.items()):
            old = results[work]
            payload = json.loads(json.dumps(old.get("payload") or {}))
            targets = payload.get("targets") or []
            for index, patch in changes:
                if index < len(targets):
                    targets[index].update(patch)
                    corrected_rows += 1
            new_id = client.record_verdict(
                claim_id=claim_id, work_id=int(work), tier=TIER_EXTRACT,
                # Everything but the payload is the old row's: the ending, the
                # generation and the confidence are unchanged by a field correction.
                verdict=str(old.get("verdict") or ""),
                model=str(old.get("model") or ""),
                prompt_hash=str(old.get("prompt_hash") or ""),
                confidence=str(old.get("confidence") or ""),
                quote=str(old.get("quote") or ""),
                cost={"correction": batch_label},
                payload=payload)
            client.supersede_verdict(str(old["id"]), new_id)
    finally:
        client.release_claim(claim_id, "complete")
    return {"works": len(by_work), "rows": corrected_rows, "unmatched": unmatched,
            "claim": claim_id}
