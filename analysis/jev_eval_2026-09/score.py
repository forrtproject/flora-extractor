"""Score the pre-screen runs in out/: single voters, AND-pairs, Jev thresholds, error overlap.

Same rules as analysis/prescreen_eval/score_prescreen.py (imported): only an explicit
parsed "no" discards; the shipped bypasses (regex, short, curated) run first; rows
Stage 2 kills are excluded. Jev says "no" when P(yes) < T.
"""
import json
import sys
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL = Path(__file__).resolve().parents[1] / "prescreen_eval"
sys.path.insert(0, str(EVAL.parents[1]))
sys.path.insert(0, str(EVAL))
from score_prescreen import bypasses_for, split_of, wilson  # noqa: E402

SHIPPED = {"regex", "short", "curated"}
CHAT = {"qwen": "qwen_qwen3-30b-a3b-instruct-2507",
        "mistral": "mistralai_mistral-small-24b-instruct-2501",
        "gpt5nano": "openai_gpt-5-nano",
        "gpt41nano": "openai_gpt-4.1-nano"}
BUCKETS = {"goldneg_screen": False, "goldneg_curated": False,
           "goldpos_flora": True, "goldpos_repro": True}


def load() -> tuple[dict, dict]:
    cases, votes = {}, {}
    for b, pos in BUCKETS.items():
        for c in json.loads((EVAL / f"cases_live_{b}.json").read_text(encoding="utf-8")):
            if c.get("filter_status") != "false_positive":
                cases[c["id"]] = {**c, "_pos": pos, "_bucket": b}
        for name, stem in {**CHAT, "jev": "jev"}.items():
            p = HERE / "out" / f"{stem}_{b}.json"
            if p.exists():
                for r in json.loads(p.read_text(encoding="utf-8")):
                    votes.setdefault(name, {})[r["id"]] = r
    return cases, votes


def says_no(rec: "dict | None", voter: str) -> bool:
    if not rec or rec.get("error"):
        return False
    if voter.startswith("jev"):
        q, t = voter.split(":")[1], float(voter.split(":")[2])
        p = (rec.get("p") or {}).get(q)
        return p is not None and p < t
    return rec.get("verdict") == "no"


def score(cases: dict, votes: dict, voters: list[str], split: str = "all") -> dict:
    ids = [i for i in cases if split == "all" or split_of(i) == split]
    ids = [i for i in ids if all(i in votes[v.split(":")[0]] for v in voters)]
    out = {"n_neg_screen": 0, "neg_screen": 0, "n_neg_cur": 0, "neg_cur": 0,
           "n_pos": 0, "pos_raw": 0, "pos": 0, "lost": []}
    for i in ids:
        c = cases[i]
        no = all(says_no(votes[v.split(":")[0]].get(i), v) for v in voters)
        discard = no and not (bypasses_for(c) & SHIPPED)
        if c["_pos"]:
            out["n_pos"] += 1
            out["pos_raw"] += no
            out["pos"] += discard
            if discard:
                out["lost"].append(i)
        elif c["_bucket"] == "goldneg_screen":
            out["n_neg_screen"] += 1
            out["neg_screen"] += discard
        else:
            out["n_neg_cur"] += 1
            out["neg_cur"] += discard
    return out


def line(label: str, s: dict) -> str:
    lo, hi = wilson(s["pos"], s["n_pos"])
    return (f"{label:<34} screen-neg {s['neg_screen']:>3}/{s['n_neg_screen']:<3} "
            f"{100*s['neg_screen']/max(1,s['n_neg_screen']):>3.0f}%  curated-neg "
            f"{s['neg_cur']:>3}/{s['n_neg_cur']:<3} {100*s['neg_cur']/max(1,s['n_neg_cur']):>3.0f}%  "
            f"pos lost raw {s['pos_raw']:>2} / after bypass {s['pos']:>2} of {s['n_pos']} "
            f"(CI {100*hi:.1f}%)")


def auc(cases: dict, rec: dict, q: str, neg_bucket: str) -> float:
    pos = [rec[i]["p"][q] for i in rec if cases.get(i, {}).get("_pos") and not rec[i].get("error")]
    neg = [rec[i]["p"][q] for i in rec if cases.get(i, {}).get("_bucket") == neg_bucket
           and not rec[i].get("error")]
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / max(1, len(pos) * len(neg))


def cost(votes: dict) -> None:
    print("\n--- cost (OpenRouter-reported, all 4 buckets) ---")
    for v, recs in votes.items():
        usd = sum(((r.get("usage") or {}).get("cost") or 0) for r in recs.values())
        tin = sum(((r.get("usage") or {}).get("in") or (r.get("usage") or {}).get("input_tokens") or 0)
                  for r in recs.values())
        print(f"{v:<10} rows {len(recs):>5}  ${usd:.4f}  ${1000*usd/max(1,len(recs)):.4f}/1k rows  "
              f"{tin/max(1,len(recs)):.0f} in-tokens/row  errors "
              f"{sum(1 for r in recs.values() if r.get('error'))}")


def main() -> None:
    cases, votes = load()
    print("voters loaded:", {v: len(r) for v, r in votes.items()})
    print("\n--- single voters (after the shipped bypasses) ---")
    for v in CHAT:
        if v in votes:
            print(line(v, score(cases, votes, [v])))
    if "jev" in votes:
        for q in ("p7", "direct"):
            print(f"  jev {q}: AUC vs screen-neg {auc(cases, votes['jev'], q, 'goldneg_screen'):.3f}"
                  f"  vs curated-neg {auc(cases, votes['jev'], q, 'goldneg_curated'):.3f}")
            for t in (0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50):
                print(line(f"jev {q} P<{t:.2f}", score(cases, votes, [f"jev:{q}:{t}"])))

    print("\n--- AND pairs ---")
    pool = [v for v in CHAT if v in votes] + (
        ["jev:p7:0.10", "jev:p7:0.20", "jev:direct:0.05", "jev:direct:0.10"] if "jev" in votes else [])
    for a, b in combinations(pool, 2):
        if a.startswith("jev") and b.startswith("jev"):
            continue
        print(line(f"{a} + {b}", score(cases, votes, [a, b])))

    print("\n--- error overlap on gold positives (raw 'no', before bypass) ---")
    pos_ids = [i for i in cases if cases[i]["_pos"]]
    for a, b in combinations(pool, 2):
        if a.startswith("jev") and b.startswith("jev"):
            continue
        va, vb = votes[a.split(":")[0]], votes[b.split(":")[0]]
        A = {i for i in pos_ids if says_no(va.get(i), a)}
        B = {i for i in pos_ids if says_no(vb.get(i), b)}
        print(f"{a:<18} {len(A):>3} no  {b:<18} {len(B):>3} no  both {len(A & B):>2}")
    print("\n--- agreement on screen-confirmed negatives (both 'no' vs independence) ---")
    neg_ids = [i for i in cases if cases[i]["_bucket"] == "goldneg_screen"]
    for a, b in combinations(pool, 2):
        if a.startswith("jev") and b.startswith("jev"):
            continue
        va, vb = votes[a.split(":")[0]], votes[b.split(":")[0]]
        A = {i for i in neg_ids if says_no(va.get(i), a)}
        B = {i for i in neg_ids if says_no(vb.get(i), b)}
        n = len(neg_ids)
        print(f"{a:<18} {len(A)/n:>4.0%}  {b:<18} {len(B)/n:>4.0%}  both {len(A & B)/n:>4.0%} "
              f"(independent {len(A)/n*len(B)/n:>4.0%})")
    cost(votes)

    if "jev" in votes:
        print("\n--- Jev threshold chosen on dev (max discard, 0 positives lost), reported on test ---")
        for q in ("p7", "direct"):
            best = max((t for t in [x / 100 for x in range(1, 60)]
                        if score(cases, votes, [f"jev:{q}:{t}"], "dev")["pos"] == 0),
                       default=None)
            if best is not None:
                print(line(f"jev {q} dev-picked P<{best:.2f} [test]",
                           score(cases, votes, [f"jev:{q}:{best}"], "test")))


if __name__ == "__main__":
    main()
