"""Pilot of the final AI check on human-validated Stage 3 rows.

The evaluation behind the release rule in `extract/final_check.py`. Selects rows from
the 207 records validated since August
(`work/stage3_removal/tier_features.csv` in the main checkout), joins each to its row
in the newest `data/extracted.csv` on doi_r + doi_o, attaches the human verdict per
field, runs the blind recode (`final_check.py`) and writes `pilot_results.csv`.

Human truth per field, from `sb.json`:
  - a record with final_* values (validated / consensus): correct iff final == shipped;
  - a rejected record: type wrong, original and outcome not applicable;
  - otherwise the human checks: correct / incorrect when they agree, split when not.

`--batch 1` and `--batch 2` re-score the recorded samples; `--cached-only` refuses to
buy a coding the cache (`cache/final_check/`) does not hold. `--draw` draws a new informative sample. Run from
a checkout holding `data/`, `work/` and `cache/`; from a worktree, name that checkout:

    FLORA_MAIN_CHECKOUT=/path/to/checkout FLORA_CACHE_DIR=/path/to/checkout/cache \
      .venv/bin/python analysis/final_ai_check_2026-10/pilot.py --batch 1 --cached-only
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from extract.final_check import (ROOT, SourceIndex, _acquired_document,  # noqa: E402
                                 build_prompt, compare_work, document_text, recode)
from shared.utils import clean_doi  # noqa: E402

# A worktree has no data/ or work/ of its own: the newest export and the validation
# dump live in the main checkout, uncommitted.
MAIN = Path(os.environ.get("FLORA_MAIN_CHECKOUT", ROOT))
WORK = MAIN / "work/stage3_removal"
OUT = Path(__file__).resolve().parent
DRAW_SEED = 1


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
        named = {_doi_in(x) for x in [rec["final_doi_o"] or ""]
                 + [q["corrected_doi_o"] or "" for q in qs]} - {"", clean_doi(rec["doi_o"] or "")}
        rows.append({"rid": rid, "doi_r": clean_doi(rec["doi_r"] or ""),
                     "doi_o": clean_doi(rec["doi_o"] or ""), "sb_outcome": rec["outcome"],
                     "status": rec["validation_status"],
                     "truth_type": truth["type"], "truth_original": truth["original"],
                     "truth_outcome": truth["outcome"],
                     "final_outcome": rec["final_outcome"] or "",
                     "human_corrections": " | ".join(sorted(corrected)),
                     "human_named_dois": " ".join(sorted(named))})
    return pd.DataFrame(rows).merge(tf[["rid", "lm", "fulltext"]], on="rid")


def _doi_in(text: str) -> str:
    """The DOI inside a validator's entry, which may be a doi.org or psycnet URL."""
    m = re.search(r"10\.\d{4,9}/\S+", text or "")
    return clean_doi(m.group(0)) if m else ""


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
        row = {**(same if len(same) else m).iloc[0].to_dict(), **t.to_dict(),
               "truth_original_raw": t.truth_original}
        # The unit is the replication-original PAIR. A validator shown one row of a
        # multi-original work cannot see its siblings, and "corrects" the original to
        # one the pipeline already shipped as another row: that pair exists and is not
        # an error of this row. The outcome judgment made alongside it was about the
        # other original, so it is not scored against this row either.
        siblings = {r["_o"] for r in work_rows(row)} - {t.doi_o}
        named = set(t.human_named_dois.split())
        if t.truth_original in ("incorrect", "split") and named and named <= siblings:
            row["truth_original"] = "sibling"
            if t.truth_outcome != "correct":
                row["truth_outcome"] = "unscored"
        joined.append(row)
    return pd.DataFrame(joined)


def _draw(stratum: pd.DataFrame, n: int) -> pd.DataFrame:
    """A fixed-seed draw, independent of the input's row order."""
    return stratum.sort_values("rid").sample(min(n, len(stratum)), random_state=DRAW_SEED)


def select(df: pd.DataFrame) -> pd.DataFrame:
    """About ten informative rows: errors of each kind, the weak link method, full text.

    A new draw. The recorded batch 1 was taken in file order and is identified by the
    rids in `pilot_results.csv` (`recorded`), not by this function.
    """
    df = df.assign(any_wrong=(df[["truth_type", "truth_original", "truth_outcome"]]
                              == "incorrect").any(axis=1),
                   all_right=(df[["truth_type", "truth_original", "truth_outcome"]]
                              == "correct").all(axis=1))
    picks = [
        _draw(df[df.truth_type == "incorrect"], 2),                       # rejected / wrong type
        # The raw human verdict, before sibling reassignments are recognised, so the
        # sample stays the one the pilot was drawn as.
        _draw(df[(df.truth_original_raw == "incorrect") & (df.truth_type == "correct")], 4),
        _draw(df[(df.truth_outcome == "incorrect") & (df.final_outcome != "")], 2),
        _draw(df[(df.truth_outcome == "incorrect") & (df.final_outcome == "")], 1),
        _draw(df[df.all_right & (df.lm == "llm_title_search") & (df.fulltext == True)], 2),
        _draw(df[df.all_right & (df.fulltext == False) & (df.lm != "llm_title_search")], 1),
        _draw(df[df.all_right & (df.type == "reproduction")], 1),
    ]
    return pd.concat(picks).drop_duplicates("rid")


def recorded(name: str) -> set[str]:
    """The rids of a recorded batch: its results file is the record of its draw."""
    return set(pd.read_csv(OUT / name, usecols=["rid"], dtype=str).rid)


def recorded_rows(joined: pd.DataFrame, name: str) -> pd.DataFrame:
    """A recorded batch's rows, whole: a re-score overwrites the batch's results file,
    so a rid that no longer joins must stop the run rather than shrink the record."""
    missing = recorded(name) - set(joined.rid)
    if missing:
        raise SystemExit(f"{len(missing)} rid(s) of {name} no longer join: {sorted(missing)}")
    return joined[joined.rid.isin(recorded(name))]


def _human_verdict(df: pd.DataFrame) -> pd.Series:
    """correct / error / undetermined per row, scored on the pair."""
    truth = df[["truth_type", "truth_original", "truth_outcome"]]
    out = pd.Series("undetermined", index=df.index)
    out[(truth == "incorrect").any(axis=1)] = "error"
    out[(truth["truth_type"] == "correct") & truth["truth_original"].isin(["correct", "sibling"])
        & (truth["truth_outcome"] == "correct")] = "correct"
    return out


def run_one(row: dict, cached_only: bool = False) -> dict:
    """Recode the work once and return this row's result within the work comparison."""
    text, method, texts = document_text(row)
    if not text and _acquired_document(row):
        raise SystemExit(f"{row['rid']}: its document is not in this parse cache; a "
                         "re-score from the abstract would change the evidence")
    rec = recode(build_prompt(row, text), cached_only=cached_only)
    sources = [SourceIndex(f"{row.get('title_r', '')}\n{row.get('abstract_r', '')}")]
    sources += [SourceIndex(t) for t in texts.values()]
    mine = next(r for r in compare_work(work_rows(row), rec["coding"], sources)
                if r["pair_id"] == row["pair_id"])
    return {"rid": row["rid"], "fulltext_sent": method, "fulltext_chars": len(text),
            "seconds": rec["seconds"], "input_tokens": rec["input_tokens"],
            "output_tokens": rec["output_tokens"], **mine}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch", type=int, default=1, choices=(1, 2))
    ap.add_argument("--draw", action="store_true",
                    help="draw a new sample with select() instead of a recorded batch")
    ap.add_argument("--cached-only", action="store_true",
                    help="raise on a cache miss rather than call claude -p")
    args = ap.parse_args()
    joined = join_rows(human_truth())
    print(f"{len(joined)} validated records joined to extracted.csv")
    batch_file = "pilot_results.csv" if args.batch == 1 else "pilot_results_batch2.csv"
    if args.draw:
        taken = recorded("pilot_results.csv") | recorded("pilot_results_batch2.csv")
        if (OUT / "pilot_results_draw.csv").exists():
            taken |= recorded("pilot_results_draw.csv")
        pilot = select(joined[~joined.rid.isin(taken)])
    else:
        pilot = recorded_rows(joined, batch_file)
    print(f"{len(pilot)} pilot rows")
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda r: run_one(r, args.cached_only),
                                [r.to_dict() for _, r in pilot.iterrows()]))
    keep = ["rid", "pair_id", "doi_r", "openalex_id_r", "url_r", "title_r", "n_originals",
            "link_method", "pdf_source", "type", "doi_o",
            "authors_o", "year_o", "title_o", "outcome", "outcome_computation",
            "outcome_robustness", "outcome_phrase", "out_quote_source", "status",
            "truth_type", "truth_original", "truth_outcome", "final_outcome",
            "human_corrections", "human_named_dois"]
    out = pilot[keep].merge(pd.DataFrame(results).drop(columns="pair_id"), on="rid")
    out["human_verdict"] = _human_verdict(out)
    name = "pilot_results_draw.csv" if args.draw else batch_file
    out.to_csv(OUT / name, index=False, encoding="utf-8-sig")
    show = ["link_method", "type", "blind_type", "truth_type", "original_grade",
            "truth_original", "outcome", "blind_outcome", "outcome_grade", "truth_outcome",
            "quote_verdict", "work_n_shipped", "work_n_blind", "released", "human_verdict"]
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(out[show].to_string())


if __name__ == "__main__":
    main()
