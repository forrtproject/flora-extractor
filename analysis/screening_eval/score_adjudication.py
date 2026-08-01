"""Score flash-lite / gemma / gpt-5-mini against the blinded Sonnet judge panel.

Ground truth = majority of 3 independent Sonnet judges per case. Cases without a
2/3 majority are reported separately as genuinely ambiguous rather than forced.
"""
import glob
import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Three independent judges per batch; hojudge_* files are the held-out set's panel and
# are deliberately excluded by this glob.
JUDGE_GLOB = str(HERE / "judge_b*_j*.json")

cases = {c["id"]: c for c in json.load(open(HERE / "adjudication_cases.json"))}

votes = defaultdict(list)
files = sorted(glob.glob(JUDGE_GLOB))
for f in files:
    try:
        for v in json.load(open(f)):
            if v.get("id") in cases and v.get("verdict") in {"yes", "no", "unclear"}:
                votes[v["id"]].append((v["verdict"], f))
    except Exception as e:  # noqa: BLE001 - boundary
        print(f"  !! could not read {f}: {e}")

print(f"judge files: {len(files)};  cases with votes: {len(votes)}/{len(cases)}")
nv = Counter(len(v) for v in votes.values())
print(f"votes per case: {dict(sorted(nv.items()))}\n")

truth, ambiguous = {}, []
for cid, vs in votes.items():
    c = Counter(v for v, _ in vs)
    top, n = c.most_common(1)[0]
    if n >= 2 and len(vs) >= 2:
        truth[cid] = top
    else:
        ambiguous.append((cid, dict(c)))

unan = sum(1 for cid in truth
           if len(set(v for v, _ in votes[cid])) == 1 and len(votes[cid]) >= 3)
print(f"adjudicated: {len(truth)}  (unanimous: {unan});  no majority: {len(ambiguous)}")
if ambiguous:
    for cid, c in ambiguous[:6]:
        print(f"   ambiguous {cid} {c}  bucket={cases[cid]['bucket']}")
print()

MODELS = [("gemini-3.5-flash-lite", "_flash_lite"),
          ("gemma-4-31b-it", "_gemma"),
          ("gpt-5-mini (2nd live voter)", "_ref")]


def report(ids, label):
    if not ids:
        return
    print(f"── {label}  (n={len(ids)}) ".ljust(72, "─"))
    dist = Counter(truth[i] for i in ids)
    print(f"   judge panel says: {dict(dist)}")
    for name, key in MODELS:
        ok = sum(1 for i in ids if cases[i][key] == truth[i])
        print(f"   {name:<28} correct: {ok}/{len(ids)} = {ok/len(ids):.1%}")
    print()


all_ids = sorted(truth)
report(all_ids, "ALL adjudicated cases")

# Where the panel says this IS a replication, who keeps it?
yes_ids = [i for i in all_ids if truth[i] == "yes"]
no_ids = [i for i in all_ids if truth[i] == "no"]
print("── Directional errors ".ljust(72, "─"))
for name, key in MODELS:
    fn = sum(1 for i in yes_ids if cases[i][key] == "no")      # real replication called 'no'
    fp = sum(1 for i in no_ids if cases[i][key] == "yes")      # non-replication called 'yes'
    print(f"   {name:<28} misses {fn}/{len(yes_ids)} real replications; "
          f"false-positives on {fp}/{len(no_ids)}")
print()

for b in sorted({c["bucket"] for c in cases.values()}):
    ids = [i for i in all_ids if cases[i]["bucket"] == b]
    report(ids, f"bucket {b}")

json.dump({"truth": truth, "ambiguous": ambiguous},
          open(HERE / "adjudicated.json", "w"), indent=2)
print("-> adjudicated.json")
