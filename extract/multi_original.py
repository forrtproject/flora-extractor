"""
multi_original.py — Pipeline for identifying multiple original studies in
multi-target replication papers.

Public API:
    run_multi_original_for_doi(doi_r, all_rep_df, cands_df) → dict

Pipeline: load metadata → OpenAlex candidates → PDF acquisition → GROBID
reference extraction → LLM identifies ALL originals.  The LLM step is the
sole source of the originals list; if it returns fewer items than expected,
the caller (run_extract.py) detects is_false_positive and re-routes to the
single-original pipeline.

Known limitations
-----------------
- No try/except around PDF or GROBID steps — exceptions propagate to the
  run_extract.py orchestrator, which catches them and writes an api_error row.
- originals_json is a JSON string (not a Python list) in the returned dict;
  callers must parse it with _parse_originals() or json.loads().
- _rep_row() silently returns "" for any column absent from all_rep_df; columns
  from the old pipeline (multi_target, readiness_level, etc.) will be blank
  when called from the filtered.csv-based orchestrator.

The returned dict contains:
  base_*            — metadata from all_rep_df (or filtered.csv)
  n_candidates      — number of OpenAlex candidate originals found
  pdf_*             — PDF acquisition result
  grobid_status     — GROBID extraction status
  n_grobid_refs     — number of references parsed by GROBID
  is_false_positive — True when LLM found ≤1 original despite multi_target flag
  n_originals       — number of originals identified by LLM
  originals_json    — JSON string: list of {rank, title, doi, first_author, ...}
  llm_*             — LLM metadata (source, reasoning)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from shared.config import log
from shared.grobid import run_grobid
from shared.llm_client import identify_all_originals_with_llm
from shared.openalex_client import find_all_candidates
from shared.pdf_sources import acquire_pdf
from shared.utils import cache_key, clean_doi

# Columns to pull from all_replications.csv
_REP_COLS = [
    "study_r", "abstract_r", "year_r", "url_r",
    "outcome", "validation_status", "readiness_level",
    "multi_target", "original_source", "pathway_source",
    "ref_r", "doi_o", "study_o",
    "openalex_id_r",         # may not exist in all versions
    "author_year_pattern_r", # may not exist in all versions
]


def _rep_row(doi_r: str, all_rep_df: pd.DataFrame) -> dict:
    """Return all_replications.csv fields for doi_r."""
    out = {c: "" for c in _REP_COLS}
    if all_rep_df is None or all_rep_df.empty:
        return out
    doi_r_clean = clean_doi(doi_r)
    matches = all_rep_df[all_rep_df["doi_r"].apply(clean_doi) == doi_r_clean]
    if matches.empty:
        return out
    row = matches.iloc[0]
    for col in _REP_COLS:
        out[col] = str(row.get(col, "") or "")
    return out


def run_multi_original_for_doi(doi_r:       str,
                                all_rep_df:  Optional[pd.DataFrame] = None,
                                cands_df:    Optional[pd.DataFrame] = None,
                                force_multi: bool = False) -> dict:
    """
    Run the multi-original pipeline for doi_r.

    Pipeline stages:
      1. Load base data from all_replications.csv
      2. Re-query OpenAlex for candidate originals
      3. PDF acquisition
      4. GROBID / pdfminer reference extraction
      5. LLM identifies ALL original studies (or flags false positive)

    Returns a flat dict with all output columns.
    """
    doi_r = clean_doi(doi_r)
    rep   = _rep_row(doi_r, all_rep_df)

    study_r    = rep.get("study_r",   "")
    abstract_r = rep.get("abstract_r", "")
    year_r_str = rep.get("year_r",    "")
    oa_id_r    = rep.get("openalex_id_r", "")
    pattern_r  = rep.get("author_year_pattern_r", "")

    try:
        year_r = int(year_r_str) if year_r_str else 2099
    except (ValueError, TypeError):
        year_r = 2099

    # ── Stage 2: OpenAlex re-query ───────────────────────────────────────────
    candidates = find_all_candidates(
        doi_r, oa_id_r, study_r, abstract_r, year_r, pattern_r
    )
    log.info("[multi/%s] %d candidate(s) from OpenAlex", doi_r, len(candidates))

    # ── Stage 3: PDF acquisition ─────────────────────────────────────────────
    pdf = acquire_pdf(doi_r, study_r)
    log.info("[multi/%s] PDF: %s (%s)", doi_r, pdf["pdf_source"], pdf["pdf_url"])

    # ── Stage 4: GROBID ──────────────────────────────────────────────────────
    pdf_path = Path(pdf["pdf_path"]) if pdf.get("pdf_path") else None
    grobid   = run_grobid(doi_r, pdf_path)
    sections = grobid.get("sections", {})
    log.info("[multi/%s] GROBID: %s (%d refs)", doi_r,
             grobid["grobid_status"], grobid["n_refs_parsed"])

    # ── Stage 5: Multi-original LLM ──────────────────────────────────────────
    pdf_url_for_llm = pdf.get("pdf_url", "") if not pdf.get("pdf_ok") else ""
    llm = identify_all_originals_with_llm(
        doi_r, study_r, abstract_r, candidates, sections,
        pdf_url     = pdf_url_for_llm,
        html_text   = pdf.get("html_text", ""),
        force_multi = force_multi,
    )
    log.info("[multi/%s] LLM: n_originals=%d false_positive=%s source=%s",
             doi_r, llm["n_originals"], llm["is_false_positive"], llm["llm_source"])

    return {
        "doi_r"              : doi_r,
        **{f"base_{k}": v for k, v in rep.items()},

        # OpenAlex re-query
        "n_candidates"       : len(candidates),
        "all_candidates_json": json.dumps(candidates, ensure_ascii=False),

        # PDF
        "pdf_url"    : pdf.get("pdf_url",    ""),
        "pdf_source" : pdf.get("pdf_source", "none"),
        "pdf_path"   : pdf.get("pdf_path",   ""),
        "pdf_ok"     : bool(pdf.get("pdf_ok", False)),

        # GROBID
        "grobid_status" : grobid.get("grobid_status", "not_attempted"),
        "n_grobid_refs" : grobid.get("n_refs_parsed",  0),

        # Multi-original result
        "is_false_positive": llm["is_false_positive"],
        "n_originals"      : llm["n_originals"],
        "originals_json"   : json.dumps(llm["originals"], ensure_ascii=False),
        "llm_source"       : llm["llm_source"],
        "llm_model"        : llm.get("llm_model", ""),
        "llm_reasoning"    : llm["llm_reasoning"],
    }
