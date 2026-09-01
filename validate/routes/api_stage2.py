"""api_stage2.py — Stage 2 read out of the code, the specs and the routing store.

Same split as `api_stage1.py`. What Stage 2 ASKS — the rule book, the piles, the gate,
the voters — is read from the artifacts the engine itself loads (`filter/spec/*.json`,
`shared/config.py`, `shared/llm_client.screen_gate`). What Stage 2 DID — every pile
count, every rule's hits, the shadow evaluations — is read from the routing store for
the release actually on this machine, and arrives with its provenance.

The store queries are `GROUP BY`s over a 5.1M-row DuckDB table, which is columnar and
local: tens of milliseconds, not the per-column parquet sweep Stage 1 has to keep off
the request path. They run read-only, so a screen or extract tier running alongside is
never blocked.

One thing this deliberately cannot read: the tier VERDICTS. They live in Postgres
behind `ClaimsClient`, and a checkout with no `SUPABASE_URL` has no access to them —
which is most checkouts. That is reported as an absence naming what is missing, never
as "no work was screened".
"""
import json
from typing import Any, Optional

from flask import Blueprint, jsonify, request

from validate import sources

stage2_bp = Blueprint("stage2_api", __name__)


def _release_record(release_id: str) -> dict:
    """The six inputs the release id was built from, if the record is on disk."""
    try:
        from filter.engine.release import read_release
        return read_release(release_id)
    except Exception:                     # no record here — the id still stands
        return {}


def _alias_count() -> Optional[int]:
    """How many OpenAlex work ids the alias map folds onto another id.

    This is the whole of the pool → routed drop: routing keys by the alias-resolved
    id, so every alias collapses two pool rows into one routed work. Reported so the
    difference reads as deduplication rather than as loss.
    """
    try:
        from shared.config import BASE_DIR
        data = json.loads((BASE_DIR / "filter" / "spec" / "aliases.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    aliases = data.get("aliases")
    return len(aliases) if isinstance(aliases, dict) else None


def _routing(con, release_id: str) -> dict:
    """Everything the routing table says about this release, in three group-bys."""
    piles = {p: c for p, c in con.execute(
        "SELECT pile, count(*) FROM routing WHERE release_id = ? GROUP BY 1",
        [release_id]).fetchall()}
    reasons = {r or "": c for r, c in con.execute(
        "SELECT pending_reason, count(*) FROM routing "
        "WHERE release_id = ? AND pile = 'pending' GROUP BY 1", [release_id]).fetchall()}
    # Which rule actually WON each row, and into which pile. A rule appears twice when
    # some of its rows were downgraded to pending/no_text — that pairing is the point.
    by_rule = [{"rule_id": r, "pile": p, "count": c} for r, p, c in con.execute(
        "SELECT rule_id, pile, count(*) FROM routing "
        "WHERE release_id = ? AND rule_id <> '' GROUP BY 1, 2 ORDER BY 3 DESC",
        [release_id]).fetchall()]
    # What every rule MATCHED, live and shadow. The difference between a rule's
    # evaluations and its wins is exactly what precedence took away from it.
    evaluations = [{"spec_id": s, "matched": c} for s, c in con.execute(
        "SELECT spec_id, count(*) FROM evaluations "
        "WHERE release_id = ? AND matched GROUP BY 1 ORDER BY 2 DESC",
        [release_id]).fetchall()]
    return {"piles": piles, "pending_reasons": reasons,
            "by_rule": by_rule, "evaluations": evaluations}


def _domains(con, release_id: str) -> list[dict]:
    """Per live domain-declaring rule: population, matches, and the paid-for gap."""
    try:
        from filter.engine.spec import load_specs
        from filter.engine.store import domain_coverage
        from shared.config import BASE_DIR
        specs = load_specs(BASE_DIR / "filter" / "spec")
        return domain_coverage(con, release_id, specs)
    except Exception:                     # a panel state, never a 500
        return []


def _screen() -> dict:
    """What the expensive screen asks, and what decides its answer.

    Read from the constants the tier actually calls with, so a voter swap in
    `shared/config.py` moves this page too. The effort is shown beside each model
    because it is part of the cache key and was load-bearing in evaluation — the
    DeepSeek voter discarded 7 settled positives at effort "none".
    """
    from shared import config as C

    record: dict[str, Any] = {
        "voters": [
            {"slot": 1, "model": C.SCREENING_MODEL_1, "effort": C.SCREENING_EFFORT_1},
            {"slot": 2, "model": C.SCREENING_MODEL_2, "effort": C.SCREENING_EFFORT_2},
        ],
        "cheap_voters": [
            {"slot": 1, "model": C.PRESCREEN_MODEL_1},
            {"slot": 2, "model": C.PRESCREEN_MODEL_2},
        ],
        "prompt_version": None,
        "workers": getattr(C, "ENGINE_TIER_WORKERS", None),
    }
    for name in ("_CLASSIFY_PROMPT", "build_classify_prompt"):
        try:
            from shared.prompts import prompt_version
            record["prompt_version"] = prompt_version(name)
            record["prompt_name"] = name
            break
        except Exception:
            continue
    return record


def _handoff() -> dict:
    """The contract the screened rows cross into Stage 3 on.

    Deliberately does NOT report `handoff.HANDOFF_CSV`. That constant is a default
    name for a file nothing in the standard flow writes or reads: Stage 3's tier
    builds its worklist in process from `decisions()` plus `iter_export_rows` +
    `screen_columns`, and the one command that can materialise a CSV (`export-csv`)
    requires `--out`, so the default never applies. Serving it made the page promise a
    `data/filtered.csv` that exists on no checkout.
    """
    from filter.engine.handoff import HANDOFF_PILES
    from shared.schema import SCREEN_COLS

    return {"piles": list(HANDOFF_PILES), "screen_cols": list(SCREEN_COLS)}


# The screen's verdicts come over the network from Postgres, and the whole set is
# SLOW — measured at 31 s for 7,760 rows on 2026-09-01, because `decisions()` pages
# through every verdict. That is why this has its own endpoint: `/api/stage2` stays
# local-only and answers in milliseconds, and the page fills the last two funnel steps
# in when this arrives. Memoised for ten minutes because the verdicts only move when
# somebody runs a screen, and paying 31 s per reload to learn nothing changed is waste.
_VERDICT_TTL_SECONDS = 600
_verdict_memo: Optional[tuple] = None


def _screen_verdicts() -> tuple[dict, Any]:
    """What the expensive screen DECIDED: how many settled, dropped, and proceeded.

    This is the one number that says how the admitted pile becomes Stage 3's worklist,
    and it exists nowhere local — `decisions()` reads the permanent verdict rows from
    the state authority. A checkout that cannot reach it gets an absence naming the
    reason, never a zero: "no verdicts readable" and "nothing was screened" are
    opposite facts about a pile.

    Counted the way `handoff.py` counts: `drop` is every live discard, and the mapping
    holds only the works the tier SETTLED, so a half-screened work is in neither.
    """
    global _verdict_memo
    import time
    now = time.monotonic()
    if _verdict_memo and now - _verdict_memo[0] < _VERDICT_TTL_SECONDS:
        return _verdict_memo[1], _verdict_memo[2]

    try:
        from filter.engine.claims import ClaimsClient
        from filter.engine.handoff import decisions
        drop, decided = decisions(ClaimsClient())
    except Exception as exc:
        result: dict = {}
        prov = sources.absent("screen verdicts", str(exc)[:300])
    else:
        by_type: dict[str, int] = {}
        for record in decided.values():
            key = record.get("record_type") or "(no qualifying vote)"
            by_type[key] = by_type.get(key, 0) + 1
        result = {"settled": len(decided), "dropped": len(drop),
                  "proceeded": len(decided) - len(drop), "by_record_type": by_type}
        prov = sources._prov("screen verdicts", "live")
    _verdict_memo = (now, result, prov)
    return result, prov


@stage2_bp.route("/api/stage2")
def api_stage2():
    """The rule book's effect on this machine's release, and the hand-off to Stage 3."""
    from filter.engine.route import ADMITTED_PILES, _TEXT_PILES

    payload: dict[str, Any] = {
        "screen": _screen(),
        "handoff": _handoff(),
        "admitted_piles": sorted(ADMITTED_PILES),
        "text_piles": sorted(_TEXT_PILES),
        "aliases": _alias_count(),
        "release": None,
        "routing": {},
        "domains": [],
    }

    totals, pool_prov = sources.pool_totals_live()
    payload["pool"] = {"total": totals.get("total") if totals else None,
                       "provenance": pool_prov}

    con, store_prov = sources.routing_store()
    payload["store_provenance"] = store_prov
    if con is None:
        return jsonify(payload)

    try:
        stats, _ = sources.filtered_stats()
        release_id = stats.get("release_id")
        if not release_id:
            return jsonify(payload)
        payload["release"] = {
            "id": release_id,
            "created_at": stats.get("release_created_at"),
            "inputs": _release_record(release_id),
        }
        payload["routing"] = _routing(con, release_id)
        payload["domains"] = _domains(con, release_id)
    finally:
        con.close()
    return jsonify(payload)


@stage2_bp.route("/api/stage2/verdicts")
def api_stage2_verdicts():
    """The screen's decisions — its own endpoint because it is the slow one.

    Everything else on the Stage 2 page is local and answers in milliseconds; this
    pages 7,760 verdict rows out of Postgres and took 31 s cold. Keeping it here lets
    the page render whole and fill the last two funnel steps in afterwards, instead of
    holding every panel behind the slowest read.
    """
    verdicts, prov = _screen_verdicts()
    return jsonify({"verdicts": verdicts, "provenance": prov})


@stage2_bp.route("/api/stage2/no-text")
def api_stage2_no_text():
    """The works a screening rule claimed and the no-text downgrade held back.

    These are the one gap on the dashboard that Check cannot open, and the reason is
    structural rather than an oversight: a `pending/no_text` work has no row in
    `extracted.csv` or in any set-aside file — it never reached Stage 3 at all. It
    exists only as a routing row. So what can be offered is the routing row itself:
    the work id, the rule that wanted it, and what that rule matched on.

    Deliberately does not join the pool for titles. That would mean scanning 2,232
    parquet files on a web request; the OpenAlex id is one click from the record, and
    the id is what a `--redo` or a backfill worklist would take anyway.
    """
    limit = min(500, max(1, int(request.args.get("limit", 200))))
    payload: dict[str, Any] = {"rows": [], "total": None, "limit": limit}

    con, prov = sources.routing_store()
    payload["provenance"] = prov
    if con is None:
        return jsonify(payload)
    try:
        stats, _ = sources.filtered_stats()
        release_id = stats.get("release_id")
        if not release_id:
            return jsonify(payload)
        total = con.execute(
            "SELECT count(*) FROM routing WHERE release_id = ? AND pending_reason = ?",
            [release_id, "no_text"]).fetchone()
        payload["total"] = int(total[0]) if total else 0
        rows = con.execute(
            "SELECT work_id, rule_id, evidence FROM routing "
            "WHERE release_id = ? AND pending_reason = ? ORDER BY work_id LIMIT ?",
            [release_id, "no_text", limit]).fetchall()
        payload["rows"] = [{"work_id": int(w), "rule_id": r, "evidence": e or ""}
                           for w, r, e in rows]
    finally:
        con.close()
    return jsonify(payload)
