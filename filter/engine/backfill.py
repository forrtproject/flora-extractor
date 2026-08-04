"""Filling a text overlay: the six abstract sources, run over a routing worklist.

The sources, their order, their batch shapes, their per-identifier cache and
their checkpoint namespaces all come from `search/fetch_abstracts.py` unchanged —
this module imports its phase runners rather than restating them. That file's
ordering (OpenAlex → Europe PMC → S2 → CrossRef → Scopus) is measured, its
transient-vs-definitive contract is subtle and load-bearing, and a second copy of
either would drift. What is new here is only the two ends: the worklist comes
from the routing table instead of `candidates.csv`, and the results land in an
overlay chunk instead of being merged back into a CSV.

Sharing the cache is deliberate. A DOI Stage 1 already asked Europe PMC about is
answered from `cache/abstracts/` here for free, and a miss recorded here is a
miss Stage 1 will not re-buy.

The OSF source (registrant 10.17605, first in the order) is the one that is not
an abstract lookup. Those records have no abstract anywhere — they have a
registration template and a responses form — so the phase writes the template
name as the first line of the recovered text and the responses under it. That
line is what `osf-registration-completed` and `osf-registration-protocol` read:
a Replication Recipe post-completion record is a finished replication with its
outcome already coded, a preregistration on the same registrant is a plan.

**Dry-run is the default.** Issue #146 §6: a backfill over the pool needs
per-source quota estimates BEFORE fetching — Scopus alone is a ~10k/week ceiling
against a worklist that can hold a million rows. `--run` is the only thing that
spends anything.

    python -m filter.engine.backfill --worklist wl.parquet --overlay-dir data/overlay
    python -m filter.engine.backfill --worklist wl.parquet --overlay-dir D --run --limit 500
"""

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filter.engine.overlay import (
    freeze, overlay_work_ids, read_worklist, write_chunk,
)
from search.fetch_abstracts import (
    OSF_REGISTRANT, _DATASET_PREFIXES, _fetch_crossref_abstract, _fetch_epmc_batch,
    _fetch_openalex_batch, _fetch_osf_registration, _fetch_s2_abstract,
    _fetch_s2_batch, _fetch_scopus_abstract, _load_checkpoint, _load_found_index,
    _phase_targets, _read_abstract_cache, _run_batch_phase, _run_item_phase,
)
from shared.config import (
    CROSSREF_RATE_SEC, ELSEVIER_API_KEY, EPMC_BATCH_SIZE, EPMC_RATE_SEC,
    OA_BATCH_SIZE, OPENALEX_RATE_SEC, OSF_RATE_SEC, S2_API_KEY, S2_BATCH_RATE_SEC,
    S2_BATCH_SIZE, SCOPUS_DEFAULT_LIMIT, SCOPUS_RATE_SEC, log,
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

# The measured order (see fetch_abstracts' module docstring). Also the priority
# order a recovered abstract is attributed to a source in — which is why `osf`
# leads: an OSF registration's own template line has to be the text the overlay
# stores, and a later source's abstract for the same row would displace it.
SOURCE_ORDER = ("osf", "openalex", "epmc", "s2", "crossref", "scopus")


# ---------------------------------------------------------------------------
# Worklist
# ---------------------------------------------------------------------------


def _rows(worklist_path: Path, limit: Optional[int] = None) -> tuple[list[dict], int]:
    """Worklist rows in the shape fetch_abstracts' phase runners consume.

    `{"work_id", "oa", "doi_r"}` — `oa`/`doi_r` are exactly the keys
    `_phase_targets()` reads. Dataset-prefix DOIs are dropped: no source has an
    abstract for a Dataverse deposit, so every phase would spend calls
    rediscovering that forever. Returns (rows, n_dataset_dropped).
    """
    rows: list[dict] = []
    dropped = 0
    for record in read_worklist(worklist_path):
        doi = clean_doi(str(record.get("doi") or ""))
        if doi and doi.split("/")[0] in _DATASET_PREFIXES:
            dropped += 1
            continue
        rows.append({
            "work_id": int(record["work_id"]),
            "oa": f"https://openalex.org/W{int(record['work_id'])}",
            "doi_r": doi,
        })
        if limit and len(rows) >= limit:
            break
    return rows, dropped


# ---------------------------------------------------------------------------
# Estimates (the dry run)
# ---------------------------------------------------------------------------


def estimate(rows: list[dict], sources=SOURCE_ORDER) -> list[dict]:
    """Per-source targets and request counts for *rows*, spending nothing.

    Counted against the live checkpoint and found-index, so the estimate is what
    a `--run` would actually do next, not what a fresh worklist would cost.
    """
    done = _load_checkpoint()
    found = _load_found_index()
    estimates: list[dict] = []
    for source in sources:
        namespace = NAMESPACES[source]
        if source == "openalex":
            targets = [r["oa"] for r in rows
                       if r["oa"] and f"{namespace}:{r['oa']}" not in done]
        elif source == "osf":
            targets = _osf_targets(rows, done, found)
        else:
            targets = _phase_targets(rows, namespace, done, found)
        estimates.append(dict(_source_shape(source),
                              source=source,
                              targets=len(targets)))
        estimates[-1]["requests"] = _requests(estimates[-1])
    return estimates


def _osf_targets(rows: list[dict], done: set[str], found: set[str]) -> list[str]:
    """The OSF-registrant DOIs the OSF phase still has to try.

    The only source restricted to a registrant: the endpoint answers about OSF
    GUIDs and nothing else, so every other DOI is a call whose answer is known
    before it is made.
    """
    return [doi for doi in _phase_targets(rows, NAMESPACES["osf"], done, found)
            if doi.split("/", 1)[0] == OSF_REGISTRANT]


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
             f"{'source':<10} {'targets':>10} {'requests':>10}  {'rate':>6}  note",
             "-" * 62]
    for item in estimates:
        note = item["skipped"] and f"SKIPPED — {item['skipped']}"
        if not note and item["quota"] and item["targets"] > item["quota"]:
            note = (f"capped at {item['quota']:,}/run "
                    f"({item['targets'] - item['quota']:,} left over)")
        lines.append(f"{item['source']:<10} {item['targets']:>10,} "
                     f"{item['requests']:>10,}  {item['rate_sec']:>6}  {note}")
    lines += ["", "DRY RUN — nothing fetched. Re-run with --run to spend."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _resolved(row: dict) -> Optional[tuple[str, str]]:
    """(abstract, source) for *row* from the shared per-identifier cache, or None.

    The same key order `_lookup_cached_abstract()` uses; it returns only the text
    and the overlay records which source supplied it, so the lookup is repeated
    here rather than the source being guessed afterwards.
    """
    for source in SOURCE_ORDER:
        namespace = NAMESPACES[source]
        ident = f"{namespace}:{row['oa']}" if source == "openalex" \
            else (f"{namespace}:{row['doi_r']}" if row["doi_r"] else None)
        if not ident:
            continue
        value = _read_abstract_cache(ident)
        if value is not None and value != "__none__":
            return value, source
    return None


def run(worklist_path: Path, overlay_dir: Path, sources=SOURCE_ORDER,
        limit: Optional[int] = None, scopus_limit: int = SCOPUS_DEFAULT_LIMIT) -> dict:
    """Fetch text for the worklist and append it to *overlay_dir* as one chunk.

    Resumable in two independent places: the phase runners skip identifiers their
    checkpoint already holds, and the chunk write skips work ids the overlay
    already covers — so an interrupted run re-fetches nothing and cannot write a
    work into a second chunk (which `validate()` would refuse).
    """
    rows, dropped = _rows(worklist_path, limit)
    done = _load_checkpoint()
    found = _load_found_index()
    log.info("Backfill: %d worklist row(s)%s.", len(rows),
             f", {dropped} dataset DOI(s) dropped" if dropped else "")

    for source in sources:
        namespace = NAMESPACES[source]
        shape = _source_shape(source)
        if shape["skipped"]:
            log.info("%s: skipped (%s).", source, shape["skipped"])
            continue
        label = f"backfill — {source}"
        if source == "osf":
            _run_item_phase(label, namespace, _osf_targets(rows, done, found),
                            OSF_RATE_SEC, _fetch_osf_registration, found,
                            progress_every=200)
        elif source == "openalex":
            _run_batch_phase(
                label, namespace,
                [r["oa"] for r in rows if r["oa"] and f"{namespace}:{r['oa']}" not in done],
                OA_BATCH_SIZE, OPENALEX_RATE_SEC, _fetch_openalex_batch, found)
        elif source == "epmc":
            _run_batch_phase(label, namespace, _phase_targets(rows, namespace, done, found),
                             EPMC_BATCH_SIZE, EPMC_RATE_SEC, _fetch_epmc_batch, found)
        elif source == "s2":
            _run_batch_phase(label, namespace, _phase_targets(rows, namespace, done, found),
                             S2_BATCH_SIZE, S2_BATCH_RATE_SEC,
                             lambda batch: _fetch_s2_batch(batch, S2_API_KEY), found)
        elif source == "crossref":
            _run_item_phase(label, namespace, _phase_targets(rows, namespace, done, found),
                            CROSSREF_RATE_SEC, _fetch_crossref_abstract, found,
                            progress_every=2000)
        elif source == "scopus":
            targets = _phase_targets(rows, namespace, done, found)
            if scopus_limit and scopus_limit > 0:
                targets = targets[:scopus_limit]
            _run_item_phase(label, namespace, targets, SCOPUS_RATE_SEC,
                            _scopus_fetcher(), found, progress_every=500)

    return _write_overlay(rows, overlay_dir)


def _scopus_fetcher():
    def fetch(doi: str) -> tuple[Optional[str], str]:
        abstract, quota_exhausted = _fetch_scopus_abstract(doi, ELSEVIER_API_KEY)
        if quota_exhausted:
            return None, "stop"
        return (abstract, "ok") if abstract else (None, "empty")
    return fetch


def _write_overlay(rows: list[dict], overlay_dir: Path) -> dict:
    already = overlay_work_ids(overlay_dir)
    fetched_at = datetime.now(timezone.utc).isoformat()
    chunk: list[dict] = []
    by_source: dict[str, int] = {}
    seen: set[int] = set()
    for row in rows:
        if row["work_id"] in already or row["work_id"] in seen:
            continue
        hit = _resolved(row)
        if hit is None:
            continue
        seen.add(row["work_id"])
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
    parser.add_argument("--overlay-dir", type=Path, required=True,
                        help="Overlay release directory; chunks are appended here.")
    parser.add_argument("--run", action="store_true",
                        help="Actually fetch. Without it this only estimates.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicit form of the default.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Only the first N actionable worklist rows (pilots).")
    parser.add_argument("--source", action="append", choices=SOURCE_ORDER,
                        help="Restrict to one source (repeatable).")
    parser.add_argument("--scopus-limit", type=int, default=SCOPUS_DEFAULT_LIMIT,
                        metavar="N", help="Max Scopus calls this run (weekly quota "
                                          f"~10k; default {SCOPUS_DEFAULT_LIMIT}).")
    parser.add_argument("--freeze", action="store_true",
                        help="Freeze the overlay manifest after a successful run.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    sources = tuple(s for s in SOURCE_ORDER if not args.source or s in args.source)

    if not args.run:
        rows, dropped = _rows(args.worklist, args.limit)
        print(render_estimate(estimate(rows, sources), len(rows), dropped))
        return 0

    result = run(args.worklist, args.overlay_dir, sources=sources, limit=args.limit,
                 scopus_limit=args.scopus_limit)
    print(f"{result['rows']:,} overlay row(s) written"
          + (f" -> {result['chunk']}" if result["chunk"] else ""))
    for source, count in result["by_source"].items():
        print(f"  {source:<10} {count:,}")
    if args.freeze:
        manifest = freeze(args.overlay_dir)
        print(f"frozen: overlay {manifest['overlay_hash'][:12]}  "
              f"{manifest['rows']:,} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
