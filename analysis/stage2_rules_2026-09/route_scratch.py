"""Route the pool in memory, write per-work evaluation masks — no store, no release.

The side-effect-free twin of `python -m filter.engine route`: it reads the same
input path (`iter_pool_batches` with the frozen overlay and the alias map) and
evaluates the same specs with the same backend (`eval_all`), but it writes a
parquet under `cache/stage2_rules_2026-09/` instead of a release in the DuckDB
store, and registers nothing with Postgres. `route` cannot be pointed at a
scratch store safely: it also registers the release it mints with the state
authority (`_register_release` in `filter/engine/cli.py`).

Every live pile is a pure function of the masks (first non-shadow spec by
precedence wins; a screening pile is downgraded to `pending/no_text` when the
abstract is empty and the row is not a titled OSF record), so one pass answers
any "what if spec X were shadow / promoted" question offline — see
`variants.py`.

    .venv/bin/python -m analysis.stage2_rules_2026-09.route_scratch \
        --extra-spec-dir analysis/stage2_rules_2026-09/specs --out cache/stage2_rules_2026-09/evals.parquet
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from filter.engine.backends import BatchContext
from filter.engine.pool_reader import iter_pool_batches
from filter.engine.route import _screenable_on_its_title, eval_all
from filter.engine.spec import load_specs
from filter.engine.workids import load_aliases, resolve, work_id
from shared.config import OVERLAY_DIR, SNAPSHOT_POOL_DIR

SPEC_DIR = Path("filter/spec")


def _source_name(primary_location: str) -> str:
    try:
        loc = json.loads(primary_location or "{}") or {}
        return ((loc.get("source") or {}).get("display_name")) or ""
    except (ValueError, TypeError, AttributeError):
        return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra-spec-dir", type=Path, default=None,
                    help="Candidate specs evaluated alongside the bundle (ids must be new).")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    specs = load_specs(SPEC_DIR)
    extra = load_specs(args.extra_spec_dir) if args.extra_spec_dir else []
    every = specs + extra
    aliases = load_aliases(SPEC_DIR / "aliases.json")
    writer = None
    for batch in iter_pool_batches(SNAPSHOT_POOL_DIR, OVERLAY_DIR, aliases=aliases):
        ctx = BatchContext(batch)
        evals = eval_all(every, batch, ctx)
        masks = {sid: np.asarray(m.to_numpy(zero_copy_only=False), dtype=bool)
                 for sid, m in evals.items()}
        any_hit = np.zeros(batch.num_rows, dtype=bool)
        for m in masks.values():
            any_hit |= m
        idx = np.nonzero(any_hit)[0]
        if not len(idx):
            continue
        empty = np.asarray(ctx.abstract_empty.to_numpy(zero_copy_only=False), dtype=bool)
        ids = batch.column("id").to_pylist()
        titles = ctx.title.to_pylist()
        abstracts = ctx.abstract.to_pylist()
        dois = ctx.doi.to_pylist()
        years = batch.column("publication_year").to_pylist()
        types = batch.column("type").to_pylist()
        locs = batch.column("primary_location").to_pylist()
        cols = {
            "work_id": [resolve(work_id(ids[i]), aliases) for i in idx],
            "raw_work_id": [work_id(ids[i]) for i in idx],
            "doi": [dois[i] for i in idx],
            "title": [titles[i] for i in idx],
            "abstract_len": [len(abstracts[i] or "") for i in idx],
            "abstract_head": [(abstracts[i] or "")[:400] for i in idx],
            "year": [years[i] for i in idx],
            "type": [types[i] for i in idx],
            "source": [_source_name(locs[i]) for i in idx],
            "abstract_empty": [bool(empty[i]) for i in idx],
            "screenable_title": [bool(empty[i]) and _screenable_on_its_title(ctx, int(i))
                                 for i in idx],
        }
        for sid, m in masks.items():
            cols["m:" + sid] = m[idx].tolist()
        table = pa.table(cols)
        if writer is None:
            writer = pq.ParquetWriter(args.out, table.schema)
        writer.write_table(table)
    if writer:
        writer.close()
    order = [s.id for s in every]
    args.out.with_suffix(".specs.json").write_text(json.dumps(
        {"order": order, "live": [s.id for s in specs],
         "shadow": {s.id: s.shadow for s in every},
         "pile": {s.id: s.pile for s in every},
         "precedence": {s.id: s.precedence for s in every}}, indent=1))


if __name__ == "__main__":
    main()
