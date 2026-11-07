"""The 2026-11-03 decision models beside Luna: cut-off sweep, separation inside Luna's `none`,
type accuracy proxy, cost. Span-02 and Clef are repeated as the bar to beat. -> score_new_out.txt"""
import combos as C

lnone = [i for i in C.ids if C.says_none("luna", i)]
def auc_in_luna_none(k):  # what decides the gate: ranking of the works Luna calls none
    pos = [C.q(k, i) for i in lnone if C.cases[i]["truth"] == "yes"]
    neg = [C.q(k, i) for i in lnone if C.cases[i]["truth"] == "no"]
    return sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))
def spread(k):  # share of Luna-none negatives the model leaves undecided (0.1 <= P < 0.9)
    neg = [C.q(k, i) for i in lnone if C.cases[i]["truth"] == "no"]
    return sum(0.1 <= p < 0.9 for p in neg) / len(neg)

show = ["span02", "clef"] + list(C.NEW)
print(f"Luna says none on {len(lnone)} works ({sum(C.cases[i]['truth'] == 'yes' for i in lnone)} positives)\n")
print("| voter | $/1k | AUC in Luna-none | undecided negs | lost / human / pipeline at 0.3 | 0.5 | 0.7 | 0.9 |")
print("|---|--:|--:|--:|---|---|---|---|")
for k in show:
    cells = []
    for t in (0.3, 0.5, 0.7, 0.9):
        lost, h, p, _ = C.score(("luna", (k, t)))
        cells.append(f"{lost} / {100*h:.0f}% / {100*p:.0f}%")
    print(f"| {k} | {C.USD[k]:.3f} | {auc_in_luna_none(k):.3f} | {100*spread(k):.0f}% | " + " | ".join(cells) + " |")

print("\nBest third voter for Luna + span02<0.7 (unanimous):\n\n| third | lost | human | pipeline | $/1k |\n|---|--:|--:|--:|--:|")
base = ("luna", ("span02", 0.7))
third = sorted(((C.score(base + ((k, t),)), k, t) for k in C.CUT if k not in ("span02", "pplx11*") for t in C.CUT[k]),
               key=lambda x: (x[0][0], -x[0][1]))
for (lost, h, p, _), k, t in third[:12]:
    print(f"| {k}<{t} | {lost} | {100*h:.0f}% | {100*p:.0f}% | {C.cost(base + ((k, t),)):.3f} |")
