"""The strict admission gate (#229) under each screen pair -> score_strict_out.txt.

Runs the production `screen_gate`, `screen_strict` and `_decision_vote` over the stored
votes, with no provider call. The eval set over-represents positives, so its shares are
not population shares: part 2 carries them to the release 61f07fd07f19 population
through each stored vote pattern (analysis/rule_gate_2026-10/out), varying the unknown
share of real replications among the works both old voters were sure of.

    .venv/bin/python -m analysis.jev_eval_2026-10.score_strict
"""
import collections
import json
from pathlib import Path

import pandas as pd

from filter.engine.handoff import human_judged_work_ids
from filter.engine.spec import LENIENT, SCREEN_PILES, load_specs
from shared.llm_client import (SCREEN_QUALIFYING, _decision_vote, screen_gate,
                               screen_strict)

EV = Path(__file__).resolve().parent
GATE = EV.parent / "rule_gate_2026-10" / "out"
cases = {c["id"]: c for c in json.loads((EV / "cases.json").read_text())}


def load(name: str) -> dict:
    return {r["id"]: r for r in json.loads((EV / "out" / f"{name}.json").read_text())
            if not r.get("error")}


luna, ds4, span = load("luna"), load("ds4"), load("span02_full2")
ids = [i for i in cases if i in luna and i in ds4 and i in span]
span_vote = {i: _decision_vote({"probs": span[i]["probs"], "model": "span"}) for i in ids}


def old_pair(i: str) -> list[dict]:
    return [ds4[i], luna[i]]


def new_pair(i: str) -> list[dict]:
    return [luna[i], span_vote[i]]


def group(i: str) -> str:
    c = cases[i]
    if c["truth"] == "yes":
        return "positive"
    if c["source"] == "goldneg_curated":
        return "curated negative"
    return "human negative" if c["source"] in ("human", "screeneval") else "pipeline negative"


GROUPS = ("positive", "human negative", "pipeline negative", "curated negative")


def ending(votes: list[dict]) -> str:
    if screen_gate(votes) == "discard":
        return "discard"
    return "admit" if screen_strict(votes) else "defer"


print(f"## 1. Eval set, every rule treated as strict ({len(ids)} works with all three voters)")
print("| group | n | pair | discard | proceed | deferred | deferred/proceed |\n|---|--:|---|--:|--:|--:|--:|")
for g in GROUPS:
    members = [i for i in ids if group(i) == g]
    for label, pair in (("DeepSeek + Luna", old_pair), ("Luna + Span-02", new_pair)):
        n = collections.Counter(ending(pair(i)) for i in members)
        proceed = n["admit"] + n["defer"]
        print(f"| {g} | {len(members)} | {label} | {n['discard']} | {proceed} | {n['defer']} | "
              f"{100 * n['defer'] / proceed:.1f}% |")

print("\n## 1b. Why Luna + Span-02 defers a work (first failing test, in this order)")
print("| group | Luna unqualified or unsure | Span P<0.7 (none) | Span P>=0.9, type share <0.7 (unclear) "
      "| Span 0.7<=P<0.9, unclear | Span 0.7<=P<0.9, typed | total |\n|---|--:|--:|--:|--:|--:|--:|")
for g in GROUPS:
    r = collections.Counter()
    for i in ids:
        if group(i) != g or ending(new_pair(i)) != "defer":
            continue
        s = span_vote[i]
        p = sum(span[i]["probs"][c] for c in SCREEN_QUALIFYING)
        if not (luna[i]["classification"] in SCREEN_QUALIFYING and luna[i]["confident"]):
            r["luna"] += 1
        elif s["classification"] == "none":
            r["none"] += 1
        elif s["classification"] == "unclear":
            r["hi_unclear" if p >= 0.9 else "mid_unclear"] += 1
        else:
            r["mid_typed"] += 1
    print(f"| {g} | {r['luna']} | {r['none']} | {r['hi_unclear']} | {r['mid_unclear']} | "
          f"{r['mid_typed']} | {sum(r.values())} |")

print("\n## 1c. A work the old pair admits that the new pair does not, and the reverse")
print("| group | admit->defer | admit->discard | defer->admit | defer->discard | discard->defer | discard->admit |\n|---|--:|--:|--:|--:|--:|--:|")
for g in GROUPS:
    t = collections.Counter((ending(old_pair(i)), ending(new_pair(i))) for i in ids if group(i) == g)
    print(f"| {g} | {t['admit', 'defer']} | {t['admit', 'discard']} | {t['defer', 'admit']} | "
          f"{t['defer', 'discard']} | {t['discard', 'defer']} | {t['discard', 'admit']} |")

print("\n## 1d. Exploratory, not the agreed design: the same gate if a typed Span-02 vote counted as sure from 0.7")
print("| group | deferred at 0.9 | deferred at 0.7 | admitted at 0.9 | admitted at 0.7 |\n|---|--:|--:|--:|--:|")
def admit_at_07(i: str) -> bool:
    return luna[i]["classification"] in SCREEN_QUALIFYING and luna[i]["confident"] \
        and span_vote[i]["classification"] in SCREEN_QUALIFYING
for g in GROUPS:
    members = [i for i in ids if group(i) == g and ending(new_pair(i)) != "discard"]
    at09 = sum(ending(new_pair(i)) == "admit" for i in members)
    at07 = sum(admit_at_07(i) for i in members)
    print(f"| {g} | {len(members) - at09} | {len(members) - at07} | {at09} | {at07} |")


def pattern(vote: dict) -> str:
    """A stored vote as the only facts the strict gate reads."""
    if vote["classification"] in SCREEN_QUALIFYING:
        return "sure" if vote["confident"] else "unsure"
    return "none" if vote["classification"] == "none" else "unclear"


# Part 2: strict-gated works of the release that hold a DeepSeek + Luna verdict.
specs = {s.id: s for s in load_specs(Path("filter/spec")) if s.pile in SCREEN_PILES}
routing = pd.read_parquet(GATE / "routing.parquet")
screen = pd.read_parquet(GATE / "screen.parquet")
df = routing.merge(screen, on="work_id")
df["votes"] = df.votes.map(json.loads)
df["strict_gate"] = [not any(specs.get(r) and specs[r].admission == LENIENT for r in json.loads(m))
                     for m in df.matched_rules]
df["luna_pair"] = [v[1]["model"] == "gpt-6-luna" for v in df.votes]
exempt = human_judged_work_ids()
pop = df[df.strict_gate & df.luna_pair & ~df.work_id.isin(exempt)].copy()
pop["stratum"] = [(pattern(v[0]), pattern(v[1])) for v in pop.votes]
pop["old"] = [ending(v) for v in pop.votes]


def strata(members: list[str]) -> dict:
    out: dict = collections.defaultdict(collections.Counter)
    for i in members:
        out[(pattern(ds4[i]), pattern(luna[i]))][ending(new_pair(i))] += 1
    return out


POS = [i for i in ids if cases[i]["truth"] == "yes"]
NEG = [i for i in ids if cases[i]["truth"] == "no" and group(i) != "curated negative"]
ALL = POS + NEG
print(f"\n## 2. Strict-gated works of release 61f07fd07f19 with a stored DeepSeek + Luna verdict: {len(pop):,}")
print(f"(left out: {int((df.strict_gate & ~df.luna_pair).sum()):,} strict-gated works whose stored pair is DeepSeek + gpt-5.4-mini, no Luna vote; "
      f"{int((df.strict_gate & df.luna_pair & df.work_id.isin(exempt)).sum()):,} a person has judged (data/human_judged.csv), never deferred)")
old = pop.old.value_counts()
print(f"Stored votes: discard {old.get('discard', 0):,}, admit {old.get('admit', 0):,}, defer {old.get('defer', 0):,}; "
      f"deferred/proceed {100 * old.get('defer', 0) / (old.get('admit', 0) + old.get('defer', 0)):.1f}%")
pos_rates, neg_rates, all_rates = strata(POS), strata(NEG), strata(ALL)


def carried(pi: "float | None") -> collections.Counter:
    """The population's new endings, cell by cell, from the eval rates.

    Only the stratum both DeepSeek and Luna were sure of is mixed by truth: it holds
    54% of the population and 1,409 eval works, and its truth mix is the unknown. Every
    other stratum uses its pooled eval rate, because its cells are too small to split.
    A stratum the eval set never saw stays deferred, its stored ending. *pi* None keeps
    the eval set's own mix (97% positive).
    """
    totals: collections.Counter = collections.Counter()
    for stratum, n in pop.stratum.value_counts().items():
        if stratum == ("sure", "sure") and pi is not None:
            for k in ("discard", "admit", "defer"):
                totals[k] += n * (pi * pos_rates[stratum][k] / sum(pos_rates[stratum].values())
                                  + (1 - pi) * neg_rates[stratum][k] / sum(neg_rates[stratum].values()))
            continue
        cell = all_rates.get(stratum)
        if not cell:
            totals["defer"] += n
            continue
        for k in ("discard", "admit", "defer"):
            totals[k] += n * cell[k] / sum(cell.values())
    return totals


print("\n| share of real replications among works both old voters were sure of | discard | admit | defer | deferred/proceed |\n|---|--:|--:|--:|--:|")
for label, pi in (("eval mix (97%)", None), ("92% (validated strict passes, 466 of 504)", 0.92),
                  ("75%", 0.75), ("60% (about the strict rules' base rate over all their matches; a floor)", 0.60)):
    t = carried(pi)
    print(f"| {label} | {t['discard']:,.0f} | {t['admit']:,.0f} | {t['defer']:,.0f} | "
          f"{100 * t['defer'] / (t['admit'] + t['defer']):.1f}% |")

print("\n| stored DeepSeek, Luna pattern | works in release | stored ending | eval positives: new discard/admit/defer | eval hard negatives: new discard/admit/defer |\n|---|--:|---|---|---|")
fmt = lambda c: f"{c['discard']}/{c['admit']}/{c['defer']}" if c else "–"
for stratum, n in pop.stratum.value_counts().items():
    stored = pop[pop.stratum == stratum].old.iloc[0]
    print(f"| {stratum[0]}, {stratum[1]} | {n:,} | {stored} | {fmt(pos_rates.get(stratum))} | {fmt(neg_rates.get(stratum))} |")

print("\n## 3. All rules: how much of the release the gate would reach")
proceeds = df[df.outcome == "proceed"]
print(f"Proceeds {len(proceeds):,}; strict-gated {int(proceeds.strict_gate.sum()):,}; of those with a Luna pair {int((proceeds.strict_gate & proceeds.luna_pair).sum()):,}")
