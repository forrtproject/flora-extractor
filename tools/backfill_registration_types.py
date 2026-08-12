"""backfill_registration_types.py — six OSF-registration works typed as replications.

Each of these works is a registration whose form is the only document the pipeline
ever saw, so the screen read a protocol template rather than a completed study and the
row went out with no `type`. The maintainer confirmed each one is a replication; this
writes that, and only that, onto the stored verdicts.

One-off. It supersedes each work's latest result verdict with a copy whose targets
carry `type = replication` — the tier's own correction route
(`supersede_targets()` in `extract/tier.py`), so the ending, the generation, the
confidence and every other field are the old row's. `type` is not a FILTERED_COLS
value, so it lives on the target entry and no `paper_type`/`filter_status` key is
touched. `python -m extract.export --release <id>` renders the correction.

The works listed here all belong to one routing release; `supersede_targets()`
refuses a patch set that spans several, and that refusal is left to surface.

    .venv/bin/python -m tools.backfill_registration_types                  # sandbox
    .venv/bin/python -m tools.backfill_registration_types --mode live
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from extract.export import latest_results
from extract.tier import supersede_targets
from filter.engine.claims import ClaimsClient, ClaimsNotConfigured

# The OSF registrations the maintainer confirmed as replications. W6944082684 is
# deliberately absent: its three rows go to the human queue.
WORK_IDS = (6887736624, 6906241149, 6925301846, 6962545633, 6963062034, 6963112413)

NEW_TYPE = "replication"
BATCH_LABEL = "backfill-registration-types"


def patches(results: dict[int, dict]) -> dict[str, dict]:
    """`pair_id → {"type": "replication"}` for every target of the six works.

    Prints each work's rows as `old → new`, so a run is readable before and after it
    writes. A work with no stored verdict in this mode is reported and skipped: the
    sandbox holds a different set of verdicts from the live store.
    """
    out: dict[str, dict] = {}
    for work in WORK_IDS:
        result = results.get(work)
        if result is None:
            print(f"  W{work}: no result verdict in this mode — skipped")
            continue
        targets = (result.get("payload") or {}).get("targets") or []
        if not targets:
            print(f"  W{work}: the stored verdict has no target rows — skipped")
            continue
        for target in targets:
            pair = str(target.get("pair_id") or "")
            old = str(target.get("type") or "") or "(blank)"
            if not pair:
                print(f"  W{work}: a target carries no pair_id — skipped")
                continue
            print(f"  W{work} {pair}: {old} → {NEW_TYPE}")
            out[pair] = {"type": NEW_TYPE}
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.backfill_registration_types",
        description="Set type=replication on the six confirmed OSF-registration "
                    "works, by superseding their stored result verdicts.")
    parser.add_argument("--mode", choices=("live", "validation"),
                        default="validation",
                        help="Which runs' verdicts to correct (claim meta.mode). "
                             "Default validation, so the sandbox is exercised first.")
    args = parser.parse_args(argv)

    try:
        client = ClaimsClient()
    except ClaimsNotConfigured as exc:
        raise SystemExit(f"{exc}. The verdicts this corrects live in the state "
                         "authority, so there is nothing to correct without it.")

    results, _ = latest_results(client, mode=args.mode)
    print(f"mode {args.mode}  —  {len(WORK_IDS)} work(s) to type as {NEW_TYPE}")
    changes = patches(results)
    if not changes:
        print("  nothing to correct")
        return 1

    report = supersede_targets(client, changes, batch_label=BATCH_LABEL,
                               mode=args.mode)
    print(f"  {report['works']:,} work(s), {report['rows']:,} row(s) corrected "
          f"under claim {report['claim'][:12]}")
    if report["unmatched"]:
        print(f"  unmatched pair_id(s): {', '.join(report['unmatched'])}")
    print("  Render the CSV with: .venv/bin/python -m extract.export --release <id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
