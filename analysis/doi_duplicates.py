"""Find the pool's same-DOI work groups and write them into `filter/spec/aliases.json`.

OpenAlex holds more than one work record for one article — an institutional-repository
deposit and the publisher's version of record — and the engine keys everything on the
work id, so both are routed, both are claimed, and both run the whole Stage 3 ladder to
buy the same answer twice (issue #193). `aliases.json` is the mechanism that already
collapses two ids into one work; it can only carry merges OpenAlex has itself performed,
which these are not.

A DOI is a work-level identifier, so two pool works carrying the same DOI SHOULD be one
article — but in this pool they very often are not. Of the 16,374 DOIs held under more
than one work id, 13,172 hold records whose titles have nothing to do with each other
("Adolescentes y métodos anticonceptivos" under the DOI of a hand-trauma paper): the
OpenAlex record carries a DOI that is not its own. Merging on the identifier alone would
therefore hide one real paper behind another 13,172 times, so a group is merged only when
its records also AGREE on what the paper is — every pair of titles at Jaccard
`MIN_TITLE_AGREEMENT` or above. 3,202 groups (6,407 works) pass that, and the count moves
little across the threshold (3,359 at 0.5, 2,981 at 0.8), so nothing here turns on where
it sits. Read at the boundary, the pairs it admits differ by a stripped subtitle, an HTML
entity, or a version suffix.

Which record of an agreeing group is CANONICAL needs its own rule, and this is it,
in order:

  1. a work whose primary location is a repository loses to one whose is not — the
     version of record carries the real journal name and the publisher's PDF URL, and
     the deposit carries a repository name and an OAI identifier;
  2. among works that tie, the lowest work id wins. Arbitrary, and deterministic, which
     is what the alias file needs.

OpenAlex's own `works/doi:<doi>` lookup is not the tie-break: asked for the confirmed
pair it returns the KU Leuven deposit rather than the Frontiers article.

The DOI-less duplicates the issue also reports — 46 title groups over the export's
DOI-less rows — are NOT merged here. Title is a last-resort identifier for a stated
reason (`shared/row_key.py`, issue #53): Reply/Commentary pairs and RRR stubs share
titles without being one work. `--titles` reports them for a human to decide on.

    .venv/bin/python -m analysis.doi_duplicates                 # report, the default
    .venv/bin/python -m analysis.doi_duplicates --apply         # write aliases.json
    .venv/bin/python -m analysis.doi_duplicates --titles        # the DOI-less report

Writing the file changes `alias_release`, which is an input to the routing release id,
so a run of `.venv/bin/python -m filter.engine route` has to follow it before anything
reads the new grouping.
"""

import argparse
import csv
import itertools
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from shared.config import DATA_DIR, SNAPSHOT_POOL_DIR
from shared.disambiguation import jaccard_similarity

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"
ALIASES = SPEC_DIR / "aliases.json"

# How alike two records' titles must be for the DOI they share to be believed.
MIN_TITLE_AGREEMENT = 0.6


def _groups(pool_dir: Path) -> list[tuple[str, list[dict]]]:
    """Every (doi, works) the pool holds under more than one work id.

    One DuckDB pass over the parquet, reading three columns. The whole 5.1M-row pool
    answers in under a second, so there is no sampling and no incremental mode.
    """
    import duckdb

    rows = duckdb.connect().execute(f"""
        with w as (
          select
            regexp_replace(lower(trim(coalesce(doi, ''))),
                           '^(https?://(dx\\.)?doi\\.org/|doi:)', '') as doi,
            cast(regexp_extract(id, '(\\d+)$', 1) as bigint) as work,
            coalesce(display_name, title, '') as title,
            coalesce(primary_location, '') as location
          from read_parquet('{pool_dir}/*.parquet')
        )
        select doi, work, title, location from w
        where doi <> '' and doi in (
          select doi from w where doi <> '' group by doi having count(distinct work) > 1)
        order by doi, work
    """).fetchall()

    grouped: dict[str, dict[int, dict]] = defaultdict(dict)
    for doi, work, title, location in rows:
        grouped[doi][work] = {"work": work, "title": title,
                              "repository": _is_repository(location)}
    return [(doi, list(works.values())) for doi, works in grouped.items()
            if len(works) > 1]


def _agree(works: list[dict]) -> bool:
    """Whether every pair of the group's titles is the same paper's."""
    return all(jaccard_similarity(a["title"], b["title"]) >= MIN_TITLE_AGREEMENT
               for a, b in itertools.combinations(works, 2))


def _is_repository(location: str) -> bool:
    """Whether the work's primary location is a repository rather than a publisher.

    Read off the JSON with a regex rather than parsed: the field is a JSON string in
    the parquet, the only key needed is the source's `type`, and parsing 33k of them
    to reach one string costs more than it says.
    """
    match = re.search(r'"type":\s*"([^"]+)"', location or "")
    return bool(match) and match.group(1) == "repository"


def _canonical(works: list[dict]) -> dict:
    """The work the others alias to — see the rule in the module docstring."""
    return sorted(works, key=lambda w: (w["repository"], w["work"]))[0]


def _report(groups: list[tuple[str, list[dict]]],
            rejected: list[tuple[str, list[dict]]]) -> None:
    works = sum(len(w) for _, w in groups)
    split = sum(1 for _, w in groups if any(x["repository"] for x in w)
                and not all(x["repository"] for x in w))
    print(f"{len(groups) + len(rejected)} DOIs are held under more than one work id")
    print(f"{len(rejected)} of them hold records whose titles disagree — the DOI is on a "
          f"record that is not its own, and nothing is merged there")
    print(f"{len(groups)} agree and are merged, covering {works} works "
          f"({works - len(groups)} redundant)")
    print(f"{split} of the merged groups are a repository deposit beside a published "
          f"record; the rest are decided by the lowest work id")
    for doi, entries in groups[:10]:
        canonical = _canonical(entries)
        print(f"\n  {doi}")
        for entry in sorted(entries, key=lambda e: e["work"]):
            mark = "canonical" if entry is canonical else "alias    "
            where = "repository" if entry["repository"] else "published"
            print(f"    {mark}  W{entry['work']}  {where}  {entry['title'][:60]}")
    if len(groups) > 10:
        print(f"\n  … {len(groups) - 10} more groups")


def _apply(groups: list[tuple[str, list[dict]]]) -> int:
    """Write the alias entries, replacing whatever the file held. Returns the count.

    Replacing rather than merging: the file's whole content is derived from the pool by
    this script, so a second run over a re-scanned pool must not carry forward aliases
    the pool no longer supports.
    """
    aliases: dict[str, str] = {}
    for _, entries in groups:
        canonical = _canonical(entries)
        for entry in entries:
            if entry is not canonical:
                aliases[f"W{entry['work']}"] = f"W{canonical['work']}"
    # `resolve()` follows exactly one hop, so a canonical id that is itself an alias
    # would leave two ids for one work — the thing this is here to prevent. A work
    # carries one DOI and the groups are one per DOI, so it cannot happen; assert it
    # rather than trust it, because the file is written unread.
    chained = set(aliases.values()) & set(aliases)
    if chained:
        raise AssertionError(f"alias chain: {sorted(chained)[:5]}")
    ALIASES.write_text(json.dumps(
        {"version": 1, "aliases": dict(sorted(aliases.items()))},
        indent=2) + "\n", encoding="utf-8")
    return len(aliases)


def _title_groups() -> None:
    """The export's DOI-less rows grouped on a normalised title — a report only."""
    path = DATA_DIR / "extracted.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))
    field = lambda row, key: (row.get(key) or "").strip()  # noqa: E731
    norm = lambda text: re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()  # noqa: E731

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if not field(row, "doi_r") and field(row, "title_r"):
            grouped[norm(field(row, "title_r"))].append(row)
    multi = {title: group for title, group in grouped.items()
             if len({field(r, "oa_work_id_r") for r in group}) > 1}
    agree = sum(1 for group in multi.values()
                if len({field(r, "doi_o") for r in group}) == 1)
    print(f"{len(multi)} title groups over the DOI-less rows of {path.name}, covering "
          f"{sum(len(g) for g in multi.values())} rows")
    print(f"{agree} of them resolved to one and the same doi_o")
    for title, group in list(multi.items())[:10]:
        ids = ", ".join(sorted({field(r, "oa_work_id_r") for r in group}))
        dois = ", ".join(sorted({field(r, "doi_o") for r in group}))
        print(f"\n  {title[:70]}\n    {ids}\n    doi_o: {dois}")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the alias entries into filter/spec/aliases.json")
    parser.add_argument("--titles", action="store_true",
                        help="report the export's DOI-less title groups instead")
    parser.add_argument("--pool-dir", type=Path, default=SNAPSHOT_POOL_DIR)
    args = parser.parse_args(argv)

    if args.titles:
        _title_groups()
        return 0
    if not args.pool_dir.exists():
        print(f"no pool at {args.pool_dir}", file=sys.stderr)
        return 2
    found = _groups(args.pool_dir)
    groups = [(doi, works) for doi, works in found if _agree(works)]
    rejected = [(doi, works) for doi, works in found if not _agree(works)]
    _report(groups, rejected)
    if args.apply:
        written = _apply(groups)
        print(f"\nwrote {written} aliases to {ALIASES}")
        print("run `.venv/bin/python -m filter.engine route` — the alias file is an "
              "input to the routing release id")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
