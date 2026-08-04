"""`python -m filter.engine <command>` — the engine's six operations.

Each subcommand is a thin call into one module: the CLI decides nothing about
routing, it only supplies paths and prints. The one judgement it does make is
what a routing release is made of, because the six release inputs come from six
different places and assembling them is not a module's job.

A pool with no readable ledger routes under `pool_manifest_hash = "unmanifested"`
rather than failing: the release id then honestly says the pool's provenance was
unknown, which is a different claim from "the pool was the one in the ledger" and
must not be silently substituted for it.
"""

import argparse
import datetime
import hashlib
import sys
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

from filter.engine import ENGINE_VERSION
from filter.engine.backends import verify_backends
from filter.engine.diagnostics import diagnose, render_text
from filter.engine.export import (
    ALIASES_FILENAME, SPEC_DIR, StaleBundleError, export_pile,
)
from filter.engine.overlay import OverlayError, worklist
from filter.engine.pool_reader import iter_pool_batches, overlay_manifest_hash
from filter.engine.release import read_release, releases_dir, routing_release, write_release
from filter.engine.spec import bundle_hash, load_specs
from filter.engine.store import (
    DEFAULT_STORE_PATH, build_routing, open_store, pile_counts, releases, sample_pile,
)
from filter.engine.workids import alias_release, load_aliases
from shared.config import SNAPSHOT_POOL_DIR
from shared.schema import ENGINE_EXPORTED_COLS

UNMANIFESTED = "unmanifested"


def schema_version() -> str:
    """The export contract's own version — a hash of the column list it writes."""
    return "csv:" + hashlib.sha256(
        ",".join(ENGINE_EXPORTED_COLS).encode("utf-8")).hexdigest()[:12]


def _pool_manifest_hash(given: Optional[str]) -> str:
    if given:
        return given
    try:
        from search.pool_sync import pool_manifest
        return pool_manifest().get("ledger_hash") or UNMANIFESTED
    except Exception:
        return UNMANIFESTED


def _release_inputs(spec_dir: Path, pool_manifest_hash: str,
                    overlay_dir: Optional[Path] = None) -> dict:
    return {
        "pool_manifest_hash": pool_manifest_hash,
        "overlay_hash": overlay_manifest_hash(overlay_dir) if overlay_dir else None,
        "bundle_hash": bundle_hash(spec_dir),
        "engine_version": ENGINE_VERSION,
        "alias_release": alias_release(spec_dir / ALIASES_FILENAME),
        "schema_version": schema_version(),
    }


def _sample_batches(pool_dir: Path, sample_files: int) -> pa.Table:
    files = sorted(Path(pool_dir).glob("*.parquet"))[:sample_files]
    if not files:
        raise SystemExit(f"no parquet files under {pool_dir}")
    batches = []
    for path in files:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000):
            batches.append(batch)
            break
    return pa.Table.from_batches(batches)


def _resolve_release(con, given: Optional[str]) -> str:
    if given:
        return given
    present = releases(con)
    if len(present) == 1:
        return present[0]
    raise SystemExit("the store holds {} releases — name one with --release".format(
        len(present)))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_specs(args) -> int:
    specs = load_specs(args.spec_dir)
    print(f"{'id':<26} {'pile':<17} {'prec':>5}  {'shadow':<6} measured")
    for spec in specs:
        levels = ",".join(str(e.get("level")) for e in spec.measured) or "-"
        print(f"{spec.id:<26} {spec.pile:<17} {spec.precedence:>5}  "
              f"{str(spec.shadow).lower():<6} {levels}")
    print(f"\n{len(specs)} spec(s)  bundle {bundle_hash(args.spec_dir)[:12]}  "
          f"engine {ENGINE_VERSION}  schema {schema_version()}")
    return 0


def cmd_verify(args) -> int:
    specs = load_specs(args.spec_dir)
    table = _sample_batches(args.pool, args.sample_files)
    mismatches = verify_backends(specs, table)
    for line in mismatches[:50]:
        print(line)
    print(f"{len(mismatches)} mismatch(es) over {table.num_rows:,} sampled row(s)")
    return 1 if mismatches else 0


def cmd_route(args) -> int:
    specs = load_specs(args.spec_dir)
    try:
        # An overlay dir holding chunks but no frozen manifest raises: routing
        # must not bind a release id to bytes nobody named.
        inputs = _release_inputs(args.spec_dir,
                                 _pool_manifest_hash(args.pool_manifest_hash),
                                 args.overlay)
    except OverlayError as exc:
        raise SystemExit(str(exc))
    release_id = routing_release(**inputs)

    con = open_store(args.store)
    aliases = load_aliases(args.spec_dir / ALIASES_FILENAME)
    counters = build_routing(con, args.pool, specs, release_id, aliases=aliases,
                             batches=iter_pool_batches(args.pool, args.overlay,
                                                       aliases=aliases))
    # Only now: the release record is the claim that this release is routed, and
    # a build that raised must not leave that claim behind. The record lives
    # beside the store it describes, so a store pointed somewhere else does not
    # deposit its releases in the default cache.
    write_release(dict(inputs, created_at=_now()), cache_dir=args.store.parent)

    print(f"release {release_id}")
    print(f"  pool {args.pool} — {counters['files']} file(s), "
          f"{counters['pool_rows']:,} pool row(s) -> {counters['rows']:,} work(s)")
    if args.overlay:
        print(f"  overlay {args.overlay} — {inputs['overlay_hash'][:12]}")
    for pile, count in sorted(pile_counts(con, release_id).items()):
        print(f"  {pile:<18} {count:,}")
    con.close()
    return 0


def cmd_diagnose(args) -> int:
    print(render_text(diagnose(args.pool, args.spec_dir, args.spec,
                               sample_n=args.sample)))
    return 0


def cmd_export(args) -> int:
    con = open_store(args.store)
    release_id = _resolve_release(con, args.release)
    try:
        record = read_release(release_id, cache_dir=args.store.parent)
    except FileNotFoundError:
        raise SystemExit(
            f"no release record for {release_id[:12]} beside {args.store} — the "
            "export cannot prove which bundle routed it. Re-run "
            "`python -m filter.engine route`.")
    try:
        manifest = export_pile(con, args.pool, args.pile, Path(args.out), release_id,
                               from_year=args.from_year, to_year=args.to_year,
                               specs=load_specs(args.spec_dir), spec_dir=args.spec_dir,
                               aliases=load_aliases(args.spec_dir / ALIASES_FILENAME),
                               expect_bundle_hash=record.get("bundle_hash"),
                               expect_alias_release=record.get("alias_release"),
                               overlay_dir=args.overlay,
                               expect_overlay_hash=record.get("overlay_hash"),
                               created_at=_now())
    except (StaleBundleError, OverlayError) as exc:
        # An operator refusal, not a crash: the message IS the whole answer.
        raise SystemExit(str(exc))
    con.close()
    print(f"{manifest['rows']:,} row(s) -> {args.out}")
    print(f"  release {manifest['release_id']}  pile {manifest['pile']}")
    print(f"  sha256  {manifest['sha256']}")
    return 0


def cmd_worklist(args) -> int:
    con = open_store(args.store)
    release_id = _resolve_release(con, args.release)
    rows = worklist(con, release_id, args.pool, Path(args.out),
                    aliases=load_aliases(args.spec_dir / ALIASES_FILENAME))
    con.close()
    print(f"{rows:,} no_text work(s) -> {args.out}")
    return 0


def cmd_status(args) -> int:
    cache_dir = args.store.parent
    on_disk = sorted(p.stem for p in releases_dir(cache_dir).glob("*.json"))
    con = open_store(args.store)
    routed = set(releases(con))
    print(f"store {args.store}")
    for release_id in sorted(set(on_disk) | routed):
        record = read_release(release_id, cache_dir=cache_dir) \
            if release_id in on_disk else {}
        created = record.get("created_at", "?")
        print(f"\n{release_id[:12]}  created {created}"
              f"{'' if release_id in routed else '  (no routing in this store)'}")
        for pile, count in sorted(pile_counts(con, release_id).items()):
            print(f"  {pile:<18} {count:,}")
    con.close()
    return 0


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m filter.engine",
        description=("Route the survivor pool through the declarative filter bundle "
                     "(issue #146). Rules route and discard; only LLM tiers admit."),
        epilog=(f"Default pool: {SNAPSHOT_POOL_DIR} · default spec dir: {SPEC_DIR} · "
                f"default store: {DEFAULT_STORE_PATH}"))
    parser.add_argument("--spec-dir", type=Path, default=SPEC_DIR,
                        help="Filter spec bundle directory.")
    sub = parser.add_subparsers(dest="command", required=True)

    specs = sub.add_parser("specs", help="List the loaded bundle and its hash.")
    specs.set_defaults(func=cmd_specs)

    verify = sub.add_parser("verify", help="Check the two backends agree on pool rows.")
    verify.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    verify.add_argument("--sample-files", type=int, default=3,
                        help="Pool files to sample the first batch of (default 3).")
    verify.set_defaults(func=cmd_verify)

    route = sub.add_parser("route", help="Route the pool into a release in the store.")
    route.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    route.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    route.add_argument("--pool-manifest-hash", default=None,
                       help="Pool provenance for the release id (default: the local "
                            "ledger's hash, else 'unmanifested').")
    route.add_argument("--overlay", type=Path, default=None,
                       help="Frozen text-overlay directory; its manifest hash "
                            "enters the release id.")
    route.set_defaults(func=cmd_route)

    diagnose_cmd = sub.add_parser("diagnose", help="What one rule moves, covers and misses.")
    diagnose_cmd.add_argument("--spec", required=True, help="Spec id to diagnose.")
    diagnose_cmd.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    diagnose_cmd.add_argument("--sample", type=int, default=20)
    diagnose_cmd.set_defaults(func=cmd_diagnose)

    export = sub.add_parser("export", help="Write a pile as a Stage 3 CSV + manifest.")
    export.add_argument("--pile", required=True)
    export.add_argument("--out", required=True)
    export.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    export.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    export.add_argument("--release", default=None,
                        help="Release to export (default: the store's only one).")
    export.add_argument("--from-year", type=int, default=None)
    export.add_argument("--to-year", type=int, default=None)
    export.add_argument("--overlay", type=Path, default=None,
                        help="The overlay the release was routed under (must match).")
    export.set_defaults(func=cmd_export)

    worklist_cmd = sub.add_parser(
        "worklist", help="Export the no_text rows as a backfill worklist.")
    worklist_cmd.add_argument("--out", required=True)
    worklist_cmd.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    worklist_cmd.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    worklist_cmd.add_argument("--release", default=None,
                              help="Release to read (default: the store's only one).")
    worklist_cmd.set_defaults(func=cmd_worklist)

    status = sub.add_parser("status", help="Releases on disk and their pile counts.")
    status.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
