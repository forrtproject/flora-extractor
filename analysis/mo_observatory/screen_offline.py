"""Screen the Observatory works our pool never held — same models, outside the pipeline.

573 of the Observatory's replication DOIs are in OpenAlex but never reached the survivor
pool: the Stage 1 search gate reads a replication stem in the title or the abstract, and
these carry none (`analysis/mo_observatory/not_in_pool_report.md`). No Stage 2 rule can
reach them — a rule routes rows that are IN the pool — so the only way to learn whether
they are worth adding is to ask the screen directly.

This asks it. `classify_replication()` is called unchanged, so the prompt
(`_CLASSIFY_PROMPT`), the voter pair (`SCREENING_MODEL_1`/`_2` at their own efforts),
the per-vote cache keys and `screen_gate()` are all the shipped ones. A work screened
here gets the same answer it would get inside the engine, and the votes land in the
shared LLM cache under the same keys — so if these works are later added to the pool and
screened for real, the engine reads them back as cache hits and pays nothing.

**What it deliberately does NOT do.** It writes no routing row, no claim, no verdict in
the state authority, and nothing under `data/`. The engine's screen tier is claimed,
budget-gated and release-scoped; this is a measurement over works that have no release
and no work id in any pile. Its output is a CSV for a human to read.

The abstract is whatever the recovery waterfall found (`--abstracts`, default the
shared abstract store), because the gate miss and the missing abstract are the same
population: 417 of the 651 had no abstract in OpenAlex at all.

    .venv/bin/python -m analysis.mo_observatory.screen_offline            # dry run: what it would ask
    .venv/bin/python -m analysis.mo_observatory.screen_offline --run
    .venv/bin/python -m analysis.mo_observatory.screen_offline --run --limit 20

A run with one voter's key missing records what the other said and leaves
`screen_verdict` empty — `screen_gate()` returns None under two votes, and an
incomplete screen is an API failure rather than a verdict. Because the cache is
per-vote, finishing the pair later re-buys only the missing voter.
"""

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from shared import abstract_store
from shared.config import SCREENING_MODEL_1, SCREENING_MODEL_2
from shared.llm_client import classify_replication, provider_for, screen_voters
from shared.utils import clean_doi

csv.field_size_limit(10 ** 9)

HERE = Path(__file__).resolve().parent
CAUSES = HERE / "not_in_pool_causes.csv"
OUT = HERE / "screened.csv"

COLS = ("doi_r", "title_r", "abstract_chars", "abstract_source", "screen_verdict",
        "screen_record_type", "screen_categories", "screen_votes", "screen_evidence",
        "screen_reasoning", "mo_result", "mo_type", "mo_year", "mo_field", "cause")

# The namespaces the recovery waterfall writes, best first. `oa` is the OpenAlex
# abstract the work already had; the rest are what the waterfall went and got.
SOURCES = ("oa", "epmc", "doi", "scopus", "s2", "osf")


def text_for(doi: str, oa_id: str) -> tuple[str, str]:
    """(abstract, which source supplied it) from the shared store, or ("", "")."""
    for ns in SOURCES:
        # The OpenAlex phase keys on the exact identifier it was handed, which is the
        # URL form the pool stores (`oa:https://openalex.org/W…`) — not the bare id.
        key = f"oa:{oa_id}" if ns == "oa" and oa_id else f"{ns}:{doi}"
        found, value = abstract_store.lookup(key)
        if found and value:
            return value, ns
    return "", ""


def worklist(causes: Path) -> list[dict]:
    """Every gate-miss work, with whatever text the store now holds for it.

    Works already in published FLoRA or the validation tables are dropped here rather
    than screened: the question is which works are worth ADDING, and those are already
    ours. Works with no text at all are kept but not screened — a screen with nothing
    to read is not an answer, and the count of them is part of the finding.
    """
    from shared.config import DATA_DIR, FLORA_SHEET_PATH

    def dois_of(path: Path, column: str) -> set[str]:
        if not path.exists():
            return set()
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or column not in reader.fieldnames:
                return set()
            return {d for d in (clean_doi(str(r.get(column) or "")) for r in reader)
                    if d.startswith("10.")}

    ours = (dois_of(DATA_DIR / "flora.csv", "doi_r")
            | dois_of(FLORA_SHEET_PATH, "doi_r")
            | dois_of(DATA_DIR / "validated_skip.csv", "doi"))

    rows = []
    with causes.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            if not raw["cause"].startswith(("F_", "G_")):
                continue
            doi = clean_doi(raw["doi_clean"] or raw["doi_r"])
            if doi in ours:
                continue
            abstract, source = text_for(doi, raw.get("oa_id") or "")
            rows.append({"doi": doi, "title": raw.get("replication_title") or "",
                         "abstract": abstract, "source": source, "raw": raw})
    return rows


def screen_one(row: dict) -> dict:
    """One work through the shipped front door, flattened into an output row."""
    result = classify_replication(row["doi"], row["title"], row["abstract"])
    votes = result.get("votes") or []
    return {
        "doi_r": row["doi"],
        "title_r": row["title"],
        "abstract_chars": len(row["abstract"]),
        "abstract_source": row["source"],
        "screen_verdict": result.get("screen_verdict") or "",
        "screen_record_type": result.get("record_type") or "",
        "screen_categories": "|".join(result.get("categories") or ()),
        "screen_votes": "; ".join(
            f"{v.get('model', '?')}={v.get('classification')}"
            f"{'/confident' if v.get('confident') else ''}" for v in votes),
        "screen_evidence": (result.get("evidence_quote")
                            or (votes[0].get("quote") if votes else "") or "")[:500],
        "screen_reasoning": (result.get("reasoning") or "")[:500],
        "mo_result": row["raw"].get("result") or "",
        "mo_type": row["raw"].get("replication_type") or "",
        "mo_year": row["raw"].get("replication_year") or "",
        "mo_field": row["raw"].get("field") or "",
        "cause": row["raw"]["cause"],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--run", action="store_true",
                        help="actually call the models (default: report only)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--include-textless", action="store_true",
                        help="also screen works no source gave an abstract for, on "
                             "the title alone — what `_screenable_on_its_title` does "
                             "for OSF records, and these titles are often explicit "
                             "(\"… A Replication Study\"). The classify prompt "
                             "renders a missing abstract as '(not available)'.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--causes", default=str(CAUSES))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)

    rows = worklist(Path(args.causes))
    with_text = [r for r in rows if r["abstract"]]
    print(f"gate-miss works not already ours : {len(rows):5,}")
    print(f"  have text to screen            : {len(with_text):5,}")
    print(f"  no text from any source        : {len(rows) - len(with_text):5,}")
    by_source: dict[str, int] = {}
    for r in with_text:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    for source, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"      {source:8s} {n:5,}")

    print("\nvoters:")
    for provider, model, env, effort in screen_voters():
        import os
        print(f"  {model} @ {effort or '(none)'} -> {provider} "
              f"[{env}: {'set' if os.getenv(env) else 'MISSING'}]")

    if not args.run:
        n = len(rows if args.include_textless else with_text)
        print(f"\nDry run. --run screens {min(n, args.limit or n):,} works.")
        return 0

    todo = rows if args.include_textless else with_text
    todo = todo[:args.limit] if args.limit else todo
    print(f"\nScreening {len(todo):,} works…")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        out = list(pool.map(screen_one, todo))

    path = Path(args.out)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLS))
        writer.writeheader()
        writer.writerows(out)

    verdicts: dict[str, int] = {}
    for r in out:
        key = r["screen_verdict"] or "(incomplete)"
        verdicts[key] = verdicts.get(key, 0) + 1
    print(f"\nwrote {path}")
    for verdict, n in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        print(f"  {verdict:14s} {n:5,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
