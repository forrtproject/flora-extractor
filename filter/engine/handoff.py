"""The Stage 3 switch: the engine's two screen piles as the file Stage 3 reads.

Issue #146 §7 step 4 — "the old Stage 2 path is superseded when the engine's
exports feed Stage 3". This is that feed. It writes `data/filtered.csv` in
`ENGINE_EXPORTED_COLS` order (`FILTERED_COLS` first, provenance after), which is
the one external contract the redesign preserves: `extract/run_extract.py` reads
the file by column name and passes trailing columns through untouched.

Three decisions worth stating.

**Both screen piles, expensive first.** `screen_expensive` is where the rules had
the most to say; putting it first means a `--limit`ed Stage 3 run works through
the strongest signal before the murky residue, without anyone having to sort a
4 GB CSV. Within a pile the order is the pool's.

**Live tier verdicts are applied HERE, not in the routing table.** Routing is
derived data — the next `route` run recomputes it from pool and specs — so a
verdict written into it would be erased. A work a live `screen_cheap` run
discarded is left out of the file; a work a live `screen_expensive` run typed
carries that type as its `filter_status`, and one it discarded is left out too,
because that is the same validated gate Stage 3 would apply to it a second time.
Verdicts from a `validation`-mode run change nothing, which is what that mode
means. Without Supabase configured there is nothing to read, and the piles hand
off exactly as routed — said out loud rather than assumed.

**The handoff file is not an immutable export.** `export_pile()` writes a
manifest that may never be overwritten, because an export is an artifact someone
else may have read. The handoff is a materialized view of the current release
that Stage 3 re-reads: it is rewritten whenever the release or the verdicts
move, and its manifest is rewritten with it. When an immutable copy is wanted,
`export` is still the command for it.
"""

import csv
import datetime
import hashlib
import json
from pathlib import Path
from typing import Optional

from filter.engine.export import (SPEC_DIR, UNCHECKED, check_release_binding,
                                  iter_export_rows, load_conventions)
from filter.engine.spec import FilterSpec
from shared.schema import ENGINE_EXPORTED_COLS

# The order Stage 3 meets the piles in.
HANDOFF_PILES = ("screen_expensive", "screen_cheap")


def write_handoff(con, pool_dir: Path, out_csv: Path, release_id: str, *,
                  drop: Optional[set[int]] = None,
                  record_types: Optional[dict[int, str]] = None,
                  conventions: Optional[dict] = None,
                  specs: Optional[list[FilterSpec]] = None,
                  aliases: Optional[dict[int, int]] = None,
                  spec_dir: Path = SPEC_DIR,
                  expect_bundle_hash: Optional[str] = None,
                  expect_alias_release: Optional[str] = None,
                  overlay_dir: Optional[Path] = None,
                  expect_overlay_hash: object = UNCHECKED,
                  from_year: Optional[int] = None, to_year: Optional[int] = None,
                  created_at: str = "") -> dict:
    """Write the two screen piles of *release_id* to *out_csv* for Stage 3.

    *drop* is the set of work ids a live tier run discarded; *record_types* maps a
    work id to the paper type a live `screen_expensive` run settled on, which
    becomes its `filter_status`. Both are read from the verdict rows by
    `decisions()` — they are passed in so this function stays a pure mapping from
    (store, pool, decisions) to a file, and so a caller with no Supabase can call
    it with neither.
    """
    check_release_binding(spec_dir, release_id, expect_bundle_hash,
                          expect_alias_release, overlay_dir, expect_overlay_hash)
    drop = drop or set()
    record_types = record_types or {}
    conventions = conventions or load_conventions()

    by_pile: dict[str, list[dict]] = {pile: [] for pile in HANDOFF_PILES}
    dropped = 0
    retyped = 0
    for pile, work, row in iter_export_rows(
            con, pool_dir, list(HANDOFF_PILES), release_id,
            from_year=from_year, to_year=to_year, conventions=conventions,
            specs=specs, aliases=aliases, spec_dir=spec_dir,
            overlay_dir=overlay_dir):
        if work in drop:
            dropped += 1
            continue
        record_type = record_types.get(work)
        if record_type:
            row["filter_status"] = record_type
            row["filter_method"] = "screen"
            retyped += 1
        by_pile[pile].append(row)

    rows = [row for pile in HANDOFF_PILES for row in by_pile[pile]]
    _write_csv(out_csv, rows)
    manifest = {
        "release_id": release_id,
        "piles": list(HANDOFF_PILES),
        "rows": len(rows),
        "rows_per_pile": {pile: len(by_pile[pile]) for pile in HANDOFF_PILES},
        "dropped_by_tier_verdict": dropped,
        "typed_by_tier_verdict": retyped,
        "csv": Path(out_csv).name,
        "sha256": hashlib.sha256(Path(out_csv).read_bytes()).hexdigest(),
        "created_at": created_at or _now(),
        "columns": ENGINE_EXPORTED_COLS,
    }
    Path(str(out_csv) + ".manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


def decisions(client, release_id: str) -> tuple[set[int], dict[int, str]]:
    """`(work ids to drop, work id → record type)` from every LIVE tier run.

    Reads the permanent verdict rows, not a run report. A `validation`-mode run
    contributes nothing here — its claim says `mode: validation` and this asks
    only for `live` — which is how "verdicts recorded, no pile effect" survives
    all the way to the file Stage 3 reads.
    """
    from filter.engine.tiers import DISCARD, TIER_CHEAP, TIER_EXPENSIVE, tier_decisions

    drop: set[int] = set()
    record_types: dict[int, str] = {}
    for tier in (TIER_CHEAP, TIER_EXPENSIVE):
        for work, decision in tier_decisions(client, release_id, tier).items():
            if decision["outcome"] == DISCARD:
                drop.add(work)
            elif tier == TIER_EXPENSIVE and decision.get("record_type"):
                record_types[work] = decision["record_type"]
    return drop, record_types


def _write_csv(out_csv: Path, rows: list[dict]) -> None:
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=ENGINE_EXPORTED_COLS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: "" if row.get(col) is None else row.get(col)
                             for col in ENGINE_EXPORTED_COLS})


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
