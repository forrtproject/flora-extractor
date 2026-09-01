"""The engine's single input path: pool batches with the text overlay applied.

An overlay row REPLACES the pool's own `abstract_text`, empty or not. Every
overlay row was written deliberately by a backfill this project ran, and the rows
that need replacing rather than filling are exactly the boilerplate abstracts
("International audience", bare keyword lists) that a fill-only overlay would
leave sitting in front of the screen's voters. `overlay_hash` is a routing
release input either way, so the text a release routed under is named whichever
side supplied it.

This is the opposite of the fill-only rule this docstring described until
2026-09-01. `_apply_overlay` had already stopped obeying that rule — the code and
its own comment are the behaviour; this paragraph was simply stale.

Nor is the overlay confined to no-text rows. `overlay.worklist()` takes every
routed row whose `pending_reason` is `no_text` PLUS every ADMITTED row that
identifies an OSF record, text or no text: the OSF phase fetches the registration
template line the `osf-registration-*` specs match on, and no abstract
substitutes for it. OSF is therefore the bulk of a real overlay.

The overlay is loaded once, as a `work_id -> text` dict, and applied per batch.
It stays small relative to the pool — only backfilled rows are ever in it — so
the dict is a few hundred MB at worst against 5.1M pool rows; joining a
5.1M-row table per 50k-row batch is what this avoids.
"""

from pathlib import Path
from typing import Iterator, Optional

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from filter.engine.overlay import load_overlay, overlay_manifest_hash  # noqa: F401
from filter.engine.workids import resolve, work_id

__all__ = ["iter_pool_batches", "overlay_manifest_hash", "load_overlay"]

_ABSTRACT = "abstract_text"


def iter_pool_batches(pool_dir: Path, overlay_dir: Optional[Path] = None,
                      batch_size: int = 50_000,
                      aliases: Optional[dict[int, int]] = None,
                      ) -> Iterator[pa.RecordBatch]:
    """Every pool batch under *pool_dir*, overlay text applied over the pool's own.

    With no *overlay_dir* this is exactly `pq.ParquetFile(...).iter_batches()` —
    the same stream `build_routing()` reads on its own — so the overlay is an
    addition to the input, not a different reader.

    *aliases* is the map routing keys work by. The overlay is keyed by the same
    alias-resolved id (it is exported from the routing table), so a pool row
    carrying a merged id finds its backfilled text only when the same map is
    passed here.
    """
    overlay = load_overlay(overlay_dir) if overlay_dir else {}
    for path in sorted(Path(pool_dir).glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_size):
            yield _apply_overlay(batch, overlay, aliases or {}) if overlay else batch


def _apply_overlay(batch: pa.RecordBatch, overlay: dict[int, str],
                   aliases: dict[int, int]) -> pa.RecordBatch:
    index = batch.schema.get_field_index(_ABSTRACT)
    if index < 0:
        return batch
    # An overlay row WINS over pool text, empty or not: every overlay row was
    # written deliberately by a backfill this project ran, and the rows that
    # need replacing rather than filling are exactly the boilerplate abstracts
    # ("International audience", keyword lists) a fill-only overlay would leave
    # in front of the voters. The overlay hash names the text either way.
    ids = batch.column("id").to_pylist()
    texts = batch.column(index).to_pylist()
    replaced = 0
    for position, ident in enumerate(ids):
        text = overlay.get(resolve(work_id(ident), aliases))
        if text and text != texts[position]:
            texts[position] = text
            replaced += 1
    if not replaced:
        return batch
    return batch.set_column(index, batch.schema.field(index),
                            pa.array(texts, type=pa.string()))
