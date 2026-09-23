"""Show what the model saw and said for one extracted row: python show.py <doi_r> [doi_o]."""
import json, re, sys
import pandas as pd

idx = pd.read_parquet("analysis/cbd_investigation/out/cache_index.parquet")
d = pd.read_csv("data/extracted.csv", dtype=str, keep_default_na=False)


def tnorm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())[:120]


def entries_for(row):
    e = idx[idx.tnorm == tnorm(row.title_r)].sort_values("mtime")
    return e


def evidence_part(prompt):
    i = prompt.find("\nPAPER\n")
    body = prompt[i:] if i >= 0 else prompt[-12000:]
    body = re.split(r"\n\n(WORKS THIS PAPER CITES|REFERENCE LIST:)", body)[0]
    return body


def target_for(entry, doi_o):
    for t in entry.get("targets") or []:
        rec = t.get("record") or {}
        if doi_o and str(rec.get("doi", "")).lower() == doi_o.lower():
            return t
    ts = entry.get("targets") or []
    return ts[0] if len(ts) == 1 else None


if __name__ == "__main__":
    doi_r = sys.argv[1]
    doi_o = sys.argv[2] if len(sys.argv) > 2 else ""
    rows = d[(d.doi_r == doi_r) & ((d.doi_o == doi_o) | (doi_o == ""))]
    row = rows.iloc[0]
    print(row[["title_r", "link_method", "link_llm_model", "pdf_source", "parse_method", "outcome", "outcome_reasoning"]].to_string())
    print("abstract_r:", row.abstract_r[:1500])
    for _, e in entries_for(row).iterrows():
        j = json.load(open(e.file))
        t = target_for(j, row.doi_o) if e.kind == "targetoutcome" else j
        ob = (t or {}).get("outcome_block", t) if e.kind == "targetoutcome" else j
        print(f"\n=== {e.kind} {e.rung} {e.stage} {e.model} prov={e.prov} outs={e.outcomes} plen={e.plen}")
        if ob:
            print("  ->", ob.get("outcome"), "|", ob.get("outcome_reasoning"), "| quote:", str(ob.get("outcome_phrase"))[:300])
    if "--prompt" in sys.argv:
        e = entries_for(row)
        e = e[e.rung.str.startswith("fulltext")] if (e.rung.str.startswith("fulltext")).any() else e
        print("\n\n##### PROMPT EVIDENCE (latest)\n", evidence_part(json.load(open(e.iloc[-1].file))["llm_prompt"]))
