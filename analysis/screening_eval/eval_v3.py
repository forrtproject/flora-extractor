"""Run the v3 screening prompt (or the production prompt) over a case set.

Usage:  python3 eval_v3.py <model-id> <caseset.json> [--prod] [--prompt=FILE]
                          [--tag=NAME] [--limit=N]
  model-id starting "gemini"  -> Gemini API (GEMINI_API_KEY)
  model-id starting "gpt-"    -> OpenAI direct (OPENAI_API_KEY)
  model-id containing "/"     -> OpenRouter (OPENROUTER_API_KEY)

Writes voter_v3_<model>_<caseset>.json (or voter_prod_<model>_<caseset>.json with
--prod). `--prompt=prompt_v31.txt` runs a different prompt file with the same v3 JSON
schema and parser; the output tag is then the file's version stem (`v31`) unless
`--tag=` overrides it. Results are resumable: cases already recorded without an error
are skipped.

A prompt asking for the v3.2 schema — a binary `"confident": true|false` in place of the
three-level `"confidence"` — is detected from the prompt text and parsed accordingly
(`--schema=v3|v32` forces one).
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
from typing import Any, Optional

HERE = Path(__file__).resolve().parent

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
{"is_replication": "<yes|no|unclear>", "classification_confidence": "<high|medium|low>", "evidence_quote": "<exact short quote from the abstract, or empty>", "reasoning": "<one sentence>"}"""

V3_PROMPT = (HERE / "prompt_v3.txt").read_text()

CLASSES = {"replication", "reproduction", "both", "none", "unclear"}
CONFS = {"high", "medium", "low"}


def build_prompt(template: str, title: str, abstract: str) -> str:
    return (template.replace("{title}", title or "(not available)")
                    .replace("{abstract}", (abstract or "(not available)")[:6000]))


def call_gemini(model: str, prompt: str) -> tuple[Optional[str], Optional[str], dict]:
    key = os.environ.get("GEMINI_API_KEY", "")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={key}")
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"responseMimeType": "application/json",
                                    "maxOutputTokens": 4096}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    um = d.get("usageMetadata", {})
    usage = {"in": um.get("promptTokenCount", 0),
             "out": um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)}
    cands = d.get("candidates") or []
    if not cands:
        return None, f"no candidates: {json.dumps(d)[:200]}", usage
    parts = cands[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    return text, None, usage


def call_chat(model: str, prompt: str) -> tuple[Optional[str], Optional[str], dict]:
    if "/" in model:
        url = "https://openrouter.ai/api/v1/chat/completions"
        hdr = {"Content-Type": "application/json",
               "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"}
        payload: dict[str, Any] = {"model": model,
                                   "messages": [{"role": "user", "content": prompt}],
                                   "temperature": 0, "max_tokens": 3000,
                                   "provider": {"sort": "price"}}
    else:
        url = "https://api.openai.com/v1/chat/completions"
        hdr = {"Content-Type": "application/json",
               "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "max_completion_tokens": 3000}
        if model.startswith(("gpt-5", "o3", "o4")):
            payload["reasoning_effort"] = "low"
        else:
            payload["temperature"] = 0
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    u = d.get("usage", {})
    usage = {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}
    return d["choices"][0]["message"]["content"], None, usage


def call(model: str, prompt: str, retries: int = 3) -> tuple[Optional[str], Optional[str], dict]:
    for attempt in range(retries):
        try:
            if model.startswith("gemini"):
                return call_gemini(model, prompt)
            return call_chat(model, prompt)
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:200]
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, f"HTTP {e.code}: {msg}", {}
        except Exception as e:  # noqa: BLE001 - API boundary
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, str(e)[:200], {}
    return None, "exhausted", {}


def parse_v3(text: Optional[str]) -> dict:
    if not text:
        return {"schema_error": "empty"}
    stripped = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip())
    m = re.search(r"\{.*\}", stripped, re.S)
    try:
        d = json.loads(m.group(0) if m else stripped)
    except Exception:  # noqa: BLE001
        return {"schema_error": "unparseable"}
    if not isinstance(d, dict):
        return {"schema_error": "not_object"}
    cls = str(d.get("classification", "")).strip().lower()
    conf = str(d.get("confidence", "")).strip().lower()
    cats = d.get("categories")
    cats = [str(c).strip().lower() for c in cats] if isinstance(cats, list) else []
    err = None
    if cls not in CLASSES:
        err = "bad_classification"
    elif conf not in CONFS:
        err = "bad_confidence"
    elif not cats:
        err = "missing_categories"
    return {"classification": cls or None, "confidence": conf or None,
            "categories": cats, "evidence_quote": str(d.get("evidence_quote", ""))[:300],
            "reasoning": str(d.get("reasoning", ""))[:400],
            "schema_error": err}


def parse_v32(text: Optional[str]) -> dict:
    """v3.2 schema: identical to v3 except `confidence` is replaced by a boolean
    `confident`. Strings "true"/"false" are accepted because models emit them."""
    if not text:
        return {"schema_error": "empty"}
    stripped = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip())
    m = re.search(r"\{.*\}", stripped, re.S)
    try:
        d = json.loads(m.group(0) if m else stripped)
    except Exception:  # noqa: BLE001
        return {"schema_error": "unparseable"}
    if not isinstance(d, dict):
        return {"schema_error": "not_object"}
    cls = str(d.get("classification", "")).strip().lower()
    raw_conf = d.get("confident", None)
    if isinstance(raw_conf, bool):
        confident: Optional[bool] = raw_conf
    elif isinstance(raw_conf, str) and raw_conf.strip().lower() in {"true", "false"}:
        confident = raw_conf.strip().lower() == "true"
    else:
        confident = None
    cats = d.get("categories")
    cats = [str(c).strip().lower() for c in cats] if isinstance(cats, list) else []
    err = None
    if cls not in CLASSES:
        err = "bad_classification"
    elif "confident" not in d:
        err = "missing_confident"
    elif confident is None:
        err = "bad_confident"
    elif not cats:
        err = "missing_categories"
    return {"classification": cls or None, "confident": confident,
            "categories": cats, "evidence_quote": str(d.get("evidence_quote", ""))[:300],
            "reasoning": str(d.get("reasoning", ""))[:400],
            "schema_error": err}


def parse_prod(text: Optional[str]) -> dict:
    if not text:
        return {"schema_error": "empty"}
    stripped = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip())
    m = re.search(r"\{.*\}", stripped, re.S)
    try:
        d = json.loads(m.group(0) if m else stripped)
    except Exception:  # noqa: BLE001
        return {"schema_error": "unparseable"}
    if not isinstance(d, dict):
        return {"schema_error": "not_object"}
    v = str(d.get("is_replication", "")).strip().lower()
    conf = str(d.get("classification_confidence", "")).strip().lower()
    return {"verdict": v or None, "confidence": conf or None,
            "evidence_quote": str(d.get("evidence_quote", ""))[:300],
            "reasoning": str(d.get("reasoning", ""))[:400],
            "schema_error": None if v in {"yes", "no", "unclear"} else "bad_verdict"}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    model, caseset = args[0], args[1]
    prod = "--prod" in flags
    limit = next((int(f.split("=")[1]) for f in flags if f.startswith("--limit=")), None)
    pfile = next((f.split("=", 1)[1] for f in flags if f.startswith("--prompt=")), None)

    if prod:
        template, tag = PROD_PROMPT, "prod"
    elif pfile:
        template = (HERE / Path(pfile).name).read_text()
        tag = Path(pfile).stem.replace("prompt_", "")
    else:
        template, tag = V3_PROMPT, "v3"
    tag = next((f.split("=", 1)[1] for f in flags if f.startswith("--tag=")), tag)

    schema = next((f.split("=", 1)[1] for f in flags if f.startswith("--schema=")), None)
    if schema is None:
        schema = "v32" if (not prod and '"confident"' in template) else "v3"
    parser = {"v3": parse_v3, "v32": parse_v32}[schema] if not prod else parse_prod

    stem = Path(caseset).stem.replace("_cases", "")
    out = HERE / f"voter_{tag}_{model.replace('/', '_')}_{stem}.json"

    cases = json.loads((HERE / Path(caseset).name).read_text())
    if limit:
        cases = cases[:limit]

    done: dict[str, dict] = {}
    if out.exists():
        done = {r["id"]: r for r in json.loads(out.read_text()) if not r.get("error")}

    print(f"{model} [{tag}/{schema if not prod else 'prod'}] on {len(cases)} cases from "
          f"{caseset} ({len(done)} cached) -> {out.name}", flush=True)

    results: list[dict] = []
    tin = tout = 0
    for i, c in enumerate(cases, 1):
        if c["id"] in done:
            results.append(done[c["id"]])
            continue
        text, err, usage = call(model, build_prompt(template, c.get("title", ""),
                                                    c.get("abstract", "")))
        tin += usage.get("in", 0)
        tout += usage.get("out", 0)
        parsed = parser(text)
        row = {"id": c["id"], "bucket": c.get("bucket", stem),
               "error": "api_error" if err else None, "error_detail": err,
               "usage": usage, "raw": (text or "")[:1500]}
        row.update(parsed)
        results.append(row)
        if i % 25 == 0 or i == len(cases):
            print(f"  {i}/{len(cases)}", flush=True)
            out.write_text(json.dumps(results, indent=2))
        time.sleep(0.4)

    out.write_text(json.dumps(results, indent=2))
    errs = Counter(r["error_detail"].split(":")[0] for r in results if r.get("error_detail"))
    schema_errs = Counter(r.get("schema_error") for r in results if r.get("schema_error"))
    key = "verdict" if prod else "classification"
    print(f"\ntokens in={tin} out={tout}")
    if errs:
        print("api errors:", dict(errs))
    if schema_errs:
        print("schema errors:", dict(schema_errs))
    print("verdicts:", dict(Counter(r.get(key) for r in results)))
    ckey = "confident" if schema == "v32" and not prod else "confidence"
    print(f"{ckey}:", dict(Counter(r.get(ckey) for r in results)))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
