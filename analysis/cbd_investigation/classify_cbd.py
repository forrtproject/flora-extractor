"""For every cbd replication row, which cached calls exist and what did each conclude?

Joins on title and on the model that produced the row (link_llm_model). Writes
out/cbd_rows.csv: one row per cbd extracted row with the latest abstract/ref-rung and
full-text-rung answers for the same model.
"""
import json, re
import pandas as pd

idx = pd.read_parquet("analysis/cbd_investigation/out/cache_index.parquet")
idx = idx[idx.kind == "targetoutcome"]
d = pd.read_csv("data/extracted.csv", dtype=str, keep_default_na=False)
r = d[(d.type == "replication")].copy()
r["tnorm"] = r.title_r.map(lambda t: re.sub(r"[^a-z0-9]", "", t.lower())[:120])
UNSET = {"cannot_be_determined", "", "None"}


def pick_target(j, row):
    ts = j.get("targets") or []
    for t in ts:
        rec = t.get("record") or {}
        if row.doi_o and str(rec.get("doi", "")).lower() == row.doi_o.lower():
            return t, "doi"
    au = (row.authors_o.split(";")[0].split(",")[0].split()[-1:] or [""])[0].lower() if row.authors_o else ""
    for t in ts:
        n = str(t.get("target_as_named") or "").lower()
        if au and au in n and (not row.year_o or row.year_o[:4] in n):
            return t, "named"
    if len(ts) == 1:
        return ts[0], "only"
    return None, ""


out = []
for _, row in r.iterrows():
    e = idx[(idx.tnorm == row.tnorm) & (idx.model == row.link_llm_model)].sort_values("mtime")
    rec = dict(pair_id=row.pair_id, doi_r=row.doi_r, doi_o=row.doi_o, outcome=row.outcome,
               link_method=row.link_method, pdf_source=row.pdf_source, parse_method=row.parse_method,
               has_abs=bool(row.abstract_r.strip()), model=row.link_llm_model, n_entries=len(e))
    for rung, sel in (("pre", ~e.rung.str.startswith("fulltext")), ("ft", e.rung.str.startswith("fulltext"))):
        ee = e[sel]
        if len(ee):
            j = json.load(open(ee.iloc[-1].file))
            t, how = pick_target(j, row)
            ob = (t or {}).get("outcome_block") or {}
            rec.update({f"{rung}_file": ee.iloc[-1].file, f"{rung}_rung": ee.iloc[-1].rung,
                        f"{rung}_prov": ee.iloc[-1].prov, f"{rung}_resolved": j.get("resolved"),
                        f"{rung}_match": how, f"{rung}_outcome": ob.get("outcome", ""),
                        f"{rung}_reason": ob.get("outcome_reasoning", ""),
                        f"{rung}_all": ee.iloc[-1].outcomes})
    out.append(rec)
o = pd.DataFrame(out)
o.to_csv("analysis/cbd_investigation/out/all_rows_cache.csv", index=False)
c = o[o.outcome == "cannot_be_determined"].copy()


def cause(x):
    if x.pdf_source and isinstance(x.get("ft_outcome"), str) and x.ft_outcome not in UNSET:
        return "A_fulltext_settled_but_carried_cbd_shipped"
    if x.pdf_source and not isinstance(x.get("ft_file"), str):
        return "C_doc_but_no_fulltext_call_found"
    if x.pdf_source:
        return "B_fulltext_read_still_cbd"
    return "D_no_document"


c["cause"] = c.apply(cause, axis=1)
c.to_csv("analysis/cbd_investigation/out/cbd_rows.csv", index=False)
print(len(c), "cbd rows")
print(c.cause.value_counts())
print(pd.crosstab(c.link_method, c.cause))
print(pd.crosstab(c.cause, c.pdf_source.str.startswith("osf")))
