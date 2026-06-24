"""
old_pipeline_compare.py — Compare old pipeline (all_replications.csv) against new pipeline
(candidates.csv / filtered.csv) and write analysis/old_pipeline_comparison.json.

Computes:
  - Old pipeline breakdown: total, by type, by pathway_source, by validation_status, by outcome
  - New pipeline breakdown: candidates by source, filtered by filter_status
  - Overlap (old typed TP rows vs new candidates): DOI match, OA-ID match, not found
  - Keyword inventory: new pipeline SEARCH_PHRASES + CONCEPT_IDS
  - Gap rows: old TP DOIs / OA-IDs not found in new pipeline

Usage:
    python -m analysis.old_pipeline_compare
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from shared.config import DATA_DIR, log

ANALYSIS_DIR = DATA_DIR.parent / "analysis"
OUTPUT_JSON = ANALYSIS_DIR / "old_pipeline_comparison.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _vc(series: pd.Series) -> dict[str, int]:
    return {str(k): int(v) for k, v in series.value_counts(dropna=False).items()}


def _load_new_keywords() -> tuple[list[str], list[str]]:
    """Return (search_phrases, concept_ids) from openalex_search.py."""
    try:
        from search.openalex_search import SEARCH_PHRASES, CONCEPT_IDS
        return list(SEARCH_PHRASES), list(CONCEPT_IDS)
    except Exception:
        return [], []


# ── Old pipeline ──────────────────────────────────────────────────────────────

def _load_old_pipeline() -> dict[str, Any]:
    path = DATA_DIR / "all_replications.csv"
    if not path.exists():
        return {"error": "all_replications.csv not found"}

    log.info("Loading all_replications.csv …")
    df = pd.read_csv(path, dtype=str)
    df["doi_r"] = df["doi_r"].fillna("").str.strip().str.lower()
    df["url_r"] = df["url_r"].fillna("").str.strip()
    df["type"]  = df["type"].fillna("")

    typed = df[df["type"].isin(["replication", "reproduction"])].copy()
    fps   = df[df["validation_status"].fillna("") == "false_positive"]

    # Break down typed rows by how they're identified
    tp_with_doi    = typed[typed["doi_r"] != ""]
    tp_doi_only    = typed[(typed["doi_r"] == "") & (typed["url_r"].str.startswith("https://openalex.org/", na=False))]
    tp_no_id       = typed[(typed["doi_r"] == "") & (~typed["url_r"].str.startswith("https://openalex.org/", na=False))]

    return {
        "total": len(df),
        "tp_total": len(typed),
        "tp_replication": int((typed["type"] == "replication").sum()),
        "tp_reproduction": int((typed["type"] == "reproduction").sum()),
        "tp_with_doi": len(tp_with_doi),
        "tp_oa_id_only": len(tp_doi_only),
        "tp_no_id": len(tp_no_id),
        "fp_total": len(fps),
        "needs_review": int((df["validation_status"].fillna("") == "needs_review").sum()),
        "llm_confirmed": int((df["validation_status"].fillna("") == "llm_confirmed").sum()),
        "already_in_flora": int((df["validation_status"].fillna("") == "already_in_flora").sum()),
        "by_source": _vc(df["pathway_source"].fillna("unknown")),
        "by_validation_status": _vc(df["validation_status"].fillna("unset")),
        "by_outcome": _vc(typed["outcome"].fillna("not_set")),
    }


# ── New pipeline ──────────────────────────────────────────────────────────────

def _load_new_pipeline() -> dict[str, Any]:
    cand_path = DATA_DIR / "candidates.csv"
    filt_path = DATA_DIR / "filtered.csv"

    cand_stats: dict[str, Any] = {"total": None, "by_source": {}, "no_doi": None, "no_doi_or_url": None}
    if cand_path.exists():
        log.info("Scanning candidates.csv …")
        total = no_doi = no_doi_or_url = 0
        source_counts: dict[str, int] = {}
        for chunk in pd.read_csv(
            cand_path, encoding="utf-8-sig", dtype=str, chunksize=100_000,
            usecols=lambda c: c in ("doi_r", "url_r", "source"), on_bad_lines="skip",
        ):
            total += len(chunk)
            no_doi += int((chunk["doi_r"].fillna("") == "").sum())
            no_doi_or_url += int(
                ((chunk["doi_r"].fillna("") == "") & (chunk["url_r"].fillna("") == "")).sum()
            )
            for src, cnt in chunk["source"].fillna("unknown").value_counts().items():
                source_counts[str(src)] = source_counts.get(str(src), 0) + int(cnt)
        cand_stats = {
            "total": total,
            "by_source": source_counts,
            "no_doi": no_doi,
            "no_doi_or_url": no_doi_or_url,
        }

    filt_stats: dict[str, Any] = {"total": None, "tp": None, "fp": None, "needs_review": None}
    if filt_path.exists():
        log.info("Scanning filtered.csv …")
        status_counts: dict[str, int] = {}
        total_f = 0
        for chunk in pd.read_csv(
            filt_path, encoding="utf-8-sig", dtype=str, chunksize=100_000,
            usecols=lambda c: c in ("filter_status",), on_bad_lines="skip",
        ):
            total_f += len(chunk)
            for s, c in chunk["filter_status"].fillna("unknown").value_counts().items():
                status_counts[str(s)] = status_counts.get(str(s), 0) + int(c)
        tp  = status_counts.get("replication", 0) + status_counts.get("reproduction", 0)
        fp  = status_counts.get("false_positive", 0)
        nr  = status_counts.get("needs_review", 0)
        filt_stats = {
            "total": total_f,
            "tp": tp,
            "fp": fp,
            "needs_review": nr,
            "by_status": status_counts,
        }

    phrases, concepts = _load_new_keywords()
    return {
        "candidates": cand_stats,
        "filtered": filt_stats,
        "search_phrases": phrases,
        "concept_ids": concepts,
    }


# ── Overlap analysis ──────────────────────────────────────────────────────────

def _build_candidate_index() -> tuple[set, set]:
    """Return (doi_set, oa_id_set) from candidates.csv."""
    log.info("Building candidate index (doi_r + openalex_id_r) …")
    doi_set: set = set()
    oa_set:  set = set()
    path = DATA_DIR / "candidates.csv"
    if not path.exists():
        return doi_set, oa_set
    for chunk in pd.read_csv(
        path, encoding="utf-8-sig", dtype=str, chunksize=100_000,
        usecols=lambda c: c in ("doi_r", "openalex_id_r"), on_bad_lines="skip",
    ):
        doi_set.update(chunk["doi_r"].dropna().str.strip().str.lower())
        oa_set.update(chunk["openalex_id_r"].dropna().str.strip())
    doi_set.discard("")
    oa_set.discard("")
    log.info("  → %d DOIs, %d OA IDs", len(doi_set), len(oa_set))
    return doi_set, oa_set


def _overlap_analysis(doi_set: set, oa_set: set) -> dict[str, Any]:
    """Compare old TP rows against new candidate index. Returns overlap stats + gap rows."""
    path = DATA_DIR / "all_replications.csv"
    if not path.exists():
        return {}

    df = pd.read_csv(
        path, dtype=str,
        usecols=lambda c: c in ("doi_r", "url_r", "study_r", "year_r", "type", "pathway_source"),
    )
    df["doi_r"]  = df["doi_r"].fillna("").str.strip().str.lower()
    df["url_r"]  = df["url_r"].fillna("").str.strip()
    df["type"]   = df["type"].fillna("")
    typed = df[df["type"].isin(["replication", "reproduction"])].copy()

    doi_rows    = typed[typed["doi_r"] != ""]
    no_doi_rows = typed[typed["doi_r"] == ""]

    # DOI match
    doi_in     = doi_rows[doi_rows["doi_r"].isin(doi_set)]
    doi_out    = doi_rows[~doi_rows["doi_r"].isin(doi_set)]

    # OA-ID match for rows without DOI
    # url_r for openalex-sourced no-doi rows IS an openalex.org URL — same as openalex_id_r
    oaid_in    = no_doi_rows[no_doi_rows["url_r"].isin(oa_set)]
    oaid_out   = no_doi_rows[~no_doi_rows["url_r"].isin(oa_set)]

    gap_doi_rows   = doi_out[["doi_r", "url_r", "study_r", "year_r", "pathway_source"]].fillna("").to_dict("records")
    gap_url_rows   = oaid_out[["doi_r", "url_r", "study_r", "year_r", "pathway_source"]].fillna("").to_dict("records")

    return {
        "tp_total": len(typed),
        "doi_rows": len(doi_rows),
        "doi_in_new": len(doi_in),
        "doi_not_in_new": len(doi_out),
        "oaid_rows": len(no_doi_rows),
        "oaid_in_new": len(oaid_in),
        "oaid_not_in_new": len(oaid_out),
        "total_in_new": len(doi_in) + len(oaid_in),
        "total_not_in_new": len(doi_out) + len(oaid_out),
        "gap_doi_rows": gap_doi_rows,
        "gap_url_rows": gap_url_rows,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def run_comparison() -> dict[str, Any]:
    """Run full comparison, write OUTPUT_JSON, return the result dict."""
    ANALYSIS_DIR.mkdir(exist_ok=True)

    log.info("Step 1/4: Loading old pipeline …")
    old = _load_old_pipeline()

    log.info("Step 2/4: Loading new pipeline …")
    new = _load_new_pipeline()

    log.info("Step 3/4: Building new candidate index …")
    doi_set, oa_set = _build_candidate_index()

    log.info("Step 4/4: Overlap analysis …")
    overlap = _overlap_analysis(doi_set, oa_set)

    result: dict[str, Any] = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "old_pipeline": old,
        "new_pipeline": new,
        "overlap": overlap,
    }

    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Saved → %s", OUTPUT_JSON)
    return result


def load_cached() -> dict[str, Any] | None:
    if OUTPUT_JSON.exists():
        try:
            return json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


if __name__ == "__main__":
    run_comparison()
