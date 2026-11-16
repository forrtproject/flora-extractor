"""Round 2 voters over cases.json; resumable. Output: out/<voter>.json

  run2.py ds41 | jev_full | pplx_full | jev_min | pplx_min | qwen | mistral

ds41       — production screen vote (_classify_once, v3.3) on deepseek-v4.1-flash @ low;
             production vote cache read first, misses bought and not written back.
*_full     — decision model, `choice` question over the five screen classes, with the
             FULL production rules (_CLASSIFY_PROMPT from WHAT QUALIFIES up to the
             generative answer instructions — the evidence quote / reasoning / JSON /
             confidence fields are what is removed). Same construction as the 2026-10-07
             decision benchmark (lukas-reports …/flora-decision-benchmark-2026-10).
*_min      — decision model, one `noul` question, one-sentence instruction, title +
             abstract cut to MIN_ABSTRACT_CHARS (target <= 500 input tokens on Jev).
qwen/mistral — the dormant cheap tier's two voters on its p7 prompt (prescreen_eval).
"""
import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "analysis" / "prescreen_eval"))
import run as run1  # noqa: E402  (installs the usage capture on _record_tokens)
from run import lc, jev  # noqa: E402
from shared.config import OPENROUTER_API_KEY, PRESCREEN_MODEL_1, PRESCREEN_MODEL_2  # noqa: E402
from shared.prompts import _CLASSIFY_PROMPT, build_classify_prompt  # noqa: E402
from eval_prescreen import build_prompt  # noqa: E402

_spec = importlib.util.spec_from_file_location("run09", REPO / "analysis/jev_eval_2026-09/run.py")
run09 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run09)

OUT = HERE / "out"
MIN_ABSTRACT_CHARS = 800
CHOICES = {
    "replication": "Re-tests a particular earlier published finding with newly collected data.",
    "reproduction": "Re-analyses earlier data to check a published result, robustness, or reproducibility.",
    "both": "Both re-analysis of earlier data and collection of new data to re-test the finding.",
    "none": "The title and abstract describe a purpose that does not qualify under the supplied rules.",
    "unclear": "The title and abstract do not settle whether the paper qualifies.",
}
# full2: the rules' named exclusions moved into the "none" criterion, where a decision
# model weighs them; the full run leaked viral/GWAS-cohort/original-study papers.
CHOICES2 = {**CHOICES, "none": (
    "Not a check of another paper's finding. This includes: biological or technical senses "
    "of 'replication' (DNA, viral, cell); a discovery-then-replication cohort, two-stage or "
    "'independent sample' design inside this same paper (GWAS and similar); a new original "
    "study that only cites, builds on or extends earlier work; a first validation or short "
    "form of a new instrument; a review, proposal, protocol or commentary without its own "
    "re-test or re-analysis.")}
FULL_RULES = ("Screen this paper for the FLoRA replication/reproduction database. "
              "Judge only from its title and abstract. Choose exactly one category.\n\n"
              "WHAT QUALIFIES"
              + _CLASSIFY_PROMPT.split("WHAT QUALIFIES", 1)[1].split("Judge only from", 1)[0])
MIN_Q = {"type": "noul",
         "instructions": "Does this paper re-test or re-analyse a specific earlier published "
                         "study's result (a replication or reproduction)?"}
MODELS = {"jev": "typesafe/jev-1.13", "pplx": "perplexity/pplx-decider-v1.1-27b"}
P7 = (REPO / "analysis/prescreen_eval/prompt_p7.txt").read_text(encoding="utf-8")


def decide(model: str, state: str, q: dict, retries: int = 6) -> dict:
    last = ""
    for attempt in range(retries):
        try:
            r = jev.session().post("https://openrouter.ai/api/alpha/decisions", timeout=90,
                                   headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                                   json={"model": model, "state": state, "questions": {"q": q}})
            if r.status_code == 200:
                d = r.json()
                a = d["answers"]["q"]
                u = d.get("usage") or {}
                return {"error": "", "served": d.get("model"),
                        "usage": {"in": u.get("input_tokens", 0), "usd": u.get("cost", 0)},
                        **({"p": a["noul"]} if q["type"] == "noul" else
                           {"choice": a["choice"], "probs": a["probabilities"]})}
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:  # noqa: BLE001 — API boundary
            last = f"{type(e).__name__}: {e}"[:200]
        time.sleep(min(30, 2 ** attempt))
    return {"error": last}


def one(voter: str, c: dict) -> dict:
    t, a = c["title"] or "(not available)", c["abstract"] or "(not available)"
    if voter == "ds41":
        model, effort = "deepseek/deepseek-v4.1-flash", "low"
        prompt = build_classify_prompt(c["title"], c["abstract"])
        hit = lc._cached_vote(c["doi"], c["title"], prompt, model, effort)
        run1._usage.u = {}
        vote = hit or lc._classify_once(prompt, model, effort)
        if not vote:
            return {"id": c["id"], "error": "no vote"}
        return {"id": c["id"], "error": "", "cache_hit": bool(hit),
                "classification": vote.get("classification"),
                "confident": vote.get("confident"), "usage": {} if hit else dict(run1._usage.u)}
    if voter in ("qwen", "mistral"):
        m = PRESCREEN_MODEL_1 if voter == "qwen" else PRESCREEN_MODEL_2
        r = run09.chat(m, build_prompt(P7, c["title"], c["abstract"]), "")
        return {"id": c["id"], **{k: r.get(k) for k in ("error", "verdict", "usage")}}
    fam, kind = voter.split("_")
    if kind in ("full", "full2"):
        res = decide(MODELS[fam], f"Title: {t}\n\nAbstract: {a}",
                     {"type": "choice", "instructions": FULL_RULES,
                      "criteria": CHOICES if kind == "full" else CHOICES2})
    elif kind == "p7":  # round 1's exact Jev question and state, for a like-for-like swap
        res = decide(MODELS[fam], {"title": t, "abstract": a[:jev.ABSTRACT_CHARS]},
                     jev.QUESTIONS["p7"])
    elif kind == "minfa":  # minimal question, whole abstract
        res = decide(MODELS[fam], f"Title: {t}\n\nAbstract: {a}", MIN_Q)
    else:
        res = decide(MODELS[fam], f"Title: {t}\n\nAbstract: {a[:MIN_ABSTRACT_CHARS]}", MIN_Q)
    return {"id": c["id"], **res}


def run(voter: str, workers: int = 8) -> None:
    OUT.mkdir(exist_ok=True)
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    path = OUT / f"{voter}.json"
    done = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))
            if not r.get("error")} if path.exists() else {}
    todo = [c for c in cases if c["id"] not in done]
    results = list(done.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, rec in enumerate(pool.map(lambda c: one(voter, c), todo), 1):
            results.append(rec)
            if i % 200 == 0:
                path.write_text(json.dumps(results), encoding="utf-8")
                print(f"{voter}: {i}/{len(todo)}", flush=True)
    path.write_text(json.dumps(results), encoding="utf-8")
    print(f"{voter}: {len(results)} rows, {sum(1 for r in results if r.get('error'))} errors",
          flush=True)


if __name__ == "__main__":
    for v in sys.argv[1:]:
        run(v)
