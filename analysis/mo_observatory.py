"""Build the Metascience Observatory priority list and the curated rule that admits it.

The Metascience Observatory publishes a replications database
(`delton137/metascience-observatory`, `data/replications_database_*.csv`): one row per
replication–original pair, with the replication's DOI. It is a different pipeline's
answer to the question this one asks, so it is a RECALL PRIOR — a list of works
somebody already believes is a replication — and nothing more. It is not truth:
roughly 5,250 of its 8,892 rows carry an `ai_version`, meaning an LLM extracted them,
and one of its own sources is a June 2026 import of FLoRA.

So the list is used the way a curated source is used here: to ROUTE, never to admit.
`filter/spec/CONVENTIONS.md` — rules route and discard, only LLMs admit — is the whole
design. A work on this list is put in front of the expensive screen; the screen's two
voters decide what it is, exactly as they do for a work a phrase rule admitted.

What the list is worth prioritising over is the question this module answers rather
than assumes, and it answers it in two halves:

  * the half that needs no pool — how much of the Observatory is already in published
    FLoRA, already in the validation tables, already extracted here, or sitting in one
    of Stage 3's set-aside piles. That is `--report`, and it runs off `data/` alone.
  * the half that needs the pool — how many of the remainder are pool rows at all, and
    how many of THOSE the live routing release already admits. That is not written
    here: `analysis/arm_evidence.py` is the repo's scorer for exactly this question,
    and it takes the candidate spec this module writes.

        .venv/bin/python -m analysis.mo_observatory --report
        .venv/bin/python -m analysis.mo_observatory --spec
        .venv/bin/python -m analysis.arm_evidence \\
            --spec analysis/mo_observatory/curated-observatory.json

The candidate spec is written OUTSIDE `filter/spec/` on purpose. A file in that
directory is in `bundle_hash()`, so adding one mints a routing release and obliges a
re-route before anything can be exported; a candidate nobody has scored yet should
cost nothing. Promotion is `git mv` into `filter/spec/`, and the decision to promote is
read off `arm_evidence`'s "matched, not admitted" column — if the live release already
admits them all, the rule buys nothing and should not ship.

Read-only, like everything under `analysis/`: it writes into `analysis/mo_observatory/`
and nowhere else.
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from shared.config import DATA_DIR, FLORA_SHEET_PATH
from shared.utils import clean_doi

csv.field_size_limit(10 ** 9)

HERE = Path(__file__).resolve().parent / "mo_observatory"
SOURCE_URL = ("https://raw.githubusercontent.com/delton137/metascience-observatory/"
              "main/data/replications_database_2026_09_04_184008.csv")
PRIORITY_CSV = HERE / "priority.csv"
SPEC_PATH = HERE / "curated-observatory.json"

# The Observatory columns worth carrying into the priority list. Its per-row verdict
# (`result`) and its own confidence are kept because they are what a later comparison
# of the two pipelines' answers is made against — not because anything routes on them.
PRIORITY_COLS = ("doi_r", "original_url", "result", "replication_type",
                 "replication_year", "replication_title", "field",
                 "source", "validated", "confidence", "ai_version")

# Where Stage 3 files a work it has already answered. `extracted.csv` is the shipped
# answer; the rest are its quarantines, and a work in one of them has been paid for
# even though it is not in the export. Named rather than globbed so a new set-aside
# file does not silently change what this reports.
SETTLED_FILES = ("extracted.csv", "not_a_replication.csv", "no_evidence.csv",
                 "prospective_registration.csv", "keyed_link_disputed.csv")
OPEN_FILES = ("target_pending.csv", "unidentified_original.csv",
              "no_original_found.csv", "search_link_unconfirmed.csv",
              "api_error.csv")


def observatory_rows(path: Path) -> list[dict]:
    """Every row of the Observatory CSV, replication DOI cleaned into `doi_r`.

    259 of its rows identify the replication by something that is not a DOI (an OSF
    URL, a PDF link). They are dropped here rather than resolved: the rule this feeds
    matches on the pool's DOI column, so a row with no DOI cannot reach it, and
    pretending otherwise would put a number in the report that no rule can act on.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            doi = clean_doi(str(raw.get("replication_url") or ""))
            if not doi.startswith("10."):
                continue
            raw["doi_r"] = doi
            rows.append(raw)
    return rows


def _dois(path: Path, column: str = "doi_r") -> set[str]:
    """Cleaned DOIs in *column* of *path*, or an empty set if it has no such column."""
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or column not in reader.fieldnames:
            return set()
        return {d for d in (clean_doi(str(row.get(column) or "")) for row in reader)
                if d.startswith("10.")}


def known_dois(data_dir: Path) -> dict[str, set[str]]:
    """The DOI sets a priority list has to be subtracted from, by what they mean.

    `published` and `validated` are the two skip lists Stage 3 already honours
    (`shared/flora_skip.py`), so a work in either is one this pipeline will not
    extract however it is routed — putting it on a curated list would route it into a
    screen whose verdict nothing would read.
    """
    return {
        "published": _dois(data_dir / "flora.csv") | _dois(FLORA_SHEET_PATH),
        "validated": _dois(data_dir / "validated_skip.csv", "doi"),
        "settled": set().union(*(_dois(data_dir / name) for name in SETTLED_FILES)),
        "open": set().union(*(_dois(data_dir / name) for name in OPEN_FILES)),
    }


def priority(rows: list[dict], known: dict[str, set[str]]) -> list[dict]:
    """One row per Observatory DOI this pipeline could still usefully extract.

    Deduplicated on the DOI — the Observatory carries one row per replication–ORIGINAL
    pair, and Stage 3 finds the originals itself — keeping the first row's metadata.
    A work already extracted here is KEPT: the rule is about routing, and Stage 3's
    own checkpoint subtracts works it has settled. Only the two skip lists are
    subtracted, because those are the works no routing can put back in the worklist.
    """
    skip = known["published"] | known["validated"]
    seen: set[str] = set()
    out = []
    for row in rows:
        doi = row["doi_r"]
        if doi in skip or doi in seen:
            continue
        seen.add(doi)
        out.append({col: str(row.get(col) or "") for col in PRIORITY_COLS})
    return sorted(out, key=lambda r: r["doi_r"])


def report(rows: list[dict], known: dict[str, set[str]]) -> str:
    """What the Observatory holds, split by what this repo already knows about it."""
    dois = {row["doi_r"] for row in rows}
    published, validated = known["published"], known["validated"]
    settled, open_ = known["settled"], known["open"]
    remainder = dois - published - validated
    unseen = remainder - settled - open_
    spec_path = SPEC_PATH
    if SPEC_PATH.is_relative_to(Path.cwd()):
        spec_path = SPEC_PATH.relative_to(Path.cwd())
    lines = [
        (f"Metascience Observatory — {len(rows):,} rows, {len(dois):,} distinct "
         "replication DOIs"),
        "",
        f"  already in published FLoRA       {len(dois & published):6,}",
        f"  already in the validation tables {len(dois & validated):6,}",
        f"  -> candidates for this pipeline  {len(remainder):6,}",
        "",
        "  of those candidates:",
        (f"    settled here already           {len(remainder & settled):6,}"
         "   (extracted or quarantined)"),
        (f"    open in a Stage 3 pile         {len(remainder & open_):6,}"
         "   (target_pending, no original, …)"),
        (f"    never seen in data/ at all     {len(unseen):6,}"
         "   <- what a curated rule is for"),
        "",
        "  The last line is an UPPER BOUND on what the rule can buy: a work this repo",
        "  has never written a row for may be absent from the survivor pool entirely,",
        "  which is a Stage 1 gate question and no rule can fix it. Score the",
        "  candidate spec against the pool to split it:",
        f"    .venv/bin/python -m analysis.arm_evidence --spec {spec_path}",
    ]
    tags = Counter(row.get("replication_initiative_tag") or "" for row in rows)
    named = [(tag, n) for tag, n in tags.most_common() if tag]
    if named:
        lines += ["", "  Largest initiative tags: "
                  + ", ".join(f"{tag} {n}" for tag, n in named[:6])]
    return "\n".join(lines)


def build_spec(dois: Iterable[str]) -> dict:
    """The curated routing rule over *dois* — a candidate, not a shipped spec.

    `screen_expensive` and no `vocabulary`: an external database's belief that a work
    is a replication is a request for the screen's attention, not a claim about what
    the work IS, and CONVENTIONS.md reserves `vocabulary` for the latter. The
    Observatory mixes replications and reproductions under one `replication_type`
    column anyway, so naming either would be a guess.

    Precedence 745 sits inside the `replication-claim-*` band, above `title-broad`
    (740) and below `title-strong` (750), and it is deliberately NOT above the
    discards. The first draft put it at 970 to outrank them, on the reasoning that a
    curated list names works rather than a pattern so it should override a rule that
    only guessed. Measured against release `2e31c9543026`, that claim buys exactly
    ONE work: of the 2,396 listed works in the pool, a single one is discarded (by
    `osf-registration-protocol`). A precedence is a claim about which rules this one
    must outrank, and outranking every definitional discard for one work is not a
    claim worth making.

    What the rule actually buys is 1,346 works that NO rule matched — `pending` with
    `no_filter_matched` — and carry abstract text. The rest of the arithmetic, all
    off release `2e31c9543026`:

        2,396  listed works in the pool
          579  already admitted to screen_expensive
        1,508  pending/no_filter_matched      <- what only a curated list reaches
                 1,346 carry text             <- the real buy
                   164 would land in no_text
          308  pending/no_text                <- a claim rule matched; still no text
            1  discarded

    The 308 are not rescued by shipping this: the no-text downgrade is engine policy
    over any screening pile (`_screenable_on_its_title` in `filter/engine/route.py`
    exempts titled OSF records only), so they would be downgraded again.
    """
    listed = sorted(set(dois))
    return {
        "id": "curated-observatory",
        "description": (
            "TEMPORARY, CURATED: the replication DOIs of the Metascience Observatory "
            f"database ({SOURCE_URL.rsplit('/', 1)[-1]}) that are not already in "
            "published FLoRA or the validation tables, routed to the expensive screen "
            "so the two voters see them. A recall prior from another pipeline, not "
            "evidence about any individual work: the Observatory is itself largely "
            "LLM-extracted and one of its sources is a June 2026 FLoRA import. It "
            "It sits in the replication-claim band rather than above the discards: "
            "measured on release 2e31c9543026, outranking them buys ONE work, and "
            "1,346 of the 1,508 it does buy are works no rule matched at all. The "
            "screen remains the only thing that admits. Regenerate with "
            "`python -m analysis.mo_observatory --spec`. DELETE IT once the works it "
            "names have been screened and extracted: a frozen list of DOIs teaches "
            "the bundle nothing and will silently re-admit works a later discard was "
            "written to drop."),
        "match": {"doi_in": listed},
        "pile": "screen_expensive",
        "precedence": 745,
        "shadow": False,
        "measured": [],
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PRIORITY_COLS))
        writer.writeheader()
        writer.writerows(rows)


def _source(path: Optional[str]) -> Path:
    if path:
        return Path(path)
    cached = sorted(HERE.glob("replications_database_*.csv"))
    if cached:
        return cached[-1]
    raise SystemExit(
        f"No Observatory CSV. Download it once:\n"
        f"  mkdir -p {HERE} && curl -sSL -o "
        f"{HERE / SOURCE_URL.rsplit('/', 1)[-1]} {SOURCE_URL}\n"
        f"or pass --source <file>.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source", default=None,
                        help="the Observatory CSV (default: the newest cached copy)")
    parser.add_argument("--report", action="store_true",
                        help="print the overlap breakdown (the default)")
    parser.add_argument("--csv", action="store_true",
                        help=f"write the priority list to {PRIORITY_CSV}")
    parser.add_argument("--spec", action="store_true",
                        help=f"write the candidate curated rule to {SPEC_PATH}")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    args = parser.parse_args(argv)

    rows = observatory_rows(_source(args.source))
    known = known_dois(Path(args.data_dir))
    listed = priority(rows, known)

    if args.report or not (args.csv or args.spec):
        print(report(rows, known))
    if args.csv:
        _write(PRIORITY_CSV, listed)
        print(f"wrote {PRIORITY_CSV} — {len(listed):,} DOIs")
    if args.spec:
        SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
        SPEC_PATH.write_text(
            json.dumps(build_spec(row["doi_r"] for row in listed), indent=2,
                       ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {SPEC_PATH} — {len(listed):,} DOIs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
