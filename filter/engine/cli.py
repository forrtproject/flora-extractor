"""`python -m filter.engine <command>` — the engine's operations.

Each subcommand is a thin call into one module: the CLI decides nothing about
routing, it only supplies paths and prints. The one judgement it does make is
what a routing release is made of, because the six release inputs come from six
different places and assembling them is not a module's job.

`pool_manifest_hash` is a fingerprint of the POOL DIRECTORY — the search gate its
rows were admitted under (read from the pool's provenance sidecar, never from this
checkout), plus every parquet's name, size and row count
(`search.snapshot_scan.pool_fingerprint`). A pool that cannot be fingerprinted at
all — none there, unreadable, or fewer files than its sidecar says complete it —
routes under `pool_manifest_hash = "unmanifested:<discriminator>"` rather than
failing: the release id then honestly says the pool's provenance was unknown,
which is a different claim from naming a pool and must not be silently
substituted for it. The discriminator is a hash of the parquet names and sizes,
so two different unfingerprintable pools mint two different release ids instead
of sharing one release's claims and verdicts.
The fingerprint is taken again after the routing pass, because the reader re-reads
the directory and a release id must name the pool that was actually consumed.

The text overlay is read from `OVERLAY_DIR` whenever that directory holds chunks,
because an overlay nobody passed is how a live rule comes to match nothing at
all. Every command that reads it prints which overlay it read.
"""

import argparse
import datetime
import hashlib
import sys
from pathlib import Path
from typing import Optional

from filter.engine import ENGINE_VERSION
from filter.engine.diagnostics import diagnose, render_text
from filter.engine.export import (
    ALIASES_FILENAME, SPEC_DIR, StaleBundleError, check_release_binding, export_pile,
)
from filter.engine.handoff import HANDOFF_CSV, HANDOFF_UNSCREENED_CSV
from filter.engine.overlay import OverlayError, chunk_paths, worklist
from filter.engine.pool_reader import iter_pool_batches, overlay_manifest_hash
from filter.engine.release import read_release, releases_dir, routing_release, write_release
from filter.engine.spec import bundle_hash, load_specs
from filter.engine.store import (
    DEFAULT_STORE_PATH, StoreUnavailable, build_routing, drop_release, open_store,
    pile_counts, releases, sample_pile,
)
from filter.engine.workids import alias_release, load_aliases
from shared.config import OVERLAY_DIR, SNAPSHOT_POOL_DIR
from shared.schema import ENGINE_EXPORTED_COLS

UNMANIFESTED = "unmanifested"


def schema_version() -> str:
    """The export contract's own version — a hash of the column list it writes."""
    return "csv:" + hashlib.sha256(
        ",".join(ENGINE_EXPORTED_COLS).encode("utf-8")).hexdigest()[:12]


def _unmanifested(pool_dir: Optional[Path]) -> str:
    """`unmanifested:<12 hex>` — an unfingerprintable pool, told apart from others.

    A release id is the identity claims and verdicts hang off, so two different
    pools must not mint the same one. The literal `unmanifested` did exactly that:
    every half-pulled or sidecar-less pool on earth shared one release-id input, so
    two of them routed into one release and shared each other's claims and
    verdicts. The discriminator is the cheapest fact that still separates them —
    each parquet's name and size, which needs no footer read and is available
    precisely when the real fingerprint is not (a missing sidecar, a partial
    transfer). It is deliberately NOT a fingerprint: no row counts, no gate, no
    file contents, and it keeps the `unmanifested` prefix so nothing reads it as
    provenance. A pool the discriminator cannot be taken over either — an unreadable
    directory — falls back to the bare literal, which is the one case where two
    pools may still collide and the one where nothing at all is known about either.
    """
    try:
        files = sorted(Path(pool_dir).glob("*.parquet")) if pool_dir else []
        parts = "|".join(f"{p.name}:{p.stat().st_size}" for p in files)
    except OSError:
        return UNMANIFESTED
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()[:12]
    return f"{UNMANIFESTED}:{digest}"


def _unmanifested(pool_dir: Optional[Path]) -> str:
    """`unmanifested:<12 hex>` — an unfingerprintable pool, told apart from others.

    A release id is the identity claims and verdicts hang off, so two different
    pools must not mint the same one. The bare literal `unmanifested` did exactly
    that: every half-pulled or sidecar-less pool on earth shared one release-id
    input, so two of them routed into one release and shared each other's claims
    and verdicts. The discriminator is the cheapest fact that still separates
    them — each parquet's name and size, which needs no footer read and is
    available precisely when the real fingerprint is not (a missing sidecar, a
    partial transfer). It is deliberately NOT a fingerprint: no row counts, no
    gate, no file contents, and it keeps the `unmanifested` prefix so nothing
    reads it as provenance. A pool the discriminator itself cannot be taken over —
    an unreadable directory — falls back to the bare literal, which is the one
    case where two pools can still collide and the one where nothing whatever is
    known about either.
    """
    try:
        files = sorted(Path(pool_dir).glob("*.parquet")) if pool_dir else []
        parts = "|".join(f"{p.name}:{p.stat().st_size}" for p in files)
    except OSError:
        return UNMANIFESTED
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()[:12]
    return f"{UNMANIFESTED}:{digest}"


def _pool_manifest_hash(given: Optional[str], pool_dir: Optional[Path] = None) -> str:
    """The pool's identity for the release id: a hash of the pool directory itself.

    `UNMANIFESTED` here is a genuine anomaly, not a routine fallback — routing
    reads the pool, so a run that could not fingerprint one either found no pool,
    found a partial one (fewer files than its provenance sidecar says complete it),
    or could not read it. It is never returned for a pool that was read whole: an
    unreadable pool raises inside `pool_fingerprint`, and an absent or partial one
    returns None; neither can come back as a real-looking hash. What such a pool
    gets instead is `unmanifested:<discriminator>` — see `_unmanifested()`.
    """
    if given:
        return given
    try:
        from search.snapshot_scan import pool_fingerprint
        return pool_fingerprint(pool_dir) or _unmanifested(pool_dir)
    except Exception as exc:  # noqa: BLE001 — boundary: a pool we cannot read is unnamed
        provenance = _unmanifested(pool_dir)
        print(f"  WARNING: the pool at {pool_dir} could not be fingerprinted ({exc}); "
              f"this release records its provenance as '{provenance}'.")
        return provenance


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


def _overlay(args) -> Optional[Path]:
    """The overlay this command reads: `--overlay` > the standard dir > none.

    An absent flag means "use the overlay if there is one", never "run bare":
    a rule that matches only overlay text (the OSF registration pair) fires on
    nothing without it, and a rule that silently matches nothing is worse than
    no rule at all. `--no-overlay` is how a bare-pool run is asked for out loud.
    A directory with no chunks is not an overlay, so an empty or absent standard
    dir is simply none.
    """
    if getattr(args, "no_overlay", False):
        return None
    if getattr(args, "overlay", None):
        return args.overlay
    return OVERLAY_DIR if chunk_paths(OVERLAY_DIR) else None


def _print_overlay(overlay_dir: Optional[Path], indent: str = "") -> None:
    """The one line every overlay-reading command prints about the text it read.

    Which text a run read decides what its rules could match, so it is printed
    whether or not there is an overlay — "none" is the finding, not silence.
    """
    if not overlay_dir:
        print(f"{indent}overlay: none")
        return
    try:
        digest = overlay_manifest_hash(overlay_dir) or ""
    except OverlayError as exc:
        raise SystemExit(str(exc))
    print(f"{indent}overlay: {overlay_dir} (hash {digest[:12]})")


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


def cmd_route(args) -> int:
    specs = load_specs(args.spec_dir)
    overlay_dir = _overlay(args)
    try:
        # An overlay dir holding chunks but no frozen manifest raises: routing
        # must not bind a release id to bytes nobody named.
        inputs = _release_inputs(args.spec_dir,
                                 _pool_manifest_hash(args.pool_manifest_hash, args.pool),
                                 overlay_dir)
    except OverlayError as exc:
        raise SystemExit(str(exc))
    release_id = routing_release(**inputs)

    con = open_store(args.store)
    aliases = load_aliases(args.spec_dir / ALIASES_FILENAME)
    counters = build_routing(con, args.pool, specs, release_id, aliases=aliases,
                             batches=iter_pool_batches(args.pool, overlay_dir,
                                                       aliases=aliases))
    # The fingerprint was taken before the pool was read, and the reader re-globs the
    # directory: a file added, removed or rewritten in between is consumed by the
    # routing but not named by the release id. Re-fingerprinting costs ~0.4 s over the
    # real 2,232-file pool, so it is taken again here and the routing rows are dropped
    # if it moved. The rows are already committed (build_routing is one transaction of
    # its own), so this rolls them back explicitly and writes no release record —
    # nothing downstream may find a release whose id names a pool that is gone.
    if not args.pool_manifest_hash:
        after = _pool_manifest_hash(None, args.pool)
        if after != inputs["pool_manifest_hash"]:
            drop_release(con, release_id)
            con.close()
            raise SystemExit(
                f"The pool at {args.pool} CHANGED while it was being routed "
                f"(fingerprint {inputs['pool_manifest_hash'][:12]} before, {after[:12]} "
                "after). The release id names the pool as it was at the start, so this "
                "routing has been rolled back rather than stored under an id that "
                "describes something else. Re-run `route` once the pool is settled.")
    # Only now: the release record is the claim that this release is routed, and
    # a build that raised must not leave that claim behind. The record lives
    # beside the store it describes, so a store pointed somewhere else does not
    # deposit its releases in the default cache.
    write_release(dict(inputs, created_at=_now()), cache_dir=args.store.parent)

    print(f"release {release_id}")
    _register_release(release_id, args.store.parent)
    provenance = inputs["pool_manifest_hash"]
    print(f"  pool {args.pool} — {counters['files']} file(s), "
          f"{counters['pool_rows']:,} pool row(s) -> {counters['rows']:,} work(s), "
          f"provenance {provenance if provenance.startswith(UNMANIFESTED) else provenance[:12]}")
    _print_overlay(overlay_dir, indent="  ")
    for pile, count in sorted(pile_counts(con, release_id).items()):
        print(f"  {pile:<18} {count:,}")
    con.close()
    return 0


def _register_release(release_id: str, cache_dir: Path) -> bool:
    """Tell the state authority the release exists, best-effort. True if it took.

    The claim RPC rejects a release it has no row for, so a routing run that only
    wrote the local record left `screen --run` unable to claim anything at all.
    Registering here is best-effort because routing must stay usable offline —
    the claim path registers on demand as well, and this is the cheap moment to
    do it rather than the only one.
    """
    from filter.engine.claims import ClaimsClient

    try:
        ClaimsClient().register_release(read_release(release_id, cache_dir=cache_dir))
    except Exception as exc:  # noqa: BLE001 — network boundary
        print(f"  WARNING: release not registered in the state authority ({exc}). "
              "`screen --run` will try to register it before claiming; if that "
              "fails too, no claim can be made under this release.")
        return False
    print("  registered with the state authority")
    return True


def cmd_diagnose(args) -> int:
    print(render_text(diagnose(args.pool, args.spec_dir, args.spec,
                               sample_n=args.sample)))
    return 0


def cmd_export(args) -> int:
    con = open_store(args.store, read_only=True)
    release_id = _resolve_release(con, args.release)
    try:
        record = read_release(release_id, cache_dir=args.store.parent)
    except FileNotFoundError:
        raise SystemExit(
            f"no release record for {release_id[:12]} beside {args.store} — the "
            "export cannot prove which bundle routed it. Re-run "
            "`python -m filter.engine route`.")
    overlay_dir = _overlay(args)
    _print_overlay(overlay_dir)
    try:
        manifest = export_pile(con, args.pool, args.pile, Path(args.out), release_id,
                               from_year=args.from_year, to_year=args.to_year,
                               specs=load_specs(args.spec_dir), spec_dir=args.spec_dir,
                               aliases=load_aliases(args.spec_dir / ALIASES_FILENAME),
                               expect_bundle_hash=record.get("bundle_hash"),
                               expect_alias_release=record.get("alias_release"),
                               overlay_dir=overlay_dir,
                               expect_overlay_hash=record.get("overlay_hash"),
                               created_at=_now())
    except (StaleBundleError, OverlayError) as exc:
        # An operator refusal, not a crash: the message IS the whole answer.
        raise SystemExit(str(exc))
    con.close()
    print(f"{manifest['rows']:,} row(s) -> {args.out}")
    print(f"  release {manifest['release_id']}  pile {manifest['pile']}")
    print(f"  sha256  {manifest['sha256']}")
    if args.pile == "needs_human":
        # Issue #146 §2: needs_human is "exported with a size attached". The size
        # is the whole point of the pile — it is the queue a person has to work
        # through, and a number nobody prints is a queue nobody plans for.
        print(f"\n  NEEDS HUMAN: {manifest['rows']:,} row(s) await human judgement "
              "in this release.")
    return 0


def cmd_screen(args) -> int:
    from filter.engine.claims import ClaimsClient, ClaimsNotConfigured
    from filter.engine.tiers import (TIER_CHEAP, TIER_EXPENSIVE, run_screen_cheap,
                                     run_screen_expensive)

    con = open_store(args.store, read_only=True)
    release_id = _resolve_release(con, args.release)
    overlay_dir = _overlay(args)
    _print_overlay(overlay_dir)
    # The tier reads the pool through the overlay and pays per abstract, so a
    # release routed under different text must not be screened under this one:
    # the money would buy a verdict on something the rules never saw. Only the
    # overlay is checked here — bundle drift changes no text a voter reads.
    try:
        record = read_release(release_id, cache_dir=args.store.parent)
    except FileNotFoundError:
        record = {}
    if record:
        try:
            check_release_binding(args.spec_dir, release_id, None, None,
                                  overlay_dir, record.get("overlay_hash"))
        except (StaleBundleError, OverlayError) as exc:
            raise SystemExit(str(exc))
    # A dry run answers "how big and how much", which needs no state authority —
    # so the client is only demanded when something is actually about to be spent.
    try:
        client = ClaimsClient()
    except ClaimsNotConfigured as exc:
        if args.run:
            raise SystemExit(f"{exc}. Set SUPABASE_URL and SUPABASE_SERVICE_KEY, or "
                             "drop --run for a dry run.")
        client = None

    # screen_expensive IS the validated front door, so its verdicts count by
    # default; screen_cheap has to earn that on this distribution first (§2), so
    # it validates unless told otherwise.
    mode = "live" if (args.live or args.tier == TIER_EXPENSIVE) else "validation"
    runner = run_screen_cheap if args.tier == TIER_CHEAP else run_screen_expensive
    report = runner(con, client, release_id, mode=mode, batch_label=args.batch_label,
                    limit=args.limit, pool_dir=args.pool, overlay_dir=overlay_dir,
                    aliases=load_aliases(args.spec_dir / ALIASES_FILENAME),
                    run=args.run, cache_dir=args.store.parent)
    con.close()
    if report.get("dry_run"):
        return 0

    print(f"tier {report['tier']}  mode {report['mode']}  claim {report['claim_id']}")
    print(f"  {report['decided']:,} work(s) decided, "
          f"{report['verdicts']:,} verdict(s) recorded "
          f"({report.get('workers', 1)} worker(s))")
    responses = report.get("responses") or {}
    if responses:
        print(f"  response blobs — {responses.get('uploaded', 0):,} uploaded, "
              f"{responses.get('response_pending_upload', 0):,} still on disk only")
    for outcome, count in sorted(report["outcomes"].items()):
        print(f"  {outcome:<12} {count:,}")
    _print_incomplete(client, args.tier, indent="  ")
    if report.get("revalidation"):
        print(f"  re-validation — {report['revalidation']['discards']:,} discard(s) "
              "this tier would have made, by the pile the rules chose:")
        for pile, count in sorted(report["revalidation"]["by_pile"].items()):
            print(f"    {pile:<18} {count:,}")
        print("  Nothing was discarded: validation mode. Re-run with --live once "
              "these numbers have been adjudicated.")
    return 0


def _print_incomplete(client, tier: str, indent: str = "") -> None:
    """How many works this tier has verdict rows for but no decision from.

    The one number that makes a provider outage visible instead of silent. These
    works are not decided (the next `screen --run` asks them again) and not handed
    off (a half-screen never reaches Stage 3), so without this line the only trace
    of a five-minute outage is a run count that does not add up.
    """
    if client is None:
        return
    from filter.engine.tiers import incomplete_work_ids

    try:
        pending = incomplete_work_ids(client, tier)
    except Exception as exc:  # noqa: BLE001 — network boundary; a count is not the run
        print(f"{indent}incomplete screens: unknown ({exc})")
        return
    if pending:
        print(f"{indent}incomplete screens {len(pending):,}  (verdict rows short of a "
              "decision — a failed call, not a verdict; the next --run asks again)")


def cmd_reconcile(args) -> int:
    from filter.engine.claims import ClaimsClient, ClaimsNotConfigured
    from filter.engine.tiers import (_hf_upload_blockers, reconcile_responses,
                                     render_reconcile)

    # Both refusals are operator conditions with an obvious fix, and a sweep that
    # quietly found nothing to do would be indistinguishable from a clean state.
    blockers = _hf_upload_blockers()
    if blockers:
        raise SystemExit(
            "refusing to reconcile response blobs: " + "; ".join(blockers) + ". "
            "Nothing here can be pushed, so the rows would stay pending either "
            "way — configure Hugging Face and re-run.")
    try:
        client = ClaimsClient()
    except ClaimsNotConfigured as exc:
        raise SystemExit(f"{exc}. The pending rows live in the state authority, so "
                         "there is nothing to reconcile against without it.")
    print(render_reconcile(reconcile_responses(client, run=args.run)))
    return 0


def cmd_handoff(args) -> int:
    from filter.engine.handoff import decisions, write_handoff

    out_csv = Path(args.out) if args.out else Path(
        HANDOFF_UNSCREENED_CSV if args.as_routed else HANDOFF_CSV)
    con = open_store(args.store, read_only=True)
    release_id = _resolve_release(con, args.release)
    try:
        record = read_release(release_id, cache_dir=args.store.parent)
    except FileNotFoundError:
        raise SystemExit(
            f"no release record for {release_id[:12]} beside {args.store} — the "
            "handoff cannot prove which bundle routed it. Re-run "
            "`python -m filter.engine route`.")
    overlay_dir = _overlay(args)
    _print_overlay(overlay_dir)

    from filter.engine.claims import ClaimsClient, ClaimsNotConfigured

    drop: set[int] = set()
    screen: dict[int, dict] = {}
    decided: Optional[set[int]] = None
    try:
        drop, screen = decisions(ClaimsClient())
        # The settled works ARE the screen map's keys; None is the as-routed mode.
        decided = None if args.as_routed else set(screen)
    except ClaimsNotConfigured:
        # Without a claims client there are no verdicts, so screened-only would
        # export nothing — a silent empty Stage 3 input is worse than a refusal.
        # Any OTHER failure is not caught here: a transport error would hand off
        # rows a live run discarded, so it is left to raise.
        if not args.as_routed:
            raise SystemExit(
                "no state authority configured (SUPABASE_URL unset), so no tier "
                "verdicts can be read — and the handoff exports only screened "
                "rows, which would make this file empty. Configure Supabase, or "
                "pass --as-routed to hand off the piles as the rules routed them.")
        print("no state authority configured (SUPABASE_URL unset) — handing off "
              "the piles as routed, with no tier verdicts applied.")

    try:
        manifest = write_handoff(
            con, args.pool, out_csv, release_id, drop=drop,
            screen=screen, decided=decided,
            specs=load_specs(args.spec_dir),
            spec_dir=args.spec_dir,
            aliases=load_aliases(args.spec_dir / ALIASES_FILENAME),
            expect_bundle_hash=record.get("bundle_hash"),
            expect_alias_release=record.get("alias_release"),
            overlay_dir=overlay_dir,
            expect_overlay_hash=record.get("overlay_hash"),
            from_year=args.from_year, to_year=args.to_year, created_at=_now())
    except (StaleBundleError, OverlayError) as exc:
        raise SystemExit(str(exc))
    con.close()

    label = "Stage 3 input" if manifest["screened_only"] else "as routed, UNSCREENED"
    print(f"{manifest['rows']:,} row(s) -> {out_csv}   ({label})")
    for pile, count in manifest["rows_per_pile"].items():
        print(f"  {pile:<18} {count:,}")
    print(f"  dropped by a live tier verdict {manifest['dropped_by_tier_verdict']:,}; "
          f"typed by one {manifest['typed_by_tier_verdict']:,}")
    print(f"  skipped, never screened {manifest['skipped_unscreened']:,}"
          + ("" if manifest["screened_only"] else "   (--as-routed: verdicts "
             "applied, but an unscreened row still travels)"))
    if not manifest["screened_only"] and not args.out:
        print(f"  --as-routed writes {HANDOFF_UNSCREENED_CSV}, not {HANDOFF_CSV}: rows "
              "here can carry a blank screen verdict, and Stage 3 writes those "
              "target_pending. Point --filtered-csv at this file to run it anyway.")
    print(f"  release {manifest['release_id']}")
    print(f"  sha256  {manifest['sha256']}")
    return 0


def cmd_worklist(args) -> int:
    con = open_store(args.store, read_only=True)
    release_id = _resolve_release(con, args.release)
    rows = worklist(con, release_id, args.pool, Path(args.out),
                    aliases=load_aliases(args.spec_dir / ALIASES_FILENAME))
    con.close()
    print(f"{rows:,} no_text work(s) -> {args.out}")
    return 0


def cmd_status(args) -> int:
    cache_dir = args.store.parent
    on_disk = sorted(p.stem for p in releases_dir(cache_dir).glob("*.json"))
    con = open_store(args.store, read_only=True)
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

    # Tier-scoped, so it is reported once rather than per release: a verdict
    # follows the work, not the release it was routed under.
    from filter.engine.claims import ClaimsClient, ClaimsNotConfigured
    from filter.engine.tiers import TIER_CHEAP, TIER_EXPENSIVE

    try:
        client = ClaimsClient()
    except ClaimsNotConfigured:
        return 0
    print("")
    for tier in (TIER_EXPENSIVE, TIER_CHEAP):
        _print_incomplete(client, tier, indent=f"{tier}: ")
    return 0


def cmd_release_claim(args) -> int:
    """List open claims, or end one by id.

    The listing exists because a stranded claim is otherwise invisible: nothing
    else in the CLI names the claims that hold works back. Ending one calls the
    same `engine_release_claim` RPC a finishing run calls — one release
    semantics, not two.
    """
    from filter.engine.claims import (ClaimsClient, ClaimsNotConfigured,
                                      is_expired)

    try:
        client = ClaimsClient()
    except ClaimsNotConfigured as exc:
        raise SystemExit(f"{exc}. Claims live in the state authority, so there is "
                         "nothing to list or release without it.")

    if not args.claim:
        claims = client.active_claims(release_id=args.release)
        if not claims:
            print("no active claims.")
            return 0
        print(f"{len(claims)} active claim(s):\n")
        for claim in claims:
            marker = "  EXPIRED" if is_expired(claim) else ""
            print(f"{claim['id']}  {claim.get('tier', '?'):<17}"
                  f" items {client.claim_item_count(claim['id']):>7,}"
                  f"  created {claim.get('created_at', '?')}"
                  f"  expires {claim.get('expires_at', '?')}{marker}")
        print("\nAn EXPIRED claim already blocks nothing — releasing it only tidies "
              "the record.\nEnd one with: python -m filter.engine release-claim "
              "--claim <id> [--status failed] --yes")
        return 0

    if not args.yes:
        raise SystemExit(
            f"about to end claim {args.claim} as '{args.status}'. Its verdicts are "
            "permanent and are not touched; only the hold on its works ends. "
            "Re-run with --yes.")
    client.release_claim(args.claim, args.status)
    print(f"claim {args.claim} ended as '{args.status}'; its works are claimable again.")
    return 0


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_overlay_flags(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument("--overlay", type=Path, default=None,
                        help=f"Text-overlay directory to use instead of the "
                             f"default {OVERLAY_DIR}. {what}")
    parser.add_argument("--no-overlay", action="store_true",
                        help="Ignore the default overlay directory and use the "
                             "bare pool. Without either flag the default "
                             "directory is read whenever it holds chunks.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m filter.engine",
        description=("Route the survivor pool through the declarative filter bundle "
                     "(issue #146). Rules route and discard; only LLM tiers admit."),
        epilog=(f"Default pool: {SNAPSHOT_POOL_DIR} · default spec dir: {SPEC_DIR} · "
                f"default store: {DEFAULT_STORE_PATH}\n\n"
                "Three engine tools are their own commands rather than "
                "subcommands, because each is a one-off run outside the routing "
                "loop:\n"
                "  python -m filter.engine.backfill --worklist F [--run] [--freeze]\n"
                "      fetch abstracts for the no_text worklist into a text overlay\n"
                "  python -m filter.engine.supersede --old R1 --new R2 [--run]\n"
                "      record lineage where a re-route moved works already sent "
                "for validation\n"
                "  python -m filter.engine.sizing [--rows N] [--verdict-rate R]\n"
                "      estimate how much Postgres the engine's state tables need"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec-dir", type=Path, default=SPEC_DIR,
                        help="Filter spec bundle directory.")
    sub = parser.add_subparsers(dest="command", required=True)

    specs = sub.add_parser("specs", help="List the loaded bundle and its hash.")
    specs.set_defaults(func=cmd_specs)

    route = sub.add_parser("route", help="Route the pool into a release in the store.")
    route.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    route.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    route.add_argument("--pool-manifest-hash", default=None,
                       help="Pool provenance for the release id (default: a fingerprint "
                            "of the pool directory — gate, file sizes and row counts — "
                            "else 'unmanifested').")
    _add_overlay_flags(route, "The frozen manifest hash enters the release id.")
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
    _add_overlay_flags(export, "It must match the one the release was routed under.")
    export.set_defaults(func=cmd_export)

    screen = sub.add_parser(
        "screen", help="Run an LLM tier over its pile (dry run unless --run).")
    screen.add_argument("--tier", required=True,
                        choices=["screen_cheap", "screen_expensive"])
    screen.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    screen.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    screen.add_argument("--release", default=None,
                        help="Release to screen (default: the store's only one).")
    screen.add_argument("--limit", type=int, default=None,
                        help="Stop after N works — how a first live batch stays "
                             "small enough to inspect.")
    screen.add_argument("--run", action="store_true",
                        help="Claim and spend. Without it, nothing is claimed, "
                             "fetched or spent and an estimate is printed.")
    screen.add_argument("--live", action="store_true",
                        help="screen_cheap: let its discards reach the handoff. "
                             "Its default records verdicts and changes nothing "
                             "(issue #146 §2 re-validation). screen_expensive is "
                             "live either way — it is the validated screen.")
    screen.add_argument("--batch-label", default="",
                        help="A name for this batch, recorded on the claim.")
    _add_overlay_flags(screen, "It must match the one the release was routed under.")
    screen.set_defaults(func=cmd_screen)

    reconcile = sub.add_parser(
        "reconcile", help="Push response blobs an earlier run left pending "
                          "(dry run unless --run).")
    reconcile.add_argument("--run", action="store_true",
                           help="Commit the blobs and mark the rows a commit took. "
                                "Without it, nothing is uploaded or written.")
    reconcile.set_defaults(func=cmd_reconcile)

    handoff = sub.add_parser(
        "handoff", help="Write the screen piles as Stage 3's filtered.csv.")
    handoff.add_argument(
        "--out", default=None,
        help=f"Output CSV. Default: {HANDOFF_CSV} for the screened handoff, "
             f"{HANDOFF_UNSCREENED_CSV} with --as-routed.")
    handoff.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    handoff.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    handoff.add_argument("--release", default=None,
                         help="Release to hand off (default: the store's only one).")
    handoff.add_argument("--from-year", type=int, default=None)
    handoff.add_argument("--to-year", type=int, default=None)
    handoff.add_argument(
        "--as-routed", action="store_true",
        help="Export the piles as the rules routed them, applying whatever tier "
             "verdicts exist. The default exports only rows a live tier run "
             f"reached a verdict on. Writes {HANDOFF_UNSCREENED_CSV} unless --out "
             "says otherwise: an as-routed file carries blank screen columns and "
             "must not take the screened handoff's name.")
    _add_overlay_flags(handoff, "It must match the one the release was routed under.")
    handoff.set_defaults(func=cmd_handoff)

    worklist_cmd = sub.add_parser(
        "worklist", help="Export the no_text rows as a backfill worklist.")
    worklist_cmd.add_argument("--out", required=True)
    worklist_cmd.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    worklist_cmd.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    worklist_cmd.add_argument("--release", default=None,
                              help="Release to read (default: the store's only one).")
    worklist_cmd.set_defaults(func=cmd_worklist)

    release_claim = sub.add_parser(
        "release-claim",
        help="List open claims; end one that a dead run left holding its works.")
    release_claim.add_argument(
        "--claim", default=None,
        help="Claim id to end. Without it, the open claims are listed.")
    release_claim.add_argument(
        "--status", default="failed", choices=("failed", "cancelled", "complete"),
        help="How the claim ended (default: failed — a run that did not finish).")
    release_claim.add_argument("--yes", action="store_true",
                               help="Confirm ending the named claim.")
    release_claim.add_argument("--release", default=None,
                               help="Restrict the listing to one release id.")
    release_claim.set_defaults(func=cmd_release_claim)

    status = sub.add_parser("status", help="Releases on disk and their pile counts.")
    status.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except StoreUnavailable as exc:
        # An operator condition with two different fixes ("route first" vs "wait
        # for the writer"), so the message is the whole answer — not a traceback.
        raise SystemExit(str(exc))


if __name__ == "__main__":
    sys.exit(main())
