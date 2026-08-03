"""What one rule actually does — the §3 diagnostics a rule must pass to be trusted.

No rule admits, and a discard rule needs measured precision, so before a spec is
believed someone has to see the rows it moves and the rows it merely duplicates.
`diagnose()` answers that by routing the pool twice, with the spec and without
it, and reporting the difference: where rows moved, which other rules were
already claiming them, a readable seeded sample to eyeball, and the state of the
holdout and of the spec's own `measured` evidence.

The two routings run in memory rather than through the store: a diagnosis is a
question about a candidate bundle that may never become a release, and minting a
release id for it would put an unreviewed rule into the release namespace.

Only the moved rows are retained row-wise; everything else is counted as it
streams, because the delta of one rule is small and the pool is not.
"""

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
import pyarrow.parquet as pq

from filter.engine.route import eval_all, route_batch
from filter.engine.spec import FilterSpec, load_specs

HOLDOUT_FILENAME = "holdout.json"


def diagnose(pool_dir: Path, spec_dir: Path, spec_id: str, *,
             baseline_dir: Optional[Path] = None, sample_n: int = 20,
             seed: int = 17) -> dict:
    """Everything known about what *spec_id* changes, over the pool in *pool_dir*."""
    spec_dir = Path(spec_dir)
    with_specs = load_specs(spec_dir)
    target = next((s for s in with_specs if s.id == spec_id), None)
    if target is None:
        raise ValueError(f"no spec {spec_id!r} in {spec_dir}")
    without_specs = (load_specs(Path(baseline_dir)) if baseline_dir
                     else [s for s in with_specs if s.id != spec_id])

    moved: Counter = Counter()
    moved_rows: list[dict] = []
    matched = 0
    exclusive = 0
    covered = 0
    by_spec: dict[str, Counter] = defaultdict(Counter)
    rows = 0

    for path in sorted(Path(pool_dir).glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000):
            evals = eval_all(with_specs, batch)
            routed_with = route_batch(with_specs, batch, evals=evals).to_pylist()
            # Same spec objects when no baseline bundle is named, so the masks
            # already computed are the without-bundle's masks too.
            reuse = None if baseline_dir else {s.id: evals[s.id] for s in without_specs}
            routed_without = route_batch(without_specs, batch, evals=reuse).to_pylist()
            target_mask = np.asarray(
                evals[spec_id].to_numpy(zero_copy_only=False), dtype=bool)
            others = {sid: np.asarray(mask.to_numpy(zero_copy_only=False), dtype=bool)
                      for sid, mask in evals.items() if sid != spec_id}
            titles = batch.column("display_name").to_pylist()
            fallback = batch.column("title").to_pylist()
            years = batch.column("publication_year").to_pylist()
            rows += batch.num_rows

            for index in range(batch.num_rows):
                after, before = routed_with[index], routed_without[index]
                if after["pile"] != before["pile"] or after["rule_id"] != before["rule_id"]:
                    moved[f"{before['pile']}->{after['pile']}"] += 1
                    moved_rows.append({
                        "work_id": after["work_id"],
                        "title": titles[index] or fallback[index] or "",
                        "year": years[index],
                        "from_pile": before["pile"],
                        "to_pile": after["pile"],
                        "evidence": after["evidence"],
                    })
                if not target_mask[index]:
                    continue
                matched += 1
                hits = [sid for sid, mask in others.items() if mask[index]]
                if not hits:
                    exclusive += 1
                if after["rule_id"] != spec_id:
                    covered += 1
                for sid in hits:
                    by_spec[sid]["both"] += 1
                    if after["rule_id"] == sid:
                        by_spec[sid]["covered_by"] += 1

    return {
        "spec_id": spec_id,
        "pile": target.pile,
        "precedence": target.precedence,
        "shadow": target.shadow,
        "pool_rows": rows,
        "baseline": str(baseline_dir) if baseline_dir else "bundle minus the spec",
        "moved": dict(moved),
        "overlap": {
            "matched": matched,
            "exclusive": exclusive,
            "covered": covered,
            "by_spec": {sid: dict(counts) for sid, counts in sorted(by_spec.items())},
        },
        "sample": _sample(moved_rows, sample_n, seed),
        "holdout": _holdout(spec_dir),
        "measurement": _measurement(target),
    }


def _sample(rows: list[dict], n: int, seed: int) -> list[dict]:
    """*n* moved rows, ordered by a seeded digest of the work id — stable across runs."""
    ordered = sorted(rows, key=lambda r: hashlib.md5(
        f"{seed}:{r['work_id']}".encode()).hexdigest())
    return ordered[:n]


def _holdout(spec_dir: Path) -> object:
    path = spec_dir / HOLDOUT_FILENAME
    if not path.exists():
        return "not_constructed"
    return json.loads(path.read_text(encoding="utf-8"))


def _measurement(spec: FilterSpec) -> dict:
    if spec.pile != "discard":
        return {"required": False,
                "note": "only a discard rule needs measured precision"}
    return {"required": True, "shadow": spec.shadow,
            "measured": [dict(entry) for entry in spec.measured]}


def render_text(report: dict) -> str:
    """The report as a compact block for a terminal."""
    lines = [
        f"spec {report['spec_id']} — pile {report['pile']} "
        f"prec {report['precedence']}{' (shadow)' if report['shadow'] else ''}",
        f"  pool rows        {report['pool_rows']:,}   baseline: {report['baseline']}",
        "  moved:" + ("" if report["moved"] else "  (nothing)"),
    ]
    for transition, count in sorted(report["moved"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {transition:<40} {count:,}")
    overlap = report["overlap"]
    lines.append(f"  matched {overlap['matched']:,}  exclusive {overlap['exclusive']:,}"
                 f"  covered {overlap['covered']:,}")
    for sid, counts in sorted(overlap["by_spec"].items(),
                              key=lambda kv: -kv[1].get("both", 0)):
        lines.append(f"    also matched by {sid:<26} {counts.get('both', 0):,}"
                     f"  (won: {counts.get('covered_by', 0):,})")
    lines.append(f"  holdout: {report['holdout']}")
    measurement = report["measurement"]
    if measurement["required"]:
        levels = ", ".join(str(e.get("level")) for e in measurement["measured"]) \
            or ("shadow — no pile effect" if measurement["shadow"] else "NONE")
        lines.append(f"  measured: {levels}")
    lines.append("  sample:" + ("" if report["sample"] else "  (nothing moved)"))
    for row in report["sample"]:
        lines.append(f"    W{row['work_id']} {str(row['year'] or ''):<5} "
                     f"{row['from_pile']}->{row['to_pile']}  {row['title'][:60]}"
                     + (f"  [{row['evidence']}]" if row["evidence"] else ""))
    return "\n".join(lines)
