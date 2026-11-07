"""Offline (no API calls): every pair and triple of the already-run voters on the 2,186 cases. No API calls."""
import json, itertools
from pathlib import Path
EV = Path(__file__).resolve().parents[1] / "jev_eval_2026-10"  # needs PR #219's out/ files
QUAL = ("replication", "reproduction", "both")
cases = {c["id"]: c for c in json.loads((EV / "cases.json").read_text())}
load = lambda p: {r["id"]: r for r in json.loads((EV / "out" / p).read_text()) if not r.get("error")}
LLM = {"luna": "luna.json", "ds4": "ds4.json"}
DEC = {"span02": "span02_full2.json", "clef": "clef_full2.json", "clefflash": "clefflash_full2.json",
       "drex15": "drex15_full2.json", "tev1": "tev1_full2.json", "kev4b": "kev4b_full2.json",
       "pplx11*": "pplx_full2.json", "pplx12": "pplx12_full2.json", "pplx12f": "pplx12f_full2.json"}
# $ per 1,000 works, from the reports (Luna is OpenAI direct, so unbilled in its file)
USD = {"luna": .070, "ds4": .091, "span02": .022, "clef": .121, "clefflash": .048, "drex15": .083,
       "tev1": .092, "kev4b": .087, "pplx11*": .043, "pplx12": .043, "pplx12f": .021}
# Run 2026-11-03 (run_new.py); their price is read off the provider-reported usage
NEW = {"mammoth1": "mammoth1_full2.json", "elephant1": "elephant1_full2.json", "joey1": "joey1_full2.json",
       "d11": "d11_full2.json", "drex20": "drex20_full2.json", "jev114": "jev114_full2.json", "msd1": "msd1_full2.json",
       "span01": "span01_full2.json", "span01lite": "span01lite_full2.json"}  # Span-01s: yes/no only (run_span01.py)
HERE = Path(__file__).resolve().parent
NEW = {k: f"../../screen_pairs_2026-11/out/{f}" for k, f in NEW.items() if (HERE / "out" / f).exists()}
DEC.update(NEW)
R = {k: load(f) for k, f in {**LLM, **DEC}.items()}
for k in NEW:
    USD[k] = 1000 * sum((r.get("usage") or {}).get("usd") or 0 for r in R[k].values()) / len(R[k])
ids = [i for i in cases if all(i in R[k] for k in R)]
P = [i for i in ids if cases[i]["truth"] == "yes"]
HN = [i for i in ids if cases[i]["truth"] == "no" and cases[i]["source"] in ("human", "screeneval")]
PN = [i for i in ids if cases[i]["truth"] == "no" and i not in set(HN)]
q = lambda k, i: sum(R[k][i]["probs"][c] for c in QUAL)

def says_none(v, i):  # v = "luna" | "ds4" | ("span02", 0.7)
    if isinstance(v, str):
        return R[v][i]["classification"] == "none"
    return q(v[0], i) < v[1]

def name(v): return v if isinstance(v, str) else f"{v[0]}<{v[1]}"
def cost(vs): return sum(USD[v if isinstance(v, str) else v[0]] for v in vs)

_ix = {i: n for n, i in enumerate(ids)}
_mask = lambda S: sum(1 << _ix[i] for i in S)
MP, MH, MN = _mask(P), _mask(HN), _mask(PN)
_none = {}
def none_bits(v):  # bitmask over ids of the works voter v calls none
    if v not in _none:
        _none[v] = _mask(i for i in ids if says_none(v, i))
    return _none[v]

def score(vs, need=None):
    need = need or len(vs)  # discard when at least `need` voters say none (default: unanimous)
    d = 0
    for sub in itertools.combinations(vs, need):
        m = -1
        for v in sub:
            m &= none_bits(v)
        d |= m
    lost = [i for i in P if d >> _ix[i] & 1]
    return len(lost), (d & MH).bit_count() / len(HN), (d & MN).bit_count() / len(PN), lost

TRUTH = json.loads((HERE / "type_truth.json").read_text()) if (HERE / "type_truth.json").exists() else {}

def llm_type(k, i):
    c = R[k][i]["classification"]
    return "replication" if c == "both" else c if c in ("replication", "reproduction") else ""

def dec_type(k, i):  # a type only when one holds >= 0.7 of P(qualifies) (DECISION_TYPE_SHARE)
    p = R[k][i]["probs"]; rep, rp = p["replication"] + p["both"], p["reproduction"]
    if R[k][i].get("notype") or q(k, i) < 0.4 or rep + rp == 0:
        return ""
    t, s = ("reproduction", rp) if rp > rep else ("replication", rep)
    return t if s / (rep + rp) >= 0.7 else ""

def type_score(vs):
    """Production rule (_screen_record_type): the LLM voters in order, first type wins; a decision
    voter naming a DIFFERENT type overrides it (the first such). -> (correct, typed, repros found)."""
    ok = typed = rp = 0
    for i, truth in TRUTH.items():
        if i not in R["luna"]:
            continue
        t = next((llm_type(v, i) for v in vs if isinstance(v, str) and llm_type(v, i)), "")
        for v in vs:
            if not isinstance(v, str) and (d := dec_type(v[0], i)) and d != t:
                t = d
                break
        if t:
            typed += 1; ok += t == truth; rp += t == truth == "reproduction"
    return ok, typed, rp

def strict_ok(vs, i):
    """screen_strict() on the stored votes: every voter qualifying AND confident. A decision
    vote qualifies at q >= its cut-off with one type holding >= 0.7, and is confident at q >= 0.9."""
    for v in vs:
        if isinstance(v, str):
            if not (R[v][i]["classification"] in QUAL and R[v][i]["confident"]):
                return False
        else:
            k, t = v; p = R[k][i]["probs"]; qq = q(k, i); rep = p["replication"] + p["both"]
            if R[k][i].get("notype") or qq < t or qq < 0.9 or max(rep, p["reproduction"]) / qq < 0.7:
                return False
    return True

def precision(vs, S=None):
    """Of the cases in S the gate passes: {class: (passed, of which clear the strict gate)}
    for positives, human-labelled negatives and pipeline-labelled negatives."""
    S = ids if S is None else S
    passed = [i for i in S if not all(says_none(v, i) for v in vs)]
    cls = lambda i: "pos" if cases[i]["truth"] == "yes" else "hn" if cases[i]["source"] in ("human", "screeneval") else "pn"
    out = {}
    for c in ("pos", "hn", "pn"):
        g = [i for i in passed if cls(i) == c]
        out[c] = (len(g), sum(strict_ok(vs, i) for i in g))
    return out

CUT = {"span02": (0.5, 0.7, 0.9), "clef": (0.5, 0.6), "clefflash": (0.6,), "drex15": (0.7,),
       "tev1": (0.6,), "kev4b": (0.5,), "pplx11*": (0.4,), "pplx12": (0.4, 0.6), "pplx12f": (0.4,),
       **{k: (0.3, 0.5, 0.7, 0.9) for k in NEW}}
dec = [(k, t) for k, ts in CUT.items() for t in ts]
voters = ["luna", "ds4"] + dec
rows = []
for n in (1, 2, 3):
    for vs in itertools.combinations(voters, n):
        bases = [v if isinstance(v, str) else v[0] for v in vs]
        if len(set(bases)) < n:
            continue
        info = "text" if any(isinstance(v, str) for v in vs) else "probs only"
        for need in ({n} | ({2} if n == 3 else set())):
            lost, h, p, _ = score(vs, need)
            rows.append((" + ".join(map(name, vs)) + ("" if need == n else " [2-of-3]"), lost, h, p, cost(vs), info))

print(f"cases {len(ids)}: positives {len(P)}, human-labelled negatives {len(HN)}, pipeline-labelled {len(PN)}\n")
print("| gate | lost /%d | human neg disc. | pipeline neg disc. | $/1k | evidence text |" % len(P))
print("|---|--:|--:|--:|--:|---|")
# Pareto front on (lost, -human discard, cost); plus anything named in #219 for reference
def dominated(r):
    return any(o[1] <= r[1] and o[2] >= r[2] and o[4] <= r[4] and (o[1], -o[2], o[4]) != (r[1], -r[2], r[4]) for o in rows)
keep = [r for r in rows if not dominated(r) or r[0] in ("ds4 + luna", "luna + span02<0.7", "luna + clef<0.5")]
for r in sorted(keep, key=lambda r: (r[1], -r[2])):
    print(f"| {r[0]} | {r[1]} | {100*r[2]:.0f}% | {100*r[3]:.0f}% | {r[4]:.3f} | {r[5]} |")
print(f"\n{len(rows)} gates scored, {len(keep)} shown (Pareto on lost/human-discard/cost + references)")
