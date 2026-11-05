"""Open-weight decision models beside Luna, against Perplexity v1.1/v1.2 and the production
pair, on the same 2,186 cases -> score_open_out.txt. Type truth is read from the
validation tables, as in type_eval.py."""
import json, os, sys
from pathlib import Path
HERE = EV = Path(__file__).resolve().parent
sys.path.insert(0, str(EV)); sys.path.insert(0, str(EV.parents[1]))
QUAL = ("replication", "reproduction", "both")
cases = {c["id"]: c for c in json.loads((EV / "cases.json").read_text())}
load = lambda p: {r["id"]: r for r in json.loads(Path(p).read_text()) if not r.get("error")}
R = {"luna": load(EV / "out/luna.json"), "ds4": load(EV / "out/ds4.json"),
     "pplx v1.1": load(EV / "out/pplx_full2.json"), "pplx v1.2": load(EV / "out/pplx12_full2.json")}
for n in sys.argv[1:] or ["clef", "span02", "clefflash", "drex15", "tev1", "kev4b"]:
    R[n] = load(EV / f"out/{n}_full2.json")
X = [k for k in R if k not in ("luna", "ds4")]
for k in X:
    print(f"{k}: {len(R[k])} valid of {len(cases)}")
ids = [i for i in cases if all(i in R[k] for k in R)]
P = [i for i in ids if cases[i]["truth"] == "yes"]
H = [i for i in ids if cases[i]["truth"] == "no" and cases[i]["source"] != "goldneg_curated"]
E = [i for i in ids if cases[i]["source"] == "goldneg_curated"]
print(f"scored on {len(ids)} cases valid for every voter (positives {len(P)}, hard {len(H)}, curated {len(E)})")
q = lambda k, i: sum(R[k][i]["probs"][c] for c in QUAL)
def share(k, i):
    p = R[k][i]["probs"]; rep = p["replication"] + p["both"]; rp = p["reproduction"]; t = rep + rp
    return (("reproduction", rp / t) if rp > rep else ("replication", rep / t)) if t else ("replication", 0.0)
none_llm = lambda k, i: R[k][i]["classification"] == "none"
sure = lambda k, i: R[k][i]["classification"] in QUAL and R[k][i]["confident"]
def row(name, f):
    a, b, c = sum(f(i) for i in P), sum(f(i) for i in H), sum(f(i) for i in E)
    return f"| {name} | {a} | {b} ({100*b/len(H):.0f}%) | {c} ({100*c/len(E):.0f}%) |"
print(f"\n## Gate with Luna (discard only when both say none)\n\n| gate | positives lost /{len(P)} | hard discarded /{len(H)} | curated discarded /{len(E)} |\n|---|--:|--:|--:|")
print(row("ds4 + luna (production)", lambda i: none_llm("ds4", i) and none_llm("luna", i)))
print(row("luna alone", lambda i: none_llm("luna", i)))
for k in X:
    for t in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        print(row(f"luna + {k} < {t}", lambda i, k=k, t=t: none_llm("luna", i) and q(k, i) < t))
def auc(k, S):
    sp = sorted(q(k, i) for i in P); import bisect
    wins = 0.0
    for b in (q(k, i) for i in S):
        lo = bisect.bisect_left(sp, b); hi = bisect.bisect_right(sp, b)
        wins += (len(sp) - hi) + 0.5 * (hi - lo)
    return wins / (len(sp) * len(S))
lnone = [i for i in ids if none_llm("luna", i)]
def auc_sub(k):  # where it matters: among works Luna calls none
    pos = [q(k, i) for i in lnone if cases[i]["truth"] == "yes"]; neg = [q(k, i) for i in lnone if cases[i]["truth"] == "no"]
    return sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg)), len(pos), len(neg)
print("\n## Separation (AUC of P(qualifies))\n\n| voter | positives vs hard | positives vs curated | within Luna's `none` (pos/neg) |\n|---|--:|--:|--:|")
for k in X:
    a, npos, nneg = auc_sub(k)
    print(f"| {k} | {auc(k, H):.4f} | {auc(k, E):.4f} | {a:.3f} ({npos}/{nneg}) |")
print("\n## Title-search shortcut (confident Luna + P >= 0.9 + a type at the 0.7 share)\n\n| rule | positives | hard | curated |\n|---|--:|--:|--:|")
print(f"| ds4 + luna both confident | {sum(sure('ds4', i) and sure('luna', i) for i in P)} | {sum(sure('ds4', i) and sure('luna', i) for i in H)} | {sum(sure('ds4', i) and sure('luna', i) for i in E)} |")
for k in X:
    f = lambda i, k=k: sure("luna", i) and q(k, i) >= 0.9 and share(k, i)[1] >= 0.7
    print(f"| luna + {k} | {sum(f(i) for i in P)} | {sum(f(i) for i in H)} | {sum(f(i) for i in E)} |")
print("\n## Cost (provider-reported)\n")
for k in X:
    rows = [R[k][i] for i in ids]; usd = sum((r.get("usage") or {}).get("usd") or 0 for r in rows)
    print(f"- {k}: ${1000*usd/len(rows):.4f} per 1,000 works")
import type_eval as te
T = {i: t for i, t in te.truth().items() if i in R["luna"] and all(i in R[k] for k in X)}
llm_type = lambda k, i: (lambda c: "replication" if c == "both" else c if c in ("replication", "reproduction") else "")(R[k][i]["classification"])
def report(name, pred):
    got = {i: pred[i] for i in T if pred.get(i)}; ok = sum(got[i] == T[i] for i in got); rp = [i for i in T if T[i] == "reproduction"]
    print(f"| {name} | {ok}/{len(got)} ({100*ok/max(len(got),1):.1f}%) | {len(T)-len(got)} | {sum(got.get(i) == 'reproduction' for i in rp)}/{len(rp)} |")
print(f"\n## Record type vs validators ({len(T)} positives)\n\n| coder | correct / typed | untyped | reproductions found |\n|---|---|--:|---|")
luna = {i: llm_type("luna", i) for i in T}
report("luna alone", luna)
report("ds4 first, else luna (production)", {i: llm_type("ds4", i) or luna[i] for i in T})
for k in X:
    report(f"{k}: own type", {i: share(k, i)[0] for i in T})
    report(f"luna, {k} overrides at 0.7 share", {i: share(k, i)[0] if (q(k, i) >= 0.4 and share(k, i)[1] >= 0.7) else luna[i] for i in T})
