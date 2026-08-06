"""`data/extracted.csv`, rendered from the extract tier's stored verdicts.

The tier writes one result row per work into `engine_verdicts`, payload and all
(`extract/tier.py`). This turns those payloads back into the CSV Stage 3 has always
produced. It is a pure render — no network, no cache, no routing store, no pool —
which is the property the payload shape exists to guarantee: a payload that needed
any of those would make the permanent verdict a bookmark rather than evidence.

    python -m extract.export                       # → data/extracted.csv
    python -m extract.export --check               # diff against the file on disk
    python -m extract.export --current-generation-only

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
           current_generation_only: bool = False) -> dict:
    """The whole export, in memory: the main rows, the set-asides, and the counts."""
    results, stale = latest_results(
        client, mode=mode, current_generation_only=current_generation_only)
    rows = rows_from_results(results)
    main, aside = partition(rows)
    return {"works": len(results), "rows": len(rows), "main": main, "aside": aside,
            "superseded_generation": stale,
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
                    "verdicts. A pure render: no network, no cache, no pool.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--mode", choices=("live", "validation"), default="live",
                        help="Which runs' verdicts to render (claim meta.mode).")
    parser.add_argument("--check", action="store_true",
                        help="Diff the render against the file on disk and exit "
                             "non-zero if they differ. Writes nothing.")
    parser.add_argument("--current-generation-only", action="store_true",
                        help="Drop works whose only result row is from a superseded "
                             "generation, instead of carrying it forward.")
    args = parser.parse_args(argv)

    try:
        client = ClaimsClient()
    except ClaimsNotConfigured as exc:
        raise SystemExit(f"{exc}. The verdicts this renders live in the state "
                         "authority, so there is nothing to render without it.")

    report = render(client, mode=args.mode,
                    current_generation_only=args.current_generation_only)
    print(f"generation {extract_generation()}  mode {args.mode}")
    print(f"  {report['works']:,} work(s) → {report['rows']:,} row(s)")
    for ending, count in sorted(report["endings"].items()):
        print(f"    {ending:<20} {count:,}")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
