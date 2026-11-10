"""Span-01 and Span-01 Lite accept only `noul` (yes/no) questions, so they get the production
rules as one: yes = replication/reproduction/both, no = none/unclear. P(yes) is the quantity the
gate thresholds, P(qualifies). They name no type, so the row is stored with all of P(yes) on
`replication` and `notype: true` (combos.dec_type then defers to Luna). -> out/<name>_full2.json

  run_span01.py span01 | span01lite [limit]
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from shared.prompts import build_screen_decision  # noqa: E402

URL = "https://openrouter.ai/api/alpha/decisions"
MODELS = {"span01": "respan/span-01-20260925", "span01lite": "respan/span-01-lite-20260925"}

def body(model: str, title: str, abstract: str) -> dict:
    b = build_screen_decision(title, abstract)
    q = b["questions"]["screen"]; c = q["criteria"]
    instr = q["instructions"].replace("Choose exactly one category.", "Answer whether the paper qualifies.")
    return {"model": model, "state": b["state"], "questions": {"screen": {
        "type": "noul", "instructions": instr,
        "criteria": {"true": " ".join(f"{k}: {c[k]}" for k in ("replication", "reproduction", "both")),
                     "false": f"none: {c['none']} unclear: {c['unclear']}"}}}}

def decide(model: str, case: dict) -> dict:
    last = ""
    for attempt in range(5):
        if attempt:
            time.sleep(min(20, 2 ** attempt))
        try:
            r = requests.post(URL, json=body(model, case["title"], case["abstract"]), timeout=120,
                              headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
            if r.status_code in (400, 404):
                return {"id": case["id"], "error": f"HTTP {r.status_code}: {r.text[:300]}"}
            if r.status_code != 200:
                last = f"HTTP {r.status_code}: {r.text[:200]}"; continue
            d = r.json(); p = float(d["answers"]["screen"]["noul"]); u = d.get("usage") or {}
            assert 0.0 <= p <= 1.0, p
            return {"id": case["id"], "error": "", "served": d.get("model"), "notype": True,
                    "usage": {"in": u.get("input_tokens", 0), "usd": u.get("cost", 0)},
                    "probs": {"replication": p, "reproduction": 0.0, "both": 0.0, "none": 1 - p, "unclear": 0.0}}
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"[:300]
    return {"id": case["id"], "error": last}

def run(name: str, limit: int = 0) -> None:
    cases = json.loads((HERE.parent / "jev_eval_2026-10" / "cases.json").read_text())[: limit or None]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda c: decide(MODELS[name], c), cases))
    (HERE / "out" / f"{name}_full2.json").write_text(json.dumps(rows))
    errs = [r for r in rows if r.get("error")]
    usd = sum(r["usage"]["usd"] or 0 for r in rows if not r.get("error"))
    print(f"{name}: {len(rows)} rows, {len(errs)} errors, ${usd:.4f}")
    for e in errs[:2]:
        print("  error:", e["id"], e["error"][:200])

if __name__ == "__main__":
    run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0)
