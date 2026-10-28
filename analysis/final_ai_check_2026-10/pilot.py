"""Pilot of the final AI check on human-validated Stage 3 rows.

Selects rows from the 207 records validated since August
(`work/stage3_removal/tier_features.csv` in the main checkout), joins each to its row
in the newest `data/extracted.csv` on doi_r + doi_o, attaches the human verdict per
field, runs the blind recode (`final_check.py`) and writes `pilot_results.csv`.

Human truth per field, from `sb.json`:
  - a record with final_* values (validated / consensus): correct iff final == shipped;
  - a rejected record: type wrong, original and outcome not applicable;
  - otherwise the human checks: correct / incorrect when they agree, split when not.

    FLORA_CACHE_DIR=/home/lukas/flora-extractor/cache \
      /home/lukas/flora-extractor/.venv/bin/python analysis/final_ai_check_2026-10/pilot.py [--n-extra 0]
"""
from __future__ import annotations

import argparse
import collections
import json
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import pandas as pd

from final_check import (SourceIndex, build_prompt, compare,  # noqa: E402
                         document_text, recode)
from shared.utils import clean_doi

# The worktree has no data/ or work/ of its own: the newest export and the validation
# dump live in the main checkout, uncommitted.
MAIN = Path("/home/lukas/flora-extractor")
WORK = MAIN / "work/stage3_removal"
OUT = Path(__file__).resolve().parent


def human_truth() -> pd.DataFrame:
    sb = json.load(open(WORK / "sb.json"))
    u = {r["record_id"]: r for r in sb["u"]}
    checks = collections.defaultdict(list)
    for q in sb["q"]:
        if q["is_validated"] and q["validator_slot"].startswith("human"):
            checks[q["record_id"]].append(q)
    tf = pd.read_csv(WORK / "tier_features.csv")
    rows = []
    for rid in tf.rid:
        rec, qs = u[rid], checks[rid]

        def vote(field: str) -> str:
            v = {q[field] for q in qs}
            return v.pop() if len(v) == 1 else ("split" if v else "")

        if rec["validation_status"] == "rejected":
            truth = {"type": "incorrect", "original": "n/a", "outcome": "n/a"}
        elif rec["final_type"]:
            truth = {"type": "correct" if rec["final_type"] == rec["type"] else "incorrect",
                     "original": "correct" if clean_doi(rec["final_doi_o"] or "") ==
                                 clean_doi(rec["doi_o"] or "") else "incorrect",
                     "outcome": "correct" if rec["final_outcome"] == rec["outcome"]
                                else "incorrect"}
        else:
            truth = {f: vote(f"{f}_check") for f in ("type", "original", "outcome")}
        corrected = {q[f] for q in qs for f in ("corrected_type", "corrected_outcome",
                                                "corrected_doi_o") if q[f]}
        rows.append({"rid": rid, "doi_r": clean_doi(rec["doi_r"] or ""),
                     "doi_o": clean_doi(rec["doi_o"] or ""), "sb_outcome": rec["outcome"],
                     "status": rec["validation_status"],
                     "truth_type": truth["type"], "truth_original": truth["original"],
                     "truth_outcome": truth["outcome"],
                     "final_outcome": rec["final_outcome"] or "",
                     "human_corrections": " | ".join(sorted(corrected))})
    return pd.DataFrame(rows).merge(tf[["rid", "lm", "fulltext"]], on="rid")


@lru_cache(maxsize=1)
def extracted() -> pd.DataFrame:
    ex = pd.read_csv(MAIN / "data/extracted.csv", dtype=str, keep_default_na=False)
    ex["_r"], ex["_o"] = ex.doi_r.map(clean_doi), ex.doi_o.map(clean_doi)
    ex["_work"] = ex.oa_work_id_r.where(ex.oa_work_id_r != "", ex._r)
    return ex


def work_rows(row: dict) -> list[dict]:
    """Every shipped row of the same work: one per original."""
    ex = extracted()
    return ex[ex._work == row["_work"]].to_dict("records")


def join_rows(truth: pd.DataFrame) -> pd.DataFrame:
    ex = extracted()
    joined = []
    for _, t in truth.iterrows():
        m = ex[(ex._r == t.doi_r) & (ex._o == t.doi_o)]
        if m.empty:
            continue
        same = m[m.outcome == t.sb_outcome]
        joined.append({**(same if len(same) else m).iloc[0].to_dict(), **t.to_dict(),
                       "shipped_outcome_now": (same if len(same) else m).iloc[0].outcome})
    return pd.DataFrame(joined)


def select(df: pd.DataFrame) -> pd.DataFrame:
    """About ten informative rows: errors of each kind, the weak link method, full text."""
    df = df.assign(any_wrong=(df[["truth_type", "truth_original", "truth_outcome"]]
                              == "incorrect").any(axis=1),
                   all_right=(df[["truth_type", "truth_original", "truth_outcome"]]
                              == "correct").all(axis=1))
    picks = [
        df[df.truth_type == "incorrect"].head(2),                         # rejected / wrong type
        df[(df.truth_original == "incorrect") & (df.truth_type == "correct")].head(4),
        df[(df.truth_outcome == "incorrect") & (df.final_outcome != "")].head(2),
        df[(df.truth_outcome == "incorrect") & (df.final_outcome == "")].head(1),
        df[df.all_right & (df.lm == "llm_title_search") & (df.fulltext == True)].head(2),
        df[df.all_right & (df.fulltext == False) & (df.lm != "llm_title_search")].head(1),
        df[df.all_right & (df.type == "reproduction")].head(1),
    ]
    return pd.concat(picks).drop_duplicates("rid")


def run_one(row: dict) -> dict:
    text, method, texts = document_text(row)
    prompt = build_prompt(row, text)
    rec = recode(prompt)
    sources = [SourceIndex(f"{row.get('title_r', '')}\n{row.get('abstract_r', '')}")]
    sources += [SourceIndex(t) for t in texts.values()]
    return {"rid": row["rid"], "fulltext_sent": method, "fulltext_chars": len(text),
            "seconds": rec["seconds"], "input_tokens": rec["input_tokens"],
            "output_tokens": rec["output_tokens"],
            **compare(row, rec["coding"], sources, work_rows(row))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    joined = join_rows(human_truth())
    print(f"{len(joined)} validated records joined to extracted.csv")
    pilot = select(joined)
    print(f"{len(pilot)} pilot rows")
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(run_one, [r.to_dict() for _, r in pilot.iterrows()]))
    keep = ["rid", "doi_r", "openalex_id_r", "url_r", "title_r", "n_originals", "link_method", "pdf_source", "type", "doi_o",
            "authors_o", "year_o", "title_o", "outcome", "outcome_computation",
            "outcome_robustness", "outcome_phrase", "out_quote_source", "status",
            "truth_type", "truth_original", "truth_outcome", "final_outcome",
            "human_corrections"]
    out = pilot[keep].merge(pd.DataFrame(results), on="rid")
    out.to_csv(OUT / "pilot_results.csv", index=False)
    show = ["link_method", "type", "blind_type", "truth_type", "original_grade",
            "truth_original", "outcome", "blind_outcome", "outcome_grade", "truth_outcome",
            "quote_verdict", "all_agree", "seconds"]
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(out[show].to_string())


if __name__ == "__main__":
    main()
