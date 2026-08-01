"""Score a candidate second-voter model against the blinded Sonnet panel truth.

Usage:  python3 eval_second_voter.py <model-id> [prompt_variant]
  model-id starting "gpt-"  -> OpenAI direct
  model-id containing "/"   -> OpenRouter
Uses the exact production L4 prompt, rebuilt per case, so results are comparable
with the cached flash-lite / gpt-5-mini votes.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent

MODEL = sys.argv[1]
VARIANT = sys.argv[2] if len(sys.argv) > 2 else "prod"
OUT = HERE / f"voter_{MODEL.replace('/', '_')}_{VARIANT}{'_'+os.environ.get('EVALSET','') if os.environ.get('EVALSET') else ''}.json"

PROD_PROMPT = """You are classifying papers for a replication database.

A paper is a REPLICATION if it collects new data to re-test a finding reported in a
previously published study, and a REPRODUCTION if it re-analyses that study's data to
check the reported result. Re-testing in a different population, country or sample
still counts.

Using the words "replicate" or "reproduce" does not make a paper either one. It must
re-test a published finding. Evaluating, adapting or comparing a measurement
instrument, method or procedure does not count, and neither do the ordinary-language
and biological senses of the words.

Answer:
  yes     — the abstract shows it re-tests a published finding or re-analyses an
            earlier study's data
  no      — the abstract shows it does not
  unclear — the abstract does not settle it either way

A high-confidence "no" discards the paper, so use high confidence only when the
abstract clearly describes a purpose that does not qualify. Not naming an original
study is not by itself grounds for a high-confidence "no".

TITLE: {title}

ABSTRACT: {abstract}

Respond with ONLY a JSON object:
{{"is_replication": "<yes|no|unclear>", "classification_confidence": "<high|medium|low>", "evidence_quote": "<exact short quote from the abstract, or empty>", "reasoning": "<one sentence>"}}"""

PROMPTS = {"prod": PROD_PROMPT}
_V2 = HERE / "prompt_v2.txt"
if _V2.exists():
    PROMPTS["v2"] = _V2.read_text()

TEMPLATE = PROMPTS[VARIANT]
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
VIA_OR = "/" in MODEL


def call(prompt: str, retries: int = 3):
    if VIA_OR:
        url = "https://openrouter.ai/api/v1/chat/completions"
        hdr = {"Content-Type": "application/json", "Authorization": f"Bearer {OR_KEY}"}
        # 3.x Gemini emits reasoning tokens that count against max_tokens; 400 truncated
        # the JSON mid-object on 41/47 cases.
        payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0, "max_tokens": 3000, "provider": {"sort": "price"}}
    else:
        url = "https://api.openai.com/v1/chat/completions"
        hdr = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_KEY}"}
        payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                   "max_completion_tokens": 2000}
        # GPT-5.x are reasoning models; keep hidden thinking minimal for a short JSON.
        if MODEL.startswith(("gpt-5", "o3", "o4")):
            payload["reasoning_effort"] = "low"
        else:
            payload["temperature"] = 0
    body = json.dumps(payload).encode()
    for a in range(retries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, data=body, headers=hdr), timeout=120) as r:
                d = json.load(r)
            u = d.get("usage", {})
            return d["choices"][0]["message"]["content"], None, u
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:200]
            if e.code in (429, 500, 503) and a < retries - 1:
                time.sleep(10 * (a + 1))
                continue
            return None, f"HTTP {e.code}: {msg}", {}
        except Exception as e:  # noqa: BLE001 - boundary
            if a < retries - 1:
                time.sleep(5)
                continue
            return None, str(e)[:150], {}
    return None, "exhausted", {}


def parse(text):
    if not text:
        return None, None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        d = json.loads(m.group(0) if m else text)
    except Exception:  # noqa: BLE001
        return None, None
    if not isinstance(d, dict):
        return None, None
    return (str(d.get("is_replication", "")).strip().lower() or None,
            str(d.get("classification_confidence", "")).strip().lower() or None)


MODE = os.environ.get("EVALSET","")
HO = MODE == "heldout"
cases = {c["id"]: c for c in json.load(open(HERE / (
    "human_cases.json" if MODE=="human" else ("heldout_cases.json" if HO else "adjudication_cases.json"))))}
truth = json.load(open(HERE / (
    "human_truth.json" if MODE=="human" else ("heldout_truth.json" if HO else "adjudicated.json"))))["truth"]
ids = sorted(truth)
print(f"{MODEL} [{VARIANT}] on {len(ids)} adjudicated cases", flush=True)

res, tin, tout = [], 0, 0
for i, cid in enumerate(ids, 1):
    c = cases[cid]
    txt, err, u = call(TEMPLATE.format(title=c["title"] or "(not available)",
                                       abstract=(c["abstract"] or "(not available)")[:4000]))
    v, conf = parse(txt)
    tin += u.get("prompt_tokens", 0)
    tout += u.get("completion_tokens", 0)
    res.append({"id": cid, "bucket": c.get("bucket", "heldout"), "truth": truth[cid],
                "verdict": v, "conf": conf, "error": err, "raw": (txt or "")[:200]})
    if i % 12 == 0 or i == len(ids):
        print(f"  {i}/{len(ids)}", flush=True)
    time.sleep(0.3)

OUT.write_text(json.dumps(res, indent=2))
V = {"yes", "no", "unclear"}
ok = [r for r in res if r["verdict"] in V]
print(f"\nparsed {len(ok)}/{len(res)}; tokens in={tin} out={tout}")
if errs := Counter((r["error"] or "").split(":")[0] for r in res if r["error"]):
    print("errors:", dict(errs))
if not ok:
    sys.exit()
corr = sum(1 for r in ok if r["verdict"] == r["truth"])
print(f"CORRECT vs panel: {corr}/{len(ok)} = {corr/len(ok):.1%}")
yes_t = [r for r in ok if r["truth"] == "yes"]
no_t = [r for r in ok if r["truth"] == "no"]
miss = sum(1 for r in yes_t if r["verdict"] == "no")
fp = sum(1 for r in no_t if r["verdict"] == "yes")
print(f"  misses real replications: {miss}/{len(yes_t)}")
print(f"  false positives:          {fp}/{len(no_t)}")
print("  verdicts:", dict(Counter(r["verdict"] for r in ok)))
print(f"-> {OUT}")
