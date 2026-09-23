"""Summarise replay_results.csv: cbd rate per group × variant, and flips on controls."""
import pandas as pd
pd.set_option("display.width", 220)
df = pd.read_csv("analysis/cbd_investigation/out/replay_results.csv", dtype=str, keep_default_na=False)
df["o"] = df.outcome.replace("", "(no target)")
# what normalise_outcome_block ships: a prospective study_status overrides the outcome
df.loc[(df.study_status == "prospective") & (df.o != "not_a_replication"), "o"] = "prospective_registration"
print("errors:", (df.err != "").sum())
t = df.groupby(["group", "variant"]).agg(n=("o", "size"),
        cbd=("o", lambda s: (s == "cannot_be_determined").sum()),
        no_target=("o", lambda s: (s == "(no target)").sum()),
        prospective=("o", lambda s: (s == "prospective_registration").sum()),
        not_rep=("o", lambda s: (s == "not_a_replication").sum()))
t["cbd_rate"] = (t.cbd / t.n).round(2)
print(t.to_string())
w = df.pivot_table(index=["pair_id", "group"], columns="variant", values="o", aggfunc="first").reset_index()
w.to_csv("analysis/cbd_investigation/out/replay_wide.csv", index=False)
for g in ["adj", "cbd_doc", "cbd_doc_osf", "cbd_nodoc", "control_doc", "control_nodoc"]:
    x = w[w.group == g]
    for a, b in [("V0", "V0r"), ("V0", "V1"), ("V0", "V2"), ("V0", "V3"), ("V2", "V3")]:
        if a in x and b in x and x[b].notna().any():
            y = x.dropna(subset=[a, b])
            print(f"\n{g}: {a} -> {b}  (n={len(y)}, changed={(y[a] != y[b]).sum()})")
            print(pd.crosstab(y[a], y[b]).to_string())
