"""Jev (typesafe/jev-1.13) as the pre-screen's question, via OpenRouter's System One API.

Two Noul questions per request over one state (title + abstract):
  p7     — the shipped pre-screen question (analysis/prescreen_eval/prompt_p7.txt), with
           its yes/no guidance moved into Jev's criteria
  direct — the plainer "is this a replication or reproduction of a specific study?"
Jev returns P(yes) for each; thresholds are applied at scoring time, not here.
"""
import json
import sys
import time
from pathlib import Path

import threading

import requests

# One session per thread: a fresh requests.post rebuilds the SSL context and reloads
# the CA store every call, ~8 s of CPU per handshake on this machine.
_local = threading.local()


def session() -> requests.Session:
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from shared.config import OPENROUTER_API_KEY  # noqa: E402

MODEL = "typesafe/jev-1.13"
ABSTRACT_CHARS = 3000   # as analysis/prescreen_eval/eval_prescreen.py

_DEFS = ("A replication collects new data to re-test a finding reported in a previously "
         "published study. A reproduction re-analyses that study's own data to check its "
         "reported result. Re-testing in a different population, country or sample still "
         "counts.")

QUESTIONS = {
    "p7": {
        "type": "noul",
        "instructions": {
            "definitions": _DEFS,
            "question": "Does the `title` or `abstract` suggest that the authors deliberately "
                        "check a specific result from earlier published research?",
        },
        "criteria": {
            "true": "The text suggests such a check, or leaves the paper's purpose "
                    "genuinely unclear.",
            "false": "The text describes another purpose without suggesting that an "
                     "earlier reported result is being checked.",
        },
    },
    "direct": {
        "type": "noul",
        "instructions": {
            "definitions": _DEFS,
            "question": "Is this paper a replication or a reproduction of a specific "
                        "earlier published study?",
        },
        "criteria": {
            "true": "The paper re-tests or re-analyses a specific earlier study's result.",
            "false": "The paper does something else: new research, a review, a method, "
                     "or work that merely builds on earlier studies.",
        },
    },
}


def ask(title: str, abstract: str, questions: dict = QUESTIONS, retries: int = 5) -> dict:
    """{"p": {qid: prob}, "usage": {...}, "error": ""} — an error is never a probability."""
    body = {"model": MODEL,
            "state": {"title": title or "(not available)",
                      "abstract": (abstract or "(not available)")[:ABSTRACT_CHARS]},
            "questions": questions}
    last = ""
    for attempt in range(retries):
        try:
            r = session().post("https://openrouter.ai/api/v1/systemone", timeout=60,
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                              json=body)
            if r.status_code == 200:
                d = r.json()
                return {"p": {k: a.get("noul") for k, a in (d.get("answers") or {}).items()},
                        "usage": d.get("usage") or {}, "model": d.get("model", ""),
                        "error": ""}
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:  # noqa: BLE001 — API boundary
            last = f"{type(e).__name__}: {e}"[:200]
        time.sleep(min(30, 2 ** attempt))
    return {"p": {}, "usage": {}, "error": last}


if __name__ == "__main__":
    t = "A replication of Smith et al. (2009): does ego depletion reduce persistence?"
    a = ("We conducted a preregistered direct replication of Study 2 of Smith et al. (2009) "
         "with 400 participants. The original effect was not observed.")
    print(json.dumps(ask(t, a), indent=1))
    print(json.dumps(ask(t, a, {"p7": QUESTIONS["p7"]}), indent=1))
    t2 = "Soil microbial diversity along an altitudinal gradient in the Andes"
    a2 = "We sampled 120 plots and sequenced 16S rRNA to describe community turnover."
    print(json.dumps(ask(t2, a2), indent=1))
