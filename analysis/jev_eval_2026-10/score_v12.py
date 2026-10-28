"""Score Perplexity Decider v1.2 / Flash v1.2 against v1.1 (pplx_full2) -> score_v12_out.txt.

v1.1 = the stored pplx_full2 run (production prompt, WHOLE abstracts). v1.2 runs use the
production request (4,000-char abstracts); `*u` reruns the 51 long-abstract cases whole,
so "eval-state" rows compare like with like against v1.1. Type truth is read from the
validation tables (read only), exactly as analysis/jev_eval_2026-10/type_eval.py does."""
import json, os, sys
from pathlib import Path
HERE = EV = Path(__file__).resolve().parent
sys.path.insert(0, str(EV)); sys.path.insert(0, str(EV.parents[1]))

QUAL = ("replication", "reproduction", "both")
cases = {c["id"]: c for c in json.loads((EV / "cases.json").read_text())}
def load(p):
    return {r["id"]: r for r in json.loads(Path(p).read_text()) if not r.get("error")} if Path(p).exists() else {}
R = {"luna": load(EV / "out/luna.json"), "ds4": load(EV / "out/ds4.json"),
     "v1.1": load(EV / "out/pplx_full2.json")}
for v in sys.argv[1:] or ["pplx12", "pplx12f"]:
    prod = load(EV / f"out/{v}_full2.json"); whole = load(EV / f"out/{v}_full2_whole.json")
    R[f"{v} prod"] = prod
    R[f"{v} eval"] = {**prod, **{i: r for i, r in whole.items() if i in prod}}
X = [k for k in R if k not in ("luna", "ds4")]
ids = [i for i in cases if all(i in R[k] for k in R)]
P = [i for i in ids if cases[i]["truth"] == "yes"]
H = [i for i in ids if cases[i]["truth"] == "no" and cases[i]["source"] != "goldneg_curated"]
E = [i for i in ids if cases[i]["source"] == "goldneg_curated"]
print(f"cases with every run valid: {len(ids)} (positives {len(P)}, hard {len(H)}, curated {len(E)})")
for k in X:
    n_err = len([i for i in cases if i not in R[k]])
    print(f"  {k}: {len(R[k])} valid, {n_err} missing/errored")

def q(k, i):
    p = R[k][i]["probs"]; return p["replication"] + p["reproduction"] + p["both"]
def share(k, i):
    p = R[k][i]["probs"]; rep = p["replication"] + p["both"]; rp = p["reproduction"]; t = rep + rp
    return (("reproduction", rp / t) if rp > rep else ("replication", rep / t)) if t else ("replication", 0.0)
def none_llm(k, i): return R[k][i]["classification"] == "none"
def sure_llm(k, i): return R[k][i]["classification"] in QUAL and R[k][i]["confident"]

def gate_row(name, says_none):
    lost = sum(says_none(i) for i in P); dh = sum(says_none(i) for i in H); de = sum(says_none(i) for i in E)
    return f"| {name} | {lost} | {dh} ({100*dh/len(H):.0f}%) | {de} ({100*de/len(E):.0f}%) |"
print("\n## Gate (discard only when every voter says none)\n\n| gate | positives lost /%d | hard discarded /%d | curated discarded /%d |\n|---|--:|--:|--:|" % (len(P), len(H), len(E)))
print(gate_row("ds4 + luna (production)", lambda i: none_llm("ds4", i) and none_llm("luna", i)))
for k in X:
    for t in (0.3, 0.4, 0.5, 0.6):
        print(gate_row(f"luna + {k} < {t}", lambda i, k=k, t=t: none_llm("luna", i) and q(k, i) < t))

def auc(k, S):
    sp = [q(k, i) for i in P]; sn = [q(k, i) for i in S]
    wins = sum((a > b) + 0.5 * (a == b) for a in sp for b in sn)
    return wins / (len(sp) * len(sn))
print("\n## Separation (AUC of P(qualifies), positives vs ...)\n\n| voter | hard negatives | curated negatives |\n|---|--:|--:|")
for k in X:
    print(f"| {k} | {auc(k, H):.4f} | {auc(k, E):.4f} |")

print("\n## Title-search shortcut (confident Luna + P >= 0.9 + a type at the 0.7 share)\n\n| rule | positives | hard | curated |\n|---|--:|--:|--:|")
print(f"| ds4 + luna both confident | {sum(sure_llm('ds4', i) and sure_llm('luna', i) for i in P)} | {sum(sure_llm('ds4', i) and sure_llm('luna', i) for i in H)} | {sum(sure_llm('ds4', i) and sure_llm('luna', i) for i in E)} |")
for k in X:
    f = lambda i, k=k: sure_llm("luna", i) and q(k, i) >= 0.9 and share(k, i)[1] >= 0.7
    print(f"| luna + {k} | {sum(f(i) for i in P)} | {sum(f(i) for i in H)} | {sum(f(i) for i in E)} |")

print("\n## Agreement with v1.1 on the discard side (luna says none)\n")
ln = [i for i in ids if none_llm("luna", i)]
for k in X:
    if k == "v1.1": continue
    flip_to_keep = sum(q("v1.1", i) < 0.4 <= q(k, i) for i in ln)
    flip_to_drop = sum(q(k, i) < 0.4 <= q("v1.1", i) for i in ln)
    print(f"- {k}: of {len(ln)} works Luna calls none, {flip_to_keep} v1.1-discards would now be kept, {flip_to_drop} v1.1-keeps would now be discarded")

print("\n## Cost (provider-reported)\n")
for k in X:
    rows = [R[k][i] for i in ids]
    usd = sum((r.get("usage") or {}).get("usd") or 0 for r in rows)
    tin = sum((r.get("usage") or {}).get("in") or 0 for r in rows) / len(rows)
    print(f"- {k}: ${1000*usd/len(rows):.4f} per 1,000 works, {tin:.0f} input tokens per call")

# Record type against the validators' labels (read-only), as type_eval.py builds them.
try:
    import type_eval as te
    te.R = R
    T = {i: t for i, t in te.truth().items() if i in R["luna"]}
except Exception as e:  # noqa: BLE001
    print("\n(type truth unavailable:", type(e).__name__, str(e)[:120], ")"); sys.exit(0)
def llm_type(i):
    c = R["luna"][i]["classification"]; return "replication" if c == "both" else c if c in ("replication", "reproduction") else ""
def report(name, pred):
    got = {i: pred[i] for i in T if pred.get(i)}
    ok = sum(got[i] == T[i] for i in got); rp = [i for i in T if T[i] == "reproduction"]
    print(f"| {name} | {ok}/{len(got)} ({100*ok/max(len(got),1):.1f}%) | {len(T)-len(got)} | {sum(got.get(i) == 'reproduction' for i in rp)}/{len(rp)} | {sum(1 for i in got if got[i] == 'reproduction' and T[i] == 'replication')} |")
print(f"\n## Record type vs validators ({len(T)} positives)\n\n| coder | correct / typed | untyped | reproductions found | replications called reproduction |\n|---|---|--:|---|--:|")
luna = {i: llm_type(i) for i in T}
report("luna alone", luna)
def ds4_type(i):
    c = R["ds4"][i]["classification"]; return "replication" if c == "both" else c if c in ("replication", "reproduction") else ""
report("ds4 first, else luna (the production pair's rule)", {i: ds4_type(i) or luna[i] for i in T})
for k in X:
    if not all(i in R[k] for i in T):
        continue
    own = {i: share(k, i)[0] for i in T}
    # _screen_record_type: the decision vote names a type only when P >= 0.4 and a type
    # holds the 0.7 share; then it wins, otherwise Luna's type (or none) stands.
    prod = {i: (share(k, i)[0] if (q(k, i) >= 0.4 and share(k, i)[1] >= 0.7) else luna[i])
            for i in T}
    report(f"{k}: its own type", own)
    report(f"luna, {k} overrides at share >= 0.7 (shipped rule)", prod)
