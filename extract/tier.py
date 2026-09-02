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
supersedes their previous result row; `--redo-status unidentified_original` names a
POPULATION instead of ids, which is how a ladder change reopens the rows it was
written for. Editing a prompt or a model mints a new generation, which reopens every
work at once — a ladder bump does not, since 2026-08-10.

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
change, which is the bulk of it. `render_payload()`'s `_repair_pair_id()` is the one
deliberate exception: a narrow, unambiguous repair of the replication-side fallback
on payloads written before it existed, not a general recompute — see its docstring.

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
                           OVERLAY_DIR, PDF_PARSE_EFFORT, PDF_PARSE_MODEL,
                           SNAPSHOT_POOL_DIR, log)
from shared.flora_skip import (VALIDATED_SKIP_NAME,
                               default_flora_skip_dois as _flora_skip_dois,
                               load_validated_skip as _load_validated_skip,
                               validated_work_id as _validated_work_id)
from shared.llm_client import cache_model_id
from shared.schema import (EXTRACTED_COLS, FILTERED_COLS, LINK_METHOD_VALUES,
                           PROVISIONAL_LINK_METHODS, RESOLVED_LINK_METHODS,
                           SCREEN_COLS, make_pair_id)
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

# The two endings that do not SETTLE a work. Every other ending does, and takes the
# work out of the worklist for good. These two are reopenable, at two different
# prices: `api_error` records a transient provider failure and re-offers on the next
# run, while `target_pending` rests until something names it (`_resting`) — the same
# evidence through the same code buys the same answer.
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
# What a result row was produced BY, in one hash: the prompts the three LLM rungs and
# the two standalone coders ask, and the models that answer them at the efforts their
# call sites send. Same contract as `screening_generation()` in
# `filter/engine/tiers.py` — a verdict only answers the question the code asks today
# if the code still asks that question — and read at CALL time so a changed constant
# is seen without a re-import.
#
# What is deliberately NOT in it: the pool, the release, the rule book. A verdict
# follows the WORK. A re-route mints a new release id without changing a single
# thing about how a paper's original is found.
#
# Nor is EXTRACT_LADDER_VERSION, since 2026-08-10. It was, and every ladder bump
# therefore reopened every settled work — 3,025 of them, a whole campaign's wall
# clock, to fix the population the bump was actually written for: ladder 22 was
# measured over the 105 `unidentified_original` rows and 23 addressed the same class.
# A ladder change alters HOW an original is found, not WHAT the pipeline asks, and it
# reaches a population its author knows at the time of writing. So the reopen is now
# named rather than inferred: `--redo-status unidentified_original` (or `--redo` with
# work ids), and each changelog entry in `link_original.py` carries the command that
# reopens what it fixed. The version is still stamped on every row, which is where a
# reader learns what produced a verdict.
#
# This costs no safety that was being enforced: the export carries forward rows from a
# superseded generation by default, so a stale-ladder verdict shipped either way. What
# it stopped doing is re-buying them. `--current-generation-only` is the separate lever
# for the render, and the honest one for that question.
_GENERATION_PROMPTS = ("build_target_outcome_prompt",
                       "build_repro_target_outcome_prompt",
                       "build_outcome_prompt",
                       "build_repro_outcome_prompt",
                       # The pooled-candidate pick DECIDES a link now, so an edit to it
                       # changes what a row concludes and must reopen the works it
                       # decided. It was outside the fingerprint while it was a
                       # tie-breaker of last resort; it is not one any more.
                       "build_author_year_pick_prompt",
                       # The keyed-record confirm (issue #186 Shape 1) can demote a
                       # resolved row to keyed_link_disputed, so an edit to it changes
                       # what a row concludes and must reopen the works it decided.
                       "build_keyed_confirm_prompt",
                       # The search-link grade (issues #183/#186 Shape 2) sets
                       # `link_confidence` on every llm_title_search and
                       # llm_author_year_search row, so an edit to it changes a
                       # shipped field and must reopen the works it graded.
                       "build_search_confirm_prompt",
                       # The two reference-extraction prompts (`shared/grobid.py`).
                       # The reference list they produce IS the key namespace the
                       # reference-list rung picks a target out of, so an edit to
                       # either changes which originals a row can name.
                       "PDF_REFERENCES_PROMPT",
                       "PDF_IMAGE_REFERENCES_PROMPT")


# ── Declared answer-preserving prompt edits (issue #171) ─────────────────────
# `prompt: (version_after_the_edit, version_before_it)`. The fingerprint reports the
# BEFORE version while the prompt hashes to exactly the reviewed AFTER version, so an
# edit a maintainer reviewed and judged answer-preserving does not reopen every work
# this tier has already settled. The pair is the guard: a further edit produces a
# third version, matches nothing here, and moves the generation as it should.
#
# An entry is declared only for an edit that provably cannot change an answer — a
# category renamed, a typo, a reordering the model cannot read differently — and the
# recipe a declaration has to follow whole is "Editing a prompt without invalidating
# its cache" in CLAUDE.md.
_GENERATION_PROMPT_EQUIVALENCES: dict[str, tuple[str, str]] = {}


def generation_inputs() -> dict:
    """Everything `extract_generation()` hashes, as a plain dict.

    Public so the pinning test can assert the INPUTS rather than the digest: a bump
    should read as a diff of what changed, not as one opaque hex string becoming
    another.
    """
    from shared.prompts import prompt_version

    def version(name: str) -> str:
        current = prompt_version(name)
        after, before = _GENERATION_PROMPT_EQUIVALENCES.get(name, ("", ""))
        return before if current == after else current

    return {
        "prompts": {name: version(name) for name in _GENERATION_PROMPTS},
        "models": {
            "linking": cache_model_id(LINKING_MODEL, LINKING_EFFORT),
            "outcome": cache_model_id(OUTCOME_MODEL, OUTCOME_EFFORT),
            "pdf_parse": cache_model_id(PDF_PARSE_MODEL, PDF_PARSE_EFFORT),
        },
    }


def extract_generation() -> str:
    """Fingerprint of the prompts and models this tier extracts with now."""
    payload = json.dumps(generation_inputs(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── Declared equivalent generations ──────────────────────────────────────────
# `current: (generations whose verdicts still answer the current question)`. The
# fingerprint is a hash of a dict, so REMOVING a key from that dict moves it exactly as
# a prompt edit would — as the removal of `EXTRACT_LADDER_VERSION` did: every verdict
# on the server carried `{ladder: N, prompts…, models…}`, the tier now asks
# `{prompts…, models…}` and matched none of them, so 3,025 settled works reopened at
# once. An entry belongs here when the move left what the tier ASKS untouched, or when
# it changes a field over a population its author can name on the command line
# (`--redo-status …`) for a fraction of a campaign's spend.
#
# Keyed by the CURRENT generation, which is what makes a declaration self-limiting: a
# later prompt or model change produces a digest that matches no key here, and every
# work reopens strictly, as it should.
_GENERATION_EQUIVALENCES: dict[str, tuple[str, ...]] = {
    # 2026-08-15: LINKING_MODEL and OUTCOME_MODEL moved from gpt-5.4-mini to
    # gpt-5.6-luna part-way through the 2026-08 re-extract, with 1,567 of 5,928
    # works settled under mini. Those verdicts stay current: the question is the
    # same, the mini answers were bought under an evaluated configuration, and every
    # verdict stamps the models that produced it. The residue is bought under luna.
    "5b716d061bb336f5": ("dd7572887420ef65",),
}


def equivalent_generations() -> tuple[str, ...]:
    """Older generations whose verdicts the current one still accepts."""
    return _GENERATION_EQUIVALENCES.get(extract_generation(), ())


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
    # An errored target first, ahead of everything: a work with one original found and
    # another whose search never completed has not been answered, and settling it
    # closes the second original for good. Flagged twice in review and deferred twice;
    # the trade the primary metric asks for is that an incomplete answer settles
    # nothing, and `api_error` is the ending a re-run is meant to redo.
    if API_ERROR in methods:
        return API_ERROR
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
    change (a `paper_type` the screen retyped, a normalised `year_r`) is stored on
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


# Column names a stored payload may carry under an older name → today's name.
# The one declared exception to "no backward-compatibility shims", and it is
# declared for the same reason `read_cache_migrating` is: a permanent verdict is
# production data that cannot be re-bought freely — re-extracting every settled
# work to move a key would cost a campaign's worth of provider and OpenAlex spend
# — and the answer under the old key is provably the answer under the new one,
# because only the NAME moved (issue #93). Read-side only: nothing writes an old
# key, so the map shrinks as generations turn over rather than spreading.
_PAYLOAD_COLUMN_RENAMES = {"filter_status": "paper_type"}


def _renamed_columns(stored: dict) -> dict:
    """*stored* with any pre-rename column name mapped to today's."""
    if not any(old in stored for old in _PAYLOAD_COLUMN_RENAMES):
        return stored
    row = dict(stored)
    for old, new in _PAYLOAD_COLUMN_RENAMES.items():
        value = row.pop(old, None)
        if value is not None and not row.get(new):
            row[new] = value
    return row


def _repair_pair_id(row: dict) -> None:
    """Add the replication-side fallback to a `pair_id` frozen before it existed.

    Every write path now calls `make_pair_id()` with `oa_work_id_r`/`title_r`, so a
    result written today already carries the fallback (`shared/schema.py`). A
    payload written before that fix hashed a blank `doi_r` as the literal "", so
    two distinct DOI-less replications of the same original collided on one
    `pair_id`. This repairs those stored payloads at render time, so the fix
    reaches them without a re-extraction.

    It cannot safely touch the doi_o side the same way: the single- and
    multi-original writers differ in whether a blank `doi_o`'s own fallback
    (`oa_work_id_o`/`title_o`) was used when the row was written, and that choice
    is not recoverable from the row alone once both fields are populated — feeding
    it in unconditionally would re-key a row the single-original writer
    deliberately left alone (see `make_pair_id`'s docstring). So this only acts
    when the doi_o side is unambiguous: `doi_o` present (the fallback fields are
    irrelevant to the hash either way), or `doi_o`, `oa_work_id_o` and `title_o`
    all blank (the second component is "" either way). Anything else is left for a
    live re-extraction (`--redo`) to settle.
    """
    if row.get("doi_r"):
        return
    doi_o = row.get("doi_o", "") or ""
    oa_work_id_o = row.get("oa_work_id_o", "") or ""
    title_o = row.get("title_o", "") or ""
    if not doi_o and (oa_work_id_o or title_o):
        return
    row["pair_id"] = make_pair_id(
        clean_doi(str(row.get("doi_r", "") or "")), doi_o, oa_work_id_o, title_o,
        str(row.get("oa_work_id_r", "") or row.get("openalex_id_r", "") or ""),
        str(row.get("title_r", "") or ""),
    )


def render_payload(payload: dict) -> list[dict]:
    """A stored result payload back as its `EXTRACTED_COLS` rows.

    The inverse of `_result_payload`, and the only place a stored verdict becomes a
    CSV row. Pure: no network, no cache, no store, no pool — which is the property
    the payload shape exists to make true (see the module docstring).

    Both halves of the payload go through `_renamed_columns`: the input row, and
    each target — a target carries a FILTERED_COLS value the run CHANGED, so a work
    the screen retyped holds the paper type there and not in `input`. `pair_id`
    then goes through `_repair_pair_id`, the one deliberate exception to "nothing is
    recomputed at export" (see the module docstring): a targeted repair for the
    replication-side fallback, not a general recompute.
    """
    input_row = _renamed_columns(payload.get("input") or {})
    base = {col: str(input_row.get(col, "") or "") for col in INPUT_COLS
            if col in EXTRACTED_COLS}
    rows = []
    for target in payload.get("targets") or []:
        target = _renamed_columns(target)
        row = {col: "" for col in EXTRACTED_COLS}
        row.update(base)
        row.update({k: v for k, v in target.items() if k in EXTRACTED_COLS})
        _repair_pair_id(row)
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


def _resting(decision: dict) -> bool:
    """True when a current-generation target_pending verdict holds the work back.

    A rested work is subtracted from the worklist exactly as a settled one is, and
    it stays there: re-running the same code over the same evidence buys the same
    answer, so nothing re-offers the work by itself. The row still does not SETTLE,
    which is what keeps the reopeners meaningful — `--redo`, `--redo-status
    target_pending`, and a new generation, which changes what the pipeline asks and
    puts every work back on the list. Without the hold, every run re-bought each
    unresolvable work's OpenAlex queries: the 2026-08-09/10 campaign re-attempted
    ~830 such works on all five of its runs.

    `api_error` is deliberately not rested: it marks a provider's bad minute, and
    the retry is cheap and usually different.
    """
    return decision.get("outcome") == TARGET_PENDING


def settled_work_ids(client: ClaimsClient, mode: str = "live") -> set[int]:
    """Works whose latest current-generation *mode* result row SETTLES them —
    plus the target_pending works, which rest (`_resting`).

    `api_error` is excluded on purpose: it is the ending a re-run is meant to
    redo immediately (`UNSETTLING_VERDICTS`).

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
    out: set[int] = set()
    for work, rows_ in _by_work(TIER_EXTRACT, rows, generations).items():
        decision = _decide(rows_)
        if decision.get("settles") or _resting(decision):
            out.add(work)
    return out


def works_with_status(client: ClaimsClient, statuses: Iterable[str],
                      mode: str = "live") -> set[int]:
    """Works whose latest current-generation *mode* result ended in one of *statuses* —
    what `--redo-status` turns a name into.

    The reopen a ladder bump needs, named instead of inferred. A change to how an
    original is FOUND reaches a population its author already knows: ladder 22 was
    measured over the `unidentified_original` rows before it shipped, and 23 addressed
    the same class. Reopening every settled work to reach them cost a campaign's wall
    clock; this asks for the class.

    A status is one of three things. A RESULT VERDICT — the work's whole ending,
    `RESULT_VERDICTS`. A LINK METHOD, which names a row: `unidentified_original` and
    `keyed_link_disputed` are both `provisional` endings, and a ladder change that
    reaches one does not reach the other. Or `field=value` over any column of the
    exported row, which is what a PROMPT edit needs.

    The third form exists because the first two cannot name what a prompt decides. A
    ladder change alters how an original is FOUND, and its population is a link
    method. A prompt change alters what the model CONCLUDES, and its population is a
    conclusion: `outcome=cannot_be_determined` is every row the outcome prompt could
    not settle, `abstract_r=` every row it had nothing to read. Without it the only
    ways to re-ask a changed prompt were to reopen every settled work — a whole
    campaign's spend — or to leave the old answers standing.

    It pairs with `_GENERATION_EQUIVALENCES`, and neither half works alone: editing a
    prompt moves `extract_generation()`, which reopens everything by itself, so the
    declaration is what holds the settled works still while this names the few to
    re-buy. The recipe is "Re-asking a changed prompt over one population" in
    CLAUDE.md.

    A work matches when ANY row of its result matches, so the finer name is the one to
    reach for. Values are compared against the RENDERED row — what appears in
    extracted.csv — case-insensitively and stripped; an empty value means blank.

    Same reading of the store as `settled_work_ids` — latest result, current
    generation, one mode — because a work the worklist would offer anyway needs no
    reopening. The caller feeds the result to `--redo`, which supersedes the old row.
    """
    from filter.engine.tiers import _by_work

    from shared.schema import EXTRACTED_COLS

    # A token is one of three things. The two bare forms are unchanged; `field=value`
    # is the third, and it exists because the first two cannot name the populations a
    # PROMPT edit reaches. A ladder change reaches a link method; a prompt change
    # reaches whatever the prompt decided — `outcome=cannot_be_determined` is the row
    # the outcome prompt failed to settle, and `abstract_r=` is the row it had nothing
    # to read. Neither is a verdict, and neither could be asked for before.
    wanted = {str(v).strip() for v in statuses if str(v).strip()}
    fields: list[tuple[str, str]] = []
    bare: set[str] = set()
    for token in wanted:
        if "=" in token:
            name, _, value = token.partition("=")
            name = name.strip()
            if name not in EXTRACTED_COLS:
                raise ValueError(
                    f"not a column of the exported row: {name}\n"
                    f"  columns: {', '.join(EXTRACTED_COLS)}")
            # Compared against the RENDERED row, so the value is the one that appears
            # in extracted.csv. An empty value means blank, which is how a row with no
            # abstract is named: `abstract_r=`.
            fields.append((name, value.strip().lower()))
        else:
            bare.add(token)
    unknown = bare - set(RESULT_VERDICTS) - LINK_METHOD_VALUES
    if unknown:
        raise ValueError(
            f"not a result verdict, a link method or a column selector: "
            f"{', '.join(sorted(unknown))}\n"
            f"  verdicts:     {', '.join(RESULT_VERDICTS)}\n"
            f"  link methods: {', '.join(sorted(LINK_METHOD_VALUES))}\n"
            f"  or field=value over any column of extracted.csv, e.g. "
            f"outcome=cannot_be_determined (blank value means empty: abstract_r=)")
    methods = bare & LINK_METHOD_VALUES
    verdicts = bare & set(RESULT_VERDICTS)

    claims = client.claims(tier=TIER_EXTRACT)
    generations = {c["id"]: (c.get("meta") or {}).get("generation") for c in claims}
    in_mode = {c["id"] for c in claims
               if ((c.get("meta") or {}).get("mode") or "live") == mode}
    want_payload = bool(methods or fields)
    rows = [r for r in client.verdicts(TIER_EXTRACT, with_payload=want_payload)
            if r.get("claim_id") in in_mode]

    def _matches(row: dict) -> bool:
        """Whether one rendered row answers to any link method or column selector.

        A work matches when ANY of its rows does, the same rule link methods already
        used: a paper with three originals is one work, and a prompt that failed to
        settle one of them is a reason to re-ask the whole work.
        """
        if str(row.get("link_method") or "") in methods:
            return True
        return any(str(row.get(name) or "").strip().lower() == value
                   for name, value in fields)

    named: set[int] = set()
    for work, rows_ in _by_work(TIER_EXTRACT, rows, generations).items():
        decision = _decide(rows_)
        if decision["outcome"] in verdicts:
            named.add(work)
            continue
        if not want_payload or decision["row"] is None:
            continue
        payload = decision["row"].get("payload") or {}
        if any(_matches(r) for r in render_payload(payload)):
            named.add(work)
    return named


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
                row["paper_type"] = decision["record_type"]
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
# Update them in the same commit as a price or a model change. gpt-5.6-luna at flex
# tier, 2026-08-15: $0.10 / $0.60 per 1M.
EXTRACT_PRICE_PER_1K_IN = 0.00010
EXTRACT_PRICE_PER_1K_OUT = 0.00060
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
    equivalent_generations=equivalent_generations,
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
                     allow_extra_works: bool = False,
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

    *redo* is a NAMED population: it re-admits works the checkpoint subtracted, and a
    run that would also extract works nobody named refuses unless
    *allow_extra_works* says otherwise. `None` means no population was named; an
    empty set means one was named and matched nothing.
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
    if redo is not None and only is None and not allow_extra_works:
        _refuse_unnamed_works({int(w) for w in redo}, works)

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


def _refuse_unnamed_works(named: set[int], works: list[ExtractWork]) -> None:
    """Stop a `--redo`/`--redo-status` run that would extract works nobody named.

    `--redo` ADDS to the worklist; `--only` RESTRICTS it. With a stable generation the
    difference is invisible — every other work is settled and subtracted, so the
    worklist is the named set anyway. A new generation reopens every settled work, the
    subtraction takes nothing away, and the same command becomes a whole campaign.
    That is exactly the moment an operator reaches for `--redo`, so the check is here
    rather than in the operator's head.
    """
    extra = [w.work_id for w in works if w.work_id not in named]
    if not extra:
        return
    ids = ",".join(str(w) for w in sorted(named)) if 0 < len(named) <= 20 else "<ids>"
    raise SystemExit(
        f"--redo/--redo-status named {len(named):,} work(s), but the worklist holds "
        f"{len(works):,} — this run would also extract {len(extra):,} work(s) you did "
        "not name.\n"
        "  --redo and --redo-status ADD to the worklist; they do not restrict it. A "
        "new extract generation (a changed prompt or model) reopens every settled "
        "work, so there is nothing left for the checkpoint to subtract and the "
        "baseline worklist is the whole admitted population.\n"
        f"  Extract exactly the named works:  --only {ids}\n"
        "  Or accept the full worklist:      the same command plus --allow-extra-works")


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
    parser.add_argument("--redo-status", default=None,
                        help="Reopen a POPULATION: a result verdict, a link method, or "
                             "field=value over any column of extracted.csv "
                             "(outcome=cannot_be_determined, abstract_r= for "
                             "blank). Comma-separated. ADDS to the worklist; "
                             "only --only restricts it.")
    parser.add_argument("--allow-extra-works", action="store_true",
                        help="Run the full worklist even though --redo/--redo-status "
                             "named a smaller population.")
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
        release_id = resolve_release(con, args.release, cache_dir=args.store.parent)
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

    redo = set(_ids(args.redo) or ())
    if args.redo_status:
        if client is None:
            raise SystemExit("--redo-status reads the verdicts from the state "
                             "authority; set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        try:
            named = works_with_status(
                client, [s.strip() for s in args.redo_status.split(",")], args.mode)
        except ValueError as exc:
            raise SystemExit(str(exc))
        print(f"--redo-status {args.redo_status}: {len(named):,} work(s) reopened")
        redo |= named

    try:
        report = run_extract_tier(
            con, client, release_id, mode=args.mode, batch_label=args.batch_label,
            limit=args.limit, only=_ids(args.only),
            # An empty list, not None, when a population was named and matched
            # nothing: None means nobody named one, and only that reading lets the
            # unnamed-works refusal fire on a redo that resolved to zero works.
            redo=sorted(redo) if (args.redo or args.redo_status) else None,
            allow_extra_works=args.allow_extra_works,
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
    # Naming the release this run extracted under, because the export renders only
    # what a release admits and a bare invocation refuses in a multi-release store.
    # This line is what a maintainer copies at the end of a paid run.
    print(f"  Render the CSV with: python -m extract.export "
          f"--release {release_id[:12]}")
    # The caches are content-keyed and shareable, so this run's answers are answers
    # no collaborator has to buy again — but only once they are pushed, and nothing
    # else in the pipeline prompts for it. A run that decided nothing bought nothing.
    if report.get("decided"):
        print("  The LLM/API caches are shared — publish this run's answers with: "
              "python -m shared.cache_sync --push")
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
    # pair_id → (work_id, target index). A work's targets are its payload's rows, in
    # the same order `render_payload` renders them, so the index below is keyed off
    # the RENDERED pair_id — the one the CSV and every audit patch actually carry —
    # rather than the raw stored one. A payload written before `_repair_pair_id`
    # existed still holds the legacy hash on disk; indexing on that would leave every
    # such row unmatched the moment its patch arrives keyed by the repaired id.
    located: dict[str, tuple[int, int]] = {}
    for work, result in results.items():
        for index, row in enumerate(render_payload(result.get("payload") or {})):
            pair = str(row.get("pair_id") or "")
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
