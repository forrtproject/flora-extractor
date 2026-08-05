"""
abstract_store.py — The abstract cache, as one SQLite file instead of 478k of them.

Every identifier a phase in `search/fetch_abstracts.py` tries gets one row here:
the text it recovered, or NULL for a definitive miss. That single fact replaces
three files that had to agree with each other —

* `cache/abstracts/<md5>.json`, one ~90-byte file per identifier. 42 MB of content
  occupying 1.8 GB, because a 4 KB block is the smallest thing a filesystem hands
  out, and any operation over the whole cache became half a million syscalls: a
  scan took ~4 minutes, packing it for `shared/cache_sync.py` ~100 seconds.
* `cache/fetch_abstracts_done.txt`, the checkpoint — "this identifier has been
  tried" — which is now simply whether the row exists.
* `cache/fetch_abstracts_found.txt`, the sidecar index that existed ONLY because
  answering "did an earlier phase already resolve this row?" from the files
  themselves took ~2 hours. It is now `WHERE abstract IS NOT NULL`.

The two sidecars were both workarounds for the file-per-key layout, and each was
a store that could drift from the cache it described. A miss and its checkpoint
line can no longer disagree, because they are one row.

A NULL abstract is a definitive miss, kept deliberately: not re-buying a known
miss is most of what the cache is for. What is never recorded is a TRANSIENT
failure — the phase runners simply do not call `record()` — so a 429 stays
retryable rather than being frozen into a fact.

Writers are the phase runners, which are sequential; WAL mode plus a connection
lock keeps a reader in another thread safe anyway.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from shared.config import ABSTRACT_DB_PATH, log

_SCHEMA = """
CREATE TABLE IF NOT EXISTS abstracts (
    ident      TEXT PRIMARY KEY,   -- "<namespace>:<id>", e.g. "scopus:10.1/x"
    abstract   TEXT,               -- NULL is a definitive miss, not "not tried"
    fetched_at TEXT NOT NULL
);
-- Answers "which identifiers actually resolved?" without reading the text column,
-- which is the question `_already_resolved` asks once per row of the worklist.
CREATE INDEX IF NOT EXISTS abstracts_found ON abstracts(ident) WHERE abstract IS NOT NULL;
"""

_conn: Optional[sqlite3.Connection] = None
_conn_path: Optional[Path] = None
# Reentrant: the write helpers hold the lock across `connection()`, which takes it
# again to create the connection on first use. A plain Lock deadlocks there.
_lock = threading.RLock()


def connection(path: Optional[Path] = None) -> sqlite3.Connection:
    """The open connection, created on first use.

    Reopened when *path* names a different file than the one currently open —
    which is what lets a test point the store at a tmp_path without leaking the
    real cache into it.
    """
    global _conn, _conn_path
    target = Path(path or ABSTRACT_DB_PATH)
    with _lock:
        if _conn is not None and _conn_path == target:
            return _conn
        if _conn is not None:
            _conn.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(target, check_same_thread=False)
        # WAL so a reader never blocks the writing phase; NORMAL because losing the
        # last few rows of a killed run costs a re-fetch, not correctness, and the
        # phases write one row per network call — fsyncing each would dominate.
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
        _conn_path = target
        return _conn


def close() -> None:
    """Close the connection, if one is open. For tests and one-shot scripts."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn, _conn_path = None, None


def checkpoint_wal() -> None:
    """Fold the write-ahead log back into the database file.

    WAL mode means recent rows live in `abstracts.sqlite-wal`, not in
    `abstracts.sqlite`. Anything that reads the FILE rather than the connection —
    `shared/cache_sync.py` uploading it, a person copying it — would otherwise ship
    a database missing its most recent writes, which is exactly the abstracts a
    push is being run to share.
    """
    with _lock:
        connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")


def lookup(ident: str) -> tuple[bool, Optional[str]]:
    """``(tried, abstract)`` for *ident*.

    Both halves in one query because the caller always wants both and they were
    the thing that used to live in two files: `(False, None)` is "never tried",
    `(True, None)` is "tried, and this source has nothing".
    """
    row = connection().execute(
        "SELECT abstract FROM abstracts WHERE ident = ?", (ident,)).fetchone()
    return (False, None) if row is None else (True, row[0])


def record(ident: str, abstract: Optional[str]) -> None:
    """Record what a source definitively answered for *ident*. Never call this for
    a transient failure — an unanswered request must stay retryable."""
    with _lock:
        conn = connection()
        conn.execute(
            "INSERT INTO abstracts (ident, abstract, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(ident) DO UPDATE SET abstract=excluded.abstract, "
            "fetched_at=excluded.fetched_at",
            (ident, abstract or None,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()


def record_many(entries: Iterable[tuple[str, Optional[str]]]) -> None:
    """`record()` for a whole batch, in one transaction."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [(ident, abstract or None, stamp) for ident, abstract in entries]
    if not rows:
        return
    with _lock:
        conn = connection()
        conn.executemany(
            "INSERT INTO abstracts (ident, abstract, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(ident) DO UPDATE SET abstract=excluded.abstract, "
            "fetched_at=excluded.fetched_at", rows)
        conn.commit()


def tried_idents() -> set[str]:
    """Every identifier any phase has already answered — the old checkpoint file."""
    return {r[0] for r in connection().execute("SELECT ident FROM abstracts")}


def found_idents() -> set[str]:
    """Every identifier that resolved to real text — the old found-index sidecar."""
    return {r[0] for r in connection().execute(
        "SELECT ident FROM abstracts WHERE abstract IS NOT NULL")}


def source_evidence() -> dict[str, dict[str, int]]:
    """``{namespace: {"hits": n, "misses": n}}`` — what each source actually recovered.

    `shared/cache_sync.py` shares this so a puller can tell a source that had
    nothing from a source this machine could not read. One grouped query rather
    than the half-million file reads the old layout needed.
    """
    evidence: dict[str, dict[str, int]] = {}
    for ident_prefix, hits, misses in connection().execute(
            "SELECT substr(ident, 1, instr(ident, ':') - 1) AS ns, "
            "       SUM(abstract IS NOT NULL), SUM(abstract IS NULL) "
            "FROM abstracts WHERE instr(ident, ':') > 0 GROUP BY ns"):
        evidence[ident_prefix] = {"hits": int(hits or 0), "misses": int(misses or 0)}
    return evidence


def drop_misses(namespaces: Iterable[str]) -> int:
    """Forget the MISSES recorded under *namespaces*; keep every hit. Returns rows removed.

    Reopens exactly the identifiers whose "no abstract" was never established —
    what used to require deleting matching JSON files and rewriting a 478k-line
    checkpoint in step.
    """
    names = list(namespaces)
    if not names:
        return 0
    with _lock:
        conn = connection()
        removed = 0
        for namespace in names:
            removed += conn.execute(
                "DELETE FROM abstracts WHERE abstract IS NULL AND ident LIKE ?",
                (f"{namespace}:%",)).rowcount
        conn.commit()
    return removed


def count() -> tuple[int, int]:
    """``(rows, hits)``."""
    row = connection().execute(
        "SELECT COUNT(*), SUM(abstract IS NOT NULL) FROM abstracts").fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def import_rows(rows: Iterable[tuple[str, Optional[str]]], *,
                overwrite: bool = False) -> int:
    """Merge *rows* into the store; returns how many were written.

    Used by a `cache_sync` pull. Without *overwrite* an identifier already here is
    left alone: the local answer was produced by this machine's configuration, and
    the remote's is at best the same one.
    """
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = [(ident, abstract or None, stamp) for ident, abstract in rows]
    if not payload:
        return 0
    verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
    with _lock:
        conn = connection()
        before = conn.total_changes
        conn.executemany(
            f"{verb} INTO abstracts (ident, abstract, fetched_at) VALUES (?, ?, ?)",
            payload)
        conn.commit()
        return conn.total_changes - before


def all_rows() -> Iterable[tuple[str, Optional[str]]]:
    """Every (ident, abstract) pair, for a `cache_sync` push."""
    return connection().execute("SELECT ident, abstract FROM abstracts")


def migrate_from_files(cache_dir: Path, checkpoint_path: Optional[Path] = None,
                       db_path: Optional[Path] = None) -> dict:
    """One-time import of the old `cache/abstracts/*.json` layout.

    The JSON files carry the identifier inside them, so they are the authority; the
    checkpoint file only adds identifiers that were checkpointed without a cache
    entry, which the old batch phase could produce. Those come in as misses, since
    "tried, and nothing came back" is exactly what a bare checkpoint line meant.
    """
    import json

    connection(db_path)
    seen: set[str] = set()
    batch: list[tuple[str, Optional[str]]] = []
    files = misses = 0
    for path in cache_dir.glob("*.json"):
        files += 1
        try:
            record_ = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt cache file is not fatal
            continue
        ident = str(record_.get("ident") or "")
        if not ident:
            continue
        abstract = record_.get("abstract")
        if abstract == "__none__":
            abstract, misses = None, misses + 1
        seen.add(ident)
        batch.append((ident, abstract))
        if len(batch) >= 20000:
            import_rows(batch, overwrite=True)
            batch.clear()
            log.info("  abstract migration: %d file(s) read", files)
    import_rows(batch, overwrite=True)

    orphans = 0
    if checkpoint_path and checkpoint_path.exists():
        extra = [(line.strip(), None)
                 for line in checkpoint_path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and line.strip() not in seen]
        orphans = import_rows(extra)

    checkpoint_wal()
    rows, hits = count()
    log.info("Abstract store: %d row(s), %d hit(s) — from %d file(s) (%d miss) and "
             "%d checkpoint-only identifier(s)", rows, hits, files, misses, orphans)
    return {"files": files, "rows": rows, "hits": hits, "checkpoint_only": orphans}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate the file-per-identifier abstract cache into SQLite.")
    parser.add_argument("--migrate", action="store_true", required=True,
                        help="Import cache/abstracts/*.json and the checkpoint file.")
    parser.add_argument("--cache-dir", default=None,
                        help="Old cache directory (default: cache/abstracts).")
    args = parser.parse_args()

    from shared.config import CACHE_DIR
    cache_dir = Path(args.cache_dir) if args.cache_dir else CACHE_DIR / "abstracts"
    stats = migrate_from_files(cache_dir, CACHE_DIR / "fetch_abstracts_done.txt")
    print(f"Imported {stats['rows']} identifier(s), {stats['hits']} with text, "
          f"from {stats['files']} file(s)")


if __name__ == "__main__":
    main()
