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
carries that type as its `filter_status`, and one it discarded is left out too.
Verdicts from a `validation`-mode run change nothing, which is what that mode
means. The verdicts are read across releases: the release scopes the piles, not
the evidence, and a work decided before a re-route is still a decided work.

**The expensive screen's answer TRAVELS, because it does not run twice.** The
two-voter front door used to run in both places — this tier and Stage 3's first
step — sharing a cache key so the second pass was free but the logic was written
twice. It runs here only. What Stage 3 needs is therefore written onto the row
(`SCREEN_COLS` in `shared/schema.py`): the gate outcome, the record type, the
category union, the evidence, and each voter's classification and confidence,
which the pre-PDF title-search rung is gated on. `_screen_columns()` below is
the whole translation.

**A row travels on a verdict, not on a routing decision — and only on the
expensive tier's.** The default is screened-only: a work the rules routed into a
screen pile but no live `screen_expensive` run ever decided is NOT exported.
Routing says "this deserves an LLM's attention"; only the validated pair says
"this reaches Stage 3". A CHEAP verdict never admits: its `proceed` means "on to
the expensive screen", and admitting on it — a bypass included — would hand
Stage 3 a row nothing has screened, now that Stage 3 does not screen.
`--as-routed` restores the older behaviour — export the piles as routed,
applying whatever verdicts exist — and it is the only mode in which the piles
hand off without Supabase, because with no claims client there are no verdicts
and screened-only would legitimately export nothing. Its rows can reach Stage 3
with no screen verdict at all, and Stage 3 has a defined answer for that.

Absence is accounted for either way: the manifest's `dropped_by_tier_verdict`
and `skipped_unscreened` are different facts about the same missing row, and
together with `rows` they cover every work the piles contain.

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
import os
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
                  screen: Optional[dict[int, dict]] = None,
                  decided: Optional[set[int]] = None,
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

    *drop* is the set of work ids a live tier run discarded; *screen* maps a work
    id to what a live `screen_expensive` run decided about it — the gate outcome,
    the record type and the individual votes. *decided* is every work id the
    EXPENSIVE tier settled. All three are read from the verdict rows by
    `decisions()` — they are passed in so this function stays a pure mapping from
    (store, pool, decisions) to a file.

    *decided* also chooses the export's contract, because a set and its absence
    say different things. A set (empty included) means SCREENED-ONLY: a pile row
    outside it was never judged, so it is left out and counted as
    `skipped_unscreened`. `None` means AS-ROUTED: every pile row travels except
    the ones a verdict discarded — which is the only thing a caller with no
    Supabase can ask for, and the one mode in which a row can reach Stage 3 with
    its `SCREEN_COLS` blank.

    **The screen verdict is written onto the row, not just applied to it.** The
    two-voter front door runs here and nowhere else now, so Stage 3 has to be able
    to read what it said rather than ask again: the record type, the category
    union, the evidence, and the per-voter classification/confidence its title-
    search rung is gated on. `_screen_columns()` is the whole translation.
    """
    check_release_binding(spec_dir, release_id, expect_bundle_hash,
                          expect_alias_release, overlay_dir, expect_overlay_hash)
    drop = drop or set()
    screen = screen or {}
    conventions = conventions or load_conventions()

    by_pile: dict[str, list[dict]] = {pile: [] for pile in HANDOFF_PILES}
    dropped = 0
    unscreened = 0
    retyped = 0
    for pile, work, row in iter_export_rows(
            con, pool_dir, list(HANDOFF_PILES), release_id,
            from_year=from_year, to_year=to_year, conventions=conventions,
            specs=specs, aliases=aliases, spec_dir=spec_dir,
            overlay_dir=overlay_dir):
        if work in drop:
            dropped += 1
            continue
        if decided is not None and work not in decided:
            unscreened += 1
            continue
        decision = screen.get(work)
        if decision:
            row.update(_screen_columns(row, decision))
            if decision.get("record_type"):
                row["filter_status"] = decision["record_type"]
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
        "skipped_unscreened": unscreened,
        "screened_only": decided is not None,
        "typed_by_tier_verdict": retyped,
        "csv": Path(out_csv).name,
        "sha256": hashlib.sha256(Path(out_csv).read_bytes()).hexdigest(),
        "created_at": created_at or _now(),
        "columns": ENGINE_EXPORTED_COLS,
    }
    Path(str(out_csv) + ".manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


def _screen_columns(row: dict, decision: dict) -> dict:
    """`SCREEN_COLS` for one row, from one expensive-tier decision.

    Two sources, and which fact comes from which is the point. The **verdict rows**
    are the authority: the gate outcome, the record type, and each voter's
    classification, confidence and quote are permanent evidence in Postgres, and
    everything Stage 3 DECIDES with is read from there.

    The category union and the voters' reasoning are not in that table — it is lean
    on purpose, and the full response lives in the blob — so they are read from the
    classify cache, which is content-complete and therefore provably the answer
    this row was screened with. `cached_classification()` cannot call a model, so a
    checkout without the cache writes those two columns blank rather than paying
    for them again; nothing downstream decides on either.
    """
    from shared.llm_client import cached_classification

    votes = decision.get("votes") or []
    detail = cached_classification(str(row.get("doi_r") or ""),
                                   str(row.get("title_r") or ""),
                                   str(row.get("abstract_r") or "")) or {}
    return {
        "screen_verdict": decision.get("outcome", ""),
        "screen_record_type": decision.get("record_type", ""),
        "screen_categories": "|".join(detail.get("categories") or []),
        "screen_votes": "|".join(
            f"{v.get('model', '')}={v.get('classification', '')}/"
            f"{'confident' if v.get('confident') else 'unconfident'}" for v in votes),
        "screen_evidence": next((v["quote"] for v in votes if v.get("quote")), ""),
        "screen_reasoning": str(detail.get("llm_reasoning") or ""),
    }


def decisions(client) -> tuple[set[int], dict[int, dict], set[int]]:
    """`(drop, work id → expensive-tier decision, decided)` from every LIVE run.

    Reads the permanent verdict rows, not a run report. A `validation`-mode run
    contributes nothing here — its claim says `mode: validation` and this asks
    only for `live` — which is how "verdicts recorded, no pile effect" survives
    all the way to the file Stage 3 reads.

    Across every release, deliberately: a verdict follows the WORK, and the
    release scopes the piles, not the evidence. A re-route mints a new release id
    from the same pool and the same voters' answers, and a handoff that asked only
    about the new id would find nothing decided — while the tier runner, whose
    checkpoint is tier-scoped, would skip those works as already screened. The
    file would then be silently short of every row the engine had actually paid to
    decide.

    Across every release but only within the current SCREENING GENERATION: a
    verdict from a superseded voter pair or classify prompt says what an older
    screen thought, and steering the handoff with it would hand Stage 3 rows the
    current screen has never judged. Those works read as unscreened here and are
    claimable again by the tier (`tiers._generation_current`).

    *decided* is only the works the tier actually SETTLED: a work whose votes are
    still short of a gate decision reads `incomplete` and belongs with the
    unscreened, not with the proceeds — a half-screened row is exactly the row the
    screened-only handoff exists to hold back.

    **Only the EXPENSIVE tier admits.** A cheap-tier verdict is either a discard
    (which drops the row here) or a `proceed`, and that proceed means one thing
    only: on to the expensive screen. Counting it as `decided` would let a row the
    validated pair has never seen — including a mere `prescreen_bypass`, which is
    the tier deciding not to ask — cross the screened-only handoff and reach Stage
    3, where nothing screens it any more. So the cheap tier contributes to *drop*
    and to nothing else.
    """
    from filter.engine.tiers import (DISCARD, PROCEED, TIER_CHEAP, TIER_EXPENSIVE,
                                     tier_decisions)

    drop: set[int] = set()
    screen: dict[int, dict] = {}
    decided: set[int] = set()
    for tier in (TIER_CHEAP, TIER_EXPENSIVE):
        # None: every release — see the docstring above.
        for work, decision in tier_decisions(client, None, tier).items():
            if decision["outcome"] not in (DISCARD, PROCEED):
                continue
            if decision["outcome"] == DISCARD:
                drop.add(work)
            if tier != TIER_EXPENSIVE:
                continue
            decided.add(work)
            screen[work] = decision
    return drop, screen, decided


def _write_csv(out_csv: Path, rows: list[dict]) -> None:
    """Write the handoff through a sibling temp file and `os.replace()` it in.

    The handoff is rewritten in place over the file Stage 3 reads, so writing to
    it directly means an interruption — Ctrl-C, a full disk, a raised binding
    check — leaves a truncated `filtered.csv` and destroys the previous handoff,
    which was a working input. The rename is atomic within a directory, so the
    file is either the old handoff or the new one and never half of either.
    """
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_csv.with_name(out_csv.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=ENGINE_EXPORTED_COLS,
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({col: "" if row.get(col) is None else row.get(col)
                                 for col in ENGINE_EXPORTED_COLS})
        os.replace(tmp, out_csv)
    finally:
        tmp.unlink(missing_ok=True)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
