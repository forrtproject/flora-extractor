"""Measure issue #186's Shape 1 check over the settled keyed links on disk.

`confirm_keyed_original` asks, cold, whether the record an accepted @key resolved to
is plausibly the paper the study names as its target. Before it is wired into the
ladder, this script replays it over every LLM-accepted keyed link ROW in the latest
result payloads of the four evaluation batches plus the 2026-08-08 live
re-extraction — per row, not per settled work, because that is the scope the shipped
check runs at (`_confirm_keyed_row` sees each finished row) — and reports the flags
against the adjudicated labels. What is being measured is the false-positive rate
over the known-correct links and whether the one known wrong keyed link — work
3124119366, linked to the gasoline paper — is caught. This is adjudication against
Crossref, not the issue's "precision on human-confirmed rows"; that ask stays open.

The inputs are rebuilt from the stored rows exactly as the inline call would build
them (title_r, abstract_r, the evidence quote, the record off the row), so the prompt
measured is the prompt shipped.

    python -m analysis.stage3_eval.keyed_confirm_eval [--dry-run]

Cost: one short LINKING_MODEL call per link (~300 links, no OpenAlex calls at all);
answers are cached, so a re-run is free.
"""

import argparse
import json
import re
from pathlib import Path

import shared.config  # noqa: F401  — ClaimsClient raises ClaimsNotConfigured without it
from analysis.stage3_eval.read_batch import batch_results
from extract.run_extract import evidence_quote
from extract.tier import TIER_EXTRACT
from filter.engine.claims import ClaimsClient
from shared.llm_client import confirm_keyed_original

HERE = Path(__file__).parent

BATCHES = [
    ("eval-dev-16", "labels-dev-16.json"),
    ("eval-holdout", "labels-holdout.json"),
    ("eval-holdout2", "labels-holdout2.json"),
    ("eval-holdout3", "labels-holdout3.json"),
]

# The link methods the check is scoped to: an LLM accepted a keyed record. Rule
# resolutions get the standalone coder's target_check, and the provisional search
# picks were already adjudicated by pick_author_year_original.
KEYED_METHODS = {"llm_fulltext", "llm_references", "llm_cited_candidates"}

def live_rows(work_ids: set[int]) -> list[dict]:
    """Latest live result row per requested work, shaped like batch_results'."""
    client = ClaimsClient()
    rows = [r for r in client.verdicts(TIER_EXTRACT, with_payload=True)
            if int(r.get("work_id") or 0) in work_ids
            and (r.get("payload") or {}).get("kind") == "result"]
    latest: dict[int, dict] = {}
    for row in sorted(rows, key=lambda r: str(r.get("created_at") or "")):
        latest[int(row["work_id"])] = row
    return [latest[k] for k in sorted(latest)]


def keyed_links(row: dict) -> list[dict]:
    """The checkable links of one stored result row."""
    payload = row.get("payload") or {}
    src = payload.get("input") or {}
    out = []
    for target in payload.get("targets") or []:
        if str(target.get("link_method") or "") not in KEYED_METHODS:
            continue
        if not (target.get("doi_o") or target.get("oa_work_id_o")):
            continue
        out.append({
            "work_id": int(row.get("work_id") or 0),
            "doi_r": str(payload.get("doi_r") or ""),
            "title_r": str(src.get("title_r") or ""),
            "abstract_r": str(src.get("abstract_r") or ""),
            "quote": evidence_quote(target.get("link_evidence")),
            "record": {"doi": str(target.get("doi_o") or ""),
                       "title": str(target.get("title_o") or ""),
                       "first_author": str(target.get("authors_o") or ""),
                       "year": str(target.get("year_o") or ""),
                       "openalex_id": str(target.get("oa_work_id_o") or "")},
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="count the eligible links, call nothing")
    args = ap.parse_args()

    groups: list[tuple[str, list[dict], dict]] = []
    for batch, labels_file in BATCHES:
        labels = json.load(open(HERE / labels_file)).get("labels", {})
        groups.append((batch, batch_results(batch), labels))
    live_ids = {int(x) for x in
                re.split(r"[\s,]+", (HERE / "redo-pre-fix-29.txt").read_text())
                if x.strip()}
    groups.append(("live-pre-fix-27", live_rows(live_ids), {}))

    flags, errors, checked = [], [], 0
    for name, rows, labels in groups:
        links = [l for row in rows for l in keyed_links(row)]
        print(f"{name}: {len(links)} keyed link(s) over {len(rows)} work(s)")
        if args.dry_run:
            continue
        for link in links:
            v = confirm_keyed_original(link["doi_r"], link["title_r"],
                                       link["abstract_r"], link["quote"],
                                       link["record"])
            if v["plausible"] is None:
                errors.append((name, link["work_id"], v["llm_error"]))
                continue
            checked += 1
            if not v["plausible"]:
                label = (labels.get(str(link["work_id"])) or {}).get("label", "live")
                flags.append((name, link["work_id"], label, v["confident"],
                              link["record"]["doi"], v["reasoning"]))

    if args.dry_run:
        return
    print(f"\nchecked: {checked}   flagged not-plausible: {len(flags)}   "
          f"no answer: {len(errors)}")
    for name, wid, label, confident, doi, why in flags:
        print(f"  FLAG [{name}] work {wid} ({label}) "
              f"{'confident' if confident else 'unconfident'} doi_o={doi}\n"
              f"       {why}")
    for name, wid, err in errors:
        print(f"  ERROR [{name}] work {wid}: {err}")


if __name__ == "__main__":
    main()
