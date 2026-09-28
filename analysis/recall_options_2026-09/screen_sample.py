"""Measure each option's precision with the production screen, off the live store.

Seeded random samples of the works each option would ADD to screen_expensive,
screened by calling `shared.llm_client.classify_replication()` directly on the pool
row's title + abstract — the same (clean doi, display_name-or-title, abstract) triple
`filter/engine/tiers.pile_works()` hands `_expensive_judge()`, so each vote lands in
the same per-vote cache a later real screen would replay. No claim, no verdict row,
no release: nothing reaches the store or Postgres.

Strata (disjoint by construction except B/C, which overlap and are sampled
independently):
  B   120 from the new works of `replication-claim-general`
  C   120 from the new works of `replication-claim-text`
  R   120 from what `-residual` adds on top of `-text` (D = C ∪ R)
  PM  every new PubMed-supplement row (recall_pregate probe) any option admits

    OPENAI_DAILY_TOKEN_BUDGET=0 .venv/bin/python -m analysis.recall_options_2026-09.screen_sample [--n 120] [--limit K]
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from shared.llm_client import classify_replication
from shared import token_counter

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SEED = 20260925


def samples(n: int) -> pd.DataFrame:
    sets = pd.read_parquet(OUT / "option_sets.parquet")
    ct = pd.read_parquet(OUT / "cand_text.parquet").set_index("work_id")
    rng = np.random.default_rng(SEED)
    parts = []
    for stratum, mask in (("B", sets.B), ("C", sets.C), ("R", sets.D & ~sets.C)):
        ids = np.sort(sets.work_id[mask].to_numpy())
        pick = rng.choice(ids, size=min(n, len(ids)), replace=False)
        s = ct.loc[pick, ["doi", "title", "abstract", "domain"]].reset_index()
        s["stratum"] = stratum
        parts.append(s)
    pg = pd.read_parquet(OUT / "pubmed_gap_routed.parquet")
    adm = pg[(pg["pop"] == "pubmed") & ~pg.curated & (pg.pile == "screen_expensive")]
    txt = pd.read_parquet(OUT / "pubmed_rows.parquet")
    txt = txt[txt["pop"] == "pubmed"].copy()
    txt["work_id"] = txt.raw_id.str.rsplit("/", n=1).str[-1].str.lstrip("W").astype("int64")
    pm = txt[txt.work_id.isin(set(adm.work_id))].drop_duplicates("work_id")
    pm = pm.assign(stratum="PM", domain="")[["work_id", "doi", "title", "abstract",
                                               "domain", "stratum"]]
    return pd.concat(parts + [pm], ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--limit", type=int, default=None, help="screen only the first K works")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    s = samples(args.n)
    s.to_parquet(OUT / "sample.parquet", index=False)
    uniq = s.drop_duplicates("work_id")
    if args.limit:
        uniq = uniq.head(args.limit)
    print(f"{len(s)} sampled rows, {len(uniq)} works to screen", flush=True)
    token_counter.set_stage("recall_options_screen")
    res = {}
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(classify_replication, r.doi, r.title, r.abstract): r.work_id
                for r in uniq.itertuples()}
        for i, f in enumerate(as_completed(futs), 1):
            out = f.result()
            res[futs[f]] = {
                "verdict": out.get("screen_verdict") or "",
                "record_type": out.get("record_type") or "",
                "classification": out.get("screen_classification") or "",
                "categories": "|".join(out.get("categories") or []),
                "votes": json.dumps([{k: v.get(k) for k in ("model", "classification",
                                                             "confident", "evidence_quote")}
                                     for v in out.get("votes") or []]),
            }
            if i % 25 == 0:
                print(i, flush=True)
    r = pd.DataFrame.from_dict(res, orient="index").rename_axis("work_id").reset_index()
    prev = OUT / "screened.parquet"
    if prev.exists():
        r = pd.concat([pd.read_parquet(prev), r]).drop_duplicates("work_id", keep="last")
    r.to_parquet(prev, index=False)
    token_counter.print_summary()


if __name__ == "__main__":
    main()
