"""Proceed rates per option from the screened samples, with 95% CIs, and the
proceed rows to hand-check (out/handcheck_<stratum>.txt, 20 per stratum, seeded).

Estimation: B-family options read the B sample restricted to the option's works;
C-family read the C sample; D = C cell (C sample) + residual cell (R sample),
weighted by cell size; C+B = C cell + (B minus C) cell (B-sample works outside C).
Single-cell CIs are Wilson; stratified CIs are normal on the weighted variance.

    .venv/bin/python -m analysis.recall_options_2026-09.summarise
"""

import math
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SHL = {"Social", "Health", "Life"}


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def main() -> None:
    sets = pd.read_parquet(OUT / "option_sets.parquet").set_index("work_id")
    s = pd.read_parquet(OUT / "sample.parquet")
    r = pd.read_parquet(OUT / "screened.parquet")
    s = s.merge(r, on="work_id", how="left")
    s["proceed"] = s.verdict == "proceed"
    print("incomplete screens:", int((s.verdict.fillna("") == "").sum()))
    print(s.groupby("stratum").agg(n=("proceed", "size"), proceed=("proceed", "sum")).to_string())
    print(s[s.proceed].groupby(["stratum", "record_type"]).size().unstack(fill_value=0).to_string())

    def cell(stratum: str, members: "set") -> tuple[float, int, int]:
        x = s[(s.stratum == stratum) & s.work_id.isin(members)]
        return x.proceed.mean() if len(x) else float("nan"), int(x.proceed.sum()), len(x)

    new = {k: set(sets.index[sets[k]]) for k in sets.columns}
    C, R = new["C"], new["D"] - new["C"]
    plans = {
        "B": [("B", new["B"])], "B_abs": [("B", new["B_abs"])], "B_SHL": [("B", new["B_SHL"])],
        "C": [("C", C)], "C_SHL": [("C", new["C_SHL"])], "C_SH": [("C", new["C_SH"])],
        "D": [("C", C), ("R", R)],
        "D_SHL": [("C", new["D_SHL"] & C), ("R", new["D_SHL"] & R)],
        "C+B": [("C", C), ("B", new["C+B"] - C)],
        "C_SHL+B": [("C", new["C_SHL+B"] & C), ("B", new["C_SHL+B"] - C)],
    }
    rows = []
    for opt, cells in plans.items():
        N = len(new[opt])
        est, var, k_all, n_all = 0.0, 0.0, 0, 0
        for stratum, members in cells:
            p, k, n = cell(stratum, members)
            w = len(members) / N
            est += w * p
            var += w * w * p * (1 - p) / max(n, 1)
            k_all, n_all = k_all + k, n_all + n
        if len(cells) == 1:
            lo, hi = wilson(k_all, n_all)
        else:
            lo, hi = est - 1.96 * math.sqrt(var), est + 1.96 * math.sqrt(var)
        rows.append({"option": opt, "new": N, "sample_n": n_all, "proceed_k": k_all,
                     "proceed_rate": round(est, 3), "ci_lo": round(max(lo, 0), 3),
                     "ci_hi": round(min(hi, 1), 3), "exp_proceeds": round(est * N)})
    pm = s[s.stratum == "PM"]
    lo, hi = wilson(int(pm.proceed.sum()), len(pm))
    rows.append({"option": "PubMed-admits", "new": len(pm), "sample_n": len(pm),
                 "proceed_k": int(pm.proceed.sum()), "proceed_rate": round(pm.proceed.mean(), 3),
                 "ci_lo": round(lo, 3), "ci_hi": round(hi, 3), "exp_proceeds": None})
    tab = pd.DataFrame(rows)
    print(tab.to_string(index=False))
    tab.to_csv(OUT / "proceed_rates.csv", index=False)
    # the PubMed rows per option
    pg = pd.read_parquet(OUT / "pubmed_gap_routed.parquet")
    pg = pg[(pg["pop"] == "pubmed") & ~pg.curated & (pg.pile == "screen_expensive")]
    for opt in "BCD":
        ids = set(pg.work_id[pg.option == opt])
        x = pm[pm.work_id.isin(ids)]
        print(f"PubMed admits under {opt}: {len(ids)}, proceed {int(x.proceed.sum())}/{len(x)}")

    # domain of proceeds in B and C samples
    for st in ("B", "C", "R"):
        x = s[s.stratum == st]
        print(st, x.groupby("domain").proceed.agg(["size", "sum"]).to_dict("index"))

    rng = np.random.default_rng(7)
    for st in ("B", "C", "R", "PM"):
        x = s[(s.stratum == st) & s.proceed]
        x = x.iloc[rng.permutation(len(x))[:20]]
        with open(OUT / f"handcheck_{st}.txt", "w") as fh:
            for row in x.itertuples():
                fh.write(f"### {row.work_id} [{row.record_type}] dom={row.domain} doi={row.doi}\n"
                         f"T: {row.title}\nA: {(row.abstract or '')[:900]}\n\n")


if __name__ == "__main__":
    main()
