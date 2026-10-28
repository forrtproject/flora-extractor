"""Score out/{ds4,luna,jev}.json against cases.json. Prints the report tables.

Gate is G-unanimous (shared.llm_client.screen_gate): a work is DISCARDED only when every
voter says "none"; Jev says "none" when P(yes) < T. A miss = a positive discarded.
Hard negatives = human-confirmed screen false positives + screeneval "no" +
screen-confirmed negatives; curated negatives are the easy, off-topic ones.
Luna cost is computed from tokens at flex list price (shared/config.py: $0.05/M in,
$0.25/M out; cached input at 10% of input), since OpenAI reports no USD.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LUNA_IN, LUNA_CACHED, LUNA_OUT = 0.05e-6, 0.005e-6, 0.25e-6

cases = {c["id"]: c for c in json.loads((HERE / "cases.json").read_text(encoding="utf-8"))}
runs = {v: {r["id"]: r for r in json.loads((HERE / "out" / f"{v}.json").read_text(encoding="utf-8"))
            if not r.get("error")} for v in ("ds4", "luna", "jev")}
ids = [i for i in cases if all(i in runs[v] for v in runs)]
pos = [i for i in ids if cases[i]["truth"] == "yes"]
hard = [i for i in ids if cases[i]["truth"] == "no" and cases[i]["source"] != "goldneg_curated"]
easy = [i for i in ids if cases[i]["source"] == "goldneg_curated"]


def says_none(v: str, i: str) -> bool:
    if v.startswith("jev"):
        return runs["jev"][i]["p"] < float(v.split("<")[1])
    return runs[v][i]["classification"] == "none"


def row(voters: list[str]) -> str:
    d = lambda S: [i for i in S if all(says_none(v, i) for v in voters)]  # noqa: E731
    miss, dh, de = d(pos), d(hard), d(easy)
    by_src = {}
    for i in miss:
        by_src[cases[i]["source"]] = by_src.get(cases[i]["source"], 0) + 1
    return (f"| {' + '.join(voters)} | {len(miss)} ({100*len(miss)/len(pos):.1f}%) {by_src or ''} "
            f"| {len(dh)}/{len(hard)} ({100*len(dh)/len(hard):.0f}%) "
            f"| {len(de)}/{len(easy)} ({100*len(de)/len(easy):.0f}%) |")


def auc(scores_pos: list[float], scores_neg: list[float]) -> float:
    w = sum((p > n) + 0.5 * (p == n) for p in scores_pos for n in scores_neg)
    return w / (len(scores_pos) * len(scores_neg))


def main() -> None:
    print(f"cases scored: {len(ids)} / {len(cases)}  pos {len(pos)}  hard-neg {len(hard)}  "
          f"curated-neg {len(easy)}\n")
    print("| gate (discard only if ALL say none) | positives lost | hard-neg discarded | "
          "curated-neg discarded |\n|---|---|---|---|")
    for g in (["ds4"], ["luna"], ["jev<0.3"], ["jev<0.5"], ["jev<0.7"],
              ["ds4", "luna"],
              ["jev<0.5", "luna"], ["jev<0.7", "luna"], ["jev<0.8", "luna"],
              ["jev<0.5", "ds4"], ["jev<0.7", "ds4"],
              ["ds4", "luna", "jev<0.5"], ["ds4", "luna", "jev<0.7"], ["ds4", "luna", "jev<0.8"]):
        print(row(g))

    jp = [runs["jev"][i]["p"] for i in pos]
    print(f"\nJev AUC: vs hard neg {auc(jp, [runs['jev'][i]['p'] for i in hard]):.3f}, "
          f"vs curated {auc(jp, [runs['jev'][i]['p'] for i in easy]):.3f}")

    both = [i for i in pos if says_none("ds4", i) and says_none("luna", i)]
    print(f"\npositives BOTH ds4 and luna discard: {len(both)}; "
          f"Jev P on them: {sorted(runs['jev'][i]['p'] for i in both)}")
    jmiss = [i for i in pos if says_none("jev<0.5", i)]
    print(f"positives Jev<0.5 discards: {len(jmiss)}; of those ds4 says none "
          f"{sum(says_none('ds4', i) for i in jmiss)}, luna says none "
          f"{sum(says_none('luna', i) for i in jmiss)}")
    for i in both:
        c = cases[i]
        print(f"  {i} [{c['source']}] P={runs['jev'][i]['p']} {c['title'][:90]}")

    print("\n| voter | calls bought | cache hits | in tok/call | out tok/call | cached-in % | $/call | $ per 1k works |"
          "\n|---|---|---|---|---|---|---|---|")
    for v in ("ds4", "luna", "jev"):
        bought = [r for r in runs[v].values() if r.get("usage")]
        hits = sum(1 for r in runs[v].values() if r.get("cache_hit"))
        n = len(bought) or 1
        tin = sum(r["usage"].get("in", 0) for r in bought)
        tout = sum(r["usage"].get("out", 0) for r in bought)
        tc = sum(r["usage"].get("cached_in", 0) for r in bought)
        usd = (sum(r["usage"].get("usd", 0) or 0 for r in bought) if v != "luna" else
               (tin - tc) * LUNA_IN + tc * LUNA_CACHED + tout * LUNA_OUT)
        print(f"| {v} | {len(bought)} | {hits} | {tin/n:.0f} | {tout/n:.0f} | "
              f"{100*tc/max(tin,1):.0f}% | ${usd/n:.6f} | ${1000*usd/n:.3f} |")


if __name__ == "__main__":
    main()
