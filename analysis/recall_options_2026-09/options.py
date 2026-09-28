"""Step 7 decision table: entries, arm and domain breakdown, known-replication yield and
recall for each admission option, offline over release c048ab6483d3's inputs.

Inputs: the masks `stage2_rules_2026-09/route_scratch.py` wrote (evals_proposed.parquet,
c048 bundle + proposed specs, overlay and aliases applied), out/cand_text.parquet
(pool_pass.py), out/pubmed_gap_routed.parquet (route_pubmed_gap.py). Writes
out/option_sets.parquet (work -> admitted/new per option) for screen_sample.py.

    .venv/bin/python -m analysis.recall_options_2026-09.options
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "stage2_rules_2026-09"))
from variants import live_set, load, route  # noqa: E402

from filter.engine.workids import load_aliases, resolve, work_id  # noqa: E402
from shared.utils import clean_doi  # noqa: E402

SCRATCH = ROOT / "cache" / "stage2_rules_2026-09"
OUT = HERE / "out"
G, T, R, CUR = ("replication-claim-general", "replication-claim-text",
                "replication-claim-residual", "curated-observatory")
SHL = {"Social", "Health", "Life"}
SH = {"Social", "Health"}


def main() -> None:
    df, meta = load(SCRATCH / "evals_proposed.parquet")
    ct = pd.read_parquet(OUT / "cand_text.parquet")
    df = df.merge(ct[["work_id", "domain"]], on="work_id", how="left")
    df["domain"] = df["domain"].fillna("")
    has_abs = ~df.abstract_empty
    shl, sh = df.domain.isin(SHL), df.domain.isin(SH)

    obs = pd.read_csv(ROOT / "analysis/mo_observatory/replications_database_2026_09_04_184008.csv",
                      dtype=str, usecols=["replication_url"])
    obs_d = {clean_doi(u) for u in obs.replication_url.dropna()} - {""}
    flora = pd.read_csv(ROOT / "data/flora.csv", dtype=str)
    flora_d = {clean_doi(u) for u in flora.doi_r.dropna()} - {""}
    ex = pd.read_csv(ROOT / "data/extracted.csv", dtype=str, usecols=["openalex_id_r", "doi_r"])
    ex_w = set(ex.openalex_id_r.dropna().map(work_id))
    df["obs"] = df.doi.isin(obs_d) & (df.doi != "")
    df["flora"] = df.doi.isin(flora_d) & (df.doi != "")
    df["extracted"] = df.raw_work_id.isin(ex_w)
    df["known"] = df.obs | df.flora | df.extracted
    sd = pd.read_parquet(SCRATCH / "screen_decisions_allgen.parquet")
    df["screen_prev"] = df.work_id.map(dict(zip(sd.work_id, sd.outcome))).fillna("")

    live = live_set(meta)
    base = df.copy()

    def variant(promote: set, restrict: "dict | None" = None, drop: set = frozenset()):
        d = base
        if restrict:
            d = base.copy()
            for sid, cond in restrict.items():
                d["m:" + sid] = d["m:" + sid] & cond
        p, _r = route(d, meta, (live - set(drop)) | promote)
        return (p == "screen_expensive").to_numpy()

    opts = {
        "A": variant(set()),
        "B": variant({G}),
        "B_abs": variant({G}, {G: has_abs}),
        "B_SHL": variant({G}, {G: shl}),
        "C": variant({T}),
        "C_SHL": variant({T}, {T: shl}),
        "C_SH": variant({T}, {T: sh}),
        "D": variant({T, R}),
        "D_SHL": variant({T, R}, {T: shl, R: shl}),
        "C+B": variant({T, G}),
        "C_SHL+B": variant({T, G}, {T: shl}),
    }
    today = opts["A"]
    sets = pd.DataFrame({"work_id": df.work_id})
    rows = []
    for k, adm in opts.items():
        new = adm & ~today
        sets[k] = new
        n = int(new.sum())
        rows.append({"option": k, "admitted": int(adm.sum()), "new": n,
                     "new_no_abs": int((new & ~has_abs).sum()),
                     "known_new": int((new & df.known).sum()),
                     "obs": int((new & df.obs).sum()), "flora": int((new & df.flora).sum()),
                     "extracted": int((new & df.extracted).sum()),
                     "per_1k": round(1000 * (new & df.known).sum() / max(n, 1), 1),
                     "prev_screened": int((new & (df.screen_prev != "")).sum()),
                     "prev_proceed": int((new & (df.screen_prev == "proceed")).sum())})
        for dom in ("Social", "Health", "Life", "Physical", "Other", ""):
            rows[-1]["dom_" + (dom or "none")] = int((new & (df.domain == dom)).sum())
    tab = pd.DataFrame(rows).set_index("option")
    pd.set_option("display.width", 250)
    print("== entries (new = beyond today's pile)\n", tab.to_string())
    tab.to_csv(OUT / "options_table.csv")

    # per-arm, among each rule's new works (non-exclusive; and 'only this arm')
    ct = ct.set_index("work_id")
    for k, prefix in (("B", "arm-g"), ("C", "arm-t"), ("D", "arm-")):
        new_ids = df.work_id[sets[k]]
        sub = ct.reindex(new_ids)
        if k == "D":
            sub = ct.reindex(df.work_id[sets["D"] & ~sets["C"]])
            prefix = "arm-r"
        cols = [c for c in ct.columns if c.startswith(prefix)]
        m = sub[cols].fillna(False).astype(bool)
        kn = df.set_index("work_id").reindex(sub.index).known
        only = m.sum(axis=1) == 1
        print(f"\n== arms, {k}{' (residual increment over C)' if k == 'D' else ''}: "
              f"{len(sub)} works")
        for c in cols:
            print(f"  {c:8s} any {int(m[c].sum()):6d}  only {int((m[c] & only).sum()):6d}  "
                  f"known {int((m[c] & kn).sum()):4d}  "
                  f"per1k {1000 * (m[c] & kn).sum() / max(m[c].sum(), 1):5.1f}")

    # recall 1: the 1,345 works only curated-observatory admits
    lost = today & ~variant(set(), drop={CUR})
    print(f"\n== curated-only works: {int(lost.sum())}")
    for k, promote in (("A", set()), ("B", {G}), ("C", {T}), ("D", {T, R}),
                       ("C_SHL", None), ("B_abs", None)):
        if promote is None:
            restrict = {"C_SHL": {T: shl}, "B_abs": {G: has_abs}}[k]
            promote = {"C_SHL": {T}, "B_abs": {G}}[k]
        else:
            restrict = None
        adm = variant(promote, restrict, drop={CUR})
        rec = lost & adm
        print(f"  {k:6s} admits {int(rec.sum()):5d}  (screen-proceed "
              f"{int((rec & (df.screen_prev == 'proceed')).sum())}, extracted "
              f"{int((rec & df.extracted).sum())})")
    print(f"  of the lost: screen proceed {int((lost & (df.screen_prev == 'proceed')).sum())}, "
          f"extracted {int((lost & df.extracted).sum())}")

    # recall 2: the 165 gap works and the 1,132 PubMed rows
    pg = pd.read_parquet(OUT / "pubmed_gap_routed.parquet")
    a = pg[pg.pile == "screen_expensive"].groupby(["pop", "curated", "option"]).work_id.nunique()
    print("\n== recall_pregate populations admitted\n", a.unstack().to_string())

    # recall 3: FLoRA replications in the pool that today's pile does not admit
    aliases = load_aliases(HERE / "spec_head" / "filter" / "spec" / "aliases.json")
    pdois = pd.read_parquet(SCRATCH / "pool_dois.parquet", columns=["id", "cdoi"])
    pdois = pdois[pdois.cdoi.isin(flora_d)]
    pdois["work_id"] = [resolve(work_id(x), aliases) for x in pdois.id]
    fw = set(pdois.work_id)
    in_df = df.work_id.isin(fw)
    adm_today = set(df.work_id[today])
    not_adm = fw - adm_today
    print(f"\n== FLoRA: {len(flora_d)} distinct doi_r; {len(set(pdois.cdoi))} DOIs / {len(fw)} works "
          f"in the pool; admitted today {len(fw & adm_today)}; not admitted {len(not_adm)} "
          f"(of those {len(not_adm - set(df.work_id))} match no spec at all)")
    p_today, r_today = route(df, meta, live)
    na = in_df & ~today
    print("  today's pile of the not-admitted FLoRA works that match some spec:")
    print("   ", pd.Series(p_today[na]).value_counts().to_dict(),
          "no_text:", int((na & ~has_abs).sum()))
    for k in ("B", "B_abs", "C", "C_SHL", "D", "D_SHL", "C+B"):
        print(f"  {k:6s} newly admits {int((sets[k] & in_df).sum())}")
    sets.to_parquet(OUT / "option_sets.parquet", index=False)


if __name__ == "__main__":
    main()
