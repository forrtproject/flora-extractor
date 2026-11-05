"""Span-02 decision-prompt variants beside Luna -> score_variants_out.txt (REPORT_prompt_ablation.md).

The split is fixed by a hash of the case id (even md5 = dev), chosen before any error
was read; patterns were read on dev only. Negatives are reported by label provenance:
human-labelled (validators, the hand-coded screening set) and pipeline-labelled
(earlier screen discards, older-pipeline LLM labels). Type truth is read from the
validation tables, as in type_eval.py."""
import hashlib, json, sys
from pathlib import Path
EV = Path(__file__).resolve().parent
sys.path.insert(0, str(EV)); sys.path.insert(0, str(EV.parents[1]))
cases = {c['id']: c for c in json.loads((EV/'cases.json').read_text())}
split = {i: ('dev' if int(hashlib.md5(i.encode()).hexdigest(), 16) % 2 == 0 else 'test') for i in cases}
L = lambda p: {r['id']: r for r in json.loads(Path(p).read_text()) if not r.get('error')}
luna, ds4 = L(EV/'out/luna.json'), L(EV/'out/ds4.json')
R = {'v1 (shipped #219)': L(EV/'out/span02_full2.json'), 'v2 (all changes)': L(EV/'out/span02v2_full2.json'),
     'v3 (none + title-only)': L(EV/'out/span02v3_full2.json'),
     'v4 (title-only only)': L(EV/'out/span02v4_full2.json')}
for k, M in R.items(): print(k, len(M), 'valid')
QUAL = ('replication', 'reproduction', 'both')
q = lambda M, i: sum(M[i]['probs'][c] for c in QUAL)
ln = lambda i: luna[i]['classification'] == 'none'
def grp(part):
    ids = [i for i in cases if part in ('all', split[i])]
    return ([i for i in ids if cases[i]['truth'] == 'yes'],
            [i for i in ids if cases[i]['truth'] == 'no' and cases[i]['source'] in ('human', 'screeneval')],
            [i for i in ids if cases[i]['truth'] == 'no' and cases[i]['source'] in ('goldneg_screen', 'goldneg_curated')])
def gate(part, f):
    P, H, X = grp(part)
    return f"{sum(ln(i) and f(i) for i in P):2d} lost {100*sum(ln(i) and f(i) for i in H)/len(H):3.0f}%/{100*sum(ln(i) and f(i) for i in X)/len(X):3.0f}%"
print("\n## Gate with Luna: positives lost, human-labelled / pipeline-labelled negatives discarded")
print(f"{'':28s}{'dev':24s}{'test':24s}all")
f0 = lambda i: ds4[i]['classification'] == 'none'
print(f"{'ds4 + luna (production)':28s}{gate('dev', f0):24s}{gate('test', f0):24s}{gate('all', f0)}")
for k, M in R.items():
    for c in (0.5, 0.7):
        f = lambda i, M=M, c=c: q(M, i) < c
        print(f"{k[:20]+f' <{c}':28s}{gate('dev', f):24s}{gate('test', f):24s}{gate('all', f)}")
def auc(M, part):
    ids = [i for i in cases if part in ('all', split[i]) and ln(i)]
    pos = [q(M, i) for i in ids if cases[i]['truth'] == 'yes']; neg = [q(M, i) for i in ids if cases[i]['truth'] == 'no' and cases[i]['source'] in ('human', 'screeneval')]
    return sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg)) if pos and neg else float('nan')
print("\n## AUC within Luna's none (positives vs human-labelled negatives): dev / test")
for k, M in R.items(): print(f"  {k:26s} {auc(M,'dev'):.3f} / {auc(M,'test'):.3f}")
print("\n## Positives where Span answers mostly 'unclear' (P(unclear) > 0.5)")
P_all = [i for i in cases if cases[i]['truth'] == 'yes']
for k, M in R.items(): print(f"  {k:26s} {sum(M[i]['probs']['unclear'] > 0.5 for i in P_all)}")
print("\n## Title-search shortcut (confident Luna + P >= 0.9 + type share >= 0.7): positives / hard / curated")
def share(M, i):
    p = M[i]['probs']; rep = p['replication'] + p['both']; rp = p['reproduction']; t = rep + rp
    return (('reproduction', rp / t) if rp > rep else ('replication', rep / t)) if t else ('replication', 0.0)
sure = lambda i: luna[i]['classification'] in QUAL and luna[i]['confident']
H_all = [i for i in cases if cases[i]['truth'] == 'no' and cases[i]['source'] != 'goldneg_curated']; E_all = [i for i in cases if cases[i]['source'] == 'goldneg_curated']
for k, M in R.items():
    g = lambda i, M=M: sure(i) and q(M, i) >= 0.9 and share(M, i)[1] >= 0.7
    print(f"  {k:26s} {sum(g(i) for i in P_all)} / {sum(g(i) for i in H_all)} / {sum(g(i) for i in E_all)}")
import type_eval as te
T = {i: t for i, t in te.truth().items() if all(i in M for M in R.values())}
lt = lambda i: (lambda c: 'replication' if c == 'both' else c if c in ('replication', 'reproduction') else '')(luna[i]['classification'])
print(f"\n## Type, Luna overridden at a 0.7 share ({len(T)} positives with validator types): correct/typed, reproductions found — dev | test")
for k, M in R.items():
    pred = {i: (share(M, i)[0] if (q(M, i) >= 0.4 and share(M, i)[1] >= 0.7) else lt(i)) for i in T}
    out = []
    for part in ('dev', 'test'):
        ids = [i for i in T if split[i] == part and pred[i]]
        ok = sum(pred[i] == T[i] for i in ids); rp = [i for i in T if split[i] == part and T[i] == 'reproduction']
        out.append(f"{ok}/{len(ids)} ({100*ok/len(ids):.1f}%), repro {sum(pred[i]=='reproduction' for i in rp)}/{len(rp)}")
    print(f"  {k:26s} " + " | ".join(out))
