"""Recount the marginal admission evidence from the committed frozen artifacts.

    .venv/bin/python -m analysis.admission_rules_2026_10.summarise
"""
import json
import math
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent


def main() -> None:
    populations = pd.read_csv(HERE / "populations.csv")
    cases = pd.read_csv(HERE / "screened_cases.csv", keep_default_na=False)
    if cases.work_id.duplicated().any():
        raise RuntimeError("Marginal evidence repeats a work")
    rows = []
    for r in populations.itertuples():
        sub = cases[cases.rule == r.rule]
        old = sub[sub.stratum == "previously_reviewed"]
        fresh = sub[sub.stratum == "fresh_pilot"]
        if len(old) != r.previously_reviewed or len(fresh) != r.fresh_sample_n:
            raise RuntimeError(f"Incomplete evidence for {r.rule}")
        k, n = int(fresh.actionable.sum()), len(fresh)
        p = k / n
        if n == r.fresh_population:
            lo = hi = p
        else:
            z = 1.96
            centre = (p + z*z/(2*n)) / (1 + z*z/n)
            width = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
            lo, hi = centre - width, centre + width
        known = int(old.actionable.sum())
        estimate = (known + r.fresh_population*p) / r.marginal_works
        rows.append({"rule": r.rule, "available_works": r.available_works,
                     "marginal_works": r.marginal_works, "previously_reviewed": len(old),
                     "previously_actionable": known, "fresh_sample_n": n,
                     "fresh_actionable": k, "fresh_clear": int((fresh.label == "clear").sum()),
                     "fresh_uncertain": int((fresh.label == "uncertain").sum()),
                     "fresh_no": int((fresh.label == "no").sum()),
                     "fresh_prospective": int((fresh.label == "prospective").sum()),
                     "fresh_actionable_rate": p, "estimated_actionable_rate": estimate,
                     "estimate_low": (known + r.fresh_population*lo) / r.marginal_works,
                     "estimate_high": (known + r.fresh_population*hi) / r.marginal_works,
                     "census": n == r.fresh_population})
    summary = pd.DataFrame(rows)
    summary.to_csv(HERE / "summary.csv", index=False)
    print(summary.to_string(index=False))
    print(json.dumps({"marginal_union": int(summary.marginal_works.sum()),
                      "reviewed_cases": len(cases)}, indent=2))


if __name__ == "__main__":
    main()
