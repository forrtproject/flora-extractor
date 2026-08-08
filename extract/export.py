"""`data/extracted.csv`, rendered from the extract tier's stored verdicts.

The tier writes one result row per work into `engine_verdicts`, payload and all
(`extract/tier.py`). This turns those payloads back into the CSV Stage 3 has always
produced. It is a pure render of the VERDICTS — no network, no cache, no pool —
which is the property the payload shape exists to guarantee: a payload that needed
any of those would make the permanent verdict a bookmark rather than evidence. The
routing store is read, for one question only: which works the release admits.
`--all-releases` skips even that.

    python -m extract.export --release bc38ddd787e0          # → data/extracted.csv
    python -m extract.export --release bc38ddd787e0 --check  # diff against disk
    python -m extract.export --release bc38ddd787e0 --current-generation-only
    python -m extract.export --all-releases        # every stored verdict, unfiltered

The release may be omitted where the store holds exactly one; holding several, the
export refuses rather than choosing, as `extract/tier.py` and `filter/engine/cli.py`
already do. "The newest" is deliberately not the default: the store holds seven
routings of one pool, one of them a retired rule book that admitted 89,113 works
against today's 4,650, so a newest-wins default would make this file's contents
depend on whichever routing anyone last ran, unstated in its own output.

**A verdict outlives the routing that bought it, and `--release` is how that is
undone.** A stored result row is selected by kind, mode and generation, by nothing
about routing — so a work the rule book no longer admits keeps rendering forever
because its verdict exists. That is not hypothetical: a live discard rule for OSF
preregistrations could not reach ~450 registrations on release bc38ddd787e0, and each
was screened, extracted and settled as `cannot_be_determined`. Re-routing puts them in
the `discard` pile; without `--release` the export would still ship them to the
validation import. With it, a verdict renders only if its work sits in an ADMITTED pile
of the named release (`ADMITTED_PILES` in `filter/engine/route.py`), and the count
dropped is printed whether or not it is zero. The default is unchanged, because most
renders are not asking a routing question.

**Two generations, and why the older one still counts.** A verdict belongs to a
generation — the ladder, the prompts and the models it was produced by
(`extract_generation()`). The strict reading is that a superseded generation says
nothing about what today's ladder would find, and `--current-generation-only` gives
exactly that view. The DEFAULT is not strict, because the two questions the export
answers are different: the tier's worklist asks "what should I pay to extract now",
where a stale verdict must not stop a re-extraction, while the export asks "what has
this pipeline concluded", where dropping a paper because its prompt was edited would
delete a real finding from the corpus and hand the validators a shorter file with no
explanation. So a work with no current-generation result row falls back to its newest
row of ANY generation, and the count of such rows is printed. They are rows awaiting
re-extraction, not rows to be silently discarded.

**Quarantine happens on the way out, through the same rules.** `sanity_check`
partitions extracted.csv after the fact; here the partition is applied as the rows are
written, by the same `classify_row()` — one definition of where a row belongs, so a
row lands in the same set-aside CSV whichever hand wrote it.

**The mode filter is the claim's, not the row's.** A `validation`-mode run records
real verdicts that must not reach the live file, and where that is written down is
`claim.meta.mode` — the same place the screens keep it.

**This is the only writer of `data/extracted.csv`.** Nothing appends to it any more:
the CSV runner that used to stream rows into it is gone (parked on `wip/csv-runner`),
`extract/sanity_check.py` reports rather than moves, and the two retroactive tools
correct the verdicts instead of the file. Each render writes the whole file, sorted by
`(work_id, original_rank)`, through a temp file and one rename.

The file currently tracked in git is the OLD pipeline's — 285 rows written row by row
in append order, before this module existed. The first live export will therefore
replace it whole, in one large diff: every row re-ordered, and only the works the
extract tier has verdicts for. That diff is expected and is not a data loss; the
previous contents stay in git history, and `--check` shows the difference before
anything is written.
"""

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from filter.engine.claims import ClaimsClient, ClaimsNotConfigured
from extract.sanity_check import classify_row, demote_malformed
from extract.tier import (RESULT_VERDICTS, TIER_EXTRACT, extract_generation,
                          render_payload)
from shared.config import DATA_DIR
from shared.schema import (EXTRACTED_COLS, SET_ASIDE_DESTINATIONS, YEAR_COLS,
                           set_aside_dir, year_str)

DEFAULT_OUT = DATA_DIR / "extracted.csv"


def _recorded_at(row: dict) -> tuple:
    """When a verdict row was written, as far as the row can say.

    `created_at` is the only time fact the table carries — the primary key is a uuid,
    so id order is not time order — and the id is the tiebreaker.
    """
    return (str(row.get("created_at") or ""), str(row.get("id") or ""))


def latest_results(client: ClaimsClient, *, mode: str = "live",
                   current_generation_only: bool = False) -> tuple[dict[int, dict], int]:
    """`(work id → the result row that speaks for it, superseded-generation count)`.

    Three filters, in the order that makes each one's absence a different statement:

      * kind — only RESULT rows. An evidence row is what a call said, not what the
        work concluded, and rendering one would put a model name where a paper goes.
      * mode — the claim's `meta.mode` must match. A validation run's verdicts are
        recorded, readable and invisible to the live file, which is what that mode
        means.
      * generation — a current-generation row wins outright. A work with none falls
        back to its newest row of any generation and is COUNTED, because the
        alternative is a file that quietly loses papers when a prompt is edited.

    Within each group the latest row wins (`_recorded_at`): a result row is a whole
    answer about one work, so two of them are two runs, not two voters.
    """
    generation = extract_generation()
    claim_mode = {claim["id"]: (claim.get("meta") or {}).get("mode")
                  for claim in client.claims(tier=TIER_EXTRACT)}

    current: dict[int, dict] = {}
    older: dict[int, dict] = {}
    for row in client.verdicts(TIER_EXTRACT, with_payload=True):
        if str(row.get("verdict") or "") not in RESULT_VERDICTS:
            continue
        if claim_mode.get(row.get("claim_id")) != mode:
            continue
        work = int(row["work_id"])
        bucket = current if str(row.get("prompt_hash") or "") == generation else older
        if work not in bucket or _recorded_at(row) > _recorded_at(bucket[work]):
            bucket[work] = row

    if current_generation_only:
        return current, 0
    stale = {work: row for work, row in older.items() if work not in current}
    return {**stale, **current}, len(stale)


def admitted_work_ids(release: "str | None",
                      store_path: Optional[Path] = None) -> tuple[str, set[int]]:
    """`(the full release id, the works it routed into an admitted pile)`.

    The one place this module touches the routing store, and it is skipped entirely
    under `--all-releases`: the imports are local so that render still costs no
    duckdb, no pyarrow and no store lock. A work the release never routed at all is
    absent from the set exactly as a discarded one is — neither is admitted.

    *release* None means "the store's only release", and a store holding several
    refuses rather than choosing — the same refusal, in the same words, as
    `extract/tier.py` and `filter/engine/cli.py`.
    """
    from filter.engine.route import ADMITTED_PILES
    from filter.engine.store import (DEFAULT_STORE_PATH, open_store, releases,
                                     resolve_release)

    con = open_store(store_path or DEFAULT_STORE_PATH, read_only=True)
    try:
        if release:
            release_id = resolve_release(con, release)
        else:
            present = releases(con)
            if len(present) != 1:
                raise SystemExit(
                    f"the store holds {len(present)} releases — name one with "
                    "--release, or pass --all-releases to render every stored "
                    "verdict whatever routing says about it")
            release_id = present[0]
        rows = con.execute(
            "SELECT work_id FROM routing WHERE release_id = ? AND pile IN ?",
            [release_id, sorted(ADMITTED_PILES)]).fetchall()
    finally:
        con.close()
    return release_id, {int(row[0]) for row in rows}


def rows_from_results(results: dict[int, dict]) -> list[dict]:
    """Every stored result row rendered as its `EXTRACTED_COLS` rows, in a stable order.

    Sorted by `(work_id, original_rank)` so two renders of the same verdicts are the
    same file: the verdicts come back in work order, but a paper's own rows are only
    ordered by the rank the per-target adapter assigned them. A row with no work id
    cannot happen through the tier — the work id is the claim's key — but a rank that
    is not a number can (a legacy payload), so the fallback keeps such a row in a
    defined place rather than raising over a sort key.
    """
    rendered: list[tuple[tuple, dict]] = []
    for work, result in results.items():
        payload = result.get("payload") or {}
        for position, row in enumerate(render_payload(payload)):
            row = _normalise(row)
            try:
                rank = int(str(row.get("original_rank") or position + 1))
            except ValueError:
                rank = position + 1
            rendered.append(((int(work), rank, position), row))
    return [row for _, row in sorted(rendered, key=lambda pair: pair[0])]


def _normalise(row: dict) -> dict:
    """Every column present, no None, bare-integer years — the CSV's own conventions."""
    out = {col: "" for col in EXTRACTED_COLS}
    for col, value in row.items():
        if col in out:
            out[col] = "" if value is None else value
    for col in YEAR_COLS:
        if col in out:
            out[col] = year_str(out[col])
    return out


def partition(rows: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Split rendered rows into the main CSV's and each set-aside file's.

    The malformed demotion runs first, exactly as in `sanity_check`: a resolved
    link_method with no doi_o is rewritten to `target_pending` BEFORE it is bucketed,
    so it is filed as what it is rather than as what it claimed to be.
    """
    main: list[dict] = []
    aside: dict[str, list[dict]] = {}
    for row in rows:
        row = dict(row)
        row.update(demote_malformed(row) or {})
        bucket = classify_row(row)
        if bucket is None:
            main.append(row)
        else:
            aside.setdefault(SET_ASIDE_DESTINATIONS[bucket], []).append(row)
    return main, aside


def render(client: ClaimsClient, *, mode: str = "live",
           current_generation_only: bool = False,
           admitted: Optional[set[int]] = None) -> dict:
    """The whole export, in memory: the main rows, the set-asides, and the counts.

    *admitted* — when given, the works some routing release admits. It is applied
    before anything is counted or rendered, so every number below it, `--check`
    included, describes the same filtered set. The superseded-generation count is
    recomputed over what survived, or it would report rows this render did not carry.
    """
    results, stale = latest_results(
        client, mode=mode, current_generation_only=current_generation_only)
    not_admitted = 0
    if admitted is not None:
        kept = {work: row for work, row in results.items() if work in admitted}
        not_admitted = len(results) - len(kept)
        results = kept
        generation = extract_generation()
        stale = sum(1 for row in results.values()
                    if str(row.get("prompt_hash") or "") != generation)
    rows = rows_from_results(results)
    main, aside = partition(rows)
    return {"works": len(results), "rows": len(rows), "main": main, "aside": aside,
            "superseded_generation": stale, "not_admitted": not_admitted,
            "endings": Counter(str(r.get("verdict") or "") for r in results.values())}


def write(report: dict, out_csv: Path) -> dict:
    """Publish the main CSV and every set-aside file it partitioned rows into.

    Atomic per file, the same way `filter/engine/export.py:write_rows_tmp` is: a
    per-process temp file beside the target, renamed into place. A run that dies
    mid-write leaves the previous file intact rather than a truncated one, and
    `data/extracted.csv` is a file other people read.

    The set-asides go to `set_aside_dir(out_csv)`, so a render to a sandbox path
    quarantines into that sandbox's own directory and cannot settle a paper for the
    production resume (`shared/schema.py`).
    """
    out_csv = Path(out_csv)
    written = {out_csv.name: _write_csv(out_csv, report["main"])}
    out_dir = set_aside_dir(out_csv)
    for name, rows in sorted(report["aside"].items()):
        written[name] = _write_csv(out_dir / name, rows)
    return written


def _write_csv(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXTRACTED_COLS,
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") for col in EXTRACTED_COLS})
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return len(rows)


def check(report: dict, out_csv: Path) -> dict:
    """What a render would change about the file on disk, without touching it.

    Row identity is the whole CONTENT, in schema order — the same fingerprint
    `sanity_check` matches rows by — because a paper legitimately has several rows and
    what this asks is whether the file and the verdicts say the same thing.
    """
    import pandas as pd

    path = Path(out_csv)
    if not path.exists():
        return {"exists": False, "only_on_disk": [], "only_in_render": [],
                "rows_on_disk": 0, "rows_rendered": len(report["main"])}
    on_disk = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in EXTRACTED_COLS:
        if col not in on_disk.columns:
            on_disk[col] = ""
    disk = Counter(_fingerprint(row) for row in
                   on_disk[EXTRACTED_COLS].to_dict("records"))
    fresh = Counter(_fingerprint(row) for row in report["main"])
    return {
        "exists": True,
        "rows_on_disk": len(on_disk),
        "rows_rendered": len(report["main"]),
        "only_on_disk": sorted((disk - fresh).elements())[:20],
        "only_in_render": sorted((fresh - disk).elements())[:20],
        "n_only_on_disk": sum((disk - fresh).values()),
        "n_only_in_render": sum((fresh - disk).values()),
    }


def _fingerprint(row: dict) -> str:
    return "\x1f".join(str(_normalise(row).get(col, "")) for col in EXTRACTED_COLS)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m extract.export",
        description="Rebuild data/extracted.csv from the extract tier's stored "
                    "verdicts. A pure render: no network, no cache, no pool — and no "
                    "routing store unless --release is given.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--mode", choices=("live", "validation"), default="live",
                        help="Which runs' verdicts to render (claim meta.mode).")
    parser.add_argument("--check", action="store_true",
                        help="Diff the render against the file on disk and exit "
                             "non-zero if they differ. Writes nothing.")
    parser.add_argument("--current-generation-only", action="store_true",
                        help="Drop works whose only result row is from a superseded "
                             "generation, instead of carrying it forward.")
    parser.add_argument("--release", default="",
                        help="Render only works this routing release put in an "
                             "admitted pile (screen_expensive, screen_cheap, "
                             "needs_human). Works it discarded, left pending or never "
                             "routed are dropped. A release id or a unique prefix.")
    parser.add_argument("--all-releases", action="store_true",
                        help="Render every stored verdict, whatever routing now says "
                             "about its work. The pre-2026-08-08 behaviour, kept for "
                             "reading the record whole; not what the validation "
                             "import wants.")
    args = parser.parse_args(argv)

    if args.release and args.all_releases:
        raise SystemExit("--release names one rule book and --all-releases asks for "
                         "every one; pass one or the other.")

    release_id, admitted = "", None
    if args.release or not args.all_releases:
        # No default release, deliberately, and no "the newest one" either. Which
        # rule book the file reflects decides which papers reach validation, and the
        # store holds seven routings of the same pool — one of them the retired book
        # that admitted 89,113 works against today's 4,650. Picking the most recent
        # would make the file's contents depend on whichever routing anyone last ran,
        # unstated in the output. `extract.tier` and `filter.engine` already refuse
        # the same ambiguity in the same words (tier.py, cli.py); this is that rule
        # applied to the file the validation import reads.
        from filter.engine.store import StoreUnavailable
        try:
            release_id, admitted = admitted_work_ids(args.release or None)
        except StoreUnavailable as exc:
            raise SystemExit(str(exc))

    try:
        client = ClaimsClient()
    except ClaimsNotConfigured as exc:
        raise SystemExit(f"{exc}. The verdicts this renders live in the state "
                         "authority, so there is nothing to render without it.")

    report = render(client, mode=args.mode,
                    current_generation_only=args.current_generation_only,
                    admitted=admitted)
    print(f"generation {extract_generation()}  mode {args.mode}")
    print(f"  {report['works']:,} work(s) → {report['rows']:,} row(s)")
    for ending, count in sorted(report["endings"].items()):
        print(f"    {ending:<20} {count:,}")
    if admitted is not None:
        # Printed at zero too: a render that filtered by a release should say what it
        # excluded rather than look like an unfiltered one.
        print(f"  works not admitted by release {release_id[:12]}: "
              f"{report['not_admitted']:,}  (dropped from the render; "
              f"{len(admitted):,} work(s) admitted)")
    else:
        print("  --all-releases: every stored verdict rendered, whatever routing "
              "says about its work")
    if report["superseded_generation"]:
        print(f"  rows from a superseded generation: "
              f"{report['superseded_generation']:,}  (carried forward; "
              "--current-generation-only drops them)")

    if args.check:
        diff = check(report, args.out)
        if not diff["exists"]:
            print(f"  {args.out} does not exist — the render would create it with "
                  f"{diff['rows_rendered']:,} row(s)")
            return 1
        print(f"  on disk {diff['rows_on_disk']:,} row(s), rendered "
              f"{diff['rows_rendered']:,}")
        print(f"  only on disk   {diff['n_only_on_disk']:,}")
        print(f"  only in render {diff['n_only_in_render']:,}")
        return 1 if (diff["n_only_on_disk"] or diff["n_only_in_render"]) else 0

    written = write(report, args.out)
    for name, count in written.items():
        print(f"  {count:>6,} row(s) → {name}")

    # The writer refreshes Stage 4's mirror, as every runner does (CLAUDE.md,
    # `shared/dashboard_cache.py`). This used to be the CSV runner's last act; it
    # belongs to whoever writes the file, and that is now this.
    _refresh_dashboard(args.out)
    return 0


def _refresh_dashboard(out_csv: Path) -> None:
    """Rebuild the dashboard's parquet mirror for the CSV just written.

    Only for the two paths the dashboard knows about — a render to some other path is
    not a stage it displays, and refreshing "extracted" after writing elsewhere would
    describe a file this run did not touch. A failure here must not fail an export
    that has already published its rows.
    """
    from shared.dashboard_cache import _STAGE_CSV, refresh

    stage = next((name for name, path in _STAGE_CSV.items()
                  if Path(path).resolve() == Path(out_csv).resolve()), "")
    if not stage:
        return
    try:
        refresh(stage)
    except Exception as exc:   # noqa: BLE001 — the CSV is already written
        print(f"  (dashboard mirror not refreshed: {exc})")


if __name__ == "__main__":
    sys.exit(main())
