"""Run one voter over the pre-screen eval's four case sets; resumable.

  run.py jev
  run.py <openrouter-model-id> [--effort=minimal]

Chat models get the shipped p7 prompt exactly as analysis/prescreen_eval/eval_prescreen.py
sends it (its build_prompt and parse are imported, so "only an explicit parsed no" is the
same rule). Output: out/<model>_<bucket>.json, one record per case, with OpenRouter's
reported cost.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[2]
EVAL = REPO / "analysis" / "prescreen_eval"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(EVAL))
from shared.config import OPENROUTER_API_KEY  # noqa: E402
from shared.prescreen import hard_signal  # noqa: E402
from eval_prescreen import build_prompt, parse  # noqa: E402
import jev  # noqa: E402

BUCKETS = ["goldneg_screen", "goldpos_flora", "goldpos_repro", "goldneg_curated"]
P7 = (EVAL / "prompt_p7.txt").read_text(encoding="utf-8")
OUT = HERE / "out"


def chat(model: str, prompt: str, effort: str, retries: int = 6) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2000 if effort else 200, "usage": {"include": True},
            "provider": {"sort": "price"}}
    if effort:
        body["reasoning"] = {"effort": effort}
    else:
        body["temperature"] = 0
    last = ""
    for attempt in range(retries):
        try:
            r = jev.session().post("https://openrouter.ai/api/v1/chat/completions", timeout=120,
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                              json=body)
            if r.status_code == 200:
                d = r.json()
                text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                u = d.get("usage") or {}
                return {"raw": text[:400], "error": "",
                        "usage": {"in": u.get("prompt_tokens", 0),
                                  "out": u.get("completion_tokens", 0),
                                  "cost": u.get("cost", 0)},
                        **parse(text)}
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            time.sleep(min(30, 3 * 2 ** attempt) if r.status_code == 429 else 2 ** attempt)
        except Exception as e:  # noqa: BLE001 — API boundary
            last = f"{type(e).__name__}: {e}"[:200]
            time.sleep(2 ** attempt)
    return {"raw": "", "error": last, "usage": {}, "verdict": ""}


def run(model: str, effort: str, workers: int) -> None:
    OUT.mkdir(exist_ok=True)
    for bucket in BUCKETS:
        cases = json.loads((EVAL / f"cases_live_{bucket}.json").read_text(encoding="utf-8"))
        path = OUT / f"{model.replace('/', '_')}_{bucket}.json"
        done = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))
                if not r.get("error")} if path.exists() else {}
        todo = [c for c in cases if c["id"] not in done]

        def one(c: dict) -> dict:
            if model == "jev":
                res = jev.ask(c.get("title", ""), c.get("abstract", ""))
            else:
                res = chat(model, build_prompt(P7, c.get("title", ""), c.get("abstract", "")),
                           effort)
            return {"id": c["id"], "hard_signal": hard_signal(c.get("title", ""),
                                                              c.get("abstract", "")), **res}

        results = list(done.values())
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, rec in enumerate(pool.map(one, todo), 1):
                results.append(rec)
                if i % 100 == 0:
                    path.write_text(json.dumps(results), encoding="utf-8")
        path.write_text(json.dumps(results), encoding="utf-8")
        errs = sum(1 for r in results if r.get("error"))
        cost = sum((r.get("usage") or {}).get("cost", 0) or 0 for r in results)
        print(f"{model} {bucket}: {len(results)} rows, {errs} errors, ${cost:.4f}", flush=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--"))
    run(args[0], opts.get("effort", ""), int(opts.get("workers", 6)))
