"""Filling a text overlay: the six abstract sources, run over a routing worklist.

The sources, their batch shapes, their per-identifier cache and their checkpoint
namespaces all come from `search/fetch_abstracts.py` unchanged — this module
imports its phase runners rather than restating them. That file's measured
ordering, and its transient-vs-definitive contract, are subtle and load-bearing,
and a second copy of either would drift. What is new here is only the two ends:
the worklist comes from the routing table instead of `candidates.csv`, and the
results land in an overlay chunk instead of being merged back into a CSV.

**Two pathways, run as two phases.** The sources do not cost the same thing, so
they are not spent on the same rows:

  bulk      Europe PMC (and, opt-in, OpenAlex). Batched, keyless, unquota'd —
            one request answers about EPMC_BATCH_SIZE / OA_BATCH_SIZE
            identifiers — and it goes FIRST so the expensive pathway never asks
            about a row it already answered.
  targeted  OSF, then Semantic Scholar, then CrossRef, then Scopus last. Each is
            gated by a key, an entitlement, a weekly quota, or one call per DOI.
            It sees only the rows the bulk pathway left without text.

`--phase bulk` and `--phase targeted` are what make that a workflow rather than
just an ordering: the bulk phase costs nothing but time, so it can be run over a
wider worklist than the targeted one. `--phase all`, the default, does both over
the one worklist given.

**The worklist is not the pool.** The only shipped producer is
`filter/engine/overlay.py:worklist()`, and it emits exactly the routing rows with
`pending_reason = 'no_text'` — screen-pile winners whose abstract is empty, a few
thousand rows. The pool's ~347k missing abstracts are not in it: most of them are
`no_filter_matched`, which no rule claimed and no backfill is asked about. A
pool-wide worklist would have to be built by hand, and the numbers in this file's
memory and quota reasoning are about that hypothetical, not about a normal run.

Sharing the cache is deliberate. A DOI Stage 1 already asked Europe PMC about is
answered from the abstract store here for free, and a miss recorded here is a
miss Stage 1 will not re-buy — which is also why a wide bulk run is worth doing
even for rows no worklist needs yet.

The OSF source (registrant 10.17605) is the one that is not an abstract lookup.
Those records have no abstract anywhere — they have a registration template and a
responses form — so the phase writes the template name as the first line of the
recovered text and the responses under it. That line is what
`osf-registration-completed` and `osf-registration-protocol` read: a Replication
Recipe post-completion record is a finished replication with its outcome already
coded, a preregistration on the same registrant is a plan. Two consequences: OSF
leads `SOURCE_ORDER`, the order a recovered abstract is ATTRIBUTED in, so no
other source's text can displace the template line; and the OSF phase is the one
targeted phase whose targets are not narrowed by what bulk found.

**Dry-run is the default.** Issue #146 §6: a backfill over the pool needs
per-source quota estimates BEFORE fetching — Scopus alone is a ~10k/week ceiling
against a worklist that can hold hundreds of thousands of rows. `--run` is the
only thing that spends anything.

    python -m filter.engine.backfill --worklist wl.parquet          # standard overlay dir
    python -m filter.engine.backfill --worklist wide.parquet --run --phase bulk
    python -m filter.engine.backfill --worklist wl.parquet --run --phase targeted
"""

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filter.engine.overlay import (
    freeze, iter_worklist, overlay_work_ids, write_chunk,
)
from search.fetch_abstracts import (
    _DATASET_PREFIXES, _already_resolved, _fetch_crossref_abstract,
    _fetch_epmc_batch, _fetch_openalex_batch, _fetch_osf_registration,
    _fetch_s2_batch, _fetch_scopus_abstract, _load_checkpoint,
    _load_found_index, _phase_targets, _read_abstract_cache, _run_batch_phase,
    _run_item_phase, osf_identifier,
)
from shared.config import (
    CROSSREF_RATE_SEC, ELSEVIER_API_KEY, EPMC_BATCH_SIZE, EPMC_RATE_SEC,
    OA_BATCH_SIZE, OPENALEX_RATE_SEC, OSF_RATE_SEC, OVERLAY_DIR, S2_API_KEY,
    S2_BATCH_RATE_SEC, S2_BATCH_SIZE, SCOPUS_DEFAULT_LIMIT, SCOPUS_RATE_SEC, log,
)
from shared.utils import clean_doi

# Source name → the checkpoint/cache namespace fetch_abstracts writes it under.
# The names are this module's CLI vocabulary; the namespaces are that module's
# on-disk contract and must not be renamed.
NAMESPACES = {
    "osf": "osf",
    "openalex": "oa",
    "epmc": "epmc",
    "s2": "s2",
    "crossref": "doi",
    "scopus": "scopus",
}

# The priority order a recovered abstract is ATTRIBUTED to a source in — which is
# why `osf` leads: an OSF registration's own template line has to be the text the
# overlay stores, and another source's abstract for the same row would displace
# it. This is not the order calls are spent in; RUN_ORDER below is.
SOURCE_ORDER = ("osf", "openalex", "epmc", "s2", "crossref", "scopus")

# The two pathways, in the order calls are SPENT (see fetch_abstracts' module
# docstring). Bulk is batched, keyless and unquota'd, so it runs first over the
# whole worklist; targeted is per-item or gated, so it only ever sees what bulk
# left unresolved. Scopus is last within targeted: an ELSEVIER_API_KEY, an
# IP-bound entitlement and a ~10k/week quota.
#
# OpenAlex is bulk-SHAPED but opt-in (`--include-openalex`, or naming it in
# `--source`): its measured yield on this corpus is 0 of 200, because the corpus
# was discovered via OpenAlex and the live API's abstracts come from the same
# deposit stream the snapshot did (search/fetch_abstracts.py). It pays only when
# the snapshot is old enough that post-snapshot deposits are plausible.
BULK_SOURCES = ("epmc",)
OPT_IN_SOURCES = ("openalex",)
ALL_BULK_SOURCES = OPT_IN_SOURCES + BULK_SOURCES
TARGETED_SOURCES = ("osf", "s2", "crossref", "scopus")
RUN_ORDER = ALL_BULK_SOURCES + TARGETED_SOURCES
DEFAULT_SOURCES = tuple(s for s in RUN_ORDER if s not in OPT_IN_SOURCES)
PHASES = ("all", "bulk", "targeted")

# Actionable worklist rows held in memory at once. A shipped worklist is a few
# thousand `no_text` rows, but a hand-built wide one can be hundreds of thousands,
# and both the worklist and the abstracts recovered from it used to be materialised
# whole, which is gigabytes of RSS for a run whose per-row work is independent.
# One slice is fetched and written to its own overlay chunk before the next is
# read, so the run's memory is a property of this constant and not of the
# worklist's size. 100k rows is a few
# hundred MB of recovered text at worst and still amortises every batched source's
# request size (the largest, OpenAlex, is a 50-DOI batch).
BATCH_ROWS = 100_000


# ---------------------------------------------------------------------------
# Worklist
# ---------------------------------------------------------------------------


def _row_batches(worklist_path: Path, limit: Optional[int] = None,
                 batch_size: int = BATCH_ROWS):
    """The worklist in slices of at most *batch_size* actionable rows.

    Yields `(rows, n_dataset_dropped)` per slice, streaming the parquet rather
    than materialising it. The slice is the unit the whole run works in — fetched,
    then written to its own overlay chunk — so a wide `--phase bulk` costs
    one slice of memory instead of the worklist plus every recovered abstract in
    it. Nothing about the ANSWER depends on the slicing: each row's sources, its
    checkpoint and its cache entry are per-identifier.
    """
    rows: list[dict] = []
    dropped = 0
    taken = 0
    for record in iter_worklist(worklist_path):
        doi = clean_doi(str(record.get("doi") or ""))
        if doi and doi.split("/")[0] in _DATASET_PREFIXES:
            dropped += 1
            continue
        rows.append({
            "work_id": int(record["work_id"]),
            "oa": f"https://openalex.org/W{int(record['work_id'])}",
            "doi_r": doi,
            # `.get`: a worklist parquet written before the url column existed has
            # no such key, and only the OSF phase reads it.
            "url_r": str(record.get("url") or ""),
        })
        taken += 1
        if len(rows) >= batch_size:
            yield rows, dropped
            rows, dropped = [], 0
        if limit and taken >= limit:
            break
    if rows or dropped:
        yield rows, dropped


# ---------------------------------------------------------------------------
# Estimates (the dry run)
# ---------------------------------------------------------------------------


def estimate_worklist(worklist_path: Path, sources=RUN_ORDER,
                      limit: Optional[int] = None) -> tuple[list[dict], int, int]:
    """Per-source targets and request counts for a worklist: `(estimates, rows, dropped)`.

    Spends nothing. Counted against the live checkpoint and found-index, so the
    estimate is what a `--run` would actually do next, not what a fresh worklist
    would cost. The targeted pathway's numbers are an UPPER BOUND: a real run
    narrows them to whatever the bulk pathway leaves without text, which cannot
    be known without spending the bulk pathway.

    Targets are counted slice by slice and summed, so a worklist too big to hold
    costs one slice of memory; the request counts and the quota caps are computed
    once at the end, over the totals, so the numbers are the ones a
    whole-worklist estimate would have printed.
    """
    done = _load_checkpoint()
    found = _load_found_index()
    totals = {source: 0 for source in sources}
    rows = dropped = 0
    for batch, batch_dropped in _row_batches(worklist_path, limit):
        rows += len(batch)
        dropped += batch_dropped
        for source in sources:
            totals[source] += len(_targets(source, batch, done, found))
    estimates = []
    for source in sources:
        estimates.append(dict(_source_shape(source), source=source,
                              phase="bulk" if source in ALL_BULK_SOURCES else "targeted",
                              targets=totals[source]))
        estimates[-1]["requests"] = _requests(estimates[-1])
    return estimates, rows, dropped


def _targets(source: str, rows: list[dict], done: set[str],
             found: set[str]) -> list[str]:
    """The identifiers *source* still has to ask about, in its own namespace."""
    namespace = NAMESPACES[source]
    if source == "openalex":
        return [r["oa"] for r in rows
                if r["oa"] and f"{namespace}:{r['oa']}" not in done]
    if source == "osf":
        return _osf_targets(rows, done)
    return _phase_targets(rows, namespace, done, found)


def _osf_targets(rows: list[dict], done: set[str]) -> list[str]:
    """The OSF identifiers the OSF phase still has to try.

    The only source restricted to one kind of record: the endpoint answers about
    OSF GUIDs and nothing else, so every other row is a call whose answer is
    known before it is made. `osf_identifier()` decides which rows those are and
    what each is keyed by — a registrant DOI, or `osf.io/<guid>` for a row that
    carries an OSF URL and no DOI at all (202 of the 367 OSF records in the
    2026-08-08 export are only reachable that way).

    It is also the only phase whose targets ignore what other sources already
    found. Every other source is asked for an abstract, and one abstract is as
    good as another; this one is asked for the registration template line the two
    `osf-registration-*` specs read, which no abstract substitutes for. Skipping a
    registrant DOI because Europe PMC happened to hold text for it would put that
    text in the overlay instead — silently, and only for the rows the bulk
    pathway happened to hit.
    """
    return [ident for ident in
            (osf_identifier(r.get("doi_r") or "", r.get("url_r") or "") for r in rows)
            if ident and f"{NAMESPACES['osf']}:{ident}" not in done]


def _source_shape(source: str) -> dict:
    shapes = {
        "osf": {"batch_size": 1, "rate_sec": OSF_RATE_SEC,
                "quota": None, "skipped": ""},
        "openalex": {"batch_size": OA_BATCH_SIZE, "rate_sec": OPENALEX_RATE_SEC,
                     "quota": None, "skipped": ""},
        "epmc": {"batch_size": EPMC_BATCH_SIZE, "rate_sec": EPMC_RATE_SEC,
                 "quota": None, "skipped": ""},
        "s2": {"batch_size": S2_BATCH_SIZE, "rate_sec": S2_BATCH_RATE_SEC,
               "quota": None,
               "skipped": "" if S2_API_KEY else "S2_API_KEY not set"},
        "crossref": {"batch_size": 1, "rate_sec": CROSSREF_RATE_SEC,
                     "quota": None, "skipped": ""},
        "scopus": {"batch_size": 1, "rate_sec": SCOPUS_RATE_SEC,
                   "quota": SCOPUS_DEFAULT_LIMIT,
                   "skipped": "" if ELSEVIER_API_KEY else "ELSEVIER_API_KEY not set"},
    }
    return shapes[source]


def _requests(item: dict) -> int:
    if item["skipped"]:
        return 0
    targets = item["targets"]
    if item["quota"]:
        targets = min(targets, item["quota"])
    return math.ceil(targets / max(item["batch_size"], 1))


def render_estimate(estimates: list[dict], rows: int, dropped: int) -> str:
    lines = [f"{rows:,} worklist row(s) actionable"
             + (f"  ({dropped:,} dataset-DOI row(s) dropped — no abstract exists)"
                if dropped else ""),
             "",
             f"{'phase':<9} {'source':<10} {'targets':>10} {'requests':>10}  "
             f"{'rate':>6}  note",
             "-" * 72]
    for item in estimates:
        note = item["skipped"] and f"SKIPPED — {item['skipped']}"
        if not note and item["quota"] and item["targets"] > item["quota"]:
            note = (f"capped at {item['quota']:,}/run "
                    f"({item['targets'] - item['quota']:,} left over)")
        lines.append(f"{item.get('phase', ''):<9} {item['source']:<10} "
                     f"{item['targets']:>10,} "
                     f"{item['requests']:>10,}  {item['rate_sec']:>6}  {note}")
    lines.append("")
    if any(item.get("phase") == "targeted" for item in estimates):
        lines.append("Targeted-phase targets are an upper bound: a run narrows them to "
                     "the rows the bulk phase leaves without text.")
    lines.append("DRY RUN — nothing fetched. Re-run with --run to spend.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _resolved(row: dict) -> Optional[tuple[str, str]]:
    """(abstract, source) for *row* from the shared per-identifier cache, or None.

    The same key order `_lookup_cached_abstract()` uses; it returns only the text
    and the overlay records which source supplied it, so the lookup is repeated
    here rather than the source being guessed afterwards.

    OSF is looked up under the identifier its phase asked and cached by, not
    under the row's DOI: a DOI-less OSF row is keyed `osf.io/<guid>`, and reading
    it back by DOI would leave its recovered registration out of the overlay
    while the run reported success.
    """
    for source in SOURCE_ORDER:
        namespace = NAMESPACES[source]
        if source == "openalex":
            ident = f"{namespace}:{row['oa']}"
        elif source == "osf":
            osf = osf_identifier(row.get("doi_r") or "", row.get("url_r") or "")
            ident = f"{namespace}:{osf}" if osf else None
        else:
            ident = f"{namespace}:{row['doi_r']}" if row["doi_r"] else None
        if not ident:
            continue
        value = _read_abstract_cache(ident)
        if value is not None and value != "__none__":
            return value, source
    return None


def run(worklist_path: Path, overlay_dir: Path, sources=DEFAULT_SOURCES,
        phase: str = "all", limit: Optional[int] = None,
        scopus_limit: int = SCOPUS_DEFAULT_LIMIT,
        batch_size: int = BATCH_ROWS) -> dict:
    """Fetch text for the worklist and append it to *overlay_dir* in chunks.

    Runs the bulk pathway over every worklist row, then the targeted pathway over
    only the rows still without text — the whole point of the split, since a
    Scopus call or a CrossRef call spent on a row Europe PMC already answered
    buys nothing. *phase* restricts the run to one pathway (`bulk` / `targeted`),
    which is how a wide cheap pass and a narrow expensive pass are run over
    different worklists.

    Resumable in two independent places: the phase runners skip identifiers their
    checkpoint already holds, and the chunk write skips work ids the overlay
    already covers — so an interrupted run re-fetches nothing and cannot write a
    work into a second chunk (which `validate()` would refuse).

    Run in slices of *batch_size* actionable rows, each fetched and then written to
    its own overlay chunk, so a wide worklist costs one slice of memory rather
    than the whole file (`BATCH_ROWS`). The slicing is also a second resumption
    point: a run killed halfway has already written the chunks for the slices it
    finished.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {PHASES}")
    done = _load_checkpoint()
    found = _load_found_index()

    # Read once: the ids already in a chunk, grown as this run writes, so a work
    # cannot land in two chunks whether the repeat comes from an earlier run or
    # from an earlier slice of this one.
    already = overlay_work_ids(overlay_dir)
    total = dropped_total = 0
    chunks: list[str] = []
    by_source: dict[str, int] = {}
    scopus_left = scopus_limit

    for rows, dropped in _row_batches(worklist_path, limit, batch_size):
        dropped_total += dropped
        log.info("Backfill: %d worklist row(s)%s.", len(rows),
                 f", {dropped} dataset DOI(s) dropped" if dropped else "")

        if phase in ("all", "bulk"):
            for source in (s for s in ALL_BULK_SOURCES if s in sources):
                _run_source(source, rows, done, found, scopus_left)

        if phase in ("all", "targeted"):
            # The narrowing that makes this pathway "targeted": everything the bulk
            # pathway (or any earlier run, through the shared store) already
            # answered with text drops out before a gated source is asked about it.
            remaining = [r for r in rows
                         if not _already_resolved(r["oa"], r["doi_r"], found)]
            log.info("Backfill targeted pathway: %d of %d row(s) still without text.",
                     len(remaining), len(rows))
            for source in (s for s in TARGETED_SOURCES if s in sources):
                if source == "scopus" and scopus_limit and scopus_left <= 0:
                    continue
                # OSF is asked about the full worklist: its template line is not an
                # abstract another source can have supplied instead (_osf_targets).
                spent = _run_source(source, rows if source == "osf" else remaining,
                                    done, found, scopus_left)
                if source == "scopus" and scopus_limit:
                    # The quota is weekly and belongs to the RUN, not to a slice:
                    # capping per slice would multiply the ceiling by the number of
                    # slices and blow through it silently.
                    scopus_left = max(0, scopus_left - spent)

        written = _write_overlay(rows, overlay_dir, already)
        total += written["rows"]
        if written["chunk"]:
            chunks.append(written["chunk"])
        for source, count in written["by_source"].items():
            by_source[source] = by_source.get(source, 0) + count

    return {"rows": total, "chunk": chunks[-1] if chunks else "",
            "chunks": chunks, "dropped": dropped_total,
            "by_source": dict(sorted(by_source.items()))}


def _run_source(source: str, rows: list[dict], done: set[str], found: set[str],
                scopus_limit: int) -> int:
    """Run one source's phase over *rows*, recording what it finds in *found*.

    Returns how many identifiers it was actually spent on, which only the Scopus
    caller reads: its quota is the run's, and a run is now several slices.
    """
    namespace = NAMESPACES[source]
    shape = _source_shape(source)
    if shape["skipped"]:
        log.info("%s: skipped (%s).", source, shape["skipped"])
        return 0
    label = f"backfill — {source}"
    targets = _targets(source, rows, done, found)
    if source == "openalex":
        _run_batch_phase(label, namespace, targets, OA_BATCH_SIZE, OPENALEX_RATE_SEC,
                         _fetch_openalex_batch, found)
    elif source == "epmc":
        _run_batch_phase(label, namespace, targets, EPMC_BATCH_SIZE, EPMC_RATE_SEC,
                         _fetch_epmc_batch, found)
    elif source == "osf":
        _run_item_phase(label, namespace, targets, OSF_RATE_SEC,
                        _fetch_osf_registration, found, progress_every=200)
    elif source == "s2":
        _run_batch_phase(label, namespace, targets, S2_BATCH_SIZE, S2_BATCH_RATE_SEC,
                         lambda batch: _fetch_s2_batch(batch, S2_API_KEY), found)
    elif source == "crossref":
        _run_item_phase(label, namespace, targets, CROSSREF_RATE_SEC,
                        _fetch_crossref_abstract, found, progress_every=2000)
    elif source == "scopus":
        if scopus_limit and scopus_limit > 0:
            targets = targets[:scopus_limit]
        _run_item_phase(label, namespace, targets, SCOPUS_RATE_SEC,
                        _scopus_fetcher(), found, progress_every=500)
    return len(targets)


def _scopus_fetcher():
    """Scopus on the phase runner's contract — which is now its own, so this only
    binds the key."""
    def fetch(doi: str) -> tuple[Optional[str], str]:
        return _fetch_scopus_abstract(doi, ELSEVIER_API_KEY)
    return fetch


def _write_overlay(rows: list[dict], overlay_dir: Path,
                   already: set[int]) -> dict:
    """Write one slice's recovered text as a chunk; *already* is read AND grown.

    The caller owns the set because a run is several slices now: a work written by
    slice one must not be written again by slice two, which re-reading the
    directory would catch but only after the chunk had been written.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    chunk: list[dict] = []
    by_source: dict[str, int] = {}
    for row in rows:
        if row["work_id"] in already:
            continue
        hit = _resolved(row)
        if hit is None:
            continue
        already.add(row["work_id"])
        chunk.append({"work_id": row["work_id"], "abstract_text": hit[0],
                      "source": hit[1], "fetched_at": fetched_at})
        by_source[hit[1]] = by_source.get(hit[1], 0) + 1

    path = write_chunk(overlay_dir, chunk)
    log.info("Backfill wrote %d overlay row(s)%s.", len(chunk),
             f" -> {path.name}" if path else " (nothing new)")
    return {"rows": len(chunk), "chunk": str(path) if path else "",
            "by_source": dict(sorted(by_source.items()))}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m filter.engine.backfill",
        description=("Fill a text overlay for the routing worklist's no_text rows "
                     "(issue #146 M3). Dry-run by default."))
    parser.add_argument("--worklist", type=Path, required=True,
                        help="Worklist parquet from `filter.engine.overlay.worklist()`.")
    parser.add_argument("--overlay-dir", type=Path, default=OVERLAY_DIR,
                        help="Overlay release directory; chunks are appended here. "
                             f"Default {OVERLAY_DIR} — the directory the engine's "
                             "commands read by default, so a backfill and a route "
                             "agree without either being told where to look.")
    parser.add_argument("--run", action="store_true",
                        help="Actually fetch. Without it this only estimates.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicit form of the default.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Only the first N actionable worklist rows (pilots).")
    parser.add_argument("--source", action="append", choices=RUN_ORDER,
                        help="Restrict to one source (repeatable).")
    parser.add_argument("--include-openalex", action="store_true",
                        help="Also ask OpenAlex, which is off by default. Its "
                             "measured yield on this corpus is 0 of 200: the pool "
                             "was discovered via OpenAlex, and the live API serves "
                             "abstracts from the same deposit stream the snapshot "
                             "did, so it re-asks for text the snapshot already "
                             "showed to be absent. It pays only when the snapshot "
                             "is old enough that deposits made since it was cut are "
                             "plausible. Naming `--source openalex` opts in too.")
    parser.add_argument("--phase", choices=PHASES, default="all",
                        help="Which pathway to spend. `bulk` is the batched, "
                             f"keyless sources ({', '.join(BULK_SOURCES)}, plus "
                             f"{', '.join(OPT_IN_SOURCES)} when asked for) and is "
                             "cheap enough for a wide worklist; `targeted` is "
                             f"the gated ones ({', '.join(TARGETED_SOURCES)}) over "
                             "the rows bulk left without text. Default all: both, "
                             "over the one worklist given.")
    parser.add_argument("--batch-size", type=int, default=BATCH_ROWS, metavar="N",
                        help="Worklist rows held in memory at once; each batch is "
                             "fetched and written to its own overlay chunk. Default "
                             f"{BATCH_ROWS:,} — a wide worklist streams, so the "
                             "run's memory is this number of rows and their recovered "
                             "text, not the worklist's size.")
    parser.add_argument("--scopus-limit", type=int, default=SCOPUS_DEFAULT_LIMIT,
                        metavar="N", help="Max Scopus calls this run (weekly quota "
                                          f"~10k; default {SCOPUS_DEFAULT_LIMIT}).")
    parser.add_argument("--freeze", action="store_true",
                        help="Freeze the overlay manifest after a successful run.")
    return parser


def select_sources(source: Optional[list[str]], phase: str,
                   include_openalex: bool) -> tuple[str, ...]:
    """The sources a run with these arguments spends, in RUN_ORDER.

    An opt-in source is in only when it was asked for — by `--include-openalex`
    or by being named in `--source`, which is as explicit a request as the flag
    and would otherwise select nothing.
    """
    wanted = set(source or ())
    return tuple(s for s in RUN_ORDER
                 if (not wanted or s in wanted)
                 and (s not in OPT_IN_SOURCES or include_openalex or s in wanted)
                 and (phase == "all"
                      or s in (ALL_BULK_SOURCES if phase == "bulk"
                               else TARGETED_SOURCES)))


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    sources = select_sources(args.source, args.phase, args.include_openalex)

    # The bulk pathway used to be two sources. It is Europe PMC alone unless
    # OpenAlex is asked for, and a script written against the old default would
    # otherwise do half the pass without saying so.
    if args.phase in ("all", "bulk") and "openalex" not in sources:
        print("note: the bulk pathway is Europe PMC only — OpenAlex is opt-in "
              "(--include-openalex, or --source openalex).")

    if not args.run:
        estimates, rows, dropped = estimate_worklist(args.worklist, sources,
                                                     args.limit)
        print(render_estimate(estimates, rows, dropped))
        return 0

    result = run(args.worklist, args.overlay_dir, sources=sources, phase=args.phase,
                 limit=args.limit, scopus_limit=args.scopus_limit,
                 batch_size=args.batch_size)
    chunks = result.get("chunks") or []
    print(f"{result['rows']:,} overlay row(s) written"
          + (f" -> {chunks[0]}" if len(chunks) == 1
             else f" -> {len(chunks)} chunk(s)" if chunks else ""))
    for source, count in result["by_source"].items():
        print(f"  {source:<10} {count:,}")
    if args.freeze:
        manifest = freeze(args.overlay_dir)
        print(f"frozen: overlay {manifest['overlay_hash'][:12]}  "
              f"{manifest['rows']:,} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
