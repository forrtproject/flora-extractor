"""Evaluate PAIRS under the production rule, not models in isolation.

Production rule (link_original.py:681-683): discard the row only when both models
return "no" AND the weaker of the two confidences is "high". Anything else either
escalates or is set aside as screen_disagreement.

What matters is therefore: how many true negatives the pair correctly discards
(throughput), and how many real replications it wrongly discards (the costly error).
"""
import glob
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent

truth = json.load(open(HERE / "adjudicated.json"))["truth"]
bench = {r["file"]: r for r in json.load(open(HERE / "gemma_eval_google_gemma-4-31b-it.json"))}
cases = {c["id"]: c for c in json.load(open(HERE / "adjudication_cases.json"))}

# Live cached votes, with confidence, for the two production models.
live = {}
for cid, c in cases.items():
    b = bench.get(c["file"])
    if b:
        live[cid] = {
            "gemini-3.5-flash-lite": (b["flash_lite"], b["flash_lite_conf"]),
            "gpt-5-mini":            (b["ref"], b["ref_conf"]),
            "gemma-4-31b-it":        (b["cand"], b["cand_conf"]),
        }

# Candidates measured by eval_second_voter.py
for f in sorted(glob.glob(str(HERE / "voter_*_prod.json"))):
    stem = Path(f).name[len("voter_"):-len("_prod.json")]
    name = stem.replace("_", "/", 1) if stem.split("_")[0] in {
        "google", "mistralai", "qwen", "meta-llama", "deepseek"} else stem
    for r in json.load(open(f)):
        live.setdefault(r["id"], {})[name] = (r["verdict"], r["conf"])

ids = [i for i in sorted(truth) if i in live]
models = sorted({m for v in live.values() for m in v})
print(f"{len(ids)} adjudicated cases; models available: {', '.join(models)}\n")


def solo(m):
    got = [(live[i].get(m), truth[i]) for i in ids if live[i].get(m)]
    got = [(v, t) for (v, c), t in got if v]
    corr = sum(1 for v, t in got if v == t)
    miss = sum(1 for v, t in got if t == "yes" and v == "no")
    fp = sum(1 for v, t in got if t == "no" and v == "yes")
    ny = sum(1 for v, t in got if t == "yes")
    nn = sum(1 for v, t in got if t == "no")
    return corr, len(got), miss, ny, fp, nn


print("── Solo accuracy vs panel ".ljust(78, "─"))
print(f"  {'model':<30} {'correct':>12}  {'misses':>9}  {'false pos':>10}")
for m in models:
    c, n, miss, ny, fp, nn = solo(m)
    print(f"  {m:<30} {c}/{n} = {c/n:5.1%}  {miss}/{ny:<7}  {fp}/{nn}")

print("\n── Pair behaviour under the production discard rule ".ljust(78, "─"))
print("   (discard iff BOTH say 'no' and the weaker confidence is 'high')\n")
print(f"  {'second voter (with flash-lite)':<32} {'correct disc':>13} {'WRONG disc':>11} {'disagree':>9}")
A = "gemini-3.5-flash-lite"
for B in models:
    if B == A:
        continue
    good = bad = dis = 0
    n = 0
    for i in ids:
        va = live[i].get(A)
        vb = live[i].get(B)
        if not va or not vb or not va[0] or not vb[0]:
            continue
        n += 1
        agree = va[0] == vb[0]
        if not agree:
            dis += 1
            continue
        if va[0] == "no" and va[1] == "high" and vb[1] == "high":
            if truth[i] == "no":
                good += 1
            else:
                bad += 1
    nn = sum(1 for i in ids if truth[i] == "no")
    print(f"  {B:<32} {good}/{nn} = {good/nn:4.0%}   {bad:>6}      {dis}/{n} = {dis/n:3.0%}")

print("\n  'correct disc' = true negatives the pair actually removes (throughput)")
print("  'WRONG disc'   = real replications silently lost — the error that matters")
print("  'disagree'     = rows sent to screen_disagreement.csv\n")

print("── Complementarity: where each candidate differs from flash-lite ".ljust(78, "─"))
for B in models:
    if B == A:
        continue
    fixes = breaks = 0
    for i in ids:
        va, vb = live[i].get(A), live[i].get(B)
        if not va or not vb or not va[0] or not vb[0]:
            continue
        if va[0] != truth[i] and vb[0] == truth[i]:
            fixes += 1
        if va[0] == truth[i] and vb[0] != truth[i]:
            breaks += 1
    print(f"  {B:<32} fixes {fixes:>2} flash-lite errors, introduces {breaks:>2} new ones")
