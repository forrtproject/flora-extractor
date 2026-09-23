"""Index cache/llm/targetoutcome_* and outcome_* entries by the TITLE in their prompt.

The files are content-hashed and store no doi, so the join to extracted.csv is on the
normalised title the prompt carries. Writes out/cache_index.parquet.
"""
import json, os, re, glob
import pandas as pd

rows = []
for pat, kind in (("cache/llm/targetoutcome_*.json", "targetoutcome"),
                  ("cache/llm/outcome_*.json", "outcome")):
    for f in glob.glob(pat):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        p = d.get("llm_prompt") or d.get("prompt") or ""
        m = re.search(r"^TITLE: (.*)$", p, re.M)
        title = m.group(1) if m else ""
        if kind == "targetoutcome":
            if "THE PAPER, as parsed" in p:
                rung = "fulltext_body"
            elif "DISCUSSION / CONCLUSION (from" in p:
                rung = "fulltext"
            elif "INTRODUCTION:\n" in p:
                rung = "fulltext_nodisc"
            else:
                rung = "abstract_or_refs"
            outs = [str((t.get("outcome_block") or {}).get("outcome", "")) for t in d.get("targets") or []]
            model = d.get("llm_model", "")
        else:
            rung = "standalone"
            outs = [str(d.get("outcome", ""))]
            model = d.get("llm_model", "") or d.get("model", "")
        rows.append(dict(file=f, kind=kind, title=title,
                         tnorm=re.sub(r"[^a-z0-9]", "", title.lower())[:120],
                         rung=rung, stage=d.get("target_stage", ""), model=model,
                         outcomes="|".join(outs), mtime=os.path.getmtime(f),
                         has_disc="DISCUSSION / CONCLUSION (from" in p,
                         prov=(re.search(r"DISCUSSION / CONCLUSION \(from ([^:]*?)\):", p) or [None, ""])[1][:40],
                         plen=len(p)))
df = pd.DataFrame(rows)
df.to_parquet("analysis/cbd_investigation/out/cache_index.parquet")
print(df.groupby(["kind", "rung"]).size())
print(df.model.value_counts().head())
