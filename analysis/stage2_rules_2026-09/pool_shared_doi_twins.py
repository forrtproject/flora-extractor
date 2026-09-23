"""Pool-wide, registry-free estimate of issue #210's twins: DOIs held by several pool
records whose titles disagree. 27 of the 30 known twins have such a sibling (the real
paper), so this reaches most of the class without one API call. It cannot say WHICH
record is the twin — that needs the registry (`doi_twins_audit.py`) — so it counts
DOI groups, and the records in them that are admitted on c048ab6483d3.

    PYTHONPATH=. .venv/bin/python analysis/stage2_rules_2026-09/pool_shared_doi_twins.py
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from twin_compare import overlap, tokens  # noqa: E402

from filter.engine.workids import work_id  # noqa: E402

SCRATCH = HERE.parents[1] / "cache" / "stage2_rules_2026-09"

pool = pd.read_parquet(SCRATCH / "pool_dois.parquet")
pool["title_"] = pool.display_name.fillna(pool.title).fillna("")
multi = pool[pool.duplicated("cdoi", keep=False)].copy()
multi["raw_work_id"] = multi.id.map(work_id)
admitted = set(pd.read_parquet(SCRATCH / "admitted_c048.parquet").raw_work_id)
rows = []
for doi, g in multi.groupby("cdoi"):
    toks = [tokens(t) for t in g.title_]
    ids = list(g.raw_work_id)
    for i, a in enumerate(toks):
        # A record that shares NO title with any sibling is the odd one out.
        best = max((overlap(a, b) for j, b in enumerate(toks) if j != i), default=float("nan"))
        if best == best and best <= 0.5:
            rows.append({"doi": doi, "work_id": ids[i], "title": g.title_.iloc[i],
                         "year": g.publication_year.iloc[i], "best_sibling_overlap": round(best, 3),
                         "n_records": len(g), "admitted_c048": ids[i] in admitted})
out = pd.DataFrame(rows)
print("pool rows with a DOI:", len(pool), " DOIs on >1 record:", multi.cdoi.nunique(),
      " records on them:", len(multi))
print("records sharing no title with any sibling:", len(out), " in DOI groups:", out.doi.nunique(),
      " admitted on c048:", int(out.admitted_c048.sum()),
      " DOI groups with an admitted record:", out[out.admitted_c048].doi.nunique())
out.to_csv(HERE / "pool_shared_doi_mismatch.csv", index=False, encoding="utf-8-sig")
