"""combos.py, minus the withdrawn Perplexity v1.1; Pareto among gates with a text (LLM) voter, plus named references."""
import combos as C
avail = [r for r in C.rows if "pplx11" not in r[0]]
txt = [r for r in avail if r[5] == "text"]
dom = lambda r, pool: any(o[1] <= r[1] and o[2] >= r[2] and o[4] <= r[4] and (o[1], -o[2], o[4]) != (r[1], -r[2], r[4]) for o in pool)
named = {"ds4 + luna", "luna + span02<0.7", "luna + span02<0.5", "luna + clef<0.5", "luna + clef<0.6", "ds4 + span02<0.5", "ds4 + span02<0.7",
         "luna + ds4 + span02<0.7", "luna + span02<0.7 + clef<0.5", "luna + span02<0.7 + clefflash<0.6", "luna + clefflash<0.6", "span02<0.7 + clef<0.5"}
print("\n\n=== AVAILABLE ONLY, text voter required (Pareto) + named ===")
print("| gate | lost | human neg | pipeline neg | $/1k | text |\n|---|--:|--:|--:|--:|---|")
for r in sorted([r for r in txt if not dom(r, txt) or r[0] in named] + [r for r in avail if r[0] in named and r[5] != "text"], key=lambda r: (r[1], -r[2])):
    print(f"| {r[0]} | {r[1]} | {100*r[2]:.0f}% | {100*r[3]:.0f}% | {r[4]:.3f} | {r[5]} |")
# which positives do the leading candidates lose, and do they overlap?
for g in (("luna", ("span02", 0.7)), ("luna", ("clef", 0.5)), ("ds4", "luna"), ("ds4", ("span02", 0.5)), ("luna", ("span02", 0.7), ("clef", 0.5))):
    print(C.name(g[0]), [C.name(v) for v in g[1:]], sorted(C.score(g)[3]))
