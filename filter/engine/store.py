"""The routing store: a local DuckDB cache of what a release routed where.

Disposable by contract. Nothing here is a source of truth — routing is a pure
function of (pool, specs, aliases, engine version), so deleting the file costs a
rebuild and nothing else. It exists because counting piles, sampling a pile and
measuring rule overlap are queries, and re-streaming the pool parquet for each
of them is not.

Three storage decisions are worth stating. `routing` holds one row per
alias-resolved WORK — `PRIMARY KEY (release_id, work_id)`, first writer wins —
not one per pool row: a pool that holds both a merged id and its canonical id
holds two rows for one work, and routing it twice would export that work twice.
`evaluations` holds only the matches — the dense spec × row matrix is ~19× the
pool for a table that is almost all False, and absence IS False, so the
cross-product diagnostics need is reconstructible by joining against `routing`
rather than by storing it. And a build is one transaction: the delete and every
insert commit together, so an interrupted run leaves the release exactly as it
was — absent, or the previous complete build — rather than half-replaced.
"""

import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Iterator, Optional

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from filter.engine.backends import BatchContext
from filter.engine.route import ADMITTED_PILES, eval_all, eval_domains, route_batch
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
        release_id TEXT,
        PRIMARY KEY (release_id, work_id)
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
    # One row per (work, spec) where the work is in the spec's declared domain.
    # Same shape as `evaluations` and for the same reason: absence IS "outside the
    # domain", and the interesting join — domain rows this spec did NOT match —
    # needs the membership per work rather than a count.
    """
    CREATE TABLE IF NOT EXISTS domains (
        work_id BIGINT,
        spec_id TEXT,
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

_DOMAIN_SCHEMA = pa.schema([
    ("work_id", pa.int64()),
    ("spec_id", pa.string()),
])


class StoreUnavailable(RuntimeError):
    """The store cannot be opened — absent, or held by another process."""


def open_store(path: Path = DEFAULT_STORE_PATH,
               read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """The store at *path*, tables created if absent. `:memory:` works too.

    *read_only* is what a command that only queries the store must pass. DuckDB
    hands one process an exclusive lock on a read-write connection, so a command
    that opens read-write blocks — and is blocked by — any other command on the
    same file: a `worklist` that only SELECTs has no business failing because a
    `screen` tier is running, and no business stopping one either. Read-only
    connections share the file, so the reading commands can all coexist.

    A read-only connection cannot create tables, so it also cannot create the
    file. "No store yet" and "store locked" need different actions from the
    operator, so they are told apart here rather than surfacing as one raw
    DuckDB error.
    """
    if str(path) == ":memory:":
        read_only = False        # nothing to share, and nothing to create it from
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        if not Path(path).exists():
            raise StoreUnavailable(
                f"no routing store at {path} — run `python -m filter.engine route` "
                "first.")
        try:
            return duckdb.connect(str(path), read_only=True)
        except duckdb.Error as exc:
            raise StoreUnavailable(
                f"cannot open {path} read-only: {exc}\n"
                "Another engine command holds the write lock (`route`, or a "
                "`supersede`). Wait for it to finish and re-run.") from exc
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
                  batch_size: int = 50_000,
                  batches: Optional[Iterator[pa.RecordBatch]] = None) -> dict:
    """Route every pool row under *specs* and persist it as *release_id*.

    *batches* replaces this function's own read of the pool with a stream the
    caller supplies — how `pool_reader.iter_pool_batches()` feeds it batches with
    a text overlay coalesced in (M3). The store stays ignorant of overlays: it
    routes whatever batches it is handed, and with *batches* absent it reads the
    pool exactly as before. *pool_dir* is still read for the file count either
    way, so a caller passing a stream passes the directory it streamed from.
    """
    files = sorted(Path(pool_dir).glob("*.parquet"))
    stream = batches if batches is not None else _iter_pool(files, batch_size)
    pool_rows = 0
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM routing WHERE release_id = ?", [release_id])
        con.execute("DELETE FROM evaluations WHERE release_id = ?", [release_id])
        con.execute("DELETE FROM domains WHERE release_id = ?", [release_id])
        hashes = {spec.id: spec_hash(spec) for spec in specs}
        for batch in stream:
            ctx = BatchContext(batch)
            evals = eval_all(specs, batch, ctx)
            routed = route_batch(specs, batch, aliases=aliases, evals=evals)
            _insert_routing(con, routed, release_id)
            _insert_evaluations(con, routed.column("work_id"), evals, hashes,
                                release_id)
            _insert_domains(con, routed.column("work_id"),
                            eval_domains(specs, batch, ctx), release_id)
            pool_rows += routed.num_rows
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    piles = pile_counts(con, release_id)
    return {"rows": sum(piles.values()), "pool_rows": pool_rows, "piles": piles,
            "files": len(files)}


def drop_release(con: duckdb.DuckDBPyConnection, release_id: str) -> None:
    """Remove every routing and evaluation row of *release_id*, in one transaction.

    `build_routing` commits on its own, so a caller that discovers AFTER the build
    that the release must not exist — the pool moved under it — needs a way to undo
    a committed build. Nothing else deletes a release: a superseded one is simply
    never read again.
    """
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM routing WHERE release_id = ?", [release_id])
        con.execute("DELETE FROM evaluations WHERE release_id = ?", [release_id])
        con.execute("DELETE FROM domains WHERE release_id = ?", [release_id])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def _iter_pool(files: list[Path], batch_size: int):
    for path in files:
        yield from pq.ParquetFile(path).iter_batches(batch_size=batch_size)


def _insert_routing(con: duckdb.DuckDBPyConnection, routed: pa.Table,
                    release_id: str) -> None:
    # First writer wins on a (release, work) collision: pool files stream in name
    # order and batches in file order, so which row that is, is deterministic.
    con.register("_routed", routed)
    con.execute("INSERT INTO routing SELECT *, ? FROM _routed "
                "ON CONFLICT DO NOTHING", [release_id])
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


def _insert_domains(con: duckdb.DuckDBPyConnection, work_ids: pa.ChunkedArray,
                    domains: dict[str, pa.Array], release_id: str) -> None:
    if not domains:
        return
    ids = np.asarray(work_ids.to_numpy(zero_copy_only=False), dtype=np.int64)
    member_ids: list[np.ndarray] = []
    member_specs: list[str] = []
    for spec_id, mask in domains.items():
        members = ids[np.asarray(mask.to_numpy(zero_copy_only=False), dtype=bool)]
        if len(members):
            member_ids.append(members)
            member_specs.extend([spec_id] * len(members))
    if not member_ids:
        return
    table = pa.Table.from_pydict({
        "work_id": pa.array(np.concatenate(member_ids)),
        "spec_id": pa.array(member_specs),
    }, schema=_DOMAIN_SCHEMA)
    con.register("_domains", table)
    con.execute("INSERT INTO domains SELECT *, ? FROM _domains", [release_id])
    con.unregister("_domains")


def domain_coverage(con: duckdb.DuckDBPyConnection, release_id: str,
                    specs: list[FilterSpec]) -> list[dict]:
    """Per domain-declaring live spec: its population, its matches, and the gap.

    `uncovered_admitted` is the number that matters — works INSIDE the rule's
    declared domain that the rule did not match and that another rule sent to a
    paying pile. A rule can be perfectly non-inert and still govern only part of
    its population: `osf-registration-protocol` matched 1,308 works on release
    bc38ddd787e0 and never reached the 878 OSF registrations whose overlay text
    the backfill's worklist had skipped, so ~450 preregistrations were admitted by
    a generic text rule and bought a two-voter screen and a Stage 3 extraction
    each.

    Counted DISTINCT by work, like `rule_hits()`, and reported for live specs
    only: a shadow rule leaves every row to another rule by design, so its whole
    domain would read as uncovered.
    """
    wanted = [spec for spec in specs if spec.domain is not None and not spec.shadow]
    if not wanted:
        return []
    rows = con.execute(
        """
        SELECT d.spec_id,
               count(DISTINCT d.work_id) AS in_domain,
               count(DISTINCT CASE WHEN e.work_id IS NOT NULL THEN d.work_id END)
                   AS matched,
               count(DISTINCT CASE WHEN e.work_id IS NULL AND r.pile IN ?
                                   THEN d.work_id END) AS uncovered_admitted
        FROM domains d
        JOIN routing r ON r.release_id = d.release_id AND r.work_id = d.work_id
        LEFT JOIN evaluations e ON e.release_id = d.release_id
                               AND e.work_id = d.work_id AND e.spec_id = d.spec_id
        WHERE d.release_id = ?
        GROUP BY d.spec_id
        """, [sorted(ADMITTED_PILES), release_id]).fetchall()
    counts = {row[0]: row[1:] for row in rows}
    return [{"spec_id": spec.id, "pile": spec.pile,
             "in_domain": counts.get(spec.id, (0, 0, 0))[0],
             "matched": counts.get(spec.id, (0, 0, 0))[1],
             "uncovered_admitted": counts.get(spec.id, (0, 0, 0))[2]}
            for spec in wanted]


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
    """Works each spec matched under *release_id*, shadow specs included.

    Counted DISTINCT by work: two pool rows for one aliased work are one routed
    work, so a rule that matched both matched one work, not two.
    """
    return dict(con.execute(
        "SELECT spec_id, count(DISTINCT work_id) FROM evaluations "
        "WHERE release_id = ? GROUP BY spec_id ORDER BY spec_id",
        [release_id]).fetchall())


def inert_rules(con: duckdb.DuckDBPyConnection, release_id: str,
                specs: list[FilterSpec]) -> dict:
    """Which specs matched NO work at all under *release_id*, live ones apart.

    Inert means matched nothing, not won nothing: a live rule that matches rows
    and is always outranked still says something, while one that matches zero
    rows decides nothing whatever it is ordered against. The question is asked
    over *specs* rather than over `rule_hits()`, because `evaluations` stores only
    matches — a spec that matched nothing has no row there, so counting the table
    alone can never see it.

    A live rule went inert on release bc38ddd787e0 (`osf-registration-protocol`,
    whose pattern reads a line only the text overlay writes) and nothing said so;
    ~450 preregistrations were admitted by a generic rule instead and bought a
    two-voter screen and a Stage 3 extraction each.
    """
    hits = rule_hits(con, release_id)
    live = [spec for spec in specs if not spec.shadow]
    return {
        "live": len(live),
        "inert": [(spec.id, spec.pile) for spec in live if not hits.get(spec.id)],
        "shadow_inert": [spec.id for spec in specs
                         if spec.shadow and not hits.get(spec.id)],
    }


def releases(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Every release the store holds routing for."""
    return [row[0] for row in con.execute(
        "SELECT DISTINCT release_id FROM routing ORDER BY release_id").fetchall()]


def resolve_release(con: duckdb.DuckDBPyConnection, given: str) -> str:
    """*given*, expanded to the full release id the routing table stores.

    `status` displays releases as 12-character prefixes, so a prefix is what an
    operator has to hand — but the routing query matches the full hash, and a
    prefix passed through verbatim matches NOTHING: every pile reads as empty,
    which prices as $0 and is indistinguishable from a genuinely settled pile.
    An unknown or ambiguous value is therefore an error here, never zero rows.
    """
    present = releases(con)
    if given in present:
        return given
    matches = [r for r in present if r.startswith(given)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"no release matches {given!r} — the store holds: "
                         + ", ".join(r[:12] for r in present))
    raise SystemExit(f"{given!r} is ambiguous: "
                     + ", ".join(r[:12] for r in matches))
