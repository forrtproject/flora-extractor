"""Step 1: cbd rates on data/extracted.csv (replication rows), by evidence and provenance."""
import pandas as pd

d = pd.read_csv("data/extracted.csv", dtype=str, keep_default_na=False)
r = d[d["type"] == "replication"].copy()
r["cbd"] = r["outcome"] == "cannot_be_determined"
r["has_abs"] = r["abstract_r"].str.strip().str.len() > 0
r["has_doc"] = r["pdf_source"].str.strip().str.len() > 0
print("replication rows", len(r), "cbd", r.cbd.sum(), f"{r.cbd.mean():.1%}")
print("\noutcome distribution\n", r.outcome.value_counts().to_string())


def tab(col):
    g = r.groupby(col)["cbd"].agg(["size", "sum", "mean"]).sort_values("size", ascending=False)
    g["mean"] = (g["mean"] * 100).round(1)
    print(f"\n== by {col}\n", g.to_string())


r["evidence"] = r.has_abs.map({True: "abs", False: "noabs"}) + "+" + r.has_doc.map({True: "doc", False: "nodoc"})
for c in ["evidence", "link_method", "out_quote_source", "pdf_source", "parse_method"]:
    tab(c)
# settled-outcome rows only (exclude pending/not_a_replication etc.)
coded = r[r.outcome.isin(["successful", "failed", "mixed", "cannot_be_determined", "descriptive_only",
                          "statistically_successful_but_flawed", "uninformative"])]
print("\ncoded rows", len(coded), f"cbd {coded.cbd.mean():.1%}")
print(pd.crosstab([r.link_method], r.evidence, values=r.cbd, aggfunc="mean").round(2).to_string())
print(pd.crosstab([r.link_method], r.evidence).to_string())
