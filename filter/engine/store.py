"""The routing store: a local DuckDB cache of what a release routed where.

Disposable by contract. Nothing here is a source of truth — routing is a pure
function of (pool, specs, aliases, engine version), so deleting the file costs a
rebuild and nothing else. It exists because counting piles, sampling a pile and
measuring rule overlap are queries, and re-streaming the pool parquet for each
of them is not.

Two storage decisions are worth stating. `routing` holds one row per pool row:
a row always has a pile, `pending` included. `evaluations` holds only the
matches — the dense spec × row matrix is ~19× the pool for a table that is
almost all False, and absence IS False, so the cross-product diagnostics need is
reconstructible by joining against `routing` rather than by storing it.

Writes are idempotent per release: a rebuild deletes the release's rows first,
so an interrupted run is repaired by re-running it, not by dropping the file.
"""

import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from filter.engine.route import eval_all, route_batch
from filter.engine.spec import FilterSpec
from shared.config import ENGINE_CACHE_DIR

DEFAULT_STORE_PATH = ENGINE_CACHE_DIR / "engine.duckdb"

_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS routing (
        work_id BIGINT,
        pile TEXT,
        pending_reason TEXT,
        rule_id TEXT,
        precedence INT,
        matched_rules TEXT[],
        evidence TEXT,
        release_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evaluations (
        work_id BIGINT,
        spec_id TEXT,
        spec_hash TEXT,
        matched BOOLEAN,
        release_id TEXT
    )
    """,
)

_EVAL_SCHEMA = pa.schema([
    ("work_id", pa.int64()),
    ("spec_id", pa.string()),
    ("spec_hash", pa.string()),
    ("matched", pa.bool_()),
])


def open_store(path: Path = DEFAULT_STORE_PATH) -> duckdb.DuckDBPyConnection:
    """The store at *path*, tables created if absent. `:memory:` works too."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    for statement in _SCHEMA_SQL:
        con.execute(statement)
    return con


def spec_hash(spec: FilterSpec) -> str:
    """A content hash of one spec, for the evaluations row that recorded it.

    Not the spec FILE's hash: `load_specs()` returns dataclasses and the file is
    gone by then. Hashing the loaded form is what the evaluation actually used,
    which is the claim the column is making.
    """
    payload = json.dumps(asdict(spec), sort_keys=True, separators=(",", ":"),
                         default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_routing(con: duckdb.DuckDBPyConnection, pool_dir: Path,
                  specs: list[FilterSpec], release_id: str,
                  aliases: Optional[dict[int, int]] = None,
                  batch_size: int = 50_000) -> dict:
    """Route every pool row under *specs* and persist it as *release_id*."""
    con.execute("DELETE FROM routing WHERE release_id = ?", [release_id])
    con.execute("DELETE FROM evaluations WHERE release_id = ?", [release_id])

    hashes = {spec.id: spec_hash(spec) for spec in specs}
    piles: Counter = Counter()
    rows = 0
    for path in sorted(Path(pool_dir).glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_size):
            evals = eval_all(specs, batch)
            routed = route_batch(specs, batch, aliases=aliases, evals=evals)
            _insert_routing(con, routed, release_id)
            _insert_evaluations(con, routed.column("work_id"), evals, hashes,
                                release_id)
            rows += routed.num_rows
            piles.update(routed.column("pile").to_pylist())
    return {"rows": rows, "piles": dict(piles), "files": len(
        sorted(Path(pool_dir).glob("*.parquet")))}


def _insert_routing(con: duckdb.DuckDBPyConnection, routed: pa.Table,
                    release_id: str) -> None:
    con.register("_routed", routed)
    con.execute("INSERT INTO routing SELECT *, ? FROM _routed", [release_id])
    con.unregister("_routed")


def _insert_evaluations(con: duckdb.DuckDBPyConnection, work_ids: pa.ChunkedArray,
                        evals: dict[str, pa.Array], hashes: dict[str, str],
                        release_id: str) -> None:
    ids = np.asarray(work_ids.to_numpy(zero_copy_only=False), dtype=np.int64)
    hit_ids: list[np.ndarray] = []
    hit_specs: list[str] = []
    for spec_id, mask in evals.items():
        hits = ids[np.asarray(mask.to_numpy(zero_copy_only=False), dtype=bool)]
        if len(hits):
            hit_ids.append(hits)
            hit_specs.extend([spec_id] * len(hits))
    if not hit_ids:
        return
    table = pa.Table.from_pydict({
        "work_id": pa.array(np.concatenate(hit_ids)),
        "spec_id": pa.array(hit_specs),
        "spec_hash": pa.array([hashes[s] for s in hit_specs]),
        "matched": pa.array([True] * len(hit_specs)),
    }, schema=_EVAL_SCHEMA)
    con.register("_evals", table)
    con.execute("INSERT INTO evaluations SELECT *, ? FROM _evals", [release_id])
    con.unregister("_evals")


def pile_counts(con: duckdb.DuckDBPyConnection, release_id: str) -> dict[str, int]:
    """Rows per pile for *release_id*."""
    return dict(con.execute(
        "SELECT pile, count(*) FROM routing WHERE release_id = ? GROUP BY pile "
        "ORDER BY pile", [release_id]).fetchall())


def sample_pile(con: duckdb.DuckDBPyConnection, release_id: str, pile: str,
                n: int = 20, seed: int = 17) -> list[dict]:
    """*n* rows of *pile*, ordered by a seeded hash so the sample is reproducible."""
    rows = con.execute(
        "SELECT work_id, pile, pending_reason, rule_id, precedence, matched_rules, "
        "evidence FROM routing WHERE release_id = ? AND pile = ? "
        "ORDER BY hash(work_id + CAST(? AS BIGINT)), work_id LIMIT ?",
        [release_id, pile, seed, n]).fetchall()
    columns = ["work_id", "pile", "pending_reason", "rule_id", "precedence",
               "matched_rules", "evidence"]
    return [dict(zip(columns, row)) for row in rows]


def rule_hits(con: duckdb.DuckDBPyConnection, release_id: str) -> dict[str, int]:
    """Rows each spec matched under *release_id*, shadow specs included."""
    return dict(con.execute(
        "SELECT spec_id, count(*) FROM evaluations WHERE release_id = ? "
        "GROUP BY spec_id ORDER BY spec_id", [release_id]).fetchall())


def releases(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Every release the store holds routing for."""
    return [row[0] for row in con.execute(
        "SELECT DISTINCT release_id FROM routing ORDER BY release_id").fetchall()]
