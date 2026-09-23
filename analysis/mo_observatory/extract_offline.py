"""Original-linking and outcome coding for the Observatory works, outside the pipeline.

THROWAWAY. These works are not in the survivor pool and are not going into it, so no
release routes them, no work id claims them, and nothing here writes a verdict, a
claim, a pool row or a file under `data/`. It exists to answer one question about a
list somebody else curated: if our Stage 3 read these papers, what original would it
find and what outcome would it code?

It asks that with Stage 3's own code rather than an imitation of it. `_process_row()`
in `extract/run_extract.py` IS the per-row pipeline — the resolution ladder, the
per-target adapter, the guards, DOI verification, outcome coding — and its CLI is
retired precisely so it can be used as a library (CLAUDE.md, Module Map). So the
answers here are the answers the pipeline would give, and every LLM call lands on the
shipped cache key: a work later extracted for real reads them back free.

**The row is synthesised, and that is the one thing to watch.** Inside the pipeline a
row comes off the pool carrying `SCREEN_COLS` written by Stage 2's screen tier. Here
the screen ran offline (`screen_offline.py`), so this builds the same columns from its
CSV, in the pipeline's own formats — `screen_votes` is `|`-joined
`<model>=<classification>/<confident|unconfident>`, which is what `_screen_from_row()`
parses. Get that wrong and the row reads as unscreened and is written `target_pending`.

**The verdict is one voter's, and the gate is why that is sound for these rows.**
`screen_gate()` discards only on unanimity, so a work whose first voter did not say
`none` proceeds whatever the second says. Only such works are offered here; their
`screen_verdict` is `proceed` on the shipped gate's own logic, not on an assumption.
What a single vote DOES cost is the `llm_title_search` rung, which fires only when both
voters were qualifying and confident — with one vote it never fires, so a row that
would have been linked by a title search here ends unresolved instead. That is a
floor on what this measures, not a wrong answer.

    .venv/bin/python -m analysis.mo_observatory.extract_offline               # dry run
    .venv/bin/python -m analysis.mo_observatory.extract_offline --run
    .venv/bin/python -m analysis.mo_observatory.extract_offline --run --no-llm

`--no-llm` runs the deterministic rungs only — title-pattern and citation/candidate
matching over OpenAlex, which cost credits but no model call. It is the whole of what
this box can do while `OPENAI_API_KEY` is unset, because `LINKING_MODEL` and
`OUTCOME_MODEL` are both `gpt-6-luna`, which `provider_for()` routes to OpenAI
direct. There is no fallback by design.
"""

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import pandas as pd

from shared.config import LINKING_MODEL, OUTCOME_MODEL
from shared.llm_client import provider_for
from shared.schema import EXTRACTED_COLS

HERE = Path(__file__).resolve().parent
SCREENED = HERE / "screened.csv"
CANDIDATES = HERE / "curated_candidates.csv"
OUT = HERE / "extracted_offline.csv"

csv.field_size_limit(10 ** 9)

# A classification that is not "none" cannot be discarded by a unanimous gate, so the
# row's verdict is settled at one vote. "unclear" qualifies as a proceed too, but it
# carries no claim about what the work IS, so it is not offered for extraction by
# default — reading a paper the screen could not characterise is what --include-unclear
# is for.
_PROCEEDS = ("replication", "reproduction", "both")


def _vote_of(row: dict) -> tuple[str, bool]:
    """(classification, confident) from the offline screener's `screen_votes` cell."""
    cell = row.get("screen_votes") or ""
    if "=" not in cell:
        return "", False
    body = cell.split("=", 1)[1]
    return body.split("/")[0].strip(), "/confident" in body


def worklist(screened: Path, include_unclear: bool) -> list[dict]:
    """The screened works whose verdict is proceed, as pipeline rows.

    The verdict is read off `screen_verdict` when the pair completed. It falls back to
    voter 1's own classification when it did not: `screen_gate()` discards only on
    unanimity, so a first voter who did not say `none` settles a proceed alone — which
    is what made this measurable before the second voter had a key at all.
    """
    wanted = _PROCEEDS + (("unclear",) if include_unclear else ())
    rows = []
    with screened.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            label, confident = _vote_of(raw)
            verdict = (raw.get("screen_verdict") or "").strip()
            if verdict == "discard":
                continue
            if verdict == "proceed":
                # The pair's own answer. `screen_record_type` is blank where neither
                # voter gave a qualifying label — an `unclear`/`none` split that
                # proceeds without saying what the work IS.
                label = (raw.get("screen_record_type") or "").strip() or "unclear"
                if label not in wanted:
                    continue
            elif label not in wanted:
                continue
            # `record_type` drives `_record_type()` and the reproduction vocabulary;
            # "both" maps to replication exactly as the screen's own resolver does.
            record_type = "replication" if label == "both" else label
            rows.append({
                "doi_r": raw["doi_r"],
                "title_r": raw["title_r"],
                "abstract_r": raw.get("abstract_text") or "",
                "year_r": raw.get("mo_year") or "",
                "authors_r": "", "journal_r": "", "url_r": "",
                "openalex_id_r": raw.get("openalex_id_r") or "",
                "paper_type": record_type if label != "unclear" else "needs_review",
                "filter_method": "screen_offline",
                "filter_confidence": "high" if confident else "medium",
                # SCREEN_COLS, in the formats `_screen_from_row()` parses.
                "screen_verdict": "proceed",
                "screen_record_type": "" if label == "unclear" else record_type,
                "screen_categories": raw.get("screen_categories") or "",
                "screen_votes": (f"{raw['screen_votes'].split('=')[0]}={label}"
                                 f"/{'confident' if confident else 'unconfident'}"),
                "screen_evidence": raw.get("screen_evidence") or "",
                "screen_reasoning": raw.get("screen_reasoning") or "",
            })
    return rows


def _abstracts(rows: list[dict], causes: Path = HERE / "not_in_pool_causes.csv") -> None:
    """Fill `openalex_id_r` and `abstract_r` — the same text the offline screen read.

    The OpenAlex id has to come from the causes file: `screened.csv` does not carry
    it, and without it `text_for()` cannot build the `oa:<url>` key the OpenAlex
    abstract phase wrote under, so every OpenAlex-sourced abstract reads as absent.
    That silently halved the population on the first run of this (161 of 214, against
    203 the screen had read an abstract for).
    """
    from analysis.mo_observatory.screen_offline import text_for
    from shared.utils import clean_doi

    oa_of: dict[str, str] = {}
    if causes.exists():
        with causes.open(newline="", encoding="utf-8-sig") as handle:
            for raw in csv.DictReader(handle):
                oa_of[clean_doi(raw["doi_clean"] or raw["doi_r"])] = raw.get("oa_id") or ""
    for row in rows:
        row["openalex_id_r"] = row.get("openalex_id_r") or oa_of.get(row["doi_r"], "")
        text, _source = text_for(row["doi_r"], row["openalex_id_r"])
        row["abstract_r"] = text


def extract_one(row: dict, no_llm: bool, no_pdf: bool) -> list[dict]:
    """One work through Stage 3's own per-row pipeline."""
    from extract.run_extract import _process_row

    observed: dict = {}
    try:
        return _process_row(pd.Series(row), row["doi_r"], no_llm=no_llm,
                            no_pdf=no_pdf, no_reproductions=False,
                            resolved_only=False, observed=observed)
    except Exception as exc:                     # noqa: BLE001 — a throwaway harness
        return [{"doi_r": row["doi_r"], "title_r": row["title_r"],
                 "link_method": "harness_error", "link_evidence": f"{type(exc).__name__}: {exc}"}]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--no-llm", action="store_true",
                        help="deterministic rungs only — no model call")
    parser.add_argument("--no-pdf", action="store_true",
                        help="skip the full-text rung (no PDF acquisition)")
    parser.add_argument("--include-unclear", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--screened", default=str(SCREENED))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)

    rows = worklist(Path(args.screened), args.include_unclear)
    _abstracts(rows)
    todo = rows[:args.limit] if args.limit else rows

    import os
    needs_openai = provider_for(LINKING_MODEL) == "openai" or provider_for(OUTCOME_MODEL) == "openai"
    have_openai = bool(os.getenv("OPENAI_API_KEY"))
    print(f"settled-proceed works to extract : {len(rows):5,}")
    print(f"  with an abstract to read       : {sum(1 for r in rows if r['abstract_r']):5,}")
    print(f"\nlinking {LINKING_MODEL} -> {provider_for(LINKING_MODEL)}"
          f" · outcome {OUTCOME_MODEL} -> {provider_for(OUTCOME_MODEL)}")
    print(f"OPENAI_API_KEY: {'set' if have_openai else 'MISSING'}")
    if needs_openai and not have_openai and not args.no_llm:
        print("\nThe LLM rungs cannot run: both models route to OpenAI direct and there\n"
              "is no fallback by design. Use --no-llm for the deterministic rungs, or\n"
              "set OPENAI_API_KEY.")
        if args.run:
            return 2

    if not args.run:
        print(f"\nDry run. --run extracts {len(todo):,} works"
              f"{' (deterministic rungs only)' if args.no_llm else ''}.")
        return 0

    print(f"\nExtracting {len(todo):,} works"
          f"{' — deterministic rungs only' if args.no_llm else ''}…")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(
            lambda r: extract_one(r, args.no_llm, args.no_pdf), todo))

    out = [r for rows_ in results for r in rows_]
    # --no-llm falls back to `_keyword_scan` for the outcome (code_outcome.py:24),
    # which reads a replication phrase out of the abstract and codes on it — at
    # `outcome_confidence: high`, from sentences like "We consider young children's
    # construals of biological phenomena". That is a stand-in for a model, not a
    # judgement, so it is moved out of `outcome` rather than shipped as one.
    for r in out:
        if str(r.get("outcome_llm_model") or "") == "keyword":
            r["outcome_keyword_guess"] = r.get("outcome") or ""
            r["outcome"] = ""
            r["outcome_confidence"] = ""
            r["outcome_phrase"] = ""
    path = Path(args.out)
    cols = list(dict.fromkeys(list(EXTRACTED_COLS)
                              + ["link_method", "link_evidence", "outcome_keyword_guess"]))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out)

    methods: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for r in out:
        methods[str(r.get("link_method") or "(none)")] = methods.get(str(r.get("link_method") or "(none)"), 0) + 1
        outcomes[str(r.get("outcome") or "(blank)")] = outcomes.get(str(r.get("outcome") or "(blank)"), 0) + 1
    print(f"\nwrote {path} — {len(out):,} row(s) for {len(todo):,} work(s)")
    print("  link_method:")
    for k, n in sorted(methods.items(), key=lambda kv: -kv[1]):
        print(f"    {k:34s} {n:5,}")
    print("  outcome:")
    for k, n in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        print(f"    {k:34s} {n:5,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
