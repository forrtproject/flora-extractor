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
  `cache/engine/responses/<hash>.json` and pushed to Hugging Face when a token and
  a repo are configured, BEFORE `record_verdict` inserts the row that names it
  (§4). A push that did not happen is recorded as `response_pending_upload`
  rather than claimed as `uploaded` — the state is about the blob, not about our
  intentions for it.

  **Per-work checkpointing.** A verdict is written as each work is decided, so an
  interrupted run's claim is failed and the next run re-claims only the works that
  have no verdict yet. Nothing local is checkpointed: the server holds the state,
  and a local file that disagreed with it would be the thing we trusted.

Neither runner touches the routing table. A live `screen_cheap` discard is applied
where the rows leave the engine (`handoff.py`), because routing is derived data —
recomputed from pool and specs — and a decision written into it would be erased by
the next `route` run.
"""

import datetime
import hashlib
import json
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from filter.engine.claims import (PENDING_UPLOAD, UPLOADED, ClaimConflict,
                                  ClaimsClient)
from filter.engine.pool_reader import iter_pool_batches
from filter.engine.workids import resolve, work_id
from shared import token_counter
from shared.config import (ENGINE_CACHE_DIR, ENGINE_CHARS_PER_TOKEN,
                           ENGINE_TIER_OUTPUT_TOKENS, ENGINE_TIER_PRICE_PER_1K_IN,
                           ENGINE_TIER_PRICE_PER_1K_OUT, FLORA_POOL_REPO,
                           SNAPSHOT_POOL_DIR)
from shared.llm_client import classify_replication, screen_gate, screen_voters
from shared.prescreen import prescreen_bypass, prescreen_voters
from shared.token_usage import TokenBudgetExhausted, check_openai_budget
from shared.utils import clean_doi

log = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class Work:
    """One routed work, with the text the tier will read."""
    work_id: int
    doi: str
    title: str
    abstract: str
    pile: str


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
                              abstract, pile))
            if limit is not None and len(works) >= limit:
                return works
    return works


# ---------------------------------------------------------------------------
# The dry run (issue #146 §6)
# ---------------------------------------------------------------------------


def estimate(works: list[Work], tier: str) -> dict:
    """What running *tier* over *works* would cost, roughly, before it is run."""
    chars = [len(w.title) + len(w.abstract) for w in works]
    in_tokens = [c / ENGINE_CHARS_PER_TOKEN for c in chars]
    out_tokens = ENGINE_TIER_OUTPUT_TOKENS.get(tier, 0)
    total_in = sum(in_tokens)
    cost = (total_in / 1000 * ENGINE_TIER_PRICE_PER_1K_IN.get(tier, 0.0)
            + len(works) * out_tokens / 1000 * ENGINE_TIER_PRICE_PER_1K_OUT.get(tier, 0.0))
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
              else [(p, m) for p, m, _ in screen_voters()])
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


def _write_response(blob: dict) -> tuple[str, str]:
    """Persist one raw response and return `(response_hash, response_state)`.

    The hash names the bytes, so the same answer written twice is one blob. The
    state is what actually happened to it: `uploaded` only when a push returned
    without raising, `response_pending_upload` when there was nothing to push
    with or the push failed. An unconfigured Hugging Face is not an error — the
    blob is on disk either way and reconciliation is a later run's job.
    """
    payload = json.dumps(blob, sort_keys=True, ensure_ascii=False, default=str)
    response_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    path = RESPONSES_DIR / f"{response_hash}.json"
    path.write_text(payload, encoding="utf-8")
    return response_hash, UPLOADED if _push_response(path) else PENDING_UPLOAD


def _push_response(path: Path) -> bool:
    """Upload one response blob to the pool repo, or return False, silently.

    Env-gated on purpose: a developer with no HF_TOKEN runs the tiers exactly as
    a deployment does, and the only difference is a response_state that says so.
    """
    import os
    if not (FLORA_POOL_REPO and os.getenv("HF_TOKEN")):
        return False
    try:
        import huggingface_hub as hf  # pipeline-only dependency
        hf.HfApi(token=os.getenv("HF_TOKEN")).upload_file(
            path_or_fileobj=str(path), path_in_repo=f"responses/{path.name}",
            repo_id=FLORA_POOL_REPO, repo_type="dataset")
        return True
    except Exception as exc:  # noqa: BLE001 — network boundary
        log.debug("response blob %s not uploaded: %s", path.name, exc)
        return False


# ---------------------------------------------------------------------------
# The shared run loop
# ---------------------------------------------------------------------------


def _run_tier(con, client: ClaimsClient, release_id: str, tier: str,
              works: list[Work], judge: Callable[[Work], tuple[str, list[dict]]],
              *, mode: str, batch_label: str, run: bool) -> dict:
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

    meta = {"batch": batch_label, "mode": mode, "engine_tier": tier}
    try:
        claim_id = client.claim(release_id, tier,
                                [(w.work_id, w.pile) for w in works], meta=meta)
    except ClaimConflict as exc:
        raise SystemExit(
            f"refusing to run tier {tier}: {exc}. Another run holds some of these "
            f"works. End that claim (or wait for it) and re-run — the batch is "
            f"rejected whole, so nothing here was claimed or spent.")

    report = {"tier": tier, "mode": mode, "dry_run": False, "estimate": est,
              "claim_id": claim_id, "release_id": release_id, "batch": batch_label,
              "decided": 0, "outcomes": {}, "discarded_work_ids": [],
              "by_pile": {}, "verdicts": 0}
    try:
        for work in works:
            check_openai_budget()
            outcome, votes = judge(work)
            for vote in votes:
                response_hash, state = _write_response(vote["blob"])
                client.record_verdict(
                    claim_id=claim_id, work_id=work.work_id, tier=tier,
                    verdict=vote["verdict"], model=vote.get("model", ""),
                    prompt_hash=vote.get("prompt_hash", ""),
                    confidence=vote.get("confidence", ""),
                    quote=vote.get("quote", ""),
                    response_hash=response_hash, response_state=state)
                report["verdicts"] += 1
            report["decided"] += 1
            report["outcomes"][outcome] = report["outcomes"].get(outcome, 0) + 1
            if outcome == DISCARD:
                report["discarded_work_ids"].append(work.work_id)
                report["by_pile"][work.pile] = report["by_pile"].get(work.pile, 0) + 1
    except TokenBudgetExhausted as exc:
        # The remaining works were never examined. Failing the claim frees them
        # for the next run; the verdicts already written stay, because they are
        # evidence and the budget did not un-spend them.
        client.release_claim(claim_id, "failed")
        report["stopped"] = f"token budget exhausted: {exc}"
        _write_run_report(report)
        raise
    except BaseException:
        client.release_claim(claim_id, "failed")
        _write_run_report(report)
        raise

    client.release_claim(claim_id, "complete")
    _write_run_report(report)
    return report


def _write_run_report(report: dict) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = RUNS_DIR / f"{report['tier']}-{stamp}-{report.get('claim_id', 'none')}.json"
    path.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    return path


def decided_work_ids(client: ClaimsClient, tier: str) -> set[int]:
    """Works this tier has already recorded a verdict for — the checkpoint."""
    return {int(row["work_id"]) for row in client.verdicts(tier)}


def tier_decisions(client: ClaimsClient, release_id: str, tier: str,
                   mode: str = "live") -> dict[int, dict]:
    """What *tier*'s *mode* runs decided, per work: `{"outcome", "record_type"}`.

    Read back from the verdict rows rather than from a run report, because the
    verdicts are the permanent evidence and a report file is a convenience. The
    gate and the record type are recomputed from the votes with the same two
    functions Stage 3 uses — a stored gate outcome could disagree with the votes
    it was supposed to summarise, and this way it cannot.

    A run's MODE lives on its claim (`meta.mode`), so a validation run's verdicts
    are recorded, readable, and invisible here — which is exactly what
    "verdicts recorded, no pile effect" has to mean downstream.
    """
    claims = [c for c in client.claims(release_id=release_id, tier=tier)
              if (c.get("meta") or {}).get("mode") == mode]
    if not claims:
        return {}
    by_work: dict[int, list[dict]] = {}
    for row in client.verdicts(tier, [c["id"] for c in claims]):
        by_work.setdefault(int(row["work_id"]), []).append(row)
    return {wid: (_cheap_decision(votes) if tier == TIER_CHEAP
                  else _expensive_decision(votes))
            for wid, votes in by_work.items()}


def _cheap_decision(votes: list[dict]) -> dict:
    """Two explicit noes and nothing else is a discard. Every other shape proceeds."""
    said = [str(v.get("verdict", "")) for v in votes]
    discard = len(said) >= 2 and all(s == "no" for s in said)
    return {"outcome": DISCARD if discard else PROCEED, "record_type": ""}


def _expensive_decision(votes: list[dict]) -> dict:
    """The gate and the record type, rebuilt from the stored votes.

    Votes are re-sorted into CALL order by their model, because
    `_screen_record_type()` breaks a replication/reproduction split by taking the
    first qualifying voter and the database returns rows in id order, which is
    not the order they were asked in.
    """
    from shared.llm_client import _screen_record_type

    order = [m for _, m, _ in screen_voters()]
    ordered = sorted(votes, key=lambda v: (order.index(v["model"])
                                           if v.get("model") in order else len(order)))
    rebuilt = [{"classification": str(v.get("verdict", "")),
                "confident": v.get("confidence") == "confident",
                "categories": []}
               for v in ordered if v.get("verdict") not in ("", "no_answer")]
    if len(rebuilt) < 2:
        return {"outcome": "incomplete", "record_type": ""}
    return {"outcome": screen_gate(rebuilt) or "incomplete",
            "record_type": _screen_record_type(rebuilt)}


# ---------------------------------------------------------------------------
# screen_cheap
# ---------------------------------------------------------------------------


def _cheap_judge(work: Work) -> tuple[str, list[dict]]:
    """Both cheap voters on one work, exactly as `shared/prescreen.py` asks them.

    `prescreen._vote()` is the seam: it holds the prompt, the parsing, the cache
    key and the rule that an unrecognised label is a non-answer rather than a no.
    Voter 2 is asked only when voter 1 said no, for the reason prescreen gives —
    once the row can no longer be discarded, a second opinion changes nothing and
    is not worth paying for.
    """
    from shared.prescreen import _vote  # the tier's one call seam
    from shared.prompts import build_prescreen_prompt, prompt_version

    token_counter.set_stage("engine_screen_cheap")
    version = prompt_version("build_prescreen_prompt")

    # The three rows prescreen refuses to let a small model end: text that states
    # the design outright, text too short to judge, and a curated-list row. The
    # bypass is recorded as a verdict rather than skipped silently — deciding not
    # to ask is a decision, and it is also this work's checkpoint.
    bypass = prescreen_bypass(work.title, work.abstract)
    if bypass:
        return PROCEED, [{
            "model": "prescreen_bypass", "verdict": PROCEED, "prompt_hash": version,
            "quote": bypass[:200],
            "blob": {"tier": TIER_CHEAP, "work_id": work.work_id, "bypass": bypass},
        }]

    prompt = build_prescreen_prompt(work.title, work.abstract)

    votes: list[dict] = []
    for provider, model in prescreen_voters():
        verdict = _vote(prompt, provider, model, work.doi, work.title)
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
                     run: bool = False) -> dict:
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
                       mode=mode, batch_label=batch_label, run=run)
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
        # Fewer than two voters answered: an API failure, not a verdict. Recorded
        # as such so the row is re-run rather than filed as a screen outcome.
        outcome = "incomplete"
    gate = screen_gate(votes_raw) if len(votes_raw) >= 2 else None

    votes: list[dict] = []
    for index, vote in enumerate(votes_raw):
        votes.append({
            "model": (screen.get("llm_model", "").split("+") + ["", ""])[index],
            "verdict": vote.get("classification", ""),
            "confidence": "confident" if vote.get("confident") else "unconfident",
            "quote": screen.get("llm_evidence", "") if index == 0 else "",
            "blob": {"tier": TIER_EXPENSIVE, "work_id": work.work_id,
                     "vote": vote, "gate": gate, "screen": {
                         k: screen.get(k) for k in
                         ("screen_verdict", "screen_classification", "record_type",
                          "categories", "llm_model", "llm_source", "llm_reasoning")}},
        })
    if not votes:
        votes = [{"model": screen.get("llm_model", ""), "verdict": "no_answer",
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
                         run: bool = False) -> dict:
    """Run Stage 3's validated front door over the `screen_expensive` pile.

    This tier has no validation mode to earn: it IS the validated screen, and its
    discard is the one Stage 3 would have made anyway. `mode` is accepted so the
    two runners have one signature, and `validation` records the votes without
    letting the discards reach the handoff.
    """
    works = _batch(con, client, release_id, TIER_EXPENSIVE, work_ids, limit,
                   pool_dir, overlay_dir, aliases, run)
    return _run_tier(con, client, release_id, TIER_EXPENSIVE, works,
                     _expensive_judge, mode=mode, batch_label=batch_label, run=run)


# ---------------------------------------------------------------------------


def _batch(con, client: ClaimsClient, release_id: str, tier: str,
           work_ids: Optional[Iterable[int]], limit: Optional[int],
           pool_dir: Path, overlay_dir: Optional[Path],
           aliases: Optional[dict[int, int]], run: bool) -> list[Work]:
    """The works this run will send: the pile, minus what is claimed or decided.

    Asked before the claim so the common case is a clean claim rather than a
    rejected one; the check that makes claiming SAFE is inside the RPC. A dry run
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
