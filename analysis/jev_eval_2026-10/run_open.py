"""Open-weight decision models on the same 2,186 cases, 2026-10-28, with the production
request (build_screen_decision, 4,000-character abstracts). Resumable; output
out/<name>_full2.json in the eval's row format.

  run_open.py clef | clefflash | drex15 | kev4b | tev1 | span02
"""
import json, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from shared.config import OPENROUTER_API_KEY as KEY  # noqa: E402
from shared.prompts import build_screen_decision  # noqa: E402
from shared.llm_client import _decision_probs  # noqa: E402
URL = "https://openrouter.ai/api/alpha/decisions"
# Dated ids where OpenRouter has one; Clef's are undated (weights unchanged on Hugging
# Face since the 2026-10-01 release; later commits are model-card edits).
MODELS = {"clef": "cloudflare/clef", "clefflash": "cloudflare/clef-flash",
          "drex15": "nace-ai/drex-v1.5-20261009", "kev4b": "jaredpalmer/kev-4b-20260924",
          "tev1": "togethercomputer/tev1-4b-experimental-20260923",
          "span02": "respan/span-02-20261015"}
_lock = threading.Lock(); _next = [0.0]; _local = threading.local()

def _pace(interval=0.15):
    with _lock:
        now = time.monotonic(); wait = _next[0] - now; _next[0] = max(now, _next[0]) + interval
    if wait > 0: time.sleep(wait)

def decide(body):
    if not hasattr(_local, "s"): _local.s = requests.Session()
    last = ""
    for attempt in range(5):
        if attempt: time.sleep(min(20, 2 ** attempt))
        _pace()
        try:
            r = _local.s.post(URL, json=body, timeout=120, headers={"Authorization": f"Bearer {KEY}"})
            if r.status_code in (400, 404):
                return {"error": f"HTTP {r.status_code}: {r.text[:300]}"}
            if r.status_code != 200:
                last = f"HTTP {r.status_code}: {r.text[:200]}"; continue
            d = r.json(); a = d["answers"]["screen"]; u = d.get("usage") or {}
            return {"error": "", "served": d.get("model"),
                    "usage": {"in": u.get("input_tokens", 0), "usd": u.get("cost", 0)},
                    "choice": a.get("choice"), "probs": _decision_probs(a.get("probabilities"))}
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"[:300]
    return {"error": last}

def run(name, limit=0, workers=8):
    model = MODELS[name]
    cases = json.loads((HERE / "cases.json").read_text())
    if limit: cases = cases[:limit]
    path = HERE / "out" / f"{name}_full2.json"
    done = {r["id"]: r for r in json.loads(path.read_text()) if not r.get("error")} if path.exists() else {}
    todo = [c for c in cases if c["id"] not in done]; results = list(done.values())
    one = lambda c: {"id": c["id"], **decide({"model": model, **build_screen_decision(c["title"], c["abstract"])})}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, rec in enumerate(pool.map(one, todo), 1):
            results.append(rec)
            if i % 250 == 0:
                path.write_text(json.dumps(results)); print(f"{name}: {i}/{len(todo)}", flush=True)
    path.write_text(json.dumps(results))
    errs = [r for r in results if r.get("error")]
    usd = sum((r.get("usage") or {}).get("usd") or 0 for r in results if not r.get("error"))
    print(f"{name}: {len(results)} rows, {len(errs)} errors, ${usd:.4f}, served {sorted({r.get('served') for r in results if not r.get('error')})}", flush=True)
    for e in errs[:2]: print("  error:", e["id"], e["error"][:200], flush=True)

if __name__ == "__main__":
    run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0)
