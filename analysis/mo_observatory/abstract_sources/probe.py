"""Probe: do Europe PMC / Semantic Scholar hold abstracts for Observatory works
the survivor pool never admitted because OpenAlex had no abstract (cause F)?

100 works sampled from `not_in_pool_causes.csv` (cause F, not already ours,
random_state=42). One EPMC searchPOST and one S2 batch call. Writes sample.csv.
"""
import json
import re
from pathlib import Path

import pandas as pd

from filter.phrase_detection import REPLICATION_STEM_PATTERN
from search.fetch_abstracts import _fetch_epmc_batch, _fetch_s2_batch
from shared.config import S2_API_KEY

HERE = Path(__file__).parent
causes = pd.read_csv(HERE.parent / "not_in_pool_causes.csv")
f = causes[(causes.cause == "F_gate_miss_no_abstract") & ~causes.ours_already]
sample = f.sample(100, random_state=42).copy()
dois = [d.lower() for d in sample.doi_repaired]

# Responses cached beside the script: a re-run reads them, never re-queries.
CACHE = HERE / "responses.json"
if CACHE.exists():
    cached = json.loads(CACHE.read_text())
    epmc, s2 = cached["epmc"], cached["s2"]
else:
    epmc = _fetch_epmc_batch(dois)
    s2 = _fetch_s2_batch(dois, S2_API_KEY)
    if epmc is None or s2 is None:
        raise SystemExit(f"a source failed (epmc={epmc is not None}, s2={s2 is not None}); nothing cached")
    CACHE.write_text(json.dumps({"epmc": epmc, "s2": s2}))
stem = re.compile(REPLICATION_STEM_PATTERN, re.I) if isinstance(REPLICATION_STEM_PATTERN, str) else REPLICATION_STEM_PATTERN
sample["epmc_abstract"] = [epmc.get(d) for d in dois]
sample["s2_abstract"] = [s2.get(d) for d in dois]
for src in ("epmc", "s2"):
    sample[f"{src}_stem"] = sample[f"{src}_abstract"].map(lambda a: isinstance(a, str) and bool(stem.search(a)))
cols = ["doi_repaired", "oa_id", "replication_title", "replication_year", "discipline",
        "epmc_abstract", "s2_abstract", "epmc_stem", "s2_stem"]
sample[cols].to_csv(HERE / "sample.csv", index=False, encoding="utf-8-sig")
print("epmc answered:", bool(epmc), "s2 answered:", bool(s2))
e, s = sample.epmc_abstract.notna(), sample.s2_abstract.notna()
print(f"EPMC {e.sum()}  S2 {s.sum()}  either {(e|s).sum()}  both {(e&s).sum()}")
print(f"stem fires: EPMC {sample.epmc_stem.sum()}  S2 {sample.s2_stem.sum()}  either {(sample.epmc_stem|sample.s2_stem).sum()}")
print(sample.assign(any_abs=e|s).groupby("discipline").any_abs.agg(["sum", "count"]))
