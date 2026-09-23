"""Print, per sampled cbd doc row: the model's reason, what was sent, and result-bearing
sentences in the parsed body that were NOT sent — for manual cause coding."""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent)); sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import pandas as pd
from replay import body_for, evidence_of

RES = re.compile(r"[^.]*\b(replicat\w*|reproduc\w*|original (?:study|finding|effect|result)s?|consistent with|in line with|contrary to|failed to|did not (?:find|replicate|support)|not significant|no significant|was significant|were significant|confirm\w*|support\w*)\b[^.]*\.", re.I)
n = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
s = pd.read_csv("analysis/cbd_investigation/out/sample.csv", dtype=str, keep_default_na=False)
c = pd.read_csv("analysis/cbd_investigation/out/cbd_rows.csv", dtype=str, keep_default_na=False)
grp = sys.argv[1]
for _, r in s[s.group == grp].iterrows():
    cc = c[c.pair_id == r.pair_id].iloc[0]
    src = r.ft_file or r.pre_file
    ev = evidence_of(json.load(open(src))["llm_prompt"])
    ev_nr = re.split(r"\n\n(?:WORKS THIS PAPER CITES|REFERENCE LIST:)", ev)[0]
    body = body_for(r)
    au = (r.authors_o.split(";")[0].split(",")[0] if r.authors_o else "").strip().split()[-1:] 
    print(f"\n##### {r.pair_id[:8]} {r.link_method} {r.pdf_source}/{r.parse_method} body={len(body)} sent_ev={len(ev_nr)} prov={cc.ft_prov[:10]}")
    print("TITLE_R:", r.title_r[:150], "| ORIG:", r.authors_o[:40], r.year_o, r.title_o[:90])
    print("PRE:", cc.pre_outcome, "::", cc.pre_reason[:250])
    print("FT :", cc.ft_outcome, "::", cc.ft_reason[:250])
    en = n(ev_nr); hits = []
    for m in RES.finditer(body):
        sent = " ".join(m.group(0).split())
        if 40 < len(sent) < 400 and n(sent)[:80] not in en:
            hits.append((m.start() / max(1, len(body)), sent))
    for pos, sent in hits[:6]:
        print(f"   [unsent @{pos:.2f}] {sent[:260]}")
    print("   (sent-abstract head)", r.abstract_r[:300].replace("\n", " "))
