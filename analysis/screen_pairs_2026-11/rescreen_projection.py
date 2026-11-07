"""Issue #227 projection: cost and effect of re-screening the expensive tier with each
combination in REPORT.md's "Combinations to consider". -> rescreen_out.txt

Corpus counts are the read-only store count posted on #227 (2026-10-28). Rates come from the
2,186-case evaluation: of the 599 cases main (DS4 + Luna) discards, the share each combination
passes ("rescued") and how many of those are real; of the 1,587 main passes, the share it would
discard. Luna is cached for the current generation's works and re-bought for the rest, at $0.07
(measured, with provider caching) to $0.22 (standard list price) per 1,000. Stage 3 costs
$0.0009-0.0018 per extracted work (#227). The rates are the evaluation set's mix, not the corpus's.
"""
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    import combos as C

CUR_PASS, CUR_DISC = 16_111, 7_095          # DS4 + Luna generation 3b93ecf06901e653: Luna cached
OLD_PASS, OLD_DISC = 8_090 + 1_290, 997 + 20  # mini-era + older generations: Luna re-bought
DISC, ALL = CUR_DISC + OLD_DISC, CUR_PASS + CUR_DISC + OLD_PASS + OLD_DISC
LUNA = (0.07, 0.22)
GATES = [("Luna + Span-02 < 0.7", ("luna", ("span02", .7))), ("Luna + Liquid d1.1 < 0.5", ("luna", ("d11", .5))),
         ("Luna + Clef < 0.5", ("luna", ("clef", .5))), ("Luna + Span-02 + Clef", ("luna", ("span02", .7), ("clef", .5))),
         ("Luna + Span-02 + d1.1", ("luna", ("span02", .7), ("d11", .5)))]

main = ("ds4", "luna")
disc = lambda g, i: all(C.says_none(v, i) for v in g)
D = [i for i in C.ids if disc(main, i)]
M = [i for i in C.ids if not disc(main, i)]
real = lambda S: sum(C.cases[i]["truth"] == "yes" for i in S)
assert (len(D), len(M)) == (599, 1587), (len(D), len(M))

print(f"Corpus: {ALL:,} screened works, {DISC:,} discards. Evaluation: main discards {len(D)}, passes {len(M)}.\n")
print("| combination | screen: discards only $ | screen: all works $ | rescued (rate, real in eval) | ~rescued works | ~real | Stage 3 $ | if passes re-screened: works dropped (real in eval) |")
print("|---|--:|--:|--:|--:|--:|--:|--:|")
for name, g in GATES:
    dec = sum(C.USD[v[0]] for v in g if not isinstance(v, str))
    s_disc = [(OLD_DISC * l + DISC * dec) / 1000 for l in LUNA]
    s_all = [((OLD_PASS + OLD_DISC) * l + ALL * dec) / 1000 for l in LUNA]
    res = [i for i in D if not disc(g, i)]; nd = [i for i in M if disc(g, i)]
    n_res = DISC * len(res) / len(D); n_real = n_res * real(res) / max(len(res), 1)
    n_drop = (CUR_PASS + OLD_PASS) * len(nd) / len(M)
    print(f"| {name} | {s_disc[0]:.2f}–{s_disc[1]:.2f} | {s_all[0]:.2f}–{s_all[1]:.2f} | {100*len(res)/len(D):.1f}% ({real(res)} of {len(res)}) "
          f"| ~{n_res:,.0f} | ~{n_real:,.0f} | {n_res*0.0009:.2f}–{n_res*0.0018:.2f} | ~{n_drop:,.0f} ({real(nd)} of {len(nd)}) |")
main_all = [(OLD_PASS + OLD_DISC) * (C.USD["ds4"] + l) / 1000 for l in LUNA]
print(f"\nFor comparison, re-screening all works with main (DS4 + Luna, both cached for the current generation): ${main_all[0]:.2f}–{main_all[1]:.2f}.")
