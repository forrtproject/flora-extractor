"""The claimed, budget-gated LLM tiers over a routed pile (issue #146 M4).

Rules route; only these tiers admit. Two of them:

  * **`screen_cheap`** — the discard-only two-voter tier `shared/prescreen.py`
    already implements. It is called through that module rather than reimplemented
    here: same prompt, same models, same parsing, same "two explicit noes or the
    row proceeds" rule. What this file adds is the pool-side plumbing — claims,
    a dry run, permanent verdicts — and a MODE, because the tier's zero-miss
    evidence was measured on a post-gate distribution and issue #146 §2 says it
    must be re-validated on this one before it discards autonomously. So
    `mode="validation"` is the default: the votes are recorded and compared
    against the rows' current piles, and nothing is discarded.
  * **`screen_expensive`** — Stage 3's validated front door, `classify_replication()`
    plus `screen_gate()`, run over the pile ahead of Stage 3 so its output can BE
    the handoff rather than a second opinion on it.

Four properties both runners share, and the reasons they are not optional:

  **Claim before spend.** `client.claim()` is called once, for the whole batch,
  before the first voter is asked. A `ClaimConflict` ends the run without a single
  call: two runners spending on the same rows is money, and the RPC is the only
  thing that can see both of them.

  **Dry run by default.** Without `run=True` nothing is claimed, nothing is
  fetched and nothing is spent; the runner prints the row count, the token-length
  distribution of the abstracts it would send and what that costs at
  `shared/config.py`'s rough per-1k prices (§6).

  **Evidence before its verdict.** Each raw response is written to
  `cache/engine/responses/<hash>.json` BEFORE `record_verdict` inserts the row that
  names it (§4). The row is inserted `response_pending_upload`; the blob travels to
  Hugging Face in a MULTI-FILE commit later in the run, and only a commit that was
  actually accepted turns the state into `uploaded` (`_ResponseUploader`). The
  state is about the blob, not about our intentions for it.

  **Per-work checkpointing, in parallel.** A verdict is written as each work is
  decided, so an interrupted run's claim is failed and the next run re-claims only
  the works that have no verdict yet. Works are independent, so they are judged by
  a pool of `ENGINE_TIER_WORKERS` threads; what bounds the request rate is the
  per-provider limiter in `shared/llm_client.py`, not the loop's shape. Nothing
  local is checkpointed: the server holds the state, and a local file that
  disagreed with it would be the thing we trusted.

  **A verdict belongs to a generation.** Each claim's meta carries
  `screening_generation(tier)` — the hash of the tier's voter pair and its prompt
  — and every read of the verdicts filters on it. Change a voter model or the
  classify prompt and the old answers stop counting: the works are claimable
  again and no longer steer the handoff. Rows written before the field exists are
  grandfathered on their recorded models (`_generation_current`).

Neither runner touches the routing table. A live `screen_cheap` discard is applied
where the rows leave the engine (`handoff.py`), because routing is derived data —
recomputed from pool and specs — and a decision written into it would be erased by
the next `route` run.
"""

import datetime
import hashlib
import json
import logging
import os
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from filter.engine.claims import (PENDING_UPLOAD, ClaimConflict, ClaimsClient,
                                  ClaimsError, UnknownRelease)
from filter.engine.pool_reader import iter_pool_batches
from filter.engine.release import read_release
from filter.engine.workids import resolve, work_id
from shared import token_counter
from shared.config import (ENGINE_CACHE_DIR, ENGINE_TIER_HF_UPLOAD,
                           ENGINE_TIER_WORKERS, FLORA_HF_COMMIT_BATCH,
                           FLORA_POOL_REPO, PRESCREEN_MODEL_1, PRESCREEN_MODEL_2,
                           SCREENING_MODEL_1, SCREENING_MODEL_2, SNAPSHOT_POOL_DIR)
from shared.llm_client import (SCREEN_EVIDENCE_SEP, classify_replication,
                               screen_gate, screen_voters)
from shared.prescreen import prescreen_bypass, prescreen_vote, prescreen_voters
from shared.token_usage import TokenBudgetExhausted, check_openai_budget
from shared.utils import clean_doi

log = logging.getLogger(__name__)

# ── The dry run's price list (issue #146 §6) ──────────────────────────────────
# Rough list prices per 1,000 tokens, SUMMED OVER A TIER'S TWO VOTERS, so a tier's
# estimate is one multiplication per row. They answer "is this run $3 or $3,000?"
# before it starts and are deliberately not a billing record — what a run actually
# cost is read from cache/token_usage.json afterwards. They live here, next to the
# estimate() that is their only reader, and move only when a price or a voter model
# does: update them in the same commit as the change they describe.
TIER_PRICE_PER_1K_IN = {
    "screen_cheap":     0.00014,
    "screen_expensive": 0.00055,
}
TIER_PRICE_PER_1K_OUT = {
    "screen_cheap":     0.00045,
    "screen_expensive": 0.00450,
}
# Output tokens one row costs a tier. The cheap tier answers with one field; the
# expensive tier returns the five-field v3.3 schema with a quote and a reasoning.
TIER_OUTPUT_TOKENS = {
    "screen_cheap":     20,
    "screen_expensive": 300,
}
# Characters per token for the estimate. Nothing is tokenized to produce a number
# nobody will be billed on; 4.0 is the usual English-prose approximation.
CHARS_PER_TOKEN = 4.0

TIER_CHEAP = "screen_cheap"
TIER_EXPENSIVE = "screen_expensive"
MODES = ("validation", "live")

RESPONSES_DIR = ENGINE_CACHE_DIR / "responses"
RUNS_DIR = ENGINE_CACHE_DIR / "runs"

# What a work in the cheap tier is decided to be. `proceed` means "on to
# screen_expensive", which is the only thing this tier is allowed to conclude
# besides discard.
DISCARD = "discard"
PROCEED = "proceed"
# Not an outcome: the shape a work is in when its verdict rows do not add up to a
# decision — a provider failure, or one voter of two. It is deliberately not a
# terminal state. A work in it is neither decided (so the next ordinary
# `screen --run` re-asks it) nor handed off (so Stage 3 never sees a half-screen).
INCOMPLETE = "incomplete"

# The model recorded on a vote nothing can attribute. Written rather than left
# blank because a blank model is read as an attribution: `provider_for("")` names
# a provider, and an empty `screen_votes` entry (`=replication/confident`) reads
# as a vote from a model whose name was lost rather than one that never had one.
UNKNOWN_MODEL = "unknown_model"


# ── The screening generation ─────────────────────────────────────────────────
# What a verdict was produced BY, in one hash: the tier's voter pair and the
# prompt they were asked. A constant of the code state, like release.py's release
# id, and for the same reason — a verdict is only an answer to the question the
# code asks today if the code still asks that question. It travels in the claim's
# meta, so every verdict a claim covers inherits it with no schema change.
_TIER_PROMPT = {TIER_CHEAP: "build_prescreen_prompt",
                TIER_EXPENSIVE: "build_classify_prompt"}
# Recorded models that name no voter: a cheap-tier bypass, the empty model a
# legacy placeholder row can carry, and the explicit marker written in its place.
_NON_MODEL_MARKERS = {"prescreen_bypass", "", UNKNOWN_MODEL}


def _tier_models(tier: str) -> tuple[str, str]:
    """The tier's voter pair, read at call time so a changed constant is seen."""
    return ((PRESCREEN_MODEL_1, PRESCREEN_MODEL_2) if tier == TIER_CHEAP
            else (SCREENING_MODEL_1, SCREENING_MODEL_2))


def screening_generation(tier: str) -> str:
    """Fingerprint of the models and prompt *tier* screens with right now.

    Sorted models: which slot a model sits in reroutes the call but not the
    question, and a swap of the two would otherwise read as a new generation.
    """
    from shared.prompts import prompt_version

    payload = json.dumps({"models": sorted(_tier_models(tier)),
                          "prompt": prompt_version(_TIER_PROMPT[tier])},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _generation_current(tier: str, generation: Optional[str],
                        rows: list[dict]) -> bool:
    """Whether one claim's verdicts still answer the question this tier asks.

    A recorded generation answers it exactly. Its absence means the row predates
    the field: the prompt it was asked under is not recoverable, but the model
    pair is the dominant determinant of what it said and IS recorded, so a legacy
    row asked of today's models is grandfathered in. Everything else is ignored —
    the work is claimable again and no longer steers the handoff.
    """
    if generation:
        return generation == screening_generation(tier)
    # An expensive-tier placeholder row records the "+"-joined pair rather than
    # one voter, which is why this splits before comparing.
    recorded = {part for row in rows
                for part in str(row.get("model") or "").split("+")
                if part not in _NON_MODEL_MARKERS}
    return recorded <= set(_tier_models(tier))


@dataclass(frozen=True)
class Work:
    """One routed work, with the text the tier will read.

    *source* is the discovery source, and it is here for one reason: the cheap tier
    must not let a 3B model discard a row a human put on a curated replication list
    (`prescreen_bypass`). The pool schema carries no source column today — every pool
    row came from the OpenAlex snapshot — so it defaults to empty and the bypass
    simply never fires; a pool that later carries curated rows must add the column in
    `_pool_record()`/`_POOL_SCHEMA` (`search/snapshot_scan.py`) for this to bite.
    """
    work_id: int
    doi: str
    title: str
    abstract: str
    pile: str
    source: str = ""


# ---------------------------------------------------------------------------
# Reading the pile
# ---------------------------------------------------------------------------


def pile_works(con, release_id: str, pile: str, pool_dir: Path = SNAPSHOT_POOL_DIR,
               overlay_dir: Optional[Path] = None,
               aliases: Optional[dict[int, int]] = None,
               only: Optional[Iterable[int]] = None,
               limit: Optional[int] = None) -> list[Work]:
    """The works routed to *pile* under *release_id*, with pool text attached.

    The routing table stores work ids, not text, so the pool is streamed through
    the same overlay-aware reader routing used — Stage 3 and the tiers must see
    the text the pile decision saw. *only* restricts to a set of ids (what a
    resumed run passes); *limit* stops early, which is what makes a first live
    batch small enough to inspect.
    """
    routed = dict(con.execute(
        "SELECT work_id, pile FROM routing WHERE release_id = ? AND pile = ?",
        [release_id, pile]).fetchall())
    if only is not None:
        wanted = {int(w) for w in only}
        routed = {w: p for w, p in routed.items() if w in wanted}

    works: list[Work] = []
    seen: set[int] = set()
    for batch in iter_pool_batches(pool_dir, overlay_dir, aliases=aliases):
        for record in batch.to_pylist():
            wid = resolve(work_id(record["id"]), aliases or {})
            if wid not in routed or wid in seen:
                continue
            seen.add(wid)
            title = record.get("display_name") or record.get("title") or ""
            abstract = record.get("abstract_text") or ""
            # A row with no text never reaches a screen pile — route.py downgrades
            # it to pending/no_text — so an empty abstract here means the pool and
            # the routing disagree, which is a bug rather than a row to skip.
            assert abstract.strip(), (
                f"work {wid} is routed to {pile} with no abstract text: the "
                "routing table and the pool disagree, re-run `route`")
            # `clean_doi` and `display_name or title` are not cosmetic here: they
            # are exactly what `_row_from_snapshot()` writes into `doi_r` and
            # `title_r`, and `classify_replication()` keys its cache on the doi and
            # the prompt built from the title. Reproducing that mapping is what
            # makes the expensive tier's call and Stage 3's front-door call the
            # SAME call, so Stage 3 replays this verdict from cache for free.
            works.append(Work(wid, clean_doi(record.get("doi") or ""), title,
                              abstract, pile, record.get("source") or ""))
            if limit is not None and len(works) >= limit:
                return works
            # Every wanted work has been found, so the rest of the 5.1M-row pool
            # can only be rows this call is not asking for. The order works come
            # back in is the pool's either way — this stops the scan, it does not
            # choose differently.
            if len(works) >= len(routed):
                return works
    return works


# ---------------------------------------------------------------------------
# The dry run (issue #146 §6)
# ---------------------------------------------------------------------------


def estimate(works: list[Work], tier: str) -> dict:
    """What running *tier* over *works* would cost, roughly, before it is run."""
    chars = [len(w.title) + len(w.abstract) for w in works]
    in_tokens = [c / CHARS_PER_TOKEN for c in chars]
    out_tokens = TIER_OUTPUT_TOKENS.get(tier, 0)
    total_in = sum(in_tokens)
    cost = (total_in / 1000 * TIER_PRICE_PER_1K_IN.get(tier, 0.0)
            + len(works) * out_tokens / 1000 * TIER_PRICE_PER_1K_OUT.get(tier, 0.0))
    return {
        "tier": tier,
        "rows": len(works),
        "input_tokens": int(total_in),
        "output_tokens": len(works) * out_tokens,
        "tokens_per_row": _quantiles(in_tokens),
        "usd": round(cost, 4),
        # What the same rows would cost at 100k of them: a batch of 500 says
        # nothing about a campaign, and the campaign is the decision being made.
        "usd_per_100k": round(cost / len(works) * 100_000, 2) if works else 0.0,
    }


def _quantiles(values: list[float]) -> dict:
    if not values:
        return {"min": 0, "p50": 0, "p90": 0, "max": 0}
    ordered = sorted(values)
    return {
        "min": int(ordered[0]),
        "p50": int(statistics.median(ordered)),
        "p90": int(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]),
        "max": int(ordered[-1]),
    }


def render_estimate(est: dict) -> str:
    q = est["tokens_per_row"]
    voters = (prescreen_voters() if est["tier"] == TIER_CHEAP
              else [(p, m) for p, m, _, _ in screen_voters()])
    lines = [
        "DRY RUN — nothing claimed, nothing fetched, nothing spent.",
        f"  tier          {est['tier']}  ({' + '.join(m for _, m in voters)})",
        f"  rows          {est['rows']:,}",
        f"  input tokens  {est['input_tokens']:,}  "
        f"(per row min {q['min']} · median {q['p50']} · p90 {q['p90']} · max {q['max']})",
        f"  output tokens {est['output_tokens']:,} (assumed)",
        f"  {est['rows']:,} row(s) → tier {est['tier']} ≈ {_usd(est['usd'])}",
        f"  scale check: {_usd(est['usd_per_100k'])} per 100,000 rows at this "
        "median length",
        "  Rough per-1k list prices from shared/config.py, summed over both voters.",
    ]
    if est["tier"] == TIER_CHEAP:
        lines.append("  Voter 2 is asked only when voter 1 said no, so this is an "
                     "upper bound.")
    lines.append("  Re-run with --run to claim and spend.")
    return "\n".join(lines)


def _usd(amount: float) -> str:
    """Money, at a precision that does not round a real cost to nothing."""
    return f"${amount:,.2f}" if amount >= 1 else f"${amount:.4f}"


# ---------------------------------------------------------------------------
# Raw responses: HF first, verdict second (§4)
# ---------------------------------------------------------------------------


def _write_response(blob: dict) -> tuple[str, Path]:
    """Persist one raw response and return `(response_hash, path)`.

    The hash names the bytes, so the same answer written twice is one blob. Disk
    is the only thing this does: the blob exists before the verdict row that names
    it, which is the ordering §4 asks for, and the upload is a separate, batched
    concern (`_ResponseUploader`).
    """
    payload = json.dumps(blob, sort_keys=True, ensure_ascii=False, default=str)
    response_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    path = RESPONSES_DIR / f"{response_hash}.json"
    path.write_text(payload, encoding="utf-8")
    return response_hash, path


def _remote_response_path(path: Path) -> str:
    """Where one blob lives on Hugging Face: `responses/<first two hex>/<hash>.json`.

    Sharded on the hash's first two characters for FILE COUNT, the same reason
    every cache part in `shared/cache_sync.py` is: Hugging Face asks for fewer
    than 10k entries per folder, and one expensive-pile screen writes ~9,300
    blobs. On-disk layout is unchanged — `RESPONSES_DIR` stays flat, and the
    reconciler finds a blob by hash there, never on the remote — so this is
    write-side only. Blobs uploaded flat before this stay where they are: nothing
    reads the remote path back, so the two layouts coexist.
    """
    return f"responses/{path.stem[:2]}/{path.name}"


def _hf_upload_target() -> Optional[str]:
    """The repo response blobs go to, or None when this run does not push.

    Three ways to be off, and none of them is an error: the config flag
    (`ENGINE_TIER_HF_UPLOAD=false`), no repo, no token. A developer with no
    HF_TOKEN runs the tiers exactly as a deployment does, and the only difference
    is a response_state that says so.
    """
    if not (ENGINE_TIER_HF_UPLOAD and FLORA_POOL_REPO and os.getenv("HF_TOKEN")):
        return None
    return FLORA_POOL_REPO


class _ResponseUploader:
    """Response blobs to Hugging Face, in multi-file commits rather than one each.

    `upload_file` per blob is one COMMIT per blob — two per work, ~3,700 for a
    single expensive-tier batch. That is the anti-pattern `search/pool_sync.py`
    already refuses for pool files, and on a live run Hugging Face answered it with
    HTTP 429 on essentially every commit. Blobs are collected as they are written
    and committed `FLORA_HF_COMMIT_BATCH` at a time, plus a final flush.

    **What that costs, and why it is paid this way.** The verdict row must be
    written when the work is decided — it is the run's checkpoint — but the commit
    carrying its blob happens later, so the upload state is not known yet at insert
    time. The row is therefore inserted `response_pending_upload` and corrected by
    `client.mark_uploaded()` once a commit has actually accepted the bytes. The
    alternative — holding verdicts back until their blob is committed — would trade
    an honest, self-correcting state for a run that loses its checkpoint on every
    interruption, and the checkpoint is what makes a killed run cheap to resume.
    Nothing here ever claims `uploaded` for a blob a commit did not take: a failed
    commit, an unconfigured HF and a failed `mark_uploaded` all leave the row
    pending, and reconciliation is a later run's job.

    Used from worker threads, so `add()` is locked and only one commit runs at a
    time — HF rate limits are per account, not per thread.
    """

    def __init__(self, client: ClaimsClient, batch_size: Optional[int] = None):
        self._client = client
        self._batch_size = max(1, batch_size or FLORA_HF_COMMIT_BATCH)
        self._pending: list[tuple[str, Path]] = []
        self._lock = threading.Lock()
        self.uploaded = 0
        self.not_uploaded = 0
        # Blobs a commit ACCEPTED, which is not the same number as `uploaded`: a
        # commit whose `mark_uploaded` then failed leaves bytes on Hugging Face and
        # a row that still says pending, and a reconciliation report that reported
        # only one of the two numbers would hide exactly that state.
        self.committed = 0

    def add(self, response_hash: str, path: Path) -> None:
        with self._lock:
            self._pending.append((response_hash, path))
            ready = (self._take(self._batch_size)
                     if len(self._pending) >= self._batch_size else [])
            if ready:
                self._commit(ready)

    def flush(self) -> None:
        with self._lock:
            while self._pending:
                self._commit(self._take(self._batch_size))

    def _take(self, n: int) -> list[tuple[str, Path]]:
        batch, self._pending = self._pending[:n], self._pending[n:]
        return batch

    def _commit(self, batch: list[tuple[str, Path]]) -> None:
        """One commit for the whole batch; anything short of success stays pending."""
        repo = _hf_upload_target()
        if repo is None:
            self.not_uploaded += len(batch)
            return
        try:
            import huggingface_hub as hf  # pipeline-only dependency
            hf.HfApi(token=os.getenv("HF_TOKEN")).create_commit(
                repo_id=repo, repo_type="dataset",
                operations=[hf.CommitOperationAdd(
                    path_in_repo=_remote_response_path(path),
                    path_or_fileobj=str(path))
                    for _, path in batch],
                commit_message=f"engine tier responses ({len(batch)} blob(s))")
        except Exception as exc:  # noqa: BLE001 — network boundary
            log.warning("%d response blob(s) not uploaded (%s); they are on disk and "
                        "their verdicts stay %s", len(batch), exc, PENDING_UPLOAD)
            self.not_uploaded += len(batch)
            return
        self.committed += len(batch)
        try:
            self._client.mark_uploaded([h for h, _ in batch])
        except Exception as exc:  # noqa: BLE001 — network boundary
            # The bytes ARE on Hugging Face; only the row saying so failed. Counted
            # as not-uploaded because that is what the database now believes.
            log.warning("%d blob(s) committed but their verdict rows still read %s "
                        "(%s)", len(batch), PENDING_UPLOAD, exc)
            self.not_uploaded += len(batch)
            return
        self.uploaded += len(batch)


def _hf_upload_blockers() -> list[str]:
    """Why this process would not push, in the operator's words. Empty when it would."""
    reasons = []
    if not ENGINE_TIER_HF_UPLOAD:
        reasons.append("ENGINE_TIER_HF_UPLOAD is off")
    if not FLORA_POOL_REPO:
        reasons.append("FLORA_POOL_REPO is unset")
    if not os.getenv("HF_TOKEN"):
        reasons.append("HF_TOKEN is unset")
    return reasons


def reconcile_responses(client: ClaimsClient, *, run: bool = False,
                        batch_size: Optional[int] = None) -> dict:
    """Push the blobs an earlier run left `response_pending_upload`, and say so.

    `_ResponseUploader` reconciles what its OWN run wrote; a run that pushed
    nothing — the flag off, no token, a commit that 429'd — leaves rows pending
    forever, and §4's "reconciled later" has to be somebody's job. This is it,
    and it is a separate pass rather than a step of the next tier run because the
    rows it repairs may belong to a tier nobody is about to run again.

    Same `_ResponseUploader` as a live run, so there is one commit path and one
    definition of what `uploaded` means: only a commit that was accepted AND a
    `mark_uploaded` that succeeded flips a row.

    A pending row whose blob is gone from `cache/engine/responses/` is a real
    state, not a crash: the disk cache is expendable and the row is honest about
    the bytes never having been pushed. It is counted and named, and nothing
    marks it.
    """
    rows = client.pending_responses()
    hashes = [h for h in dict.fromkeys(r.get("response_hash") or "" for r in rows) if h]
    on_disk: list[tuple[str, Path]] = []
    missing: list[str] = []
    for response_hash in hashes:
        path = RESPONSES_DIR / f"{response_hash}.json"
        (on_disk.append((response_hash, path)) if path.is_file()
         else missing.append(response_hash))

    report = {"pending_rows": len(rows), "pending_blobs": len(hashes),
              "on_disk": len(on_disk), "missing": len(missing),
              "missing_hashes": sorted(missing)[:20], "dry_run": not run,
              "committed": 0, "marked": 0, "not_uploaded": 0}
    if not run:
        return report

    uploader = _ResponseUploader(client, batch_size=batch_size)
    for response_hash, path in on_disk:
        uploader.add(response_hash, path)
    uploader.flush()
    report["committed"] = uploader.committed
    report["marked"] = uploader.uploaded
    report["not_uploaded"] = uploader.not_uploaded
    return report


def render_reconcile(report: dict) -> str:
    lines = [
        "DRY RUN — nothing committed, nothing marked." if report["dry_run"]
        else "response-blob reconciliation",
        f"  blob directory  {RESPONSES_DIR}",
        f"  verdict rows still {PENDING_UPLOAD:<22} {report['pending_rows']:>7,}",
        f"  {'distinct blobs they name':<41} {report['pending_blobs']:>7,}",
        f"  {'blobs present on disk':<41} {report['on_disk']:>7,}",
        f"  {'blobs MISSING from disk':<41} {report['missing']:>7,}",
    ]
    if report["missing_hashes"]:
        lines.append("    " + ", ".join(h[:12] for h in report["missing_hashes"])
                     + (" …" if report["missing"] > len(report["missing_hashes"]) else ""))
        lines.append("    Their rows stay pending: the bytes were never pushed and "
                     "are no longer on this disk.")
    if report["dry_run"]:
        lines.append(f"  Would commit {report['on_disk']:,} blob(s) to "
                     f"{FLORA_POOL_REPO or '(no repo)'} in batches of "
                     f"{FLORA_HF_COMMIT_BATCH}. Re-run with --run.")
    else:
        lines += [f"  {'committed to Hugging Face':<41} {report['committed']:>7,}",
                  f"  {'verdict rows marked uploaded':<41} {report['marked']:>7,}",
                  f"  {'left pending':<41} {report['not_uploaded']:>7,}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The shared run loop
# ---------------------------------------------------------------------------


def _claim(client: ClaimsClient, release_id: str, tier: str,
           items: list[tuple[int, str]], meta: dict,
           cache_dir: Optional[Path] = None) -> str:
    """Claim *items*, registering the release once if the server has never seen it.

    `route` registers the release it writes, but best-effort — routing must stay
    usable with no Supabase — so the first claim under a release routed offline is
    where the gap surfaces. The record on disk holds the six inputs the release id
    hashes, so registering it here restates a decision that has already been made
    rather than inventing one. Exactly one retry: a second `unknown_release` means
    the registration did not take, and looping would only spend the operator's time.
    """
    try:
        return client.claim(release_id, tier, items, meta=meta)
    except UnknownRelease:
        pass
    try:
        record = read_release(release_id, cache_dir=cache_dir)
    except OSError as exc:
        raise SystemExit(
            f"release {release_id[:12]} is not registered in the state authority "
            f"and its record could not be read ({exc}), so it cannot be "
            "registered from here. Re-run `python -m filter.engine route` on the "
            "machine holding this store.")
    try:
        client.register_release(record)
    except ClaimsError as exc:
        raise SystemExit(
            f"release {release_id[:12]} is not registered in the state authority "
            f"and registering it failed: {exc}. Nothing was claimed or spent. "
            "Fix the Supabase credentials (SUPABASE_SERVICE_KEY must be allowed "
            "to insert into engine_releases) and re-run.")
    return client.claim(release_id, tier, items, meta=meta)


def _run_tier(con, client: ClaimsClient, release_id: str, tier: str,
              works: list[Work], judge: Callable[[Work], tuple[str, list[dict]]],
              *, mode: str, batch_label: str, run: bool,
              cache_dir: Optional[Path] = None) -> dict:
    """Claim *works* for *tier*, judge each one, record its verdicts, complete.

    *judge* returns `(outcome, votes)`: the tier's decision for the work and one
    dict per voter vote, each carrying `model`, `verdict`, `blob` and optionally
    `confidence` / `quote`. Everything else — the claim, the blob ordering, the
    budget, the failure path — is the same for both tiers and lives here.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode} (expected one of {MODES})")
    est = estimate(works, tier)
    if not run:
        print(render_estimate(est))
        return {"tier": tier, "mode": mode, "dry_run": True, "estimate": est,
                "claim_id": None, "decided": 0, "outcomes": {}}

    if not works:
        raise SystemExit(f"nothing to claim for tier {tier}: the pile is empty or "
                         "every work in it already has a verdict")

    meta = {"batch": batch_label, "mode": mode, "engine_tier": tier,
            "generation": screening_generation(tier)}
    try:
        claim_id = _claim(client, release_id, tier,
                          [(w.work_id, w.pile) for w in works], meta, cache_dir)
    except ClaimConflict as exc:
        raise SystemExit(
            f"refusing to run tier {tier}: {exc}. Another run holds some of these "
            f"works. End that claim (or wait for it) and re-run — the batch is "
            f"rejected whole, so nothing here was claimed or spent.")

    report = {"tier": tier, "mode": mode, "dry_run": False, "estimate": est,
              "claim_id": claim_id, "release_id": release_id, "batch": batch_label,
              "decided": 0, "outcomes": {}, "discarded_work_ids": [],
              "by_pile": {}, "verdicts": 0, "workers": max(1, ENGINE_TIER_WORKERS)}
    uploader = _ResponseUploader(client)
    counters = threading.Lock()
    stop = threading.Event()

    def decide(work: Work) -> None:
        """One work, start to finish, on a worker thread.

        Everything a work needs is private to it except the report counters and the
        uploader's queue, both of which are locked. `stop` makes the works that were
        queued behind a failure no-ops rather than cancellations, so a run that is
        ending still drains cheaply and nothing half-finishes.
        """
        if stop.is_set():
            return
        check_openai_budget()
        outcome, votes = judge(work)
        for vote in votes:
            response_hash, path = _write_response(vote["blob"])
            client.record_verdict(
                claim_id=claim_id, work_id=work.work_id, tier=tier,
                verdict=vote["verdict"], model=vote.get("model", ""),
                prompt_hash=vote.get("prompt_hash", ""),
                confidence=vote.get("confidence", ""),
                quote=vote.get("quote", ""),
                response_hash=response_hash, response_state=PENDING_UPLOAD)
            uploader.add(response_hash, path)
            with counters:
                report["verdicts"] += 1
        with counters:
            report["decided"] += 1
            report["outcomes"][outcome] = report["outcomes"].get(outcome, 0) + 1
            if outcome == DISCARD:
                report["discarded_work_ids"].append(work.work_id)
                report["by_pile"][work.pile] = report["by_pile"].get(work.pile, 0) + 1

    try:
        failures: list[BaseException] = []
        # Works are independent and each is almost entirely provider latency, so
        # they overlap; the request RATE is still bounded by the per-provider
        # limiter in shared/llm_client.py, which reserves slots under a lock.
        with ThreadPoolExecutor(max_workers=report["workers"],
                                thread_name_prefix=f"tier-{tier}") as pool:
            futures = [pool.submit(decide, work) for work in works]
            try:
                for future in as_completed(futures):
                    try:
                        future.result()
                    except BaseException as exc:  # noqa: BLE001 — one work must not
                        stop.set()                # cost the others their verdicts
                        failures.append(exc)
            finally:
                # Leaving the pool waits for every queued work, so a Ctrl-C here
                # would otherwise sit through the whole rest of the batch. Setting
                # the flag makes the queued ones return immediately; the handful
                # already in flight still finish and record what they were paid for.
                stop.set()
        if failures:
            # A budget exhaustion is the one failure with a report line of its own,
            # and it wins over an incidental error raised in the same moment.
            raise next((exc for exc in failures
                        if isinstance(exc, TokenBudgetExhausted)), failures[0])
    except TokenBudgetExhausted as exc:
        # The remaining works were never examined. Failing the claim frees them
        # for the next run; the verdicts already written stay, because they are
        # evidence and the budget did not un-spend them.
        report["stopped"] = f"token budget exhausted: {exc}"
        _finish(report, uploader, client, claim_id, "failed")
        raise
    except BaseException:
        _finish(report, uploader, client, claim_id, "failed")
        raise

    _finish(report, uploader, client, claim_id, "complete")
    return report


def _finish(report: dict, uploader: "_ResponseUploader", client: ClaimsClient,
            claim_id: str, status: str) -> dict:
    """Push what is still queued, end the claim, write the report.

    The flush comes first so the report's upload counts are final, and it runs on
    the failure paths too: the blobs are already written and committing them costs
    nothing an ending run cares about.
    """
    uploader.flush()
    # Ordering is not a contract of the run, but it is of the report — a threaded
    # run would otherwise shuffle this list between two identical runs.
    report["discarded_work_ids"].sort()
    report["responses"] = {"uploaded": uploader.uploaded,
                           PENDING_UPLOAD: uploader.not_uploaded}
    client.release_claim(claim_id, status)
    _write_run_report(report)
    return report


def _write_run_report(report: dict) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = RUNS_DIR / f"{report['tier']}-{stamp}-{report.get('claim_id', 'none')}.json"
    path.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    return path


def _by_work(tier: str, rows: list[dict],
             generations: dict) -> dict[int, list[dict]]:
    """Verdict rows of the CURRENT generation, grouped by work id.

    Grouped by claim first because the generation is recorded on the claim and one
    claim is one screening of that work; the surviving rows are then pooled per
    work, because two claims can each hold half a screen of the same work and the
    question every reader asks is what is known about the WORK.
    """
    by_claim: dict[tuple, list[dict]] = {}
    for row in rows:
        by_claim.setdefault((row.get("claim_id"), int(row["work_id"])), []).append(row)
    by_work: dict[int, list[dict]] = {}
    for (claim_id, work), claim_rows in by_claim.items():
        if _generation_current(tier, generations.get(claim_id), claim_rows):
            by_work.setdefault(work, []).extend(claim_rows)
    return by_work


def _tier_decision(tier: str, rows: list[dict]) -> dict:
    """One work's verdict rows as that tier's decision — the one dispatch."""
    return _cheap_decision(rows) if tier == TIER_CHEAP else _expensive_decision(rows)


def checkpoint_decisions(client: ClaimsClient, tier: str) -> dict[int, dict]:
    """Every work this tier holds current-generation verdict rows for, decided.

    The checkpoint's raw material, read once and split two ways by the two
    functions below: a work whose rows add up to a decision must not be re-bought,
    and a work whose rows do not must not be treated as if they did.
    """
    generations = {c["id"]: (c.get("meta") or {}).get("generation")
                   for c in client.claims(tier=tier)}
    return {work: _tier_decision(tier, rows)
            for work, rows in _by_work(tier, client.verdicts(tier), generations).items()}


def decided_work_ids(client: ClaimsClient, tier: str) -> set[int]:
    """Works this tier has SETTLED in the current generation — the checkpoint.

    A verdict is what one model pair, asked one prompt, said; change either and it
    stops answering the question this run asks, so the work becomes claimable
    again.

    Verdict rows that do not add up to a decision do not count. They are written
    and they are permanent — a call was made and the evidence of it is not ours to
    delete (`db/migrations/0001_engine_baseline.sql`: `engine_verdicts` is
    append-only) — but a screen that got one vote of two, or none at all, is a
    provider failure and never a definitive miss. Counting it here is what stranded
    the work: skipped by the next run because it looked decided, held back by the
    handoff because it was not. So the READ side ignores those rows, and the next
    ordinary `screen --run` re-asks the work with no flag to remember. The re-ask
    is cheap where it can be: `classify_replication()` caches, so the voter that
    did answer is not re-bought.
    """
    return {work for work, decision in checkpoint_decisions(client, tier).items()
            if decision["outcome"] != INCOMPLETE}


def incomplete_work_ids(client: ClaimsClient, tier: str) -> set[int]:
    """Works whose current-generation verdict rows are short of a decision.

    The number that makes the self-healing visible: these are works a run paid for
    and got no answer about, which the next run will ask again. A run that reports
    a growing count is reporting a provider outage, not a routing decision.
    """
    return {work for work, decision in checkpoint_decisions(client, tier).items()
            if decision["outcome"] == INCOMPLETE}


def tier_decisions(client: ClaimsClient, release_id: Optional[str], tier: str,
                   mode: str = "live") -> dict[int, dict]:
    """What *tier*'s *mode* runs decided, per work: `{"outcome", "record_type"}`.

    Read back from the verdict rows rather than from a run report, because the
    verdicts are the permanent evidence and a report file is a convenience. The
    gate and the record type are recomputed from the votes with the same two
    functions Stage 3 uses — a stored gate outcome could disagree with the votes
    it was supposed to summarise, and this way it cannot.

    *release_id* `None` means EVERY release. A verdict follows the WORK, not the
    release: claims are release-scoped, but the thing they pin is a work id, and
    the tier's own checkpoint (`decided_work_ids()`) is tier-scoped for the same
    reason — a re-route mints a new release id without unasking a single voter.
    Restricting to one release is therefore an audit question ("what did this
    release's runs decide?"), not the question a consumer of the evidence asks.

    A run's MODE lives on its claim (`meta.mode`), so a validation run's verdicts
    are recorded, readable, and invisible here — which is exactly what
    "verdicts recorded, no pile effect" has to mean downstream. Superseded
    verdict rows never arrive: `ClaimsClient.verdicts()` filters
    `superseded_by is null`, so a corrected vote is counted once.

    The claim's `meta.generation` is the second filter, and it is a different axis
    from the release: a verdict from a superseded model pair or prompt is not
    evidence about what today's screen would say, so it neither settles the work
    nor reaches the handoff (`_generation_current()`).
    """
    claims = [c for c in client.claims(release_id=release_id, tier=tier)
              if (c.get("meta") or {}).get("mode") == mode]
    if not claims:
        return {}
    generations = {c["id"]: (c.get("meta") or {}).get("generation") for c in claims}
    by_work = _by_work(tier, client.verdicts(tier, list(generations)), generations)
    return {wid: _tier_decision(tier, rows) for wid, rows in by_work.items()}


def _recorded_at(row: dict) -> tuple:
    """When a verdict row was written, as far as the row can say.

    `created_at` is the only time fact the table carries — the primary key is a
    uuid, so id order is not time order — and it is only present on rows read
    through `ClaimsClient.verdicts()`. The id is the tiebreaker and the empty
    tuple element the fallback, so rows from a fixture that carries neither keep
    the order they were given in.
    """
    return (str(row.get("created_at") or ""), str(row.get("id") or ""))


def _answer_rows(rows: list[dict]) -> list[dict]:
    """One work's verdict rows as ANSWERS, at most one per voter.

    Two things are dropped here, and both are the same mistake in different
    clothes: counting a row that is not an answer, and counting one answer twice.

    * A row with no verdict, or the `no_answer` placeholder a failed call writes,
      is not an answer.
    * A voter that answers TWICE is still one voter. Rows are pooled across claims
      (`_by_work()`) so that a work whose screen did not complete is claimable
      again — but a re-run re-asks every voter, and the one that already answered
      answers again from cache. Without this, voter 1 answering on two attempts
      while voter 2 keeps failing would look exactly like two voters agreeing, and
      the work would settle on a gate decision no second voter ever contributed
      to. The LATEST answer per voter wins (`_recorded_at`): an answer is superseded
      by a later answer from the same model, never joined by it.

    Unattributed rows — legacy rows whose `model` column is blank — cannot be
    deduplicated by voter, because nothing on them says which voter they are. They
    are kept only from ONE claim, the most recent that has any: within a claim two
    such rows are the two voters of one screening, while across claims they would
    be indistinguishable from one voter retried.
    """
    answers = [r for r in rows
               if str(r.get("verdict") or "") not in ("", "no_answer")]
    by_model: dict[str, dict] = {}
    unattributed: dict[str, list[dict]] = {}
    for row in sorted(answers, key=_recorded_at):
        model = str(row.get("model") or "")
        if model and model != UNKNOWN_MODEL:
            by_model[model] = row
        else:
            unattributed.setdefault(str(row.get("claim_id") or ""), []).append(row)
    latest_unattributed = max(unattributed.values(),
                              key=lambda rs: _recorded_at(rs[-1]), default=[])
    return list(by_model.values()) + latest_unattributed


def _cheap_decision(rows: list[dict]) -> dict:
    """The discard-only tier's rule, read back from its rows.

    The tier asks voter 2 only when voter 1 said `no`, so the shapes are few and
    what separates them is whether a MISSING answer could have changed the outcome:

    | rows                        | decision   | why                                |
    | --------------------------- | ---------- | ---------------------------------- |
    | a keep (or a bypass)        | proceed    | the row can no longer be discarded, and voter 2 is never asked after a keep |
    | `no` + a keep               | proceed    | both voters answered                |
    | `no` + `no` (two voters)    | discard    | the two explicit noes the tier needs |
    | `no` + `no_answer`          | INCOMPLETE | the missing answer is exactly the second `no` that would have discarded |
    | `no` alone                  | INCOMPLETE | same, with the second call never made |
    | `no_answer` only, or none   | INCOMPLETE | nothing was concluded at all         |
    | `no` + `no` from ONE voter  | INCOMPLETE | a retry is one voter, not two (`_answer_rows`) |

    The old rule read "two explicit noes and nothing else is a discard; every other
    shape proceeds", which quietly filed the last four as a proceed — a swallowed
    provider failure checkpointed as this tier's answer.
    """
    said = [str(row.get("verdict") or "") for row in _answer_rows(rows)]
    if any(s != "no" for s in said):
        return {"outcome": PROCEED, "record_type": "", "votes": []}
    if len(said) >= 2:
        return {"outcome": DISCARD, "record_type": "", "votes": []}
    return {"outcome": INCOMPLETE, "record_type": "", "votes": []}


def _votes_from_rows(rows: list[dict]) -> list[dict]:
    """Stored verdict rows as the vote dicts `shared/llm_client.py` reads.

    The ONE translation between the two shapes, so nothing downstream invents a
    second: the row's `verdict` column is the vote's `classification`, and its
    `confidence` column is `"confident"`/`"unconfident"` where the vote wants a
    boolean. Getting the confidence wrong is not a cosmetic mismatch — the gate's
    soft-discard branch only fires when a voter declined to stand behind a
    qualifying answer, so votes rebuilt without it can only ever proceed.

    Which rows are answers at all, and which of several answers from one voter
    counts, is `_answer_rows()`'s question — the deduplication has to happen
    BEFORE the sort, or a retried voter 1 would occupy both call-order slots.

    The surviving rows are then re-sorted into CALL order by their model, because
    `_screen_record_type()` breaks a replication/reproduction split by taking the
    first qualifying voter and the database returns rows in id order, which is
    not the order they were asked in. The sort is by voter identity, not by time,
    so keeping a voter's LATEST answer does not move it out of its slot: voter 1
    re-answered on the second attempt is still voter 1, and still breaks the split.
    An unattributed row sorts last, behind both named voters.

    **A vote with no recorded model is UNATTRIBUTED, not attributed to nothing.**
    Legacy rows exist whose `model` column is blank, and blank is read as a value
    everywhere downstream: it sorts as "not a voter" here, prints as
    `=replication/confident` in `screen_votes`, and `provider_for("")` names a
    provider for it. The classification and the confidence are real evidence, so
    the vote is kept and the gate is unchanged — only the attribution is replaced
    by `UNKNOWN_MODEL`, which no reader can mistake for a model id. Dropping the
    vote instead would turn a legacy row into an incomplete screen and re-buy a
    screen whose answer we hold.
    """
    order = [m for _, m, _, _ in screen_voters()]
    ordered = sorted(_answer_rows(rows),
                     key=lambda r: (order.index(r["model"])
                                    if r.get("model") in order else len(order)))
    return [{"classification": str(r.get("verdict", "")),
             "confident": r.get("confidence") == "confident",
             "categories": [],
             # Not read by the gate; carried because the handoff writes the vote
             # detail onto the row and a vote is only attributable with its model.
             "model": str(r.get("model") or "") or UNKNOWN_MODEL,
             "quote": str(r.get("quote") or "")}
            for r in ordered]


def _expensive_decision(votes: list[dict]) -> dict:
    """The gate, the record type and the votes, rebuilt from the stored rows.

    The votes travel with the decision because Stage 3 no longer screens: it reads
    this verdict off its input CSV, and one of its rungs (the pre-PDF title search)
    is gated on both voters having given a qualifying answer AND stood behind it —
    a fact no summary of the gate preserves.
    """
    from shared.llm_client import _screen_record_type

    rebuilt = _votes_from_rows(votes)
    if len(rebuilt) < 2:
        return {"outcome": INCOMPLETE, "record_type": "", "votes": []}
    return {"outcome": screen_gate(rebuilt) or INCOMPLETE,
            "record_type": _screen_record_type(rebuilt),
            "votes": rebuilt}


# ---------------------------------------------------------------------------
# screen_cheap
# ---------------------------------------------------------------------------


def _cheap_judge(work: Work) -> tuple[str, list[dict]]:
    """Both cheap voters on one work, exactly as `shared/prescreen.py` asks them.

    `prescreen_vote()` is the public seam: it holds the prompt's cache key, the
    parsing and the rule that an unrecognised label is a non-answer rather than a
    no. THIS function holds the gate — two explicit noes discard, everything else
    proceeds — because the gate is a tier decision and the tier is here. Voter 2 is
    asked only when voter 1 said no: once the row can no longer be discarded, a
    second opinion changes nothing and is not worth paying for.
    """
    from shared.prompts import build_prescreen_prompt, prompt_version

    token_counter.set_stage("engine_screen_cheap")
    version = prompt_version("build_prescreen_prompt")

    # The three rows prescreen refuses to let a small model end: text that states
    # the design outright, text too short to judge, and a curated-list row. The
    # bypass is recorded as a verdict rather than skipped silently — deciding not
    # to ask is a decision, and it is also this work's checkpoint.
    bypass = prescreen_bypass(work.title, work.abstract, work.source)
    if bypass:
        return PROCEED, [{
            "model": "prescreen_bypass", "verdict": PROCEED, "prompt_hash": version,
            "quote": bypass[:200],
            "blob": {"tier": TIER_CHEAP, "work_id": work.work_id, "bypass": bypass},
        }]

    prompt = build_prescreen_prompt(work.title, work.abstract)

    votes: list[dict] = []
    for provider, model in prescreen_voters():
        verdict = prescreen_vote(prompt, provider, model, work.doi, work.title)
        votes.append({
            "model": model,
            "verdict": verdict or "no_answer",
            "prompt_hash": version,
            "blob": {"tier": TIER_CHEAP, "work_id": work.work_id, "provider": provider,
                     "model": model, "prompt_version": version, "prompt": prompt,
                     "verdict": verdict},
        })
        if verdict != "no":
            return PROCEED, votes
    return DISCARD, votes


def run_screen_cheap(con, client: ClaimsClient, release_id: str,
                     work_ids: Optional[Iterable[int]] = None, *,
                     mode: str = "validation", batch_label: str = "",
                     limit: Optional[int] = None,
                     pool_dir: Path = SNAPSHOT_POOL_DIR,
                     overlay_dir: Optional[Path] = None,
                     aliases: Optional[dict[int, int]] = None,
                     run: bool = False,
                     cache_dir: Optional[Path] = None) -> dict:
    """Run the discard-only tier over the `screen_cheap` pile of *release_id*.

    `mode="validation"` (the default) records both votes and changes nothing: the
    report compares the works this tier WOULD have discarded against the pile the
    rules put them in, which is the re-validation issue #146 §2 requires on this
    distribution. `mode="live"` records the same verdicts and the discards take
    effect where the rows leave the engine, in `handoff.py`.
    """
    works = _batch(con, client, release_id, TIER_CHEAP, work_ids, limit,
                   pool_dir, overlay_dir, aliases, run)
    report = _run_tier(con, client, release_id, TIER_CHEAP, works, _cheap_judge,
                       mode=mode, batch_label=batch_label, run=run,
                       cache_dir=cache_dir)
    if run and mode == "validation":
        report["revalidation"] = _revalidation(con, release_id,
                                               report["discarded_work_ids"])
        _write_run_report(report)
    return report


def _revalidation(con, release_id: str, discarded: list[int]) -> dict:
    """What the cheap tier would have discarded, by the pile the rules chose.

    The comparison issue #146 §2 asks for: the tier's zero-miss evidence was
    measured downstream of Stage 2's gate, and the pile a row is in is the best
    stand-in this engine has for "how sure the rules were" — a `screen_expensive`
    row the cheap tier wants to throw away is the case that would have to be
    adjudicated before it may discard autonomously.
    """
    if not discarded:
        return {"discards": 0, "by_pile": {}}
    rows = con.execute(
        "SELECT pile, count(*) FROM routing WHERE release_id = ? AND work_id IN "
        f"({','.join('?' * len(discarded))}) GROUP BY pile",
        [release_id, *discarded]).fetchall()
    return {"discards": len(discarded), "by_pile": dict(rows)}


# ---------------------------------------------------------------------------
# screen_expensive
# ---------------------------------------------------------------------------


def _expensive_judge(work: Work) -> tuple[str, list[dict]]:
    """Stage 3's front door on one work: two votes, one gate.

    `classify_replication()` caches its own result, so a re-run over a work whose
    verdict row was lost costs nothing. The gate outcome travels in each vote's
    blob rather than in a third verdict row: the gate is a pure function of the
    votes, and a stored row for it could disagree with them.
    """
    token_counter.set_stage("engine_screen_expensive")
    screen = classify_replication(work.doi, work.title, work.abstract)
    votes_raw = screen.get("votes") or []
    outcome = screen.get("screen_verdict") or ""
    if not outcome:
        # Fewer than two voters answered: an API failure, not a verdict. The vote
        # rows are still written — a call was made and `engine_verdicts` is
        # append-only evidence — but they do not settle the work: `_expensive_decision`
        # reads them back as INCOMPLETE, so `decided_work_ids()` leaves the work
        # claimable and the next ordinary run asks again.
        outcome = INCOMPLETE
    gate = screen_gate(votes_raw) if len(votes_raw) >= 2 else None

    # `llm_model` is a "+"-joined pair, one part per vote — but a legacy or partial
    # answer can carry fewer parts than it has votes, and a part that is blank is
    # an attribution nobody made. Recorded as UNKNOWN_MODEL rather than "" so the
    # verdict row says which of the two it is.
    models = [part or UNKNOWN_MODEL
              for part in str(screen.get("llm_model") or "").split("+")]

    # A screen served from a cache entry written before the votes carried their own
    # quote holds one quote only, in `llm_evidence`, and it is the FIRST voter's. A
    # current entry's `llm_evidence` is the joined pair, which is not a quote and must
    # not be filed as one, so the fallback applies only while no separator is in it.
    joined = str(screen.get("llm_evidence") or "")
    legacy_quote = "" if SCREEN_EVIDENCE_SEP in joined else joined

    votes: list[dict] = []
    for index, vote in enumerate(votes_raw):
        votes.append({
            "model": models[index] if index < len(models) else UNKNOWN_MODEL,
            "verdict": vote.get("classification", ""),
            "confidence": "confident" if vote.get("confident") else "unconfident",
            "quote": str(vote.get("evidence") or (legacy_quote if index == 0 else "")),
            "blob": {"tier": TIER_EXPENSIVE, "work_id": work.work_id,
                     "vote": vote, "gate": gate, "screen": {
                         k: screen.get(k) for k in
                         ("screen_verdict", "screen_classification", "record_type",
                          "categories", "llm_model", "llm_source", "llm_reasoning")}},
        })
    if not votes:
        votes = [{"model": str(screen.get("llm_model") or "") or UNKNOWN_MODEL,
                  "verdict": "no_answer",
                  "blob": {"tier": TIER_EXPENSIVE, "work_id": work.work_id,
                           "error": screen.get("llm_error", "")}}]
    return outcome, votes


def run_screen_expensive(con, client: ClaimsClient, release_id: str,
                         work_ids: Optional[Iterable[int]] = None, *,
                         mode: str = "live", batch_label: str = "",
                         limit: Optional[int] = None,
                         pool_dir: Path = SNAPSHOT_POOL_DIR,
                         overlay_dir: Optional[Path] = None,
                         aliases: Optional[dict[int, int]] = None,
                         run: bool = False,
                         cache_dir: Optional[Path] = None) -> dict:
    """Run Stage 3's validated front door over the `screen_expensive` pile.

    This tier has no validation mode to earn: it IS the validated screen, and its
    discard is the one Stage 3 would have made anyway. `mode` is accepted so the
    two runners have one signature, and `validation` records the votes without
    letting the discards reach the handoff.
    """
    works = _batch(con, client, release_id, TIER_EXPENSIVE, work_ids, limit,
                   pool_dir, overlay_dir, aliases, run)
    return _run_tier(con, client, release_id, TIER_EXPENSIVE, works,
                     _expensive_judge, mode=mode, batch_label=batch_label, run=run,
                     cache_dir=cache_dir)


# ---------------------------------------------------------------------------


def _batch(con, client: ClaimsClient, release_id: str, tier: str,
           work_ids: Optional[Iterable[int]], limit: Optional[int],
           pool_dir: Path, overlay_dir: Optional[Path],
           aliases: Optional[dict[int, int]], run: bool) -> list[Work]:
    """The works this run will send: the pile, minus what is claimed or decided.

    Asked before the claim so the common case is a clean claim rather than a
    rejected one; the check that makes claiming SAFE is inside the RPC. "Claimed"
    means an active claim whose lease has not run out — a run killed before its
    completion path stops holding its works `CLAIM_TTL_HOURS` after it took them,
    so it cannot exclude them from every future batch. A dry run
    subtracts nothing it would need the server for — it is allowed to run against
    no state authority at all, and a size estimate that quietly excluded another
    run's batch would answer a question nobody asked.
    """
    only = set(int(w) for w in work_ids) if work_ids is not None else None
    if run:
        held = client.claimed_work_ids(release_id, tier) | decided_work_ids(client, tier)
        routed = {row[0] for row in con.execute(
            "SELECT work_id FROM routing WHERE release_id = ? AND pile = ?",
            [release_id, tier]).fetchall()}
        only = (only if only is not None else routed) - held
    return pile_works(con, release_id, tier, pool_dir, overlay_dir, aliases,
                      only=only, limit=limit)
