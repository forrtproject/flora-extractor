"""Perplexity Decider v1.2 and Decider Flash v1.2 on the same 2,186 cases, 2026-10-28.

v1.1 (the pplx_full2 run) was withdrawn from OpenRouter after v1.2's release on
2026-10-16. Each request is exactly the production one, build_screen_decision() with its
4,000-character abstract; the `*_whole` runs repeat the 51 cases whose abstract is longer
with the whole abstract, as pplx_full2 was sent, so v1.2 can be read against v1.1 on
identical states. Resumable; output out/<name>.json in the eval's row format.

  run_v12.py pplx12_full2 | pplx12_full2_whole | pplx12f_full2 | pplx12f_full2_whole
"""
import json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from shared.config import OPENROUTER_API_KEY as KEY  # noqa: E402
from shared.prompts import build_screen_decision  # noqa: E402
from shared.llm_client import _decision_probs  # noqa: E402

MODELS = {"pplx12": "perplexity/pplx-decider-v1.2-20261016",
          "pplx12f": "perplexity/pplx-decider-flash-v1.2-20261016"}
URL = "https://openrouter.ai/api/alpha/decisions"
_lock = threading.Lock(); _next = [0.0]
_local = threading.local()


def _pace(interval: float = 0.2) -> None:
    with _lock:
        now = time.monotonic(); wait = _next[0] - now
        _next[0] = max(now, _next[0]) + interval
    if wait > 0:
        time.sleep(wait)


def body_for(model: str, title: str, abstract: str, truncate: bool) -> dict:
    b = build_screen_decision(title, abstract)
    if not truncate:
        b["state"] = f"Title: {title or '(not available)'}\n\nAbstract: {abstract or '(not available)'}"
    return {"model": model, **b}


def decide(body: dict) -> dict:
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    last = ""
    for attempt in range(5):
        if attempt:
            time.sleep(min(20, 2 ** attempt))
        _pace()
        try:
            r = _local.s.post(URL, json=body, timeout=90, headers={"Authorization": f"Bearer {KEY}"})
            if r.status_code == 404:
                return {"error": f"HTTP 404: {r.text[:200]}"}
            if r.status_code != 200:
                last = f"HTTP {r.status_code}: {r.text[:200]}"; continue
            d = r.json(); a = d["answers"]["screen"]; u = d.get("usage") or {}
            return {"error": "", "served": d.get("model"),
                    "usage": {"in": u.get("input_tokens", 0), "usd": u.get("cost", 0)},
                    "choice": a.get("choice"), "probs": _decision_probs(a.get("probabilities"))}
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"[:200]
    return {"error": last}


def run(variant: str, limit: int = 0, workers: int = 8) -> None:
    cases = json.loads((HERE / "cases.json").read_text())
    untrunc = variant.endswith("_whole")
    model = MODELS[variant.split("_")[0]]
    if untrunc:
        cases = [c for c in cases if len(c.get("abstract") or "") > 4000]
    if limit:
        cases = cases[:limit]
    path = HERE / "out" / f"{variant}.json"
    done = {r["id"]: r for r in json.loads(path.read_text()) if not r.get("error")} if path.exists() else {}
    todo = [c for c in cases if c["id"] not in done]
    results = list(done.values())
    def one(c):
        return {"id": c["id"], **decide(body_for(model, c["title"], c["abstract"], not untrunc))}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, rec in enumerate(pool.map(one, todo), 1):
            results.append(rec)
            if i % 250 == 0:
                path.write_text(json.dumps(results)); print(f"{variant}: {i}/{len(todo)}", flush=True)
    path.write_text(json.dumps(results))
    errs = [r for r in results if r.get("error")]
    usd = sum((r.get("usage") or {}).get("usd") or 0 for r in results if not r.get("error"))
    served = sorted({r.get("served") for r in results if not r.get("error")})
    print(f"{variant}: {len(results)} rows, {len(errs)} errors, ${usd:.4f}, served {served}", flush=True)
    for e in errs[:3]:
        print("  error:", e["id"], e["error"][:160], flush=True)


if __name__ == "__main__":
    run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0)
