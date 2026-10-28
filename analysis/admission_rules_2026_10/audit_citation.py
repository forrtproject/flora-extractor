"""Compare citation syntax over the pool without API calls or routing writes.

    .venv/bin/python -m analysis.admission_rules_2026_10.audit_citation

Counts exclude works admitted by the baseline on any sibling row. Routing additions
also respect higher-priority discards. No FLoRA/export/validation skip list is applied:
these are routing counts, not the historical available-work denominators.
"""
import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa

from filter.engine.backends import BatchContext, eval_spec_batch
from filter.engine.overlay import overlay_manifest_hash
from filter.engine.pool_reader import iter_pool_batches
from filter.engine.route import ADMITTED_PILES, route_batch
from filter.engine.spec import MatchBlock, bundle_hash, load_specs
from filter.engine.workids import load_aliases, resolve, work_id
from search.snapshot_scan import pool_fingerprint
from shared.config import OVERLAY_DIR, SNAPSHOT_POOL_DIR

HERE = Path(__file__).resolve().parent
SPEC_DIR = HERE.parents[1] / "filter" / "spec"
NEW_RULES = {"replication-claim-extend-prior", "replication-claim-cited-abstract",
             "replication-claim-failure-prior"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, default=SNAPSHOT_POOL_DIR)
    parser.add_argument("--overlay", type=Path, default=OVERLAY_DIR)
    args = parser.parse_args()
    specs = load_specs(SPEC_DIR)
    baseline = sorted((s for s in specs if not s.shadow and s.id not in NEW_RULES),
                      key=lambda s: -s.precedence)
    cited = next(s for s in specs if s.id == "replication-claim-cited-abstract")
    patterns = json.loads((HERE / "citation_review_patterns.json").read_text())
    comparisons = {name: replace(cited, id=name, match=replace(
        cited.match, abstract_regex=None,
        any_of=tuple(MatchBlock(abstract_regex=p) for p in arms)))
        for name, arms in patterns.items()}
    comparisons["revised"] = cited
    comparisons.update({s.id: s for s in specs if s.id in NEW_RULES - {cited.id}})
    # All historical and revised citation forms share this claim prefix. Gate once
    # before checking their tails, rather than scanning millions of unrelated
    # abstracts four times. This is an equivalent query optimisation, not a rule.
    prefix = patterns["measured_original"][0].split(r"[^.;\n]{0,120}", 1)[0]
    if not all(p.startswith(prefix) for arms in patterns.values() for p in arms) \
            or not cited.match.abstract_regex.startswith(prefix):
        raise RuntimeError("Citation claim prefixes differ; review the audit gate")
    gate = replace(cited, match=MatchBlock(abstract_regex=prefix))
    citation_names = set(patterns) | {"revised"}
    admission_specs = [s for s in baseline if s.pile in ADMITTED_PILES]
    aliases = load_aliases(SPEC_DIR / "aliases.json")
    members = {name: set() for name in comparisons}
    routable = {name: set() for name in comparisons}
    admitted, parts, total = set(), [], 0
    for number, batch in enumerate(iter_pool_batches(args.pool, args.overlay, aliases=aliases), 1):
        context = BatchContext(batch)
        citation_indices = np.flatnonzero(
            eval_spec_batch(gate, batch, context).to_numpy(zero_copy_only=False))
        citation_batch = batch.take(pa.array(citation_indices, type=pa.int64()))
        citation_context = BatchContext(citation_batch) if len(citation_indices) else None
        masks = {}
        for name, spec in comparisons.items():
            if name in citation_names:
                mask = np.zeros(batch.num_rows, dtype=bool)
                if len(citation_indices):
                    mask[citation_indices] = eval_spec_batch(
                        spec, citation_batch, citation_context).to_numpy(zero_copy_only=False)
                masks[name] = mask
            else:
                masks[name] = eval_spec_batch(spec, batch, context).to_numpy(zero_copy_only=False)
        candidate = np.logical_or.reduce(list(masks.values()))
        potential_admission = np.zeros(batch.num_rows, dtype=bool)
        for spec in admission_specs:
            potential_admission |= eval_spec_batch(spec, batch, context).to_numpy(zero_copy_only=False)
        selected = np.flatnonzero(candidate | potential_admission)
        # Use the actual router, including no-text downgrades and title-only OSF
        # exceptions. A matching screening rule alone is not yet an admission.
        routed = route_batch(baseline, batch.take(pa.array(selected, type=pa.int64())), aliases)
        baseline_admit = np.array([p in ADMITTED_PILES for p in routed.column("pile").to_pylist()],
                                  dtype=bool)
        precedence = routed.column("precedence").to_numpy(zero_copy_only=False)
        ids = batch.column("id").take(pa.array(selected)).to_pylist()
        works = np.array([resolve(work_id(i), aliases) for i in ids], dtype=np.int64)
        admitted.update(works[baseline_admit].tolist())
        for name, spec in comparisons.items():
            members[name].update(works[masks[name][selected]].tolist())
            routable[name].update(works[masks[name][selected] & (precedence < spec.precedence)].tolist())
        take = np.flatnonzero(candidate)
        if len(take):
            rows = batch.take(pa.array(take)).select(
                ["id", "doi", "display_name", "title", "abstract_text"]).to_pandas()
            rows["work_id"] = [resolve(work_id(i), aliases) for i in rows.id]
            rows["title"] = rows.display_name.fillna(rows.title).fillna("")
            for name, mask in masks.items():
                rows[name] = mask[take]
            parts.append(rows.drop(columns=["display_name"]))
        total += batch.num_rows
        if number % 250 == 0:
            print(f"Compared {total:,} pool rows", flush=True)
    if not parts:
        raise RuntimeError("No candidate rows; check the pool and overlay paths")
    raw = {name: ids - admitted for name, ids in members.items()}
    routes = {name: ids - admitted for name, ids in routable.items()}
    old_only = raw["measured_original"] - raw["citation_only"]
    removed = raw["previous_union"] - raw["revised"]
    added = raw["revised"] - raw["previous_union"]
    changed = old_only | removed | added
    rows = pd.concat(parts, ignore_index=True)
    cases = rows[rows.work_id.isin(changed)].drop_duplicates("work_id").copy()
    for name, ids in raw.items():
        cases[name + "_work"] = cases.work_id.isin(ids)
    cases["old_only"] = cases.work_id.isin(old_only)
    cases["removed_from_union"] = cases.work_id.isin(removed)
    cases["added_to_union"] = cases.work_id.isin(added)
    labels = pd.read_csv(HERE / "screened_cases.csv", keep_default_na=False)
    cases = cases.merge(labels[["work_id", "label", "actionable", "rule"]],
                        on="work_id", how="left", validate="one_to_one")
    cases.sort_values("work_id").to_csv(HERE / "citation_review_cases.csv", index=False,
                                       encoding="utf-8-sig")
    coverage = []
    for rule in NEW_RULES:
        labelled = labels[labels.rule == rule]
        revised_rule = "revised" if rule == cited.id else rule
        kept = labelled[labelled.work_id.isin(routes[revised_rule])]
        coverage.append({"rule": rule, "labelled": len(labelled), "retained": len(kept),
                         "actionable": int(labelled.actionable.sum()),
                         "actionable_retained": int(kept.actionable.sum())})
    extension = routes["replication-claim-extend-prior"]
    citation = routes["revised"] - extension
    failure = routes["replication-claim-failure-prior"] - extension - citation
    summary = {
        "pool_rows": total, "pool_fingerprint": pool_fingerprint(args.pool),
        "overlay_hash": overlay_manifest_hash(args.overlay),
        "reviewed_bundle_hash": bundle_hash(SPEC_DIR),
        "baseline_admitted_works": len(admitted),
        "reach_without_existing_record_skips": {name: len(ids) for name, ids in raw.items()},
        "routing_additions_without_existing_record_skips": {
            "extension": len(extension), "citation_after_extension": len(citation),
            "failure_after_both": len(failure), "union": len(extension | citation | failure)},
        "old_only": len(old_only), "old_only_retained": len(old_only & raw["revised"]),
        "removed_from_previous_union": len(removed), "added_to_previous_union": len(added),
        "label_coverage": coverage,
        "precision": None,
        "precision_note": "Retained labels describe an already selected subset; no fresh precision estimate.",
    }
    (HERE / "citation_review_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
