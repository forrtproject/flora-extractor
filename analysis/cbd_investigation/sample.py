"""Draw the replay sample (seed 7): the 19 adjudicated cbd items, 40 cbd rows with a
document (26 non-OSF, 14 OSF), 20 cbd rows without one, and 60 settled controls."""
import pandas as pd

d = pd.read_csv("data/extracted.csv", dtype=str, keep_default_na=False)
c = pd.read_csv("analysis/cbd_investigation/out/all_rows_cache.csv", dtype=str, keep_default_na=False)
x = d[d.type == "replication"].merge(c[["pair_id", "pre_file", "ft_file"]], on="pair_id", how="left").fillna("")
x = x.drop_duplicates("pair_id")
v = pd.read_csv("analysis/mo_observatory/adjudication/verdicts.csv", dtype=str, keep_default_na=False)
v = v[v.stratum == "cbd"]
adj = x.merge(v[["id", "doi_r", "doi_o"]], on=["doi_r", "doi_o"]).assign(group="adj")
rest = x[~x.pair_id.isin(adj.pair_id)]
osf = rest.doi_r.str.startswith("10.17605") | rest.pdf_source.str.startswith("osf")
cbd = rest.outcome == "cannot_be_determined"
has_doc = rest.pdf_source != ""
has_prompt = (rest.ft_file != "") | (rest.pre_file != "")
S = 7
parts = [adj,
         rest[cbd & has_doc & ~osf & has_prompt].sample(26, random_state=S).assign(group="cbd_doc"),
         rest[cbd & has_doc & osf & has_prompt].sample(14, random_state=S).assign(group="cbd_doc_osf"),
         rest[cbd & ~has_doc & (rest.pre_file != "")].sample(20, random_state=S).assign(group="cbd_nodoc"),
         rest[rest.outcome.isin(["successful", "failed", "mixed"]) & has_doc & (rest.ft_file != "") & ~osf]
             .sample(30, random_state=S).assign(group="control_doc"),
         rest[rest.outcome.isin(["successful", "failed", "mixed"]) & ~has_doc & (rest.pre_file != "")]
             .sample(30, random_state=S).assign(group="control_nodoc")]
s = pd.concat(parts)
s.to_csv("analysis/cbd_investigation/out/sample.csv", index=False)
print(s.group.value_counts(), s.groupby("group").outcome.value_counts().unstack(fill_value=0))
