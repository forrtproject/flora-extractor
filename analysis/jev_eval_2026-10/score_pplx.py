"""REPORT.md's analysis with Perplexity v1.1 in Jev's place. Prints every table.

pplx = perplexity/pplx-decider-v1.1-27b asked round 1's exact Jev `p7` question
(out/pplx_p7.json); pplx_full = the full-rules `choice` run of round 2. Jev p7 rows are
kept beside them for the like-for-like comparison. Reuses score2's spec strings.
"""
import json

import score2 as s

s.runs["pplx_p7"] = {r["id"]: r for r in json.loads((s.HERE / "out/pplx_p7.json")
                     .read_text(encoding="utf-8")) if not r.get("error")}
R, P, H, E = s.runs, s.pos, s.hard, s.easy
COST = {"ds4": 0.091, "luna": 0.070, "jev": 0.030, "pplx_p7": None, "pplx_full": 0.042}
QUAL = {"replication", "reproduction", "both"}


def main() -> None:
    bought = list(R["pplx_p7"].values())
    COST["pplx_p7"] = 1000 * sum(r["usage"]["usd"] for r in bought) / len(bought)
    print(f"pplx_p7: {len(bought)} calls, {sum(r['usage']['in'] for r in bought)/len(bought):.0f} "
          f"in tok/call, ${COST['pplx_p7']:.4f} per 1k\n")

    print("| gate (discard only if ALL say none) | positives lost | hard neg discarded | "
          "curated neg discarded |\n|---|---|---|---|")
    for g in (["ds4"], ["luna"], ["ds4", "luna"],
              *[[f"{v}<{t}"] for v in ("jev", "pplx_p7") for t in (0.3, 0.5, 0.7)],
              *[[f"{v}<{t}", "luna"] for v in ("jev", "pplx_p7") for t in (0.5, 0.7, 0.8)],
              *[[f"{v}<{t}", "ds4"] for v in ("jev", "pplx_p7") for t in (0.5, 0.7)],
              *[["ds4", "luna", f"{v}<{t}"] for v in ("jev", "pplx_p7") for t in (0.5, 0.7, 0.8)],
              *[[f"pplx_full<{t}", "luna"] for t in (0.3, 0.5)]):
        print(s.row(g))

    print("\nsweep, Luna + X (cascade = Luna on every work, X only when Luna says none):")
    luna_none = sum(s.says_none("luna", i) for i in s.ids) / len(s.ids)
    for v in ("pplx_p7", "pplx_full"):
        for t in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
            print(s.row(["luna", f"{v}<{t}"]) + f" X on every work ${COST['luna']+COST[v]:.3f}/1k,"
                  f" cascade ${COST['luna']+luna_none*COST[v]:.3f}/1k")

    print()
    for v in ("jev", "pplx_p7", "pplx_full"):
        print(s.auc(v))

    both = [i for i in P if s.says_none("ds4", i) and s.says_none("luna", i)]
    print(f"\npositives BOTH ds4 and luna discard: {len(both)}")
    for i in both:
        print(f"  {i} jev={R['jev'][i]['p']:.2f} pplx_p7={s.score('pplx_p7', i):.2f} "
              f"pplx_full={s.score('pplx_full', i):.2f}  {s.cases[i]['title'][:80]}")
    for v in ("pplx_p7<0.5", "pplx_full<0.5"):
        m = [i for i in P if s.says_none(v, i)]
        print(f"positives {v} discards: {len(m)}; ds4 also none {sum(s.says_none('ds4', i) for i in m)}, "
              f"luna also none {sum(s.says_none('luna', i) for i in m)}")

    print("\ntitle-search gate (step 4.6) — works allowed the shortcut:")
    sure = lambda v, i: R[v][i]["classification"] in QUAL and R[v][i]["confident"]  # noqa: E731
    for name, S in (("pos", P), ("hard", H), ("easy", E)):
        line = [f"{name} n={len(S)} today={sum(sure('ds4', i) and sure('luna', i) for i in S)}"]
        for v in ("pplx_p7", "pplx_full"):
            for t in (0.9, 0.95, 0.99):
                line.append(f"luna sure & {v}>={t}: "
                            f"{sum(sure('luna', i) and s.score(v, i) >= t for i in S)}")
        print(" | ".join(line))

    print("\nrecord type, positives where ds4 and luna both give replication/reproduction:")
    agree = [i for i in P if R["ds4"][i]["classification"] in ("replication", "reproduction")]
    split = [i for i in agree if R["luna"][i]["classification"] in ("replication", "reproduction")
             and R["luna"][i]["classification"] != R["ds4"][i]["classification"]]
    print(f"  ds4/luna type splits: {len(split)}; pplx_full sides with luna on "
          f"{sum(R['pplx_full'][i]['choice'] == R['luna'][i]['classification'] for i in split)}, "
          f"with ds4 on {sum(R['pplx_full'][i]['choice'] == R['ds4'][i]['classification'] for i in split)}")


if __name__ == "__main__":
    main()
