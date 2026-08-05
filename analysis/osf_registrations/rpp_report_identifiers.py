"""For each FLoRA RPP report node, every registration OSF itself knows about."""
import json, re, sys, time
import pandas as pd, requests

SP = sys.argv[1]
s = requests.Session(); s.headers["User-Agent"] = "FLoRA-Extractor/1.0"
d = pd.read_csv("data/flora.csv", low_memory=False)
rpp = d[d.doi_r == "10.1126/science.aac4716"].copy()
rx = re.compile(r"osf\.io/([a-z0-9]{5})", re.I)
rpp["node"] = [m.group(1).lower() if (m := rx.search(str(u))) else ""
               for u in rpp.url_r.fillna("")]

out = {}
for i, node in enumerate(sorted({n for n in rpp.node if n}), 1):
    regs = []
    for _ in range(3):
        r = s.get(f"https://api.osf.io/v2/nodes/{node}/registrations/", timeout=30)
        if r.status_code < 500:
            break
        time.sleep(2)
    if r.status_code == 200:
        for item in r.json().get("data", []):
            a = item.get("attributes", {})
            doi = ""
            for ident in (a.get("identifiers") or []):
                pass
            regs.append({"guid": item["id"], "title": (a.get("title") or "")[:70],
                         "registration_supplement": a.get("registration_supplement", ""),
                         "date_registered": (a.get("date_registered") or "")[:10],
                         "withdrawn": a.get("withdrawn", False)})
    out[node] = {"status": r.status_code, "registrations": regs}
    time.sleep(0.3)
    if i % 25 == 0: print(f"  {i}/91", flush=True)

json.dump(out, open(f"{SP}/node_registrations.json", "w"), indent=1)
have = {n: v for n, v in out.items() if v["registrations"]}
print(f"\nFLoRA RPP report nodes: {len(out)}")
print(f"  with at least one registration (=> a real 10.17605 DOI): {len(have)}")
print(f"  with exactly one:  {sum(1 for v in have.values() if len(v['registrations'])==1)}")
print(f"  with more than one: {sum(1 for v in have.values() if len(v['registrations'])>1)}")
from collections import Counter
print("\n templates of those registrations:")
for t, c in Counter(r["registration_supplement"] for v in have.values()
                    for r in v["registrations"]).most_common():
    print(f"   {c:4}  {t}")
