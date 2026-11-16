"""Score the 390-case screen runs in out_screen/ the way cheap_voter_2026-08.md did.

Positives: human/heldout "yes" + the 300 FLoRA positives, F140 excluded (adjudication
pending) = 319. Hard negatives: human/heldout "no" = 70. The gate is G-unanimous
(shared.llm_client.screen_gate): discard only when every voter says "none". Jev says
"none" when P(yes) < T.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SE = Path(__file__).resolve().parents[1] / "screening_eval"


def truth() -> dict:
    t = {}
    for f in ("human_truth_v32", "heldout_truth_v32"):
        t.update(json.loads((SE / f"{f}.json").read_text(encoding="utf-8"))["truth"])
    for c in json.loads((SE / "flora_positive_cases.json").read_text(encoding="utf-8")):
        if c["id"] != "F140":
            t[c["id"]] = "yes"
    return t


def load(v: str) -> dict:
    p = HERE / "out_screen" / f"{v}.json"
    return {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8"))} if p.exists() else {}


def none_vote(rec: "dict | None", voter: str) -> "bool | None":
    """True/False, or None when the voter did not answer (an incomplete screen)."""
    if not rec or rec.get("error"):
        return None
    if voter.startswith("jev"):
        _, q, t = voter.split(":")
        return rec["p"][q] < float(t)
    return rec.get("classification") == "none"


def gate(voters: list[str], T: dict, runs: dict) -> str:
    pos = [i for i in T if T[i] == "yes"]
    neg = [i for i in T if T[i] == "no"]
    votes = {i: [none_vote(runs[v.split(":")[0]].get(i), v) for v in voters] for i in T}
    complete = {i for i, vs in votes.items() if None not in vs}
    miss = [i for i in pos if i in complete and all(votes[i])]
    disc = [i for i in neg if i in complete and all(votes[i])]
    inc = len(T) - len(complete)
    return (f"{' + '.join(voters):<32} misses {len(miss):>2}/{len(pos)} {miss[:6]}  "
            f"hard-neg discard {len(disc):>2}/{len(neg)} ({100*len(disc)/len(neg):.0f}%)"
            + (f"  incomplete {inc}" if inc else ""))


def main() -> None:
    T = truth()
    runs = {v: load(v) for v in ("ds4", "ds41", "luna", "jev")}
    print({v: len(r) for v, r in runs.items()}, "cases:", len(T))
    pos = [i for i in T if T[i] == "yes"]
    print("\n--- single voters ---")
    for v in ("ds4", "ds41", "luna"):
        r = runs[v]
        if not r:
            continue
        cn = sum(1 for i in pos if r.get(i, {}).get("classification") == "none"
                 and r.get(i, {}).get("confident"))
        print(gate([v], T, runs) + f"  confident-none on positives {cn}")
    for q, ts in (("p7", (0.2, 0.3, 0.5, 0.7)), ("direct", (0.05, 0.1, 0.2))):
        for t in ts:
            print(gate([f"jev:{q}:{t}"], T, runs))
    print("\n--- G-unanimous pairs ---")
    for pair in (["ds4", "luna"], ["ds41", "luna"], ["ds4", "ds41"],
                 ["jev:p7:0.3", "luna"], ["jev:p7:0.5", "luna"], ["jev:direct:0.1", "luna"],
                 ["ds4", "jev:p7:0.5"], ["ds41", "jev:p7:0.5"]):
        print(gate(pair, T, runs))
    print("\n--- hosts that served the DeepSeek votes ---")
    for v in ("ds4", "ds41"):
        hosts: dict = {}
        for r in runs[v].values():
            hosts[r.get("served_by")] = hosts.get(r.get("served_by"), 0) + 1
        print(v, hosts)


if __name__ == "__main__":
    main()
