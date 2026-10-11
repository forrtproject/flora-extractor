"""Build the ~2k-case screen eval set for the Jev trial (2026-10).

Sources, each with its own label provenance (kept in `source`):
  human      — Supabase validation_queue human type_check per work: all 'correct' → pos,
               all 'not_validation' → neg (a screen false positive); admin final_type wins;
               conflicting works are dropped
  screeneval — analysis/screening_eval: 319 settled positives (F140 excluded), 70 hard negs
  goldpos    — analysis/prescreen_eval gold positives (FLoRA + reproductions)
  goldneg    — analysis/prescreen_eval screen-confirmed + curated negatives
Deduped on DOI (then title); the first source in that order wins.
Output: cases.json
"""
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
import shared.config  # noqa: E402,F401 — loads .env
from shared import supabase_client as sb  # noqa: E402
from shared.utils import clean_doi  # noqa: E402

SE = REPO / "analysis" / "screening_eval"
PE = REPO / "analysis" / "prescreen_eval"


def human() -> list[dict]:
    q = sb._get("validation_queue", {"select": "record_id,validator_slot,corrected_type",
                                     "is_validated": "eq.true"})
    u = {r["record_id"]: r for r in sb._get("unvalidated", {
        "select": "record_id,doi_r,oa_work_id_r,title_r,abstract_r,final_type"})}
    labels: dict = collections.defaultdict(set)
    for r in q:
        if r["validator_slot"].startswith("human"):
            labels[r["record_id"]].add("no" if r["corrected_type"] == "not_validation" else "yes")
    by_work: dict = collections.defaultdict(lambda: {"labels": set()})
    for rid, ls in labels.items():
        rec = u[rid]
        if rec["final_type"]:
            ls = {"no" if rec["final_type"] == "not_validation" else "yes"}
        w = by_work[rec["oa_work_id_r"] or rec["doi_r"]]
        w["labels"] |= ls
        w.update(doi=rec["doi_r"] or "", title=rec["title_r"] or "", abstract=rec["abstract_r"] or "")
    return [{"id": f"SB{i:04d}", "doi": w["doi"], "title": w["title"], "abstract": w["abstract"],
             "truth": next(iter(w["labels"])), "source": "human"}
            for i, w in enumerate(by_work.values()) if len(w["labels"]) == 1 and w["title"]]


def screeneval() -> list[dict]:
    t = {}
    for f in ("human_truth_v32", "heldout_truth_v32"):
        t.update(json.loads((SE / f"{f}.json").read_text(encoding="utf-8"))["truth"])
    out = []
    for f in ("human_cases", "heldout_cases", "flora_positive_cases"):
        for c in json.loads((SE / f"{f}.json").read_text(encoding="utf-8")):
            truth = "yes" if c["id"].startswith("F") else t.get(c["id"])
            if c["id"] == "F140" or truth not in ("yes", "no"):
                continue
            out.append({"id": c["id"], "doi": c.get("doi", ""), "title": c["title"],
                        "abstract": c["abstract"], "truth": truth, "source": "screeneval"})
    return out


def prescreen(bucket: str, truth: str, source: str) -> list[dict]:
    return [{"id": f"{bucket}:{c['id']}", "doi": c.get("doi", ""), "title": c["title"],
             "abstract": c.get("abstract", ""), "truth": truth, "source": source}
            for c in json.loads((PE / f"cases_live_{bucket}.json").read_text(encoding="utf-8"))]


def main() -> None:
    seen, out = set(), []
    for c in (human() + screeneval() + prescreen("goldpos_flora", "yes", "goldpos")
              + prescreen("goldpos_repro", "yes", "goldpos")
              + prescreen("goldneg_screen", "no", "goldneg_screen")
              + prescreen("goldneg_curated", "no", "goldneg_curated")):
        k = clean_doi(c["doi"]) or c["title"].strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(c)
    (HERE / "cases.json").write_text(json.dumps(out), encoding="utf-8")
    print(len(out), collections.Counter((c["source"], c["truth"]) for c in out))


if __name__ == "__main__":
    main()
