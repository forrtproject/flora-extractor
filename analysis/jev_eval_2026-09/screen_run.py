"""The front-door screen's 390-case eval (analysis/screening_eval), one voter at a time.

  screen_run.py <voter> [<voter> ...]     voters: ds4 ds41 luna jev

LLM voters go through shared.llm_client._classify_once — the production vote: v3.3
prompt, schema parse, OpenRouter host routing, the voter's effort — and nothing is read
from or written to the LLM cache. Jev gets the two Noul questions of jev.py. Output:
out_screen/<voter>.json, resumable.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[2]
SE = REPO / "analysis" / "screening_eval"
sys.path.insert(0, str(REPO))
from shared.llm_client import _classify_once  # noqa: E402
from shared.prompts import build_classify_prompt  # noqa: E402
import jev  # noqa: E402

VOTERS = {"ds4": ("deepseek/deepseek-v4-flash", "low"),
          "ds41": ("deepseek/deepseek-v4.1-flash", "low"),
          "luna": ("gpt-6-luna", "low")}
OUT = HERE / "out_screen"


def cases() -> list[dict]:
    out = []
    for f in ("human_cases", "heldout_cases", "flora_positive_cases"):
        out += json.loads((SE / f"{f}.json").read_text(encoding="utf-8"))
    return out


def run(voter: str) -> None:
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{voter}.json"
    done = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))
            if not r.get("error")} if path.exists() else {}
    todo = [c for c in cases() if c["id"] not in done]

    def one(c: dict) -> dict:
        if voter == "jev":
            return {"id": c["id"], **jev.ask(c["title"], c["abstract"])}
        model, effort = VOTERS[voter]
        vote = _classify_once(build_classify_prompt(c["title"], c["abstract"]), model, effort)
        if not vote:
            return {"id": c["id"], "error": "no vote"}
        return {"id": c["id"], "error": "", **{k: vote.get(k) for k in
                ("classification", "confident", "served_by", "model")}}

    results = list(done.values())
    with ThreadPoolExecutor(max_workers=6) as pool:
        for i, rec in enumerate(pool.map(one, todo), 1):
            results.append(rec)
            if i % 50 == 0:
                path.write_text(json.dumps(results), encoding="utf-8")
    path.write_text(json.dumps(results), encoding="utf-8")
    print(f"{voter}: {len(results)} rows, {sum(1 for r in results if r.get('error'))} errors",
          flush=True)


if __name__ == "__main__":
    for v in sys.argv[1:]:
        run(v)
