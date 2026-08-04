"""Repair works whose expensive-tier verdict is one vote instead of two.

A `screen --tier screen_expensive --run` that ran with no `GEMINI_API_KEY` got one
vote per work instead of two. `classify_replication()` refuses to cache an
incomplete screen, so nothing was checkpointed locally — but `_run_tier` had
already written the one vote it got as a permanent verdict row, and
`decided_work_ids()` treats ANY verdict row as this tier's checkpoint. Those works
are therefore excluded from every future run while `screen_gate()` returns None for
them: checkpointed as done, never screened. `CLAUDE.md`: a transient failure must
never be checkpointed as a definitive miss.

Verdicts are permanent by design (append-only trigger, migration 0001), so the
repair is additive: re-screen exactly those works and record the votes through the
same claim → judge → `record_verdict` path a normal run uses (`_run_tier` with
`_expensive_judge`), so nothing here is a second way of writing a verdict. The one
extra step is `supersede_verdict()`: the orphan vote is pointed at the new vote
from the same model, so the gate reads the two votes of one complete screen rather
than three votes of which one is a duplicate half-screen. The orphan row stays —
it is evidence of what was believed when it was spent on.

    python -m analysis.repair_single_vote                       # dry run
    python -m analysis.repair_single_vote --claim-id <uuid> --run

A one-off operator tool. It is not part of the pipeline and nothing imports it.
"""

import argparse
import copy
import sys
from pathlib import Path
from typing import Optional

from filter.engine.claims import ClaimsClient
from filter.engine.export import ALIASES_FILENAME, SPEC_DIR
from filter.engine.store import DEFAULT_STORE_PATH, open_store, releases
from filter.engine.tiers import (TIER_EXPENSIVE, Work, _expensive_decision,
                                 _expensive_judge, _run_tier, estimate,
                                 pile_works, render_estimate)
from filter.engine.workids import load_aliases
from shared import token_usage
from shared.config import SNAPSHOT_POOL_DIR


def short_voted(client: ClaimsClient, tier: str,
                claim_id: Optional[str] = None) -> dict[int, list[dict]]:
    """Works whose live verdict rows for *tier* number fewer than two.

    Restricted to *claim_id* when given: the same bug can recur, and a repair run
    should be able to name the batch it is repairing rather than sweeping every
    short-voted work the tier ever produced.
    """
    by_work: dict[int, list[dict]] = {}
    for row in client.verdicts(tier):
        by_work.setdefault(int(row["work_id"]), []).append(row)
    short = {wid: rows for wid, rows in by_work.items() if len(rows) < 2}
    if claim_id:
        short = {wid: rows for wid, rows in short.items()
                 if any(r.get("claim_id") == claim_id for r in rows)}
    return short


def _resolve_release(con, given: Optional[str]) -> str:
    if given:
        return given
    present = releases(con)
    if len(present) == 1:
        return present[0]
    raise SystemExit(f"the store holds {len(present)} releases — name one with "
                     "--release")


def _usage_delta(before: dict, after: dict) -> list[tuple[str, str, int, int]]:
    rows = []
    for provider, models in after.items():
        for model, counts in models.items():
            was = before.get(provider, {}).get(model, {})
            delta_in = counts.get("in", 0) - was.get("in", 0)
            delta_out = counts.get("out", 0) - was.get("out", 0)
            if delta_in or delta_out:
                rows.append((provider, model, delta_in, delta_out))
    return sorted(rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analysis.repair_single_vote",
        description=__doc__.splitlines()[0])
    parser.add_argument("--tier", default=TIER_EXPENSIVE)
    parser.add_argument("--release", default=None,
                        help="Release the works were screened under "
                             "(default: the store's only one).")
    parser.add_argument("--claim-id", default=None,
                        help="Repair only works with a verdict from this claim.")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    parser.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    parser.add_argument("--spec-dir", type=Path, default=SPEC_DIR)
    parser.add_argument("--overlay", type=Path, default=None)
    parser.add_argument("--batch-label", default="repair-single-vote")
    parser.add_argument("--run", action="store_true",
                        help="Claim and spend. Without it, nothing is claimed, "
                             "fetched, spent or superseded.")
    args = parser.parse_args(argv)

    con = open_store(args.store, read_only=True)
    release_id = _resolve_release(con, args.release)
    client = ClaimsClient()

    short = short_voted(client, args.tier, args.claim_id)
    if not short:
        print(f"no short-voted work in tier {args.tier}"
              + (f" under claim {args.claim_id}" if args.claim_id else ""))
        return 0

    works: list[Work] = pile_works(con, release_id, args.tier, args.pool,
                                   args.overlay,
                                   load_aliases(args.spec_dir / ALIASES_FILENAME),
                                   only=set(short))
    print(f"{len(short)} work(s) with fewer than two verdict rows in tier "
          f"{args.tier}, release {release_id[:12]}:")
    for work in works:
        votes = ", ".join(f"{r.get('model') or '?'}={r.get('verdict')}"
                          for r in short[work.work_id])
        print(f"  W{work.work_id}  {work.doi or '(no doi)':<32} [{votes}]  "
              f"{work.title[:70]}")
    missing = set(short) - {w.work_id for w in works}
    if missing:
        # Routed elsewhere (or not at all) in this release: re-screening one here
        # would record a verdict against a pile it is no longer in.
        print(f"  {len(missing)} work(s) not in this release's {args.tier} pile, "
              f"skipped: {sorted(missing)}")
    if not works:
        return 0

    est = estimate(works, args.tier)
    print()
    if not args.run:
        print(render_estimate(est))
        print("\nDry run — nothing claimed, spent or superseded. Re-run with --run.")
        return 0
    # render_estimate() speaks in the dry run's voice ("nothing spent"), so a live
    # run prints the number rather than the paragraph; what was actually spent is
    # read off token_usage below.
    print(f"about to spend ≈ ${est['usd']:.4f} on {est['rows']} work(s) "
          f"({args.tier}, both voters)")

    before = copy.deepcopy(token_usage.usage())
    report = _run_tier(con, client, release_id, args.tier, works, _expensive_judge,
                       mode="live", batch_label=args.batch_label, run=True)
    print(f"\nclaim {report['claim_id']}  —  {report['decided']:,} work(s) decided, "
          f"{report['verdicts']:,} verdict(s) recorded")
    for outcome, count in sorted(report["outcomes"].items()):
        print(f"  {outcome:<12} {count:,}")

    _supersede_orphans(client, args.tier, report["claim_id"], short)
    _verify(client, args.tier, set(short) & {w.work_id for w in works})

    print("\ntokens spent by this run (shared/token_usage.py):")
    for provider, model, tin, tout in _usage_delta(before, token_usage.usage()):
        print(f"  {provider:<12} {model:<28} in {tin:,}  out {tout:,}")
    return 0


def _supersede_orphans(client: ClaimsClient, tier: str, claim_id: str,
                       short: dict[int, list[dict]]) -> None:
    """Point each orphan vote at the new vote from the same model.

    Without this the work carries three live votes — the half-screen's one and the
    complete screen's two — and the gate would count one model twice.
    """
    fresh: dict[int, list[dict]] = {}
    for row in client.verdicts(tier, [claim_id]):
        fresh.setdefault(int(row["work_id"]), []).append(row)
    done = 0
    for work_id, orphans in short.items():
        replacements = fresh.get(work_id, [])
        if not replacements:
            continue
        for orphan in orphans:
            same = [r for r in replacements if r.get("model") == orphan.get("model")]
            new = (same or replacements)[0]
            try:
                client.supersede_verdict(orphan["id"], new["id"])
                done += 1
            except Exception as exc:  # noqa: BLE001 — reported, not fatal
                print(f"  could not supersede verdict {orphan['id']} of "
                      f"W{work_id}: {exc}")
    print(f"  {done} orphan vote(s) superseded by the re-screen's own vote")


def _verify(client: ClaimsClient, tier: str, work_ids: set[int]) -> None:
    """Read the state authority back: two votes each, and a gate that decides."""
    by_work: dict[int, list[dict]] = {}
    for row in client.verdicts(tier):
        if int(row["work_id"]) in work_ids:
            by_work.setdefault(int(row["work_id"]), []).append(row)
    still_short = [w for w in work_ids if len(by_work.get(w, [])) < 2]
    decisions = {w: _expensive_decision(rows)["outcome"] for w, rows in by_work.items()}
    split: dict[str, int] = {}
    for outcome in decisions.values():
        split[outcome] = split.get(outcome, 0) + 1
    print(f"\nverification — {len(work_ids) - len(still_short)}/{len(work_ids)} "
          "work(s) now carry two or more live verdict rows")
    if still_short:
        print(f"  STILL SHORT: {sorted(still_short)}")
    for outcome, count in sorted(split.items()):
        print(f"  gate {outcome:<12} {count:,}")


if __name__ == "__main__":
    sys.exit(main())
