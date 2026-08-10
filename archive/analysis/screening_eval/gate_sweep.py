"""Sweep candidate discard gates over the v3 screening outputs and write gate_sweep.md.

Offline only: reads the voter_v3_* result files already in this directory and the truth
files, then scores each candidate gate on two axes — missed true positives (the costly
error) and discard rate on negatives (throughput). Negatives come in two flavours: the 74
hard negatives of the human + held-out sets (both models present, so pair gates run) and
the 150-case proxy pool of past production discards (flash-lite only, so only a
single-model analogue of each gate is computable there).

Everything here is derivation data — see README.md — so these are in-sample numbers.
"""
import json
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent

FL = "gemini-3.5-flash-lite"
GPT = "gpt-5.4-mini"
MIN = "mistralai/ministral-14b-2512"
SHORT = {FL: "flash-lite", GPT: "gpt-5.4-mini", MIN: "ministral-14b"}

MAP = {"replication": "yes", "reproduction": "yes", "both": "yes",
       "none": "no", "unclear": "unclear"}

SETS = {
    "flora": ("flora_positive_cases.json", None),
    "human": ("human_cases.json", "human_truth_revised.json"),
    "heldout": ("heldout_cases.json", "heldout_truth.json"),
}

# Categories the v3 prompt treats as clear distractors: a paper tagged only with these is
# not a borderline replication, so the gate can act without demanding high confidence.
CLEAR = {"terminology_only", "tool_benchmark", "initial_validation", "about_replication",
         "builds_on_literature", "other"}

# F140 is an adjudication-pending FLoRA entry (a methods-transfer paper); counted apart
# from the settled misses everywhere it appears.
PENDING = {"F140"}


def load_cases(name: str) -> dict[str, dict]:
    return {c["id"]: c for c in json.loads((HERE / name).read_text())}


def read_v3(model: str, stem: str) -> dict[str, dict]:
    f = HERE / f"voter_v3_{model.replace('/', '_')}_{stem}.json"
    if not f.exists():
        return {}
    out = {}
    for r in json.loads(f.read_text()):
        bad = bool(r.get("schema_error")) or bool(r.get("error"))
        verdict = None if bad else MAP.get(r.get("classification") or "")
        out[r["id"]] = {"verdict": verdict, "conf": None if bad else r.get("confidence"),
                        "cats": set(r.get("categories") or []), "bad": bad}
    return out


# ---------------------------------------------------------------- gate primitives

def is_none(r: Optional[dict]) -> bool:
    return bool(r) and r["verdict"] == "no"


def conf(r: Optional[dict]) -> Optional[str]:
    return r["conf"] if r else None


def both_none(a: dict, b: dict) -> bool:
    return is_none(a) and is_none(b)


def g_strict(a: dict, b: dict) -> bool:
    return both_none(a, b) and conf(a) == "high" and conf(b) == "high"


def g_onehigh(a: dict, b: dict) -> bool:
    return both_none(a, b) and (conf(a) == "high" or conf(b) == "high")


def g_any(a: dict, b: dict) -> bool:
    return both_none(a, b)


def g_notlow(a: dict, b: dict) -> bool:
    return both_none(a, b) and conf(a) != "low" and conf(b) != "low"


def _unclear(r: Optional[dict]) -> bool:
    return bool(r) and r["verdict"] == "unclear"


def g_unclear_tol(a: dict, b: dict) -> bool:
    if both_none(a, b):
        return True
    if is_none(a) and conf(a) == "high" and _unclear(b):
        return True
    if is_none(b) and conf(b) == "high" and _unclear(a):
        return True
    return False


def g_cat(a: dict, b: dict) -> bool:
    if both_none(a, b):
        union = (a["cats"] | b["cats"])
        if union and union <= CLEAR:
            return True
    return g_strict(a, b)


def g_cat2(a: dict, b: dict) -> bool:
    if both_none(a, b) and (a["cats"] & b["cats"] & CLEAR):
        return True
    return g_strict(a, b)


def g_cat_anyhigh(a: dict, b: dict) -> bool:
    """Post-hoc hybrid: G-onehigh, plus a clear-distractor exemption from the high bar."""
    if g_onehigh(a, b):
        return True
    if both_none(a, b):
        union = a["cats"] | b["cats"]
        if union and union <= CLEAR:
            return True
    return False


def g_onenone_tol(a: dict, b: dict) -> bool:
    """Post-hoc: one voter none@high, the other not asserting a qualifying study.

    Aimed at the verdict axis rather than the confidence axis: it tolerates a partner that
    is `unclear`, or `none` at any confidence, but never one that answers qualifying.
    """
    for x, y in ((a, b), (b, a)):
        if is_none(x) and conf(x) == "high" and (is_none(y) or _unclear(y)):
            return True
    return False


def g_trio_nohighyes(recs: list[Optional[dict]]) -> bool:
    """Post-hoc trio: someone says none@high and nobody asserts qualifying at high."""
    if any(r is None for r in recs):
        return False
    if not any(is_none(r) and conf(r) == "high" for r in recs):
        return False
    return not any(r["verdict"] == "yes" and r["conf"] == "high" for r in recs)


def g_trio_maj(recs: list[Optional[dict]]) -> bool:
    nones = [r for r in recs if is_none(r)]
    return len(nones) >= 2 and any(conf(r) == "high" for r in nones)


def g_trio_unan(recs: list[Optional[dict]]) -> bool:
    return len(recs) == 3 and all(is_none(r) for r in recs)


# Solo (flash-lite-only) analogues, for the proxy pool.
def s_high(r: Optional[dict]) -> bool:
    return is_none(r) and conf(r) == "high"


def s_any(r: Optional[dict]) -> bool:
    return is_none(r)


def s_notlow(r: Optional[dict]) -> bool:
    return is_none(r) and conf(r) != "low"


def s_cat(r: Optional[dict]) -> bool:
    if is_none(r) and r["cats"] and r["cats"] <= CLEAR:
        return True
    return s_high(r)


def s_cat2(r: Optional[dict]) -> bool:
    if is_none(r) and (r["cats"] & CLEAR):
        return True
    return s_high(r)


# ---------------------------------------------------------------- gate registry

PAIR1 = (FL, GPT)
PAIR2 = (FL, MIN)

Gate = tuple[str, tuple[str, ...], Callable, Optional[Callable], str]

GATES: list[Gate] = [
    ("G-strict (current)", PAIR1, g_strict, s_high, "both none, both high"),
    ("G-onehigh", PAIR1, g_onehigh, s_high, "both none, >=1 high"),
    ("G-any", PAIR1, g_any, s_any, "both none, any confidence"),
    ("G-notlow", PAIR1, g_notlow, s_notlow, "both none, neither low"),
    ("G-unclear-tol", PAIR1, g_unclear_tol, s_any,
     "both none (any conf), or one none@high + other unclear"),
    ("G-cat", PAIR1, g_cat, s_cat,
     "both none + union of categories inside the clear-distractor set; else G-strict"),
    ("G-cat2", PAIR1, g_cat2, s_cat2,
     "both none + shared clear-distractor tag; else G-strict"),
    ("G-trio-maj", (FL, GPT, MIN), g_trio_maj, None, ">=2 of 3 none, >=1 of those high"),
    ("G-trio-unan", (FL, GPT, MIN), g_trio_unan, None, "all 3 none, any confidence"),
    ("G-onehigh [m]", PAIR2, g_onehigh, s_high, "flash-lite+ministral: both none, >=1 high"),
    ("G-any [m]", PAIR2, g_any, s_any, "flash-lite+ministral: both none, any confidence"),
    ("G-notlow [m]", PAIR2, g_notlow, s_notlow, "flash-lite+ministral: both none, neither low"),
    ("G-unclear-tol [m]", PAIR2, g_unclear_tol, s_any,
     "flash-lite+ministral: both none, or none@high + unclear"),
    ("G-cat-anyhigh (post-hoc)", PAIR1, g_cat_anyhigh, s_cat,
     "G-onehigh, or both none with all categories in the clear-distractor set"),
    ("G-onenone-tol (post-hoc)", PAIR1, g_onenone_tol, s_high,
     "one voter none@high, the other none (any conf) or unclear — never qualifying"),
    ("G-trio-nohighyes (post-hoc)", (FL, GPT, MIN), g_trio_nohighyes, None,
     ">=1 of 3 none@high and none of the three asserts qualifying at high"),
]


def fires(gate: Gate, data: dict, s: str, cid: str) -> bool:
    _, models, fn, _, _ = gate
    recs = [data[(m, s)].get(cid) for m in models]
    if len(models) == 3:
        return fn(recs)
    a, b = recs
    if a is None or b is None:
        return False
    return fn(a, b)


def pct(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "n/a"


def main() -> None:
    cases = {s: load_cases(cf) for s, (cf, _) in SETS.items()}
    truths = {}
    for s, (cf, tf) in SETS.items():
        truths[s] = ({i: "yes" for i in cases[s]} if tf is None
                     else json.loads((HERE / tf).read_text())["truth"])

    stem = {"flora": "flora_positive", "human": "human", "heldout": "heldout"}
    data = {(m, s): read_v3(m, stem[s]) for m in (FL, GPT, MIN) for s in SETS}
    proxy_cases = load_cases("coding_v3_cases.json")
    proxy = read_v3(FL, "coding_v3")

    positives = {s: [i for i in cases[s] if truths[s].get(i) == "yes"] for s in SETS}
    hard_neg = [(s, i) for s in ("human", "heldout")
                for i in cases[s] if truths[s].get(i) == "no"]

    rows = []
    for gate in GATES:
        name, models, _, solo, desc = gate
        misses = {s: [i for i in positives[s] if fires(gate, data, s, i)] for s in SETS}
        settled = {s: [i for i in misses[s] if i not in PENDING] for s in SETS}
        pending = sorted({i for s in SETS for i in misses[s] if i in PENDING})
        hn = sum(1 for s, i in hard_neg if fires(gate, data, s, i))
        if solo is None:
            px, pxs = None, "n/a (needs 3 models)"
        else:
            px = sum(1 for i in proxy_cases if solo(proxy.get(i)))
            pxs = f"{pct(px, len(proxy_cases))} ({px}/{len(proxy_cases)})"
        sig = tuple(sorted([f"{s}:{i}" for s in SETS for i in positives[s]
                            if fires(gate, data, s, i)]
                           + [f"{s}:{i}" for s, i in hard_neg if fires(gate, data, s, i)]))
        rows.append({"name": name, "desc": desc, "misses": misses, "settled": settled,
                     "pending": pending, "hn": hn, "hn_pct": hn / len(hard_neg),
                     "proxy": pxs, "proxy_n": px, "sig": sig})

    rows.sort(key=lambda r: (-r["hn"], sum(len(v) for v in r["settled"].values())))

    L: list[str] = ["# Discard-gate sweep — v3 screening prompt", ""]
    L.append("Generated by `gate_sweep.py` from the `voter_v3_*` result files in this "
             "directory. No API calls. Truth: `human_truth_revised.json` (60 cases, 10 yes), "
             "`heldout_truth.json` (30, 6 yes) and the 300 FLoRA entries in "
             "`flora_positive_cases.json` (all yes) — 316 positives, 74 hard negatives.")
    L.append("")
    L.append("These cases are derivation data (see `README.md`), so every number below is "
             "in-sample. `schema_error` / `api_error` rows count as *not* `none`, so a model "
             "that failed to answer can never contribute to a discard.")
    L.append("")
    L.append("`F140` (\"Back to the future: what would the post-2015 global development goals "
             "look like if we replicated methods used…\") is an adjudication-pending FLoRA "
             "entry and is counted separately from the settled misses.")
    L.append("")
    L.append("The 150-case proxy pool (`coding_v3_cases.json`, past production discards) was "
             "only run through flash-lite, so its column reports a **single-model analogue** "
             "of each gate — the same verdict/confidence/category conditions applied to "
             "flash-lite alone. It is not the pair gate and is not comparable to the "
             "hard-negative column in level, only in ordering.")
    L.append("")

    L += ["## Frontier", "",
          "| gate | pair | missed positives (flora / human / heldout) | pending | "
          "hard-neg discard (74) | proxy pool, flash-lite analogue (150) |",
          "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        gate = next(g for g in GATES if g[0] == r["name"])
        pair = "+".join(SHORT[m] for m in gate[1])
        m = r["settled"]
        miss = f"{len(m['flora'])} / {len(m['human'])} / {len(m['heldout'])}"
        pend = ", ".join(r["pending"]) or "—"
        L.append(f"| {r['name']} | {pair} | {miss} | {pend} | {r['hn']}/74 = "
                 f"{r['hn_pct']:.0%} | {r['proxy']} |")
    L.append("")

    L += ["Gate definitions:", ""]
    for g in GATES:
        L.append(f"- **{g[0]}** — {g[4]}")
    L.append("")
    L.append("Clear-distractor category set: " + ", ".join(f"`{c}`" for c in sorted(CLEAR))
             + ". Gray-zone categories (`clearly_declared`, `self_retest`, "
               "`measurement_validation`, `context_transfer`, `incidental_finding`) are "
               "outside it, so a case carrying one of them never qualifies for a "
               "category exemption.")
    L.append("")

    L += ["## Missed positives per gate", "",
          "| gate | settled misses | adjudication-pending |", "| --- | --- | --- |"]
    for r in rows:
        settled = ", ".join(f"{s}:{i}" for s in SETS for i in r["settled"][s]) or "none"
        L.append(f"| {r['name']} | {settled} | {', '.join(r['pending']) or 'none'} |")
    L.append("")
    touched = sorted({(s, i) for r in rows for s in SETS for i in r["misses"][s]})
    L += ["Every case any gate discards, with its title:", "",
          "| set | id | status | title |", "| --- | --- | --- | --- |"]
    for s, i in touched:
        title = cases[s][i].get("title", "")[:130].replace("\n", " ").replace("|", "/")
        L.append(f"| {s} | {i} | {'PENDING' if i in PENDING else 'settled'} | {title} |")
    L.append("")

    # ------------------------------------------------------------ dominance
    L += ["## Reading", ""]
    classes: dict[tuple, list[dict]] = {}
    for r in rows:
        classes.setdefault(r["sig"], []).append(r)
    reps = [v[0] | {"members": [x["name"] for x in v]} for v in classes.values()]
    reps.sort(key=lambda r: -r["hn"])

    L.append("**Identical-behaviour classes.** Several gates fire on exactly the same cases "
             "across all 390 scored rows, because in the v3 output almost every `none` "
             "verdict already carries `high` confidence — so relaxing the confidence bar "
             "changes nothing. Grouping gates by their discard set:")
    L.append("")
    L += ["| class | gates | hard-neg discard | settled misses |", "| --- | --- | --- | --- |"]
    for n, r in enumerate(reps, 1):
        rm = sum(len(v) for v in r["settled"].values())
        L.append(f"| {n} | {', '.join(r['members'])} | {r['hn']}/74 = {r['hn_pct']:.0%} | {rm} |")
    L.append("")

    def nmiss(r: dict) -> int:
        return sum(len(v) for v in r["settled"].values())

    frontier = [r for r in reps
                if not any(nmiss(b) <= nmiss(r) and b["hn"] >= r["hn"]
                           and (nmiss(b) < nmiss(r) or b["hn"] > r["hn"]) for b in reps
                           if b is not r)]
    L.append("**Pareto frontier** (no other class is at least as good on both axes, counting "
             "only settled misses):")
    L.append("")
    for r in frontier:
        L.append(f"- **{r['members'][0]}** — {r['hn']}/74 = {r['hn_pct']:.0%} hard negatives "
                 f"discarded, {nmiss(r)} settled misses.")
    L.append("")
    L.append("**Dominance** (between classes, representative gate named):")
    L.append("")
    dom = []
    for a in reps:
        for b in reps:
            if a is b:
                continue
            if nmiss(a) <= nmiss(b) and a["hn"] >= b["hn"] and (nmiss(a) < nmiss(b)
                                                               or a["hn"] > b["hn"]):
                dom.append(f"- **{a['members'][0]}** dominates **{b['members'][0]}** "
                           f"({a['hn']}/74 vs {b['hn']}/74; {nmiss(a)} vs {nmiss(b)} "
                           f"settled misses).")
    L += (dom or ["- none."])
    L.append("")
    L.append("**The confidence axis is inert under v3.** flash-lite emits `high` on every "
             "`none` verdict in all four sets (its only non-`high` outputs are single "
             "`unclear@low` cases), and gpt-5.4-mini nearly so, which is why G-strict, "
             "G-onehigh, G-any and G-notlow are one class rather than a ladder — and why "
             "the two category gates, which fall back to G-strict, also collapse into it. "
             "On the proxy pool all 121 flash-lite `none` verdicts are `high`, so every "
             "single-model analogue reports the same 81% (the one `unclear@low` case is the "
             "only row any of them could differ on).")
    L.append("")
    L.append("What separates the classes is therefore the verdict axis, not confidence: "
             "class 2 adds the two cases where a voter answered `unclear`, and class 1 "
             "(G-trio-maj) adds six more by letting ministral-14b outvote a single "
             "qualifying verdict.")
    L.append("")
    L.append("Every swept gate misses zero *settled* positives out of 316, so on this data "
             "the axes do not trade off within the swept family: the ranking on hard-negative "
             "discard is a total order and the top class dominates all others. The one "
             "positive any gate touches is the adjudication-pending F140, and every gate "
             "including the current one discards it.")
    L.append("")

    # ------------------------------------------------------------ leak diagnostic
    leaked = [(s, i) for s, i in hard_neg
              if not g_strict(data[(FL, s)][i], data[(GPT, s)][i])]
    pat = Counter()
    coarse = Counter()
    for s, i in leaked:
        a, b = data[(FL, s)][i], data[(GPT, s)][i]

        def lab(r: dict) -> str:
            v = r["verdict"] or "no-answer"
            v = {"yes": "qualifying", "no": "none", "unclear": "unclear"}.get(v, v)
            return f"{v}@{r['conf']}" if r["conf"] else v

        pat[f"flash-lite {lab(a)} | gpt-5.4-mini {lab(b)}"] += 1
        if both_none(a, b):
            coarse["both none, not both high"] += 1
        elif (is_none(a) and _unclear(b)) or (is_none(b) and _unclear(a)):
            coarse["one none, one unclear"] += 1
        elif _unclear(a) and _unclear(b):
            coarse["both unclear"] += 1
        elif a["verdict"] == "yes" and b["verdict"] == "yes":
            coarse["both qualifying"] += 1
        else:
            coarse["exactly one qualifying"] += 1

    L += ["## Leak diagnostic: the hard negatives G-strict does not discard", "",
          f"{len(leaked)} of the 74 hard negatives survive G-strict. Coarse pattern:", "",
          "| pattern | n | share of the 74 |", "| --- | --- | --- |"]
    for k, n in coarse.most_common():
        L.append(f"| {k} | {n} | {n / 74:.0%} |")
    L.append(f"| **total leaked** | **{len(leaked)}** | **{len(leaked) / 74:.0%}** |")
    L.append("")
    L.append("Full joint distribution of the pair's verdict+confidence on those cases:")
    L.append("")
    L += ["| flash-lite | gpt-5.4-mini | n |", "| --- | --- | --- |"]
    for k, n in pat.most_common():
        a, b = k.split(" | ")
        L.append(f"| {a.replace('flash-lite ', '')} | {b.replace('gpt-5.4-mini ', '')} | {n} |")
    L.append("")

    (HERE / "gate_sweep.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
