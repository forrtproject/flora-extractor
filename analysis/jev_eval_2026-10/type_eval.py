"""Replication vs reproduction: who codes the type best? Truth from human validators.

Truth per positive work: Supabase human validators' type — the pipeline type when they
marked type_check correct, their corrected_type when they changed it to replication or
reproduction, the admin final_type over both; works whose validators disagree are
dropped. Plus prescreen_eval's 28 known reproductions (goldpos_repro).
Caveat: "correct" confirms the type an EARLIER screen wrote, so the truth leans toward
whatever that screen said.
"""
import collections
import json
import sys

import score2 as s

sys.path.insert(0, str(s.HERE.parents[1]))
import shared.config  # noqa: E402,F401 — loads .env before supabase_client reads it
from shared import supabase_client as sb  # noqa: E402
from shared.utils import clean_doi  # noqa: E402

TYPES = ("replication", "reproduction")
R = s.runs


def truth() -> dict:
    q = sb._get("validation_queue", {"select": "record_id,validator_slot,type_check,corrected_type",
                                     "is_validated": "eq.true"})
    u = {r["record_id"]: r for r in sb._get("unvalidated", {
        "select": "record_id,doi_r,title_r,type,final_type"})}
    by_rec: dict = collections.defaultdict(set)
    for r in q:
        if not r["validator_slot"].startswith("human"):
            continue
        rec = u[r["record_id"]]
        t = rec["type"] if r["type_check"] == "correct" else r["corrected_type"]
        by_rec[r["record_id"]].add(t)
    by_key: dict = collections.defaultdict(set)
    for rid, ts in by_rec.items():
        rec = u[rid]
        if rec["final_type"]:
            ts = {rec["final_type"]}
        by_key[clean_doi(rec["doi_r"] or "") or (rec["title_r"] or "").strip().lower()] |= ts
    out = {}
    for i in s.pos:
        c = s.cases[i]
        if i.startswith("goldpos_repro"):
            out[i] = "reproduction"
            continue
        ts = by_key.get(clean_doi(c["doi"] or "") or c["title"].strip().lower())
        if ts and len(ts) == 1 and next(iter(ts)) in TYPES:
            out[i] = next(iter(ts))
    return out


def llm_type(v: str, i: str) -> str:
    c = R[v][i]["classification"]
    return "replication" if c == "both" else c if c in TYPES else ""


def pplx_type(v: str, i: str) -> tuple[str, float]:
    """Type and its share of the qualifying probability (both counts as replication)."""
    p = R[v][i]["probs"]
    rep, repro = p.get("replication", 0) + p.get("both", 0), p.get("reproduction", 0)
    tot = rep + repro or 1
    return ("reproduction", repro / tot) if repro > rep else ("replication", rep / tot)


def report(name: str, pred: dict, T: dict) -> None:
    got = {i: pred[i] for i in T if pred.get(i)}
    ok = sum(got[i] == T[i] for i in got)
    rp = [i for i in T if T[i] == "reproduction"]
    print(f"| {name} | {ok}/{len(got)} ({100*ok/max(len(got),1):.1f}%) | {len(T)-len(got)} "
          f"| {sum(got.get(i) == 'reproduction' for i in rp)}/{len(rp)} "
          f"| {sum(1 for i in got if got[i] == 'reproduction' and T[i] == 'replication')} |")


def main() -> None:
    T = truth()
    print(f"type truth: {len(T)} positives, {collections.Counter(T.values())}\n")
    print("| coder | correct / typed | no type given | reproductions found | replications called reproduction |"
          "\n|---|---|---|---|---|")
    luna = {i: llm_type("luna", i) for i in T}
    ds4 = {i: llm_type("ds4", i) for i in T}
    report("luna", luna, T)
    report("ds4", ds4, T)
    report("today: ds4 first, else luna", {i: ds4[i] or luna[i] for i in T}, T)
    for v in ("pplx_full", "pplx_full2"):
        p = s.HERE / f"out/{v}.json"
        if not p.exists():
            continue
        R[v] = {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8")) if not r.get("error")}
        pt = {i: pplx_type(v, i)[0] for i in T}
        report(v, pt, T)
        for x in (0.7, 0.8, 0.9, 0.95):
            report(f"luna, but {v} overrides when its share >= {x}",
                   {i: (pt[i] if pplx_type(v, i)[1] >= x and pt[i] != luna[i] else luna[i]) or pt[i]
                    for i in T}, T)
    wrong = [i for i in T if luna[i] and luna[i] != T[i]]
    print(f"\nluna wrong on {len(wrong)}:")
    for i in wrong:
        print(f"  {i} truth={T[i]} luna={luna[i]} ds4={ds4[i] or '-'} "
              f"pplx={pplx_type('pplx_full', i)} {s.cases[i]['title'][:70]}")


if __name__ == "__main__":
    main()
