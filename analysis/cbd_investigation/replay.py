"""Replay cached target+outcome prompts on gpt-6-luna under four variants.

  V0  HEAD rules      + the evidence the row was coded from (the cached prompt's PAPER part)
  V1  edited rules    + same evidence                       (prompt-only fix)
  V2  HEAD rules      + full parsed body instead of slices  (evidence-only fix; doc rows)
  V3  edited rules    + full parsed body                    (both; doc rows)
  V0r a repeat of V0, to measure run-to-run noise (controls + adjudicated only)

The prefix is re-rendered from HEAD's builders (variants.py), so every variant asks
the CURRENT production question and differs only by the edit; the per-row evidence is
lifted verbatim from the cached `llm_prompt`. Answers are cached under out/replay/.

Usage: .venv/bin/python analysis/cbd_investigation/replay.py [--dry] [--workers 6]
"""
import argparse
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ["OPENAI_DAILY_TOKEN_BUDGET"] = "0"
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))
ROOT = HERE.parent.parent
os.chdir(ROOT)

import pandas as pd  # noqa: E402
from shared.config import LINKING_EFFORT, LINKING_MODEL, PARSE_CACHE_DIR  # noqa: E402
from shared.llm_client import call_model  # noqa: E402
from shared.pdf_parsing import best_parse_result, read_parse_cache  # noqa: E402
from shared.row_key import primary_key  # noqa: E402
import variants as V  # noqa: E402

OUT = HERE / "out" / "replay"
OUT.mkdir(parents=True, exist_ok=True)
SLICE_HEADERS = ("INTRODUCTION:\n", "METHODS:\n", "DISCUSSION / CONCLUSION (from",
                 V.FULL_BODY_HEADER[:20])
TAIL_HEADERS = ("WORKS THIS PAPER CITES", "REFERENCE LIST:\n", "Respond with the JSON object only.")


def evidence_of(prompt: str) -> str:
    i = prompt.find("\n\nPAPER\n\n")
    return prompt[i:]


def with_full_body(evidence: str, body: str) -> str:
    """Replace the intro/methods/closing slices with the whole body (production's full_body block)."""
    starts = [evidence.find("\n\n" + h) for h in SLICE_HEADERS]
    starts = [s for s in starts if s >= 0]
    tails = [evidence.find("\n\n" + h) for h in TAIL_HEADERS]
    tail = min(t for t in tails if t >= 0)
    start = min(starts) if starts else tail
    block = "\n\n" + V.FULL_BODY_HEADER + body[:V.FULL_BODY_CHARS]
    return evidence[:start] + block + evidence[tail:]


def body_for(row: pd.Series) -> str:
    cid = row.doi_r or primary_key(row.to_dict())
    res = read_parse_cache(cid, PARSE_CACHE_DIR)
    best = best_parse_result(res) if res else None
    return str((best or {}).get("raw_text") or "").strip()


def call(prompt: str) -> dict:
    h = hashlib.sha256((LINKING_MODEL + LINKING_EFFORT + prompt).encode()).hexdigest()[:24]
    f = OUT / f"{h}.json"
    if f.exists():
        return json.load(open(f))
    result, provider, err = call_model(prompt, LINKING_MODEL, reasoning_effort=LINKING_EFFORT)
    rec = {"result": result, "err": err, "prompt_len": len(prompt)}
    if result is not None:
        json.dump(rec, open(f, "w"))
    return rec


def ref_titles(evidence: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in
            re.finditer(r"^(@\S+)\s+.*?\(\S*?\)\. (.*)$", evidence, re.M)}


def _words(s: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", s.lower()))


def match_target(answer: dict | None, row: pd.Series, evidence: str) -> dict | None:
    ts = (answer or {}).get("targets") or []
    titles = ref_titles(evidence)
    tw = _words(row.title_o)
    for t in ts:
        k = t.get("key")
        if k and k in titles and tw:
            w = _words(titles[k])
            if len(w & tw) / max(1, len(w | tw)) >= 0.5:
                return t
    au = ""
    if row.authors_o:
        first = row.authors_o.split(";")[0].strip()
        au = (first.split(",")[0] if "," in first else first.split()[-1]).lower()
    for t in ts:
        named = str(t.get("target_as_named") or "").lower()
        if au and au in named:
            return t
    return ts[0] if len(ts) == 1 else None


def run_row(row: pd.Series, dry: bool = False) -> list[dict]:
    src = row.ft_file if row.ft_file else row.pre_file
    if not src:
        return []
    cached = json.load(open(src))
    prompt = cached["llm_prompt"]
    if "outcome_computation" in prompt[:20000]:
        return []                        # reproduction prompt — out of scope
    ev = evidence_of(prompt)
    rtc = "DISCUSSION / CONCLUSION (from" in ev or V.FULL_BODY_HEADER[:20] in ev
    head = V.head_prefix(rtc)
    edited = V.apply(head)
    plans = {"V0": head + ev, "V1": edited + ev}
    if not row.pdf_source:
        plans["V1b"] = V.apply(head, V.EDITS_B) + ev
    if row.pdf_source:
        body = body_for(row)
        if body:
            ev_b = with_full_body(ev, body)
            plans["V2"] = V.head_prefix(True) + ev_b
            plans["V3"] = V.apply(V.head_prefix(True)) + ev_b
    if row.group.startswith(("control", "adj")):
        plans["V0r"] = head + ev + " "      # one trailing space: a distinct cache entry
    if dry:
        return [{"variant": k, "chars": len(p)} for k, p in plans.items()]
    out = []
    for name, p in plans.items():
        rec = call(p)
        t = match_target(rec.get("result"), row, ev)
        out.append({"pair_id": row.pair_id, "group": row.group, "variant": name,
                    "src_rung": "ft" if row.ft_file else "pre",
                    "n_targets": len((rec.get("result") or {}).get("targets") or []),
                    "matched": t is not None, "outcome": (t or {}).get("outcome", ""),
                    "reason": (t or {}).get("outcome_reasoning", ""),
                    "phrase": (t or {}).get("outcome_phrase", ""),
                    "qsrc": (t or {}).get("out_quote_source", ""),
                    "study_status": (t or {}).get("study_status", ""),
                    "rtc": (t or {}).get("record_type_check", ""),
                    "key": (t or {}).get("key") or "",
                    "match_certain": (t or {}).get("match_certain", ""),
                    "err": rec.get("err", ""), "prompt_len": len(p)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    s = pd.read_csv(HERE / "out" / "sample.csv", dtype=str, keep_default_na=False)
    if a.dry:
        plans = pd.DataFrame([r for _, x in s.iterrows() for r in run_row(x, dry=True)])
        print(plans.groupby("variant").chars.agg(["size", "sum", "mean"]))
        print("approx input tokens", plans.chars.sum() / 4)
        return
    with ThreadPoolExecutor(a.workers) as ex:
        res = [r for rows in ex.map(lambda x: run_row(x[1]), s.iterrows()) for r in rows]
    pd.DataFrame(res).to_csv(HERE / "out" / "replay_results.csv", index=False)
    print(pd.DataFrame(res).groupby(["group", "variant"]).outcome.value_counts().unstack(fill_value=0))


if __name__ == "__main__":
    main()
