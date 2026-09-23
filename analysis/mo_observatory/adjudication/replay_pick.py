"""Replay the reference-list pick (`llm_references`) with another linking model, over the
exact prompts the pipeline sent, and score it against the adjudicated originals.

Cases:
  judged    — the different-original works whose pick came from `llm_references`,
              labelled by the two blinded judges: `mo` (our pick wrong), `ours`
              (our pick right), `both`, or unsettled.
  control   — a seeded sample of single-original `llm_references` picks that agree with
              the Observatory: a correct pick the new model should keep.

The prompt is read verbatim from the pipeline's targetoutcome cache (`llm_prompt`), so
the only thing that differs is the model. Answers are cached in `out/replay/`.

    .venv/bin/python -m analysis.mo_observatory.adjudication.replay_pick --model gpt-6-luna
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication.agreement import load
from analysis.mo_observatory.adjudication.build_report import verdicts
from shared.config import LINKING_EFFORT
from shared.llm_client import call_model

HERE = Path(__file__).resolve().parents[0]
ROOT = HERE.parents[2]
N_CONTROL = 150
SEED = 20260923
_KEYLINE = re.compile(r"^(@[a-z0-9]+)\s+(.*)$", re.M)


def _norm(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def cases() -> pd.DataFrame:
    ours, mo = load()
    wk = pd.read_csv(HERE / "works.csv", dtype=str)
    v = verdicts()
    judged = v[v.stratum == "original"][["doi_r", "consensus"]].rename(columns={"consensus": "label"})
    j = ours[ours.link_method == "llm_references"].merge(judged, on="doi_r")
    j["set"] = "judged"
    sim = pd.read_csv(HERE / "original_title_sim.csv")
    ctrl = ours[ours.link_method == "llm_references"].merge(wk, on="doi_r")
    ctrl = ctrl[(ctrl.original == "same (identical sets)") & (ctrl.n_ours == "1")
                & ~ctrl.doi_r.isin(set(sim.doi_r))]
    ctrl = ctrl.sample(min(N_CONTROL, len(ctrl)), random_state=SEED).assign(label="agrees_with_mo", set="control")
    c = pd.concat([j, ctrl])[["doi_r", "title_r", "doi_o", "title_o", "set", "label"]]
    mo_titles = mo.groupby("doi_r").original_title.apply(list).to_dict()
    mo_dois = mo.groupby("doi_r").doi_o.apply(list).to_dict()
    c["mo_titles"] = c.doi_r.map(mo_titles)
    c["mo_dois"] = c.doi_r.map(mo_dois)
    return c.reset_index(drop=True)


def attach_prompts(c: pd.DataFrame) -> pd.DataFrame:
    want: dict[str, list[int]] = {}
    for i, r in c.iterrows():
        want.setdefault(r.doi_o.lower(), []).append(i)
    found: dict[int, dict] = {}
    for f in glob.glob(str(ROOT / "cache/llm/targetoutcome_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if d.get("resolution_method") != "llm_references":
            continue
        for i in want.get(str(d.get("resolved_doi_o") or "").lower(), []):
            if _norm(c.at[i, "title_r"])[:40] in _norm(d.get("llm_prompt", "")):
                found[i] = d
    c["prompt"] = [found.get(i, {}).get("llm_prompt", "") for i in c.index]
    c["old_key"] = [next((t.get("key") for t in found.get(i, {}).get("targets") or []
                          if t.get("match_certain") and t.get("key")), "") for i in c.index]
    c["old_model"] = [found.get(i, {}).get("llm_model", "") for i in c.index]
    return c[c.prompt != ""].reset_index(drop=True)


def mo_key(r) -> str:
    """The @key of the Observatory's original in this prompt's list, by DOI or title."""
    lines = dict((k, _norm(t)) for k, t in _KEYLINE.findall(r.prompt))
    for doi in r.mo_dois or []:
        if doi and doi.lower() in r.prompt.lower():
            for k, t in _KEYLINE.findall(r.prompt):
                if doi.lower() in t.lower():
                    return k
    for title in r.mo_titles or []:
        nt = _norm(title)[:50]
        if len(nt) > 15:
            for k, t in lines.items():
                if nt in t:
                    return k
    return ""


def ask(prompt: str, model: str) -> dict:
    h = hashlib.sha256(f"{model}|{LINKING_EFFORT}|{prompt}".encode()).hexdigest()[:24]
    p = HERE / "out" / "replay" / f"{h}.json"
    if p.exists():
        return json.loads(p.read_text())
    result, _prov, err = call_model(prompt, model, reasoning_effort=LINKING_EFFORT)
    if not result:
        return {"_error": err}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result))
    return result


def certain_keys(result: dict) -> list[str]:
    return [str(t.get("key")) for t in result.get("targets") or []
            if isinstance(t, dict) and t.get("key") and t.get("match_certain") is True]


def classify(r) -> str:
    new, old, mk = set(r.new_keys), r.old_key, r.mo_key
    if r.error:
        return "error"
    if not new:
        return "declines"
    if old in new and mk and mk in new and mk != old:
        return "keeps old + adds MO's"
    if old in new:
        return "same as old" if len(new) == 1 else "old + other"
    if mk and mk in new:
        return "switches to MO's"
    return "switches to another"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    c = attach_prompts(cases())
    c["mo_key"] = c.apply(mo_key, axis=1)
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(lambda p: ask(p, a.model), c.prompt))
    c["error"] = [x.get("_error", "") for x in res]
    c["new_keys"] = [certain_keys(x) for x in res]
    c["new_reasoning"] = [str(x.get("reasoning", ""))[:300] for x in res]
    c["result"] = c.apply(classify, axis=1)
    print(f"model {a.model} @ {LINKING_EFFORT}; cases with a cached prompt: {len(c)}; "
          f"old picks by {c.old_model.value_counts().to_dict()}")
    print(f"MO's original located in the offered list: {(c.mo_key != '').sum()} / {len(c)}")
    print(pd.crosstab([c.set, c.label], c.result, margins=True).to_string())
    c.drop(columns=["prompt"]).to_csv(HERE / f"replay_{a.model}.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
