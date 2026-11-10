"""Run one voter over cases.json; resumable. Output: out/<voter>.json

  run.py ds4 | luna | jev

ds4/luna: the production vote — shared.llm_client._classify_once (v3.3 prompt, schema,
host routing, effort). The production per-vote cache is READ first (`_cached_vote`, a
hit costs nothing and is the very vote the screen used); a miss is bought and NOT
written back. Per-call tokens/usd are captured by wrapping `_record_tokens`.
jev: the p7 Noul question of analysis/jev_eval_2026-09/jev.py, alone (one question per
call, as it would be wired).
"""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "analysis" / "jev_eval_2026-09"))
from shared import llm_client as lc  # noqa: E402
from shared.prompts import build_classify_prompt  # noqa: E402
import jev  # noqa: E402

VOTERS = {"ds4": ("deepseek/deepseek-v4-flash", "low"), "luna": ("gpt-6-luna", "low")}
OUT = HERE / "out"
_usage = threading.local()
_orig_record = lc._record_tokens


def _capture(provider, model, n_in, n_out, cached_in=0, cache_write_in=0, usd=0.0):
    _usage.u = {"in": n_in, "out": n_out, "cached_in": cached_in, "usd": usd}
    _orig_record(provider, model, n_in, n_out, cached_in, cache_write_in, usd)


lc._record_tokens = _capture


def one(voter: str, c: dict) -> dict:
    if voter == "jev":
        r = jev.ask(c["title"], c["abstract"], {"p7": jev.QUESTIONS["p7"]})
        return {"id": c["id"], "error": r["error"], "p": r["p"].get("p7"),
                "usage": {"in": r["usage"].get("input_tokens", 0),
                          "usd": r["usage"].get("cost", 0)}}
    model, effort = VOTERS[voter]
    prompt = build_classify_prompt(c["title"], c["abstract"])
    hit = lc._cached_vote(c["doi"], c["title"], prompt, model, effort)
    _usage.u = {}
    vote = hit or lc._classify_once(prompt, model, effort)
    if not vote:
        return {"id": c["id"], "error": "no vote"}
    return {"id": c["id"], "error": "", "cache_hit": bool(hit),
            "classification": vote.get("classification"), "confident": vote.get("confident"),
            "usage": {} if hit else dict(_usage.u)}


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
            if i % 100 == 0:
                path.write_text(json.dumps(results), encoding="utf-8")
                print(f"{voter}: {i}/{len(todo)}", flush=True)
    path.write_text(json.dumps(results), encoding="utf-8")
    print(f"{voter}: {len(results)} rows, {sum(1 for r in results if r.get('error'))} errors",
          flush=True)


if __name__ == "__main__":
    for v in sys.argv[1:]:
        run(v)
