"""Sweep discard gates over the v32 outputs on the corrected truth labels.

Same two axes as `gate_sweep_v31.py` — missed positives against discard rate on the hard
negatives — but every gate is written over the v3.2 binary `confident` field, and the
family is extended with the qualifying-side gates the binary field makes possible: a
group may discard when one voter is a confident `none` and the other's non-`none` answer
is explicitly unconfident.

v3 is swept alongside on the same labels, reading `confidence == "high"` as `confident`,
so the two prompts are compared like for like. Offline: no API calls. Writes
`gate_sweep_v32.md`.
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
    "flora": ("flora_positive_cases.json", None, "flora_positive"),
    "human": ("human_cases.json", "human_truth_v32.json", "human"),
    "heldout": ("heldout_cases.json", "heldout_truth_v32.json", "heldout"),
}

PENDING = {"F140"}


def load_cases(name: str) -> dict[str, dict]:
    return {c["id"]: c for c in json.loads((HERE / name).read_text())}


def read(tag: str, model: str, stem: str) -> dict[str, dict]:
    f = HERE / f"voter_{tag}_{model.replace('/', '_')}_{stem}.json"
    if not f.exists():
        return {}
    out = {}
    for r in json.loads(f.read_text()):
        bad = bool(r.get("schema_error")) or bool(r.get("error"))
        if "confident" in r:
            raw = r.get("confident")
            confident = raw if isinstance(raw, bool) else None
        else:
            confident = (r.get("confidence") == "high") if r.get("confidence") else None
        out[r["id"]] = {"verdict": None if bad else MAP.get(r.get("classification") or ""),
                        "confident": None if bad else confident, "bad": bad}
    return out


# ---------------------------------------------------------------- gate primitives

def answered(r: Optional[dict]) -> bool:
    return bool(r) and r["verdict"] is not None


def is_none(r: Optional[dict]) -> bool:
    return answered(r) and r["verdict"] == "no"


def is_unclear(r: Optional[dict]) -> bool:
    return answered(r) and r["verdict"] == "unclear"


def is_qual(r: Optional[dict]) -> bool:
    return answered(r) and r["verdict"] == "yes"


def conf(r: Optional[dict]) -> bool:
    return answered(r) and r["confident"] is True


def unconf(r: Optional[dict]) -> bool:
    return answered(r) and r["confident"] is False


def g_strict(recs: list[Optional[dict]]) -> bool:
    return all(answered(r) for r in recs) and all(is_none(r) and conf(r) for r in recs)


def g_allnone_oneconf(recs: list[Optional[dict]]) -> bool:
    return (all(answered(r) for r in recs) and all(is_none(r) for r in recs)
            and any(conf(r) for r in recs))


def g_any(recs: list[Optional[dict]]) -> bool:
    return all(answered(r) for r in recs) and all(is_none(r) for r in recs)


def g_softqual(recs: list[Optional[dict]]) -> bool:
    """All `none` (any confidence), or one confident `none` with every other voter's
    non-`none` answer explicitly unconfident."""
    if not all(answered(r) for r in recs):
        return False
    if all(is_none(r) for r in recs):
        return True
    if not any(is_none(r) and conf(r) for r in recs):
        return False
    return all(is_none(r) or ((is_qual(r) or is_unclear(r)) and unconf(r)) for r in recs)


def g_softqual_strict(recs: list[Optional[dict]]) -> bool:
    """Same, but the unconfident partner must be `unclear` — never `qualifying`."""
    if not all(answered(r) for r in recs):
        return False
    if all(is_none(r) for r in recs):
        return True
    if not any(is_none(r) and conf(r) for r in recs):
        return False
    return all(is_none(r) or (is_unclear(r) and unconf(r)) for r in recs)


def g_trio_maj(recs: list[Optional[dict]]) -> bool:
    nones = [r for r in recs if is_none(r)]
    return len(nones) >= 2 and any(conf(r) for r in nones)


def g_trio_unan(recs: list[Optional[dict]]) -> bool:
    return all(answered(r) for r in recs) and all(is_none(r) for r in recs)


def g_trio_maj_soft(recs: list[Optional[dict]]) -> bool:
    """>=2 `none` with >=1 confident, and the remaining voter is not a confident
    qualifying assertion."""
    if not all(answered(r) for r in recs):
        return False
    nones = [r for r in recs if is_none(r)]
    if len(nones) < 2 or not any(conf(r) for r in nones):
        return False
    return not any(is_qual(r) and conf(r) for r in recs)


def g_trio_maj_soft_strict(recs: list[Optional[dict]]) -> bool:
    """>=2 `none` with >=1 confident, and the remaining voter is `none` or an
    unconfident `unclear`."""
    if not all(answered(r) for r in recs):
        return False
    nones = [r for r in recs if is_none(r)]
    if len(nones) < 2 or not any(conf(r) for r in nones):
        return False
    return all(is_none(r) or (is_unclear(r) and unconf(r)) for r in recs)


# Single-model analogues for the flash-lite-only proxy pool. The qualifying-side clauses
# need a partner, so a soft gate's solo analogue is its confident-`none` half.
def s_strict(r: Optional[dict]) -> bool:
    return is_none(r) and conf(r)


def s_any(r: Optional[dict]) -> bool:
    return is_none(r)


PAIR1 = (FL, GPT)
PAIR2 = (FL, MIN)
TRIO = (FL, GPT, MIN)

Gate = tuple[str, tuple[str, ...], Callable, Optional[Callable], str]

GATES: list[Gate] = [
    ("G-strict", PAIR1, g_strict, s_strict, "both none, both confident"),
    ("G-oneconf", PAIR1, g_allnone_oneconf, s_strict, "both none, >=1 confident"),
    ("G-any", PAIR1, g_any, s_any, "both none, any confidence"),
    ("G-softqual", PAIR1, g_softqual, s_strict,
     "both none, or one none+confident and the other qualifying-or-unclear at "
     "confident=false"),
    ("G-softqual-strict", PAIR1, g_softqual_strict, s_strict,
     "both none, or one none+confident and the other unclear at confident=false"),
    ("G-strict [m]", PAIR2, g_strict, s_strict,
     "flash-lite+ministral: both none, both confident"),
    ("G-oneconf [m]", PAIR2, g_allnone_oneconf, s_strict,
     "flash-lite+ministral: both none, >=1 confident"),
    ("G-any [m]", PAIR2, g_any, s_any,
     "flash-lite+ministral: both none, any confidence"),
    ("G-softqual [m]", PAIR2, g_softqual, s_strict,
     "flash-lite+ministral: both none, or none+confident with an unconfident "
     "qualifying-or-unclear partner"),
    ("G-softqual-strict [m]", PAIR2, g_softqual_strict, s_strict,
     "flash-lite+ministral: both none, or none+confident with an unconfident unclear "
     "partner"),
    ("T-strict", TRIO, g_strict, None, "all 3 none, all confident"),
    ("T-unan", TRIO, g_trio_unan, None, "all 3 none, any confidence"),
    ("T-maj", TRIO, g_trio_maj, None, ">=2 of 3 none, >=1 of those confident"),
    ("T-softqual", TRIO, g_softqual, None,
     "all 3 none, or >=1 none+confident with every other voter unconfident "
     "qualifying-or-unclear"),
    ("T-softqual-strict", TRIO, g_softqual_strict, None,
     "all 3 none, or >=1 none+confident with every other voter an unconfident unclear"),
    ("T-maj-soft", TRIO, g_trio_maj_soft, None,
     ">=2 none (>=1 confident) and no voter asserts qualifying at confident=true"),
    ("T-maj-soft-strict", TRIO, g_trio_maj_soft_strict, None,
     ">=2 none (>=1 confident) and the third is none or an unconfident unclear"),
]


def fires(gate: Gate, data: dict, tag: str, s: str, cid: str) -> bool:
    _, models, fn, _, _ = gate
    return fn([data[(tag, m, s)].get(cid) for m in models])


def pct(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "n/a"


def main() -> None:
    cases = {s: load_cases(cf) for s, (cf, _, _) in SETS.items()}
    truths = {}
    for s, (_, tf, _) in SETS.items():
        truths[s] = ({i: "yes" for i in cases[s]} if tf is None
                     else json.loads((HERE / tf).read_text())["truth"])

    data = {(t, m, s): read(t, m, SETS[s][2])
            for t in ("v3", "v32") for m in (FL, GPT, MIN) for s in SETS}
    proxy_cases = load_cases("coding_v3_cases.json")
    proxy = {t: read(t, FL, "coding_v3") for t in ("v3", "v32")}

    positives = {s: [i for i in cases[s] if truths[s].get(i) == "yes"] for s in SETS}
    hard_neg = [(s, i) for s in ("human", "heldout")
                for i in cases[s] if truths[s].get(i) == "no"]
    n_pos = sum(len(v) for v in positives.values())

    def sweep(tag: str) -> list[dict]:
        rows = []
        for gate in GATES:
            name, models, _, solo, desc = gate
            misses = {s: [i for i in positives[s] if fires(gate, data, tag, s, i)]
                      for s in SETS}
            settled = {s: [i for i in misses[s] if i not in PENDING] for s in SETS}
            pending = sorted({i for s in SETS for i in misses[s] if i in PENDING})
            hn = sum(1 for s, i in hard_neg if fires(gate, data, tag, s, i))
            if solo is None:
                px, pxs = None, "n/a (needs 3 models)"
            else:
                px = sum(1 for i in proxy_cases if solo(proxy[tag].get(i)))
                pxs = f"{pct(px, len(proxy_cases))} ({px}/{len(proxy_cases)})"
            sig = tuple(sorted([f"{s}:{i}" for s in SETS for i in positives[s]
                                if fires(gate, data, tag, s, i)]
                               + [f"{s}:{i}" for s, i in hard_neg
                                  if fires(gate, data, tag, s, i)]))
            rows.append({"name": name, "gate": gate, "desc": desc, "misses": misses,
                         "settled": settled, "pending": pending, "hn": hn,
                         "hn_pct": hn / len(hard_neg), "proxy": pxs, "proxy_n": px,
                         "sig": sig,
                         "nmiss": sum(len(v) for v in settled.values())})
        rows.sort(key=lambda r: (-r["hn"], r["nmiss"]))
        return rows

    sweeps = {t: sweep(t) for t in ("v3", "v32")}

    L: list[str] = ["# Discard-gate sweep — prompt_v32, corrected truth labels", ""]
    L.append("Generated by `gate_sweep_v32.py` from the `voter_v32_*` and `voter_v3_*` "
             "result files in this directory. No API calls. Truth: the corrected labels "
             "`human_truth_v32.json` (60 cases, 13 yes / 47 no), `heldout_truth_v32.json` "
             f"(30, 10 yes / 20 no) and the 300 FLoRA entries in "
             f"`flora_positive_cases.json` (all yes) — {n_pos} positives, "
             f"{len(hard_neg)} hard negatives.")
    L.append("")
    L.append("Every gate is written over the v3.2 binary `confident` field. v3 is swept "
             "alongside with `confidence == \"high\"` read as `confident`, so the same gate "
             "name means the same rule under both prompts.")
    L.append("")
    L.append("These cases are derivation data (see `README.md`), so every number is "
             "in-sample. `schema_error` / `api_error` rows count as *not answered*, so a "
             "model that failed to answer can never contribute to a discard.")
    L.append("")
    L.append("`F140` is an adjudication-pending FLoRA entry and is counted separately from "
             "the settled misses.")
    L.append("")
    L.append("The 150-case proxy pool (`coding_v3_cases.json`, past production discards) was "
             "only run through flash-lite, so its column is a **single-model analogue** of "
             "each gate — for the soft gates, the confident-`none` half, since the "
             "qualifying-side clause needs a partner. It is comparable across prompts in "
             "level, but not to the hard-negative column.")
    L.append("")

    for t in ("v32", "v3"):
        L += [f"## Frontier — {t}", "",
              f"Sorted by hard-negative discard rate.", "",
              f"| gate | voters | missed positives (flora / human / heldout) | pending | "
              f"hard-neg discard ({len(hard_neg)}) | proxy pool, flash-lite analogue (150) |",
              "| --- | --- | --- | --- | --- | --- |"]
        for r in sweeps[t]:
            pair = "+".join(SHORT[m] for m in r["gate"][1])
            m = r["settled"]
            L.append(f"| {r['name']} | {pair} | "
                     f"{len(m['flora'])} / {len(m['human'])} / {len(m['heldout'])} | "
                     f"{', '.join(r['pending']) or '—'} | {r['hn']}/{len(hard_neg)} = "
                     f"{r['hn_pct']:.0%} | {r['proxy']} |")
        L.append("")

    L += ["## Side-by-side: hard-negative discard, v3 -> v32 (same gate, same labels)", "",
          "| gate | v3 discard | v32 discard | delta | v3 settled misses | "
          "v32 settled misses | v3 proxy | v32 proxy |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    by = {t: {r["name"]: r for r in sweeps[t]} for t in ("v3", "v32")}
    for g in GATES:
        a, b = by["v3"][g[0]], by["v32"][g[0]]
        L.append(f"| {g[0]} | {a['hn']}/{len(hard_neg)} = {a['hn_pct']:.0%} "
                 f"| {b['hn']}/{len(hard_neg)} = {b['hn_pct']:.0%} "
                 f"| {b['hn'] - a['hn']:+d} | {a['nmiss']} | {b['nmiss']} "
                 f"| {a['proxy']} | {b['proxy']} |")
    L.append("")

    L += ["Gate definitions:", ""]
    for g in GATES:
        L.append(f"- **{g[0]}** — {g[4]}")
    L.append("")

    # ------------------------------------------------------------ misses detail
    L += ["## Missed positives per gate (v32)", "",
          "| gate | settled misses | adjudication-pending |", "| --- | --- | --- |"]
    for r in sweeps["v32"]:
        settled = ", ".join(f"{s}:{i}" for s in SETS for i in r["settled"][s]) or "none"
        L.append(f"| {r['name']} | {settled} | {', '.join(r['pending']) or 'none'} |")
    L.append("")
    touched = sorted({(t, s, i) for t in ("v3", "v32") for r in sweeps[t]
                      for s in SETS for i in r["misses"][s]})
    L += ["Every positive any gate discards under either prompt:", "",
          "| prompt | set | id | status | title |", "| --- | --- | --- | --- | --- |"]
    for t, s, i in touched:
        title = cases[s][i].get("title", "")[:130].replace("\n", " ").replace("|", "/")
        L.append(f"| {t} | {s} | {i} | {'PENDING' if i in PENDING else 'settled'} "
                 f"| {title} |")
    L.append("")

    # ------------------------------------------------------------ dominance
    L += ["## Identical-behaviour classes, frontier and dominance (v32)", ""]
    classes: dict[tuple, list[dict]] = {}
    for r in sweeps["v32"]:
        classes.setdefault(r["sig"], []).append(r)
    reps = [v[0] | {"members": [x["name"] for x in v]} for v in classes.values()]
    reps.sort(key=lambda r: -r["hn"])

    L.append("Gates that fire on exactly the same cases across all scored rows are one "
             "class:")
    L.append("")
    L += ["| class | gates | hard-neg discard | settled misses |",
          "| --- | --- | --- | --- |"]
    for n, r in enumerate(reps, 1):
        L.append(f"| {n} | {', '.join(r['members'])} | {r['hn']}/{len(hard_neg)} = "
                 f"{r['hn_pct']:.0%} | {r['nmiss']} |")
    L.append("")

    frontier = [r for r in reps
                if not any(b["nmiss"] <= r["nmiss"] and b["hn"] >= r["hn"]
                           and (b["nmiss"] < r["nmiss"] or b["hn"] > r["hn"])
                           for b in reps if b is not r)]
    L.append("**Pareto frontier** (counting only settled misses):")
    L.append("")
    for r in frontier:
        L.append(f"- **{r['members'][0]}** — {r['hn']}/{len(hard_neg)} = "
                 f"{r['hn_pct']:.0%} hard negatives discarded, {r['nmiss']} settled misses.")
    L.append("")
    L.append("**Dominance** (between classes, representative gate named):")
    L.append("")
    dom = []
    for a in reps:
        for b in reps:
            if a is b:
                continue
            if (a["nmiss"] <= b["nmiss"] and a["hn"] >= b["hn"]
                    and (a["nmiss"] < b["nmiss"] or a["hn"] > b["hn"])):
                dom.append(f"- **{a['members'][0]}** dominates **{b['members'][0]}** "
                           f"({a['hn']}/{len(hard_neg)} vs {b['hn']}/{len(hard_neg)} hard "
                           f"negatives; {a['nmiss']} vs {b['nmiss']} settled misses).")
    L += (dom or ["- none."])
    L.append("")

    # ------------------------------------------------------------ zero-miss answer
    zero = [r for r in sweeps["v32"] if r["nmiss"] == 0]
    zero.sort(key=lambda r: -r["hn"])
    L += ["## Best gate subject to ZERO missed settled positives (v32)", ""]
    if not zero:
        floor = min(r["nmiss"] for r in sweeps["v32"])
        atfloor = sorted([r for r in sweeps["v32"] if r["nmiss"] == floor],
                         key=lambda r: -r["hn"])
        common = set.intersection(*[{f"{s}:{i}" for s in SETS for i in r["settled"][s]}
                                    for r in sweeps["v32"]])
        L.append(f"**No swept gate reaches zero settled misses under v32.** Every gate in "
                 f"the family loses the same {len(common)} positive"
                 f"{'s' if len(common) != 1 else ''} — "
                 + ", ".join(sorted(common)) + " — because all three models answer `none` "
                 "at `confident: true` on it, so no combination rule over these three "
                 "answers can keep it. The zero-miss constraint is unreachable without "
                 "changing a voter or the prompt, not by changing the gate.")
        L.append("")
        L.append(f"Subject to the achievable floor of **{floor} settled miss"
                 f"{'es' if floor != 1 else ''}**, the best gate is "
                 f"**{atfloor[0]['name']}** — {atfloor[0]['hn']}/{len(hard_neg)} = "
                 f"{atfloor[0]['hn_pct']:.0%} of the corrected hard negatives discarded"
                 + (f" (adjudication-pending: {', '.join(atfloor[0]['pending'])})"
                    if atfloor[0]["pending"] else "")
                 + f". Proxy-pool flash-lite analogue: {atfloor[0]['proxy']}.")
        L.append("")
        L += ["| gate | hard-neg discard | settled misses | pending | proxy pool |",
              "| --- | --- | --- | --- | --- |"]
        for r in atfloor:
            L.append(f"| {r['name']} | {r['hn']}/{len(hard_neg)} = {r['hn_pct']:.0%} "
                     f"| {r['nmiss']} | {', '.join(r['pending']) or '—'} | {r['proxy']} |")
    else:
        best = zero[0]
        ties = [r["name"] for r in zero if r["hn"] == best["hn"]]
        L.append(f"**{', '.join(ties)}** — {best['hn']}/{len(hard_neg)} = "
                 f"{best['hn_pct']:.0%} of the corrected hard negatives discarded with "
                 f"0 of {n_pos} settled positives lost"
                 + (f" (adjudication-pending: {', '.join(best['pending'])})"
                    if best["pending"] else "")
                 + f". Proxy-pool flash-lite analogue: {best['proxy']}.")
        L.append("")
        L += ["| gate | hard-neg discard | pending | proxy pool |",
              "| --- | --- | --- | --- |"]
        for r in zero:
            L.append(f"| {r['name']} | {r['hn']}/{len(hard_neg)} = {r['hn_pct']:.0%} "
                     f"| {', '.join(r['pending']) or '—'} | {r['proxy']} |")
    L.append("")
    zero3 = [r for r in sweeps["v3"] if r["nmiss"] == 0]
    zero3.sort(key=lambda r: -r["hn"])
    if zero3:
        L.append(f"For comparison, the best zero-settled-miss gate under **v3** on the same "
                 f"labels is **{zero3[0]['name']}** at {zero3[0]['hn']}/{len(hard_neg)} = "
                 f"{zero3[0]['hn_pct']:.0%} (proxy {zero3[0]['proxy']}).")
    else:
        L.append("Under **v3** on the same corrected labels, no swept gate reaches zero "
                 "settled misses.")
    L.append("")

    # ------------------------------------------------------------ leak diagnostic
    for t in ("v32", "v3"):
        for pname, pair in (("flash-lite + gpt-5.4-mini", PAIR1),
                            ("flash-lite + ministral-14b", PAIR2)):
            leaked = [(s, i) for s, i in hard_neg
                      if not g_strict([data[(t, m, s)].get(i) for m in pair])]
            coarse: Counter = Counter()
            pat: Counter = Counter()
            for s, i in leaked:
                recs = [data[(t, m, s)].get(i) for m in pair]

                def lab(r: Optional[dict]) -> str:
                    if not answered(r):
                        return "no-answer"
                    v = {"yes": "qualifying", "no": "none", "unclear": "unclear"}[r["verdict"]]
                    return f"{v}@{'conf' if r['confident'] else 'unconf'}"

                pat[" | ".join(lab(r) for r in recs)] += 1
                a, b = recs
                if is_none(a) and is_none(b):
                    coarse["both none, not both confident"] += 1
                elif (is_none(a) and is_unclear(b)) or (is_none(b) and is_unclear(a)):
                    coarse["one none, one unclear"] += 1
                elif is_unclear(a) and is_unclear(b):
                    coarse["both unclear"] += 1
                elif is_qual(a) and is_qual(b):
                    coarse["both qualifying"] += 1
                else:
                    coarse["exactly one qualifying"] += 1

            L += [f"## Leak diagnostic ({t}, {pname}): hard negatives G-strict does not "
                  f"discard", "",
                  f"{len(leaked)} of the {len(hard_neg)} corrected hard negatives survive "
                  f"G-strict.", "",
                  "| pattern | n | share of pool |", "| --- | --- | --- |"]
            for k, n in coarse.most_common():
                L.append(f"| {k} | {n} | {n / len(hard_neg):.0%} |")
            L.append(f"| **total leaked** | **{len(leaked)}** "
                     f"| **{len(leaked) / len(hard_neg):.0%}** |")
            L.append("")
            L += [f"| {SHORT[pair[0]]} | {SHORT[pair[1]]} | n |", "| --- | --- | --- |"]
            for k, n in pat.most_common():
                L.append(f"| {k} | {n} |")
            L.append("")

    (HERE / "gate_sweep_v32.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
