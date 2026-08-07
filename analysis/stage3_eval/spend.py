"""What one evaluation iteration actually cost, from the meters that already exist.

`cache/token_usage.json` is per DAY, so two runs on one day are indistinguishable in
it. This snapshots it before a run and diffs it after, which is the only honest
per-run number available without building a second metering system.

    python -m analysis.stage3_eval.spend snapshot eval-dev-0
    ...run...
    python -m analysis.stage3_eval.spend report eval-dev-0

Cached calls record nothing, by design — the diff is what was BOUGHT, which is the
number the budget is spent against, not what the run would have cost cold.
"""

import argparse
import json
from pathlib import Path

from shared.config import CACHE_DIR

# Rough list prices per 1,000 tokens, for turning a token count into dollars. Same
# status as the dry run's price list in filter/engine/tiers.py: an order-of-magnitude
# answer, not a billing record. Update in the same commit as a model change.
PRICE_PER_1K = {
    "gpt-5.4-mini":            (0.00025, 0.00200),
    "gemini-3.5-flash-lite":   (0.00010, 0.00040),
    "gemini-3-flash-preview":  (0.00030, 0.00250),
}
HERE = Path(__file__).parent
USAGE = CACHE_DIR / "token_usage.json"


def _flat(path: Path) -> dict[str, tuple[int, int]]:
    """{model: (in, out)} summed over every day and provider in the usage file."""
    raw = json.loads(path.read_text()) if path.exists() else {}
    total: dict[str, tuple[int, int]] = {}
    for by_provider in raw.values():
        for models in by_provider.values():
            for model, counts in models.items():
                had = total.get(model, (0, 0))
                total[model] = (had[0] + int(counts.get("in", 0) or 0),
                                had[1] + int(counts.get("out", 0) or 0))
    return total


def _snap_path(label: str) -> Path:
    return HERE / f".spend_{label}.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("snapshot", "report"))
    ap.add_argument("label")
    args = ap.parse_args()

    if args.action == "snapshot":
        _snap_path(args.label).write_text(json.dumps(_flat(USAGE)))
        print(f"snapshot taken for {args.label}")
        return

    before = {k: tuple(v) for k, v in
              json.loads(_snap_path(args.label).read_text()).items()}
    after, dollars = _flat(USAGE), 0.0
    print(f"{args.label}:")
    for model in sorted(after):
        d_in = after[model][0] - before.get(model, (0, 0))[0]
        d_out = after[model][1] - before.get(model, (0, 0))[1]
        if not (d_in or d_out):
            continue
        p_in, p_out = PRICE_PER_1K.get(model, (0.0, 0.0))
        cost = d_in / 1000 * p_in + d_out / 1000 * p_out
        dollars += cost
        print(f"  {model:26s} in {d_in:9,d}  out {d_out:9,d}  ~${cost:.2f}"
              + ("" if model in PRICE_PER_1K else "  (no price listed)"))
    print(f"  {'total':26s} {'':28s}~${dollars:.2f}")


if __name__ == "__main__":
    main()
