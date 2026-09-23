"""Headline table: cbd / settled / prospective per group under the shipped answer, a plain
gpt-6-luna re-run (V0), package A (full body; no prompt edit) and package B (full body +
prompt edit V1b/V3). Doc rows without a parsed body fall back to the evidence they had."""
import pandas as pd
df = pd.read_csv("analysis/cbd_investigation/out/replay_results.csv", dtype=str, keep_default_na=False)
df["o"] = df.outcome.replace("", "(no target)")
df.loc[(df.study_status == "prospective") & (df.o != "not_a_replication"), "o"] = "prospective_registration"
w = df.pivot_table(index=["pair_id", "group"], columns="variant", values="o", aggfunc="first").reset_index()
s = pd.read_csv("analysis/cbd_investigation/out/sample.csv", dtype=str, keep_default_na=False)
w = w.merge(s[["pair_id", "outcome", "pdf_source"]].rename(columns={"outcome": "shipped"}), on="pair_id")
w["A"] = w.V2.fillna(w.V0)
w["B"] = w.V3.fillna(w.V1b if "V1b" in w else w.V1).fillna(w.V1)
SETTLED = {"successful", "failed", "mixed", "uninformative", "descriptive only", "statistically successful but flawed"}
rows = []
for g, x in w.groupby("group"):
    for col in ["shipped", "V0", "A", "B"]:
        v = x[col]
        rows.append({"group": g, "answer": col, "n": len(x), "cbd": int((v == "cannot_be_determined").sum()),
                     "settled": int(v.isin(SETTLED).sum()), "prospective": int((v == "prospective_registration").sum()),
                     "no_target": int((v == "(no target)").sum())})
t = pd.DataFrame(rows)
print(t.pivot_table(index="group", columns="answer", values="cbd", aggfunc="first")[["shipped", "V0", "A", "B"]].to_string())
print(t.to_string(index=False))
ctrl = w[w.group.str.startswith("control")]
print("\ncontrols: answer differs from V0 — V0r:", int((ctrl.V0 != ctrl.V0r).sum()), " A:", int((ctrl.V0 != ctrl.A).sum()), " B:", int((ctrl.V0 != ctrl.B).sum()), " of", len(ctrl))
print("controls: settled answer turned cbd — V0r:", int((ctrl.V0.isin(SETTLED) & (ctrl.V0r == "cannot_be_determined")).sum()),
      " A:", int((ctrl.V0.isin(SETTLED) & (ctrl.A == "cannot_be_determined")).sum()), " B:", int((ctrl.V0.isin(SETTLED) & (ctrl.B == "cannot_be_determined")).sum()))
print("controls: settled -> different settled — V0r:", int((ctrl.V0.isin(SETTLED) & ctrl.V0r.isin(SETTLED) & (ctrl.V0 != ctrl.V0r)).sum()),
      " A:", int((ctrl.V0.isin(SETTLED) & ctrl.A.isin(SETTLED) & (ctrl.V0 != ctrl.A)).sum()), " B:", int((ctrl.V0.isin(SETTLED) & ctrl.B.isin(SETTLED) & (ctrl.V0 != ctrl.B)).sum()))
print("controls vs SHIPPED — V0:", int((ctrl.shipped != ctrl.V0).sum()), " A:", int((ctrl.shipped != ctrl.A).sum()), " B:", int((ctrl.shipped != ctrl.B).sum()))
w.to_csv("analysis/cbd_investigation/out/summary_wide.csv", index=False)
