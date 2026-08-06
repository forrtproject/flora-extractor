"""Text overlays: frozen releases of abstract text the pool did not ship.

Milestone 3 of issue #146. A row the scan kept with no abstract is not a row an
LLM tier may read — `route.py` downgrades it to `pending/no_text` — so the only
way it ever reaches a screen is for text to arrive from somewhere else. An
overlay is that somewhere else, and it is a RELEASE rather than a mutable table:

- **The files are append-only parquet chunks** (`overlay-<seq>.parquet`). A
  backfill run appends; it never rewrites what an earlier run recovered.
- **The manifest is immutable.** `freeze()` writes
  `overlay_manifest-<hash12>.json` and points the mutable `overlay_manifest.json`
  at it. Freezing again after the files changed mints a new manifest and moves the
  pointer; the old manifest still describes exactly the bytes it described (§4:
  "a mutable 'latest' pointer is fine, the target is not").
- **`overlay_hash` is a routing release input.** It enters
  `routing_release(...)`, so text arriving re-routes the rows it arrived for and
  mints a new release id. That is the text-revision invalidation: a release id
  cannot silently mean two different corpora.

One work id may appear in one overlay chunk only. "Later file wins" would make
the overlay's content depend on file ordering — and so on which run happened to
be interrupted — which is the opposite of a frozen release; `validate()` refuses
it.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

from filter.engine.workids import resolve, work_id

OVERLAY_SCHEMA = pa.schema([
    ("work_id", pa.int64()),
    ("abstract_text", pa.string()),
    ("source", pa.string()),
    ("fetched_at", pa.string()),
])

WORKLIST_SCHEMA = pa.schema([
    ("work_id", pa.int64()),
    ("doi", pa.string()),
    ("title", pa.string()),
    ("year", pa.int32()),
])

CHUNK_GLOB = "overlay-*.parquet"
POINTER_NAME = "overlay_manifest.json"

# The pending reason the backfill exists to clear.
NO_TEXT = "no_text"


class OverlayError(RuntimeError):
    """The overlay directory is not a valid overlay."""


# ---------------------------------------------------------------------------
# Worklist
# ---------------------------------------------------------------------------


def worklist(con, release_id: str, pool_dir: Path, out_path: Path,
             aliases: Optional[dict[int, int]] = None) -> int:
    """Write the backfill worklist for *release_id* to *out_path*; return its rows.

    Every routed row with `pending_reason = 'no_text'`, joined back to the pool
    for the identifiers a fetcher needs (doi, title, year). The join lives here
    rather than in `store.py`: the store knows routing, the pool knows metadata,
    and the overlay is the only thing that needs both.
    """
    pending = {row[0] for row in con.execute(
        "SELECT work_id FROM routing WHERE release_id = ? AND pending_reason = ?",
        [release_id, NO_TEXT]).fetchall()}

    rows: list[dict] = []
    seen: set[int] = set()
    for path in sorted(Path(pool_dir).glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(
                batch_size=50_000,
                columns=["id", "doi", "title", "display_name", "publication_year"]):
            for record in batch.to_pylist():
                resolved = resolve(work_id(record["id"]), aliases or {})
                if resolved not in pending or resolved in seen:
                    continue
                seen.add(resolved)
                rows.append({
                    "work_id": resolved,
                    "doi": record.get("doi") or "",
                    "title": record.get("display_name") or record.get("title") or "",
                    "year": record.get("publication_year"),
                })

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=WORKLIST_SCHEMA), out_path)
    return len(rows)


def read_worklist(path: Path) -> list[dict]:
    """The worklist at *path* as plain dicts."""
    return pq.read_table(path).to_pylist()


def iter_worklist(path: Path, batch_size: int = 50_000):
    """The worklist at *path* as plain dicts, one parquet batch at a time.

    The same rows `read_worklist()` returns, without holding them all. A `no_text`
    worklist is a few thousand rows — it is capped by what the rules claimed, and
    the pool's millions of textless works are `no_filter_matched`, which nothing
    puts on a worklist — but a hand-built wide one is not, and `to_pylist()` over
    the whole file is the shape that made a bulk backfill a multi-GB process.
    """
    for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_size):
        yield from batch.to_pylist()


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


def chunk_paths(overlay_dir: Path) -> list[Path]:
    return sorted(Path(overlay_dir).glob(CHUNK_GLOB))


def next_chunk_path(overlay_dir: Path) -> Path:
    """The next free `overlay-<seq>.parquet` under *overlay_dir*."""
    return Path(overlay_dir) / f"overlay-{len(chunk_paths(overlay_dir)):04d}.parquet"


def write_chunk(overlay_dir: Path, rows: list[dict]) -> Optional[Path]:
    """Append *rows* as a new overlay chunk. Returns its path, or None if empty."""
    if not rows:
        return None
    Path(overlay_dir).mkdir(parents=True, exist_ok=True)
    path = next_chunk_path(overlay_dir)
    pq.write_table(pa.Table.from_pylist(rows, schema=OVERLAY_SCHEMA), path)
    return path


def load_overlay(overlay_dir: Optional[Path]) -> dict[int, str]:
    """The whole overlay as `work_id -> abstract_text`, chunks in name order.

    First writer wins on a repeated id so the map does not depend on which run
    wrote last; `validate()` is what refuses the repeat in the first place.
    """
    overlay: dict[int, str] = {}
    if not overlay_dir or not Path(overlay_dir).exists():
        return overlay
    for path in chunk_paths(overlay_dir):
        table = pq.read_table(path, columns=["work_id", "abstract_text"])
        for wid, text in zip(table.column("work_id").to_pylist(),
                             table.column("abstract_text").to_pylist()):
            if text and wid not in overlay:
                overlay[wid] = text
    return overlay


def overlay_work_ids(overlay_dir: Optional[Path]) -> set[int]:
    """Every work id the overlay already holds a row for, text or not.

    Backfill resumption reads this: a row already written must not be written
    into a second chunk, which `validate()` would then refuse.
    """
    if not overlay_dir or not Path(overlay_dir).exists():
        return set()
    ids: set[int] = set()
    for path in chunk_paths(overlay_dir):
        ids.update(pq.read_table(path, columns=["work_id"]).column("work_id").to_pylist())
    return ids


# ---------------------------------------------------------------------------
# Validation and freezing
# ---------------------------------------------------------------------------


def validate(overlay_dir: Path) -> list[str]:
    """Error strings for *overlay_dir*; empty means it can be frozen."""
    errors: list[str] = []
    paths = chunk_paths(overlay_dir)
    if not paths:
        return [f"no {CHUNK_GLOB} files under {overlay_dir}"]

    owner: dict[int, str] = {}
    for path in paths:
        table = pq.read_table(path)
        if table.schema.names != OVERLAY_SCHEMA.names:
            errors.append(f"{path.name}: columns {table.schema.names} — expected "
                          f"{OVERLAY_SCHEMA.names}")
            continue
        if table.schema.types != OVERLAY_SCHEMA.types:
            errors.append(f"{path.name}: column types {table.schema.types} — expected "
                          f"{OVERLAY_SCHEMA.types}")
            continue
        for wid in table.column("work_id").to_pylist():
            if wid is None:
                errors.append(f"{path.name}: a row has a null work_id")
                continue
            first = owner.get(wid)
            if first is not None:
                errors.append(
                    f"work_id {wid} appears in both {first} and {path.name}. An "
                    "overlay is a frozen release: one work has one text, and "
                    "'later file wins' would make the overlay depend on which run "
                    "was interrupted. Rewrite the chunks so each work appears once.")
            else:
                owner[wid] = path.name
    return errors


def overlay_hash(overlay_dir: Path) -> str:
    """sha256 over the sorted (name, content hash) pairs of the overlay chunks."""
    digest = hashlib.sha256()
    for path in chunk_paths(overlay_dir):
        digest.update(path.name.encode("utf-8"))
        digest.update(b":")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def freeze(overlay_dir: Path, pool_manifest_hash: Optional[str] = None,
           created_at: Optional[str] = None) -> dict:
    """Validate *overlay_dir*, write its immutable manifest, move the pointer.

    Returns the manifest. Refreezing unchanged files rewrites the same manifest
    bytes under the same name; refreezing after a chunk was added or edited mints
    a new name and repoints `overlay_manifest.json` at it — the previous manifest
    stays on disk, still describing the bytes it was written for.
    """
    overlay_dir = Path(overlay_dir)
    errors = validate(overlay_dir)
    if errors:
        raise OverlayError("overlay cannot be frozen:\n  " + "\n  ".join(errors))

    files = []
    rows = 0
    sources: dict[str, int] = {}
    for path in chunk_paths(overlay_dir):
        table = pq.read_table(path)
        rows += table.num_rows
        files.append({"name": path.name, "rows": table.num_rows,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for source in table.column("source").to_pylist():
            sources[source or "unknown"] = sources.get(source or "unknown", 0) + 1

    digest = overlay_hash(overlay_dir)
    manifest = {
        "overlay_hash": digest,
        "rows": rows,
        "files": files,
        "sources": dict(sorted(sources.items())),
        # Which pool the backfilled ids were named against. An overlay keyed by
        # work ids from one pool means nothing over another.
        "parent_pool_manifest_hash": pool_manifest_hash,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }
    manifest_name = f"overlay_manifest-{digest[:12]}.json"
    (overlay_dir / manifest_name).write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    (overlay_dir / POINTER_NAME).write_text(
        json.dumps({"overlay_hash": digest, "manifest": manifest_name},
                   indent=1, sort_keys=True), encoding="utf-8")
    return manifest


def overlay_manifest_hash(overlay_dir: Optional[Path]) -> Optional[str]:
    """The frozen overlay hash for *overlay_dir*, or None if there is no overlay.

    None is the honest answer for "no overlay" and is exactly what the release id
    has carried in the `overlay_hash` slot since M1 — so a pool routed with no
    overlay keeps its release id when M3 ships. An overlay directory that exists
    but was never frozen raises: routing under unfrozen text would bind a release
    to bytes nobody named.
    """
    if not overlay_dir:
        return None
    pointer = Path(overlay_dir) / POINTER_NAME
    if not pointer.exists():
        if chunk_paths(overlay_dir):
            raise OverlayError(
                f"{overlay_dir} holds overlay chunks but no {POINTER_NAME} — run "
                "`freeze()` before routing under it, so the release id names the "
                "text it actually read.")
        return None
    return json.loads(pointer.read_text(encoding="utf-8"))["overlay_hash"]


def read_manifest(overlay_dir: Path) -> dict:
    """The manifest the pointer currently names."""
    pointer = json.loads((Path(overlay_dir) / POINTER_NAME).read_text(encoding="utf-8"))
    return json.loads((Path(overlay_dir) / pointer["manifest"]).read_text(encoding="utf-8"))
