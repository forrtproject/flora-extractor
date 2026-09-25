"""Route the recall_pregate populations (165 gap works, 1,132 new PubMed rows) under
each option, in memory, with the c048 bundle (git HEAD snapshot in spec_head/).

Same rows as `analysis/recall_pregate/route_offline.py`; this adds the proposed
`replication-claim-general` and routes each option with and without
`curated-observatory`. Writes out/pubmed_gap_routed.parquet (one row per work and
option) and out/pubmed_rows.parquet (the text the screen would read).

    .venv/bin/python -m analysis.recall_options_2026-09.route_pubmed_gap
"""

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pyarrow as pa

from analysis.recall_pregate.route_offline import gap_rows, pubmed_rows
from filter.engine.route import route_batch
from filter.engine.spec import load_specs
from search.snapshot_scan import _POOL_SCHEMA
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
SPEC_HEAD = HERE / "spec_head" / "filter" / "spec"
PROPOSED = HERE.parent / "stage2_rules_2026-09" / "proposed_specs"
G, T, R, CUR = ("replication-claim-general", "replication-claim-text",
                "replication-claim-residual", "curated-observatory")
OPTIONS = {"A": set(), "B": {G}, "C": {T}, "D": {T, R}}


def specs_for(promote: set, curated: bool) -> list:
    base = load_specs(SPEC_HEAD) + [s for s in load_specs(PROPOSED) if s.id == G]
    out = []
    for s in base:
        if s.id == CUR and not curated:
            continue
        if s.id in (G, T, R):
            s = replace(s, shadow=s.id not in promote)
        out.append(s)
    return out


def main() -> None:
    frames = []
    pops = {"gap": gap_rows(), "pubmed": pubmed_rows()}
    for pop, rows in pops.items():
        batch = pa.Table.from_pylist(rows, schema=_POOL_SCHEMA).to_batches()[0]
        for opt, promote in OPTIONS.items():
            for curated in (False, True):
                r = route_batch(specs_for(promote, curated), batch).to_pandas()
                r["pop"], r["option"], r["curated"] = pop, opt, curated
                frames.append(r[["work_id", "pile", "pending_reason", "rule_id",
                                 "pop", "option", "curated"]])
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(HERE / "out" / "pubmed_gap_routed.parquet", index=False)
    txt = pd.DataFrame([{"pop": pop, "raw_id": r["id"], "doi": clean_doi(r["doi"] or ""),
                         "title": r["display_name"] or "", "abstract": r["abstract_text"] or ""}
                        for pop, rows in pops.items() for r in rows])
    txt.to_parquet(HERE / "out" / "pubmed_rows.parquet", index=False)
    adm = out[out.pile == "screen_expensive"]
    print(adm.groupby(["pop", "curated", "option"]).work_id.nunique().unstack().to_string())
    print(out.groupby(["pop", "curated", "option"]).work_id.nunique().unstack().to_string())


if __name__ == "__main__":
    main()
