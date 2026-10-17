"""Score round 2 (run2.py) beside round 1 (run.py). Prints the report tables.

Voter spec strings:
  ds4 | ds41 | luna      — LLM screen vote; "none" discards
  jev<T                  — round-1 Jev p7 (noul)
  jev_min<T, pplx_min<T  — minimal noul; "none" when P(yes) < T
  jev_full<T, pplx_full<T — full-rules choice; "none" when P(replication+reproduction+both) < T
  cheap                  — qwen AND mistral both answer an explicit "no" (the cheap tier's rule)
A gate discards only when ALL its voters say none (G-unanimous).
Costs: provider-reported USD where given; luna from tokens at flex list price.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LUNA_IN, LUNA_CACHED, LUNA_OUT = 0.05e-6, 0.005e-6, 0.25e-6
QUAL = ("replication", "reproduction", "both")
VOTERS = ("ds4", "ds41", "luna", "jev", "jev_full", "pplx_full", "jev_min", "pplx_min",
          "qwen", "mistral")

cases = {c["id"]: c for c in json.loads((HERE / "cases.json").read_text(encoding="utf-8"))}
runs = {}
for v in VOTERS:
    p = HERE / "out" / f"{v}.json"
    runs[v] = ({r["id"]: r for r in json.loads(p.read_text(encoding="utf-8")) if not r.get("error")}
               if p.exists() else {})
ids = [i for i in cases if all(i in runs[v] for v in VOTERS if runs[v])]
pos = [i for i in ids if cases[i]["truth"] == "yes"]
hard = [i for i in ids if cases[i]["truth"] == "no" and cases[i]["source"] != "goldneg_curated"]
easy = [i for i in ids if cases[i]["source"] == "goldneg_curated"]


def score(v: str, i: str) -> float:
    """P(qualifies) for a decision voter."""
    r = runs[v][i]
    return r["p"] if "p" in r else sum(r["probs"].get(k, 0) for k in QUAL)


def says_none(spec: str, i: str) -> bool:
    if spec == "cheap":
        return runs["qwen"][i].get("verdict") == "no" and runs["mistral"][i].get("verdict") == "no"
    if "<" in spec:
        v, t = spec.split("<")
        return score(v, i) < float(t)
    return runs[spec][i]["classification"] == "none"


def row(gate: list[str]) -> str:
    d = lambda S: [i for i in S if all(says_none(v, i) for v in gate)]  # noqa: E731
    m, dh, de = d(pos), d(hard), d(easy)
    return (f"| {' + '.join(gate)} | {len(m)} ({100*len(m)/len(pos):.1f}%) "
            f"| {len(dh)}/{len(hard)} ({100*len(dh)/len(hard):.0f}%) "
            f"| {len(de)}/{len(easy)} ({100*len(de)/len(easy):.0f}%) |")


def auc(v: str) -> str:
    sp = [score(v, i) for i in pos]
    out = []
    for S in (hard, easy):
        sn = [score(v, i) for i in S]
        out.append(sum((a > b) + 0.5 * (a == b) for a in sp for b in sn) / (len(sp) * len(sn)))
    return f"{v}: AUC vs hard {out[0]:.3f}, vs curated {out[1]:.3f}"


def cost(v: str) -> str:
    rs = list(runs[v].values())
    bought = [r for r in rs if r.get("usage")]
    n = len(bought) or 1
    tin = sum(r["usage"].get("in", 0) for r in bought)
    tout = sum(r["usage"].get("out", 0) for r in bought)
    tc = sum(r["usage"].get("cached_in", 0) for r in bought)
    usd = ((tin - tc) * LUNA_IN + tc * LUNA_CACHED + tout * LUNA_OUT if v == "luna" else
           sum((r["usage"].get("usd") or r["usage"].get("cost") or 0) for r in bought))
    hits = sum(1 for r in rs if r.get("cache_hit"))
    return (f"| {v} | {len(bought)} | {hits} | {tin/n:.0f} | {tout/n:.0f} "
            f"| {100*tc/max(tin, 1):.0f}% | ${1000*usd/n:.3f} |")


def main() -> None:
    print(f"cases {len(ids)}  pos {len(pos)}  hard-neg {len(hard)}  curated-neg {len(easy)}\n")
    print("| gate (discard only if ALL say none) | positives lost | hard neg discarded | "
          "curated neg discarded |\n|---|---|---|---|")
    gates = [["ds4", "luna"], ["ds41", "luna"], ["ds4"], ["ds41"], ["luna"],
             ["jev<0.5"], ["jev<0.8", "luna"]]
    for v in ("jev_full", "pplx_full"):
        gates += [[f"{v}<{t}"] for t in (0.1, 0.3, 0.5)]
        gates += [[f"{v}<{t}", "luna"] for t in (0.3, 0.5, 0.8)]
        gates += [[f"{v}<{t}", "ds4"] for t in (0.5, 0.8)]
    for v in ("jev_min", "pplx_min"):
        gates += [[f"{v}<{t}"] for t in (0.05, 0.1, 0.2, 0.3, 0.5)]
        gates += [[f"{v}<{t}", "luna"] for t in (0.5, 0.8)]
    gates += [["cheap"], ["cheap", "jev_min<0.3"]]
    for g in gates:
        print(row(g))

    print()
    for v in ("jev", "jev_full", "pplx_full", "jev_min", "pplx_min"):
        print(auc(v))

    print("\ncategorical type on positives where ds4 and luna agree on a type:")
    agree = [i for i in pos if runs["ds4"][i]["classification"] in ("replication", "reproduction")
             and runs["ds4"][i]["classification"] == runs["luna"][i]["classification"]]
    repro = [i for i in agree if runs["luna"][i]["classification"] == "reproduction"]
    for v in ("jev_full", "pplx_full"):
        same = sum(runs[v][i]["choice"] == runs["luna"][i]["classification"] for i in agree)
        print(f"  {v}: same type {same}/{len(agree)}; reproductions recognised "
              f"{sum(runs[v][i]['choice'] == 'reproduction' for i in repro)}/{len(repro)}")
        print(f"  {v} choice distribution: "
              f"{ {k: sum(runs[v][i]['choice'] == k for i in ids) for k in ('replication', 'reproduction', 'both', 'none', 'unclear')} }")

    print("\n| voter | calls bought | cache hits | in tok/call | out tok/call | cached-in % | $ per 1k |"
          "\n|---|---|---|---|---|---|---|")
    for v in VOTERS:
        if runs[v]:
            print(cost(v))


if __name__ == "__main__":
    main()
