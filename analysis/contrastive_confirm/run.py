"""Contrastive confirmation of the reference-list pick (handover step 5), offline.

The shipped keyed-record check (`confirm_keyed_original`) sees ONE record and is told
to say no only for a different subject or author, so a same-author sibling passes by
design. This asks a model from a different vendor to look at the WHOLE offered list,
in two framings:

  blind     — which record(s) on the list is the original; compare to the pick.
  contrast  — here is the pick and the alternatives: does the evidence single the
              pick out over every alternative? Name any that fits as well or better.

Cases and prompts come from `replay_pick.py` (`cases()`, `attach_prompts()`): the 56
judged-wrong `llm_references` picks, the judged `ours`/`both`/`split` works, and the
150 agreeing controls. The paper block (title, abstract, keyed reference list) is cut
verbatim out of the cached targetoutcome prompt; the pick and its evidence quote come
from the cached `targets`. Every answer is cached in `out/` under a content key
(model, effort, framing, prompt), so a re-run is free.

    .venv/bin/python -m analysis.contrastive_confirm.run --model deepseek/deepseek-v4.1-flash
    .venv/bin/python -m analysis.contrastive_confirm.run --report
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication.replay_pick import (ROOT, attach_prompts,
                                                              cases, mo_key)
from shared.llm_client import call_model

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
MODELS = {  # OpenRouter $/M tokens (input, output), read off /api/v1/models 2026-09-23
    "deepseek/deepseek-v4.1-flash": (0.10, 0.50),
    "z-ai/glm-5.3-flash": (0.15, 0.50),
}
NEWPROMPT_CSV = ROOT / "analysis/pick_pilot/replay_newprompt.csv"

_COMMON = """You are checking which published work a study re-tests. The study replicates or
reproduces an earlier published study, and it cites that original in the list below.
Every record on the list carries an @key.

{task}

What makes this hard: the study often DESCRIBES its original ("a pioneer study",
"our previous work", "the earlier findings of reduced volume") instead of citing it by
name, and the list usually holds records that resemble the original without being it:
- other papers by the same authors on the same topic (the earlier/later paper, the
  companion paper, the follow-up);
- a registered-report protocol or preregistration of the original, vs the original;
- the paper that supplied the materials, task, scale or dataset, vs the paper whose
  FINDING is re-tested;
- a review or meta-analysis, vs the primary study it summarises;
- a meeting abstract, preprint or working paper vs the article;
- earlier replications of the same finding (the target is the one THIS study
  re-tests, usually named as such).
A record fits only when the study's own words — its title, abstract and the quoted
passage — point at it: the finding re-tested, the design, the population, the year,
the authors. Topic and authorship alone do not single a record out when another
record shares them.

THE STUDY AND THE LIST IT CITES:

{paper}

A PASSAGE THE STUDY USES TO DESCRIBE ITS TARGET: {quote}

{answer}"""

_BLIND_TASK = """Your task: say which record on the list is the original study whose finding this
study re-tests — or say that the evidence cannot tell, or that the original is not on
the list."""

_BLIND_ANSWER = """Answer with JSON and nothing else:
{
  "status": "identified" | "cannot_tell" | "not_on_list",
  "originals": ["@key", ...],
  "candidates": ["@key", ...],
  "confident": true | false,
  "reasoning": "<one or two sentences: what in the study's words decides it>"
}
"originals": the key(s) the evidence singles out — only when status is "identified".
Usually one; several only when the study re-tests several different original papers.
"candidates": when status is "cannot_tell", the records that fit equally well (else []).
Say "identified" only when no other record on the list fits the study's description as
well."""

_CONTRAST_TASK = """An earlier stage picked {pick} as the original. Your task: decide whether the
study's words single out {pick} over EVERY other record on the list, and name any other
record that fits the study's description as well as or better than {pick}.

THE PICK: {pick_line}"""

_CONTRAST_ANSWER = """Answer with JSON and nothing else:
{
  "singles_out": true | false,
  "as_good_or_better": ["@key", ...],
  "best": "@key" | null,
  "confident": true | false,
  "reasoning": "<one or two sentences: what in the study's words decides it>"
}
"singles_out": true only when the evidence fits the pick and no other record fits it as
well. "as_good_or_better": every other record that fits as well or better (else []).
"best": the record you would link if you had to choose one, or null when none fits.
A false answer is not a removal — it sends the link to a human — so a false is the
useful answer whenever another record fits as well."""


def paper_block(prompt: str) -> str:
    tail = prompt.split("\nPAPER\n", 1)[1]
    return tail.replace("Respond with the JSON object only.", "").strip()


def pick_line(prompt: str, key: str) -> str:
    for line in prompt.splitlines():
        if line.startswith(key + " "):
            return line
    return key


def build(framing: str, r) -> str:
    fill = {"paper": paper_block(r.prompt), "quote": r.quote or "(none recorded)"}
    if framing == "blind":
        return _COMMON.format(task=_BLIND_TASK, answer=_BLIND_ANSWER, **fill)
    task = _CONTRAST_TASK.format(pick=r.old_key, pick_line=pick_line(r.prompt, r.old_key))
    return _COMMON.format(task=task, answer=_CONTRAST_ANSWER, **fill)


def ask(prompt: str, model: str, effort: str) -> dict:
    h = hashlib.sha256(f"{model}|{effort}|{prompt}".encode()).hexdigest()[:24]
    p = OUT / model.replace("/", "__") / f"{h}.json"
    if p.exists():
        return json.loads(p.read_text())
    result, _prov, err = call_model(prompt, model, reasoning_effort=effort)
    if not result:
        return {"_error": err}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result))
    return result


def quotes() -> dict[str, str]:
    """prompt → the evidence quote of the picked (match_certain) target."""
    q: dict[str, str] = {}
    for f in glob.glob(str(ROOT / "cache/llm/targetoutcome_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if d.get("resolution_method") != "llm_references":
            continue
        t = next((t for t in d.get("targets") or [] if t.get("match_certain") and t.get("key")), {})
        q[d.get("llm_prompt", "")] = str(t.get("evidence_quote") or "")
    return q


def load_cases() -> pd.DataFrame:
    c = attach_prompts(cases())
    c["mo_key"] = c.apply(mo_key, axis=1)
    qm = quotes()
    c["quote"] = c.prompt.map(qm).fillna("")
    return c[c.old_key != ""].reset_index(drop=True)


def _keys(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [str(k).strip() for k in (v or []) if k and str(k).strip().startswith("@")]


def score(c: pd.DataFrame, framing: str, res: list[dict]) -> pd.DataFrame:
    s = pd.DataFrame(index=c.index)
    s["error"] = [x.get("_error", "") for x in res]
    s["confident"] = [x.get("confident") is True for x in res]
    s["reasoning"] = [str(x.get("reasoning", ""))[:300] for x in res]
    if framing == "blind":
        orig = [_keys(x.get("originals")) for x in res]
        cand = [_keys(x.get("candidates")) for x in res]
        status = [str(x.get("status", "")) for x in res]
        s["answer"] = [f"{st}:{','.join(o or cn)}" for st, o, cn in zip(status, orig, cand)]
        s["flag"] = [not (st == "identified" and r.old_key in o)
                     for st, o, r in zip(status, orig, c.itertuples())]
        s["flag_names_other"] = [bool(o) and r.old_key not in o for o, r in zip(orig, c.itertuples())]
        s["alt"] = [[k for k in (o or cn) if k != r.old_key] for o, cn, r in zip(orig, cand, c.itertuples())]
    else:
        so = [x.get("singles_out") for x in res]
        s["flag"] = [v is False or str(v).lower() == "false" for v in so]
        alts = [_keys(x.get("as_good_or_better")) for x in res]
        best = [(_keys(x.get("best")) or [""])[0] for x in res]
        s["answer"] = [f"{v}:{b}:{','.join(a)}" for v, b, a in zip(so, best, alts)]
        s["flag_names_other"] = [f and bool([k for k in a + [b] if k and k != r.old_key])
                                 for f, a, b, r in zip(s.flag, alts, best, c.itertuples())]
        s["alt"] = [[k for k in ([b] + a) if k and k != r.old_key]
                    for a, b, r in zip(alts, best, c.itertuples())]
    s.loc[s.error != "", ["flag", "flag_names_other"]] = False
    s["flag_conf"] = s.flag & s.confident
    s["alt_is_mo"] = [bool(r.mo_key) and r.mo_key != r.old_key and r.mo_key in a
                      for a, r in zip(s.alt, c.itertuples())]
    s["alt_first_is_mo"] = [bool(r.mo_key) and bool(a) and a[0] == r.mo_key != r.old_key
                            for a, r in zip(s.alt, c.itertuples())]
    return s


def group(c: pd.DataFrame) -> pd.Series:
    return c.label.map({"mo": "wrong (mo)", "ours": "right (ours/both)", "both": "right (ours/both)",
                        "split": "split", "agrees_with_mo": "control"})


def summarise(c: pd.DataFrame, s: pd.DataFrame, mask=None) -> pd.DataFrame:
    g = group(c)
    if mask is not None:
        c, s, g = c[mask], s[mask], g[mask]
    t = pd.DataFrame({"n": g.value_counts()})
    for col in ("flag", "flag_conf", "flag_names_other", "alt_is_mo", "alt_first_is_mo"):
        t[col] = s[col].groupby(g).sum()
    t["errors"] = (s.error != "").groupby(g).sum()
    return t.loc[[x for x in ["wrong (mo)", "control", "right (ours/both)", "split"] if x in t.index]]


def usage(model: str) -> tuple[int, int]:
    d = json.load(open(ROOT / "cache/token_usage.json"))
    tin = tout = 0
    for day in d.values():
        m = day.get("openrouter", {}).get(model, {})
        tin += m.get("in", 0)
        tout += m.get("out", 0)
    return tin, tout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=list(MODELS))
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--framing", choices=["blind", "contrast", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    c = load_cases()
    if a.limit:
        c = c.groupby("label", group_keys=False).head(a.limit).reset_index(drop=True)
    print(f"cases {len(c)}: {c.label.value_counts().to_dict()}; with quote {(c.quote != '').sum()}; "
          f"MO key on list {(c.mo_key != '').sum()}")
    models = [a.model] if a.model else list(MODELS)
    framings = ["blind", "contrast"] if a.framing == "both" else [a.framing]
    new = pd.read_csv(NEWPROMPT_CSV, dtype=str) if NEWPROMPT_CSV.exists() else None
    rows = []
    for model in models:
        before = usage(model)
        for fr in framings:
            prompts = [build(fr, r) for r in c.itertuples()]
            with ThreadPoolExecutor(a.workers) as ex:
                res = list(ex.map(lambda p: ask(p, model, a.effort), prompts))
            s = score(c, fr, res)
            print(f"\n== {model} @ {a.effort} — {fr}")
            print(summarise(c, s).to_string())
            out = c.drop(columns=["prompt"]).join(s)
            out.to_csv(HERE / f"results_{model.split('/')[1]}_{a.effort}_{fr}.csv",
                       index=False, encoding="utf-8-sig")
            rows.append((model, fr, s))
        after = usage(model)
        tin, tout = after[0] - before[0], after[1] - before[1]
        pin, pout = MODELS[model]
        print(f"{model}: this run {tin:,} in / {tout:,} out tokens ≈ ${tin * pin / 1e6 + tout * pout / 1e6:.3f}")
    if new is None:
        print(f"\n{NEWPROMPT_CSV.relative_to(ROOT)} not found — no new-prompt comparison.")


if __name__ == "__main__":
    main()
