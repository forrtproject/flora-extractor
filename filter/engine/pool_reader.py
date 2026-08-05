"""The engine's single input path: pool batches with the text overlay applied.

The pool's own `abstract_text` is primary evidence — it is what the snapshot
actually shipped. An overlay is recovered text from somewhere else, so it FILLS
and never replaces: a row whose pool text is non-empty comes back untouched even
when the overlay holds a different string for it. That asymmetry is the whole
contract; a coalesce in the other direction would let a backfill source quietly
rewrite the corpus the scan was measured on.

The overlay is loaded once, as a `work_id -> text` dict, and applied per batch.
Overlays are small relative to the pool by construction (only no-text rows are
ever backfilled), so the dict is a few hundred MB at worst against 5.1M pool
rows; joining a 5.1M-row table per 50k-row batch is what this avoids.
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
    """Every pool batch under *pool_dir*, overlay text coalesced over empty cells.

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
