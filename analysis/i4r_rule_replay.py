"""Replay the frozen I4R rule candidates without operational state or API calls."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pyarrow as pa

from filter.engine.backends import eval_spec_rows
from filter.engine.route import route_batch
from filter.engine.spec import load_specs
from filter.engine.workids import load_aliases
from search.snapshot_scan import _POOL_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "misc/i4r-rule-candidates-2026-11-10.jsonl.gz"


def replay(candidates: Path, specs_dir: Path) -> dict:
    """Route frozen canonical rows with and without reproduction-claim."""
    with gzip.open(candidates, "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    specs = load_specs(specs_dir)
    spec = next(s for s in specs if s.id == "reproduction-claim")
    aliases = load_aliases(specs_dir / "aliases.json")
    batch = pa.RecordBatch.from_pylist(rows, schema=_POOL_SCHEMA)
    before = route_batch(
        [s for s in specs if s.id != spec.id], batch, aliases=aliases
    ).to_pylist()
    after = route_batch(specs, batch, aliases=aliases).to_pylist()
    gains = [
        (row, current)
        for row, previous, current in zip(rows, before, after)
        if previous["pile"] == "pending" and current["pile"] == "screen_expensive"
    ]
    return {
        "candidate_sha256": hashlib.sha256(candidates.read_bytes()).hexdigest(),
        "candidate_rows": len(rows),
        "rule_matches": sum(eval_spec_rows(spec, rows)),
        "new_screen_work_ids": len({current["work_id"] for _, current in gains}),
        "discard_pile_changes": sum(
            previous["pile"] == "discard" and current["pile"] != "discard"
            for previous, current in zip(before, after)
        ),
        "still_no_text": sum(r["pending_reason"] == "no_text" for r in after),
        "i4r_numbers": sorted({n for row, _ in gains for n in row["i4r_numbers"]}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=CANDIDATES)
    parser.add_argument("--spec-dir", type=Path, default=ROOT / "filter/spec")
    args = parser.parse_args()
    print(json.dumps(replay(args.candidates, args.spec_dir), indent=2))


if __name__ == "__main__":
    main()
