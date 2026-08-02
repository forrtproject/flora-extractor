"""Run one cheap pre-screen prompt with one model over one or more case sets.

Usage:  python3 eval_prescreen.py <model-id> <prompt.txt> <caseset.json> [caseset.json ...]
                                 [--limit=N] [--workers=N]

  model-id containing "/"  -> OpenRouter (OPENROUTER_API_KEY), routed cheapest-provider
  model-id otherwise       -> OpenAI direct (OPENAI_API_KEY)

Writes pre_<promptstem>_<model>_<caseset>.json per case set. Resumable: cases already
recorded without an error are replayed from the file rather than re-called.

Deliberately standalone (urllib, no repo imports beyond shared.prescreen) so the eval is
not perturbed by production rate limits, caching or budget checks.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.prescreen import hard_signal  # noqa: E402

HERE = Path(__file__).resolve().parent
ABSTRACT_CHARS = 3000   # overridable with --chars=N, to price the input side


def build_prompt(template: str, title: str, abstract: str, chars: int = 0) -> str:
    return (template.replace("{title}", title or "(not available)")
                    .replace("{abstract}", (abstract or "(not available)")[:chars or ABSTRACT_CHARS]))


def call_chat(model: str, prompt: str,
              max_tokens: int = 200) -> tuple[Optional[str], Optional[str], dict]:
    if "/" in model:
        url = "https://openrouter.ai/api/v1/chat/completions"
        hdr = {"Content-Type": "application/json",
               "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0, "max_tokens": max_tokens,
                   "provider": {"sort": "price"}}
    else:
        url = "https://api.openai.com/v1/chat/completions"
        hdr = {"Content-Type": "application/json",
               "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "max_completion_tokens": max_tokens, "temperature": 0}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=hdr)
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    u = d.get("usage") or {}
    usage = {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}
    choices = d.get("choices") or []
    if not choices:
        return None, f"no choices: {json.dumps(d)[:200]}", usage
    choice = choices[0]
    content = (choice.get("message") or {}).get("content")
    # A reasoning model spends the budget thinking and emits its answer last, so a cap
    # sized for "yes"/"no" cuts it off mid-thought and returns empty content. Scoring
    # that as an unreadable reply blames the model for the harness — it reads as a
    # model that cannot follow the schema when it simply was not allowed to finish.
    if not content and choice.get("finish_reason") == "length":
        usage["truncated"] = True
    return content, None, usage


def call(model: str, prompt: str, retries: int = 6) -> tuple[Optional[str], Optional[str], dict]:
    """Retry hard on 429: cheapest-provider routing pins a single upstream provider,
    which rate-limits well below the concurrency this eval wants. A 429 that gives up
    would be scored as 'no answer' and silently deflate the discard rate."""
    for attempt in range(retries):
        try:
            text, err, usage = call_chat(model, prompt)
            # Give a model that ran out of room a second, generous attempt rather than
            # recording its silence as a failure to follow the schema.
            if usage.get("truncated"):
                text, err, usage = call_chat(model, prompt, max_tokens=3000)
                usage["needed_room"] = True
            return text, err, usage
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:200]
            if attempt < retries - 1:
                time.sleep(min(30, 3 * 2 ** attempt) if e.code == 429 else 2 ** attempt)
                continue
            return None, f"HTTP {e.code}: {msg}", {}
        except Exception as e:  # noqa: BLE001 - API boundary
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, str(e)[:200], {}
    return None, "exhausted", {}


def parse(text: str) -> dict:
    """Pull the yes/no out of the reply. Anything unreadable is a schema error, and a
    schema error must never become a discard — the caller treats it as 'yes'."""
    if not text:
        return {"verdict": "", "schema_error": "empty"}
    body = text.strip()
    body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.MULTILINE).strip()
    obj = None
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", body, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        return {"verdict": "", "schema_error": f"unparseable: {body[:120]}"}
    raw = str(obj.get("maybe_replication", "")).strip().lower()
    if raw not in {"yes", "no"}:
        return {"verdict": "", "schema_error": f"bad value: {raw[:60]}",
                "reason": str(obj.get("reason", ""))[:300]}
    return {"verdict": raw, "reason": str(obj.get("reason", ""))[:300]}


def run_set(model: str, template: str, tag: str, case_path: Path,
            limit: int, workers: int, chars: int = 0) -> None:
    cases = json.loads(case_path.read_text())
    if limit:
        cases = cases[:limit]
    stem = case_path.stem.replace("cases_", "")
    out_path = HERE / f"pre_{tag}_{model.replace('/', '_')}_{stem}.json"

    done: dict[str, dict] = {}
    if out_path.exists():
        done = {r["id"]: r for r in json.loads(out_path.read_text())
                if not r.get("error")}
    todo = [c for c in cases if c["id"] not in done]
    print(f"{case_path.name}: {len(cases)} cases, {len(done)} cached, {len(todo)} to call")

    def one(case: dict) -> dict:
        prompt = build_prompt(template, case.get("title", ""), case.get("abstract", ""), chars)
        text, err, usage = call(model, prompt)
        rec = {"id": case["id"], "doi": case.get("doi", ""), "usage": usage,
               "error": err or "", "raw": (text or "")[:600],
               "hard_signal": hard_signal(case.get("title", ""), case.get("abstract", ""))}
        rec.update(parse(text or ""))
        return rec

    results = list(done.values())
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, rec in enumerate(pool.map(one, todo), 1):
                results.append(rec)
                if i % 100 == 0:
                    print(f"  {i}/{len(todo)}")
                    out_path.write_text(json.dumps(results, indent=1))
    order = {c["id"]: i for i, c in enumerate(cases)}
    results.sort(key=lambda r: order.get(r["id"], 1 << 30))
    out_path.write_text(json.dumps(results, indent=1))

    verdicts = Counter(r.get("verdict") or "SCHEMA_ERROR" for r in results)
    errs = sum(1 for r in results if r.get("error"))
    tok_in = sum((r.get("usage") or {}).get("in", 0) for r in results)
    tok_out = sum((r.get("usage") or {}).get("out", 0) for r in results)
    print(f"  -> {out_path.name}: {dict(verdicts)}  api_errors={errs}  "
          f"tokens in={tok_in} out={tok_out}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a.split("=")[0]: a.split("=", 1)[1] for a in sys.argv[1:] if "=" in a}
    if len(args) < 3:
        print(__doc__)
        sys.exit(1)
    model, prompt_file, *case_files = args
    template = (HERE / prompt_file).read_text() if not Path(prompt_file).is_absolute() \
        else Path(prompt_file).read_text()
    tag = Path(prompt_file).stem.replace("prompt_", "")
    for cf in case_files:
        chars = int(opts.get("--chars", 0))
        run_set(model, template, tag + (f"c{chars}" if chars else ""), 
                HERE / cf if not Path(cf).is_absolute() else Path(cf),
                int(opts.get("--limit", 0)), int(opts.get("--workers", 8)), chars)


if __name__ == "__main__":
    main()
