"""
link_original.py — Single-DOI orchestration of the full disambiguation pipeline.

Public API:
    run_for_doi(doi_r, flora_df, cands_df, force=False, validation_comment="") → dict

The returned dict contains all columns the web app and QMD export need,
clearly prefixed by source:
  flora_*        — from FLoRA entry sheet
  (no prefix)    — from openalex_candidates.csv (pass-through columns)
  pdf_*          — PDF acquisition step
  grobid_*       — GROBID step
  resolved_*     — final resolved original study
  llm_*          — LLM step
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from shared.config import GROBID_CACHE_DIR, LLM_CACHE_DIR, OA_CACHE_DIR, log
from shared.disambiguation import resolve_same_author_year
from shared.grobid import run_grobid
from shared.llm_client import identify_original_with_llm
from shared.openalex_client import extract_author_year_patterns, find_all_candidates, fetch_openalex_by_doi
from shared.pdf_sources import acquire_pdf
from shared.utils import cache_key, clean_doi

# Columns to pass through from openalex_candidates.csv (no renaming)
_OA_PASSTHROUGH = [
    "study_r", "abstract_r", "year_r", "author_year_pattern_r",
    "openalex_id_r", "match_source", "match_status",
    "doi_o", "study_o", "year_o", "ref_o", "ref_r", "url_r",
    "llm_study_type", "llm_original_citation", "llm_original_search",
    "outcome_confidence", "outcome_original_confirmed", "outcome_original_study",
    "outcome_reasoning", "quote_validated", "quote_similarity",
    "pathway_source", "fred_match_type", "abstract_source",
    "validation_status",
]

# Columns to pull from FLoRA sheet (renamed with flora_ prefix)
_FLORA_COLS = {
    "ref_r"            : "flora_ref_r",
    "url_r"            : "flora_url_r",
    "abstract_r"       : "flora_abstract_r",
    "ref_o"            : "flora_ref_o",
    "doi_o"            : "flora_doi_o",
    "study_o"          : "flora_study_o",
    "outcome"          : "flora_outcome",
    "outcome_quote"    : "flora_outcome_quote",
    "out_quote_source" : "flora_out_quote_source",
    "prep_notes"       : "flora_prep_notes",
    "validation_status": "flora_validation_status",
}


def clear_pipeline_caches(doi_r: str) -> list[str]:
    """
    Delete all intermediate caches for *doi_r* except the PDF file itself.

    Cleared:
      - LLM result cache (full-text and abstract-only)
      - GROBID section cache (pdfminer, direct-PDF, and image-based ref extractions)
      - OpenAlex candidates cache

    Returns a list of the filenames that were actually deleted.
    """
    key = cache_key(doi_r)
    targets = [
        LLM_CACHE_DIR  / f"llm_{key}.json",
        LLM_CACHE_DIR  / f"llm_{cache_key(doi_r + '_abstract')}.json",
        GROBID_CACHE_DIR / f"{key}.json",
        GROBID_CACHE_DIR / f"{key}_direct_refs.json",
        GROBID_CACHE_DIR / f"{key}_img_refs.json",
        OA_CACHE_DIR   / f"candidates_{key}.json",
    ]
    deleted = []
    for path in targets:
        if path.exists():
            try:
                path.unlink()
                deleted.append(path.name)
            except Exception as e:
                log.warning("Could not delete cache %s: %s", path, e)
    return deleted


def _flora_row(doi_r: str, flora_df: pd.DataFrame) -> dict:
    """Return FLoRA sheet fields for *doi_r* (prefixed with flora_)."""
    out = {v: "" for v in _FLORA_COLS.values()}
    if flora_df is None or flora_df.empty:
        return out
    matches = flora_df[flora_df["doi_r"].apply(clean_doi) == clean_doi(doi_r)]
    if matches.empty:
        return out
    row = matches.iloc[0]
    for src, dst in _FLORA_COLS.items():
        out[dst] = str(row.get(src, "") or "")
    return out


def _cands_row(doi_r: str, cands_df: pd.DataFrame) -> dict:
    """Return openalex_candidates pass-through fields for *doi_r*."""
    out = {c: "" for c in _OA_PASSTHROUGH}
    if cands_df is None or cands_df.empty:
        return out
    matches = cands_df[cands_df["doi_r"].apply(clean_doi) == clean_doi(doi_r)]
    if matches.empty:
        return out
    row = matches.iloc[0]
    for col in _OA_PASSTHROUGH:
        out[col] = str(row.get(col, "") or "")
    return out


def run_for_doi(doi_r:              str,
                flora_df:           Optional[pd.DataFrame] = None,
                cands_df:           Optional[pd.DataFrame] = None,
                force:              bool = False,
                validation_comment: str  = "") -> dict:
    """
    Run the full disambiguation pipeline for *doi_r*.

    force=True clears all intermediate caches (LLM, GROBID, OpenAlex candidates)
    before running, but keeps the cached PDF so the download step is skipped.

    Pipeline stages:
      1. Load FLoRA sheet + openalex_candidates data for this DOI
      2. Re-query OpenAlex for all candidate originals
      3. Same-author/year disambiguation (fast, no PDF)
      4. If the abstract contains multiple patterns → early abstract-level LLM check
      5. PDF acquisition (7 sources)
      6. GROBID reference extraction
      7. LLM identification (Gemini → OpenAI)

    Returns a flat dict with all output columns.
    """
    doi_r = clean_doi(doi_r)

    if force:
        deleted = clear_pipeline_caches(doi_r)
        if deleted:
            log.info("[%s] Force rerun — cleared caches: %s", doi_r, ", ".join(deleted))

    # ── Stage 1: base data ───────────────────────────────────────────────────
    flora  = _flora_row(doi_r,  flora_df)
    cands_row = _cands_row(doi_r, cands_df)

    study_r   = cands_row.get("study_r",   "")
    abstract_r = cands_row.get("abstract_r", "")
    pattern_r  = cands_row.get("author_year_pattern_r", "")
    oa_id_r    = cands_row.get("openalex_id_r", "")

    try:
        year_r = int(cands_row.get("year_r") or 2099)
    except (ValueError, TypeError):
        year_r = 2099

    # ── Stage 2: OpenAlex re-query ───────────────────────────────────────────
    candidates = find_all_candidates(
        doi_r, oa_id_r, study_r, abstract_r, year_r, pattern_r
    )
    log.info("[%s] %d candidate(s) from OpenAlex re-query", doi_r, len(candidates))

    # ── FLoRA anchor injection (validated DOIs only) ──────────────────────────
    # For DOIs the FLoRA team has manually verified, inject the known-correct
    # original as a candidate (if absent) and prepend an anchor note to every
    # LLM call so the model knows to prefer it unless evidence contradicts.
    flora_val_status = flora.get("flora_validation_status", "").lower()
    flora_doi_o      = flora.get("flora_doi_o", "")
    anchor_note      = ""

    _is_validated = (
        "validated - changed"   in flora_val_status
        or "validated - unchanged" in flora_val_status
    )
    if _is_validated and flora_doi_o:
        existing_dois = {clean_doi(c.get("doi", "")) for c in candidates}
        if clean_doi(flora_doi_o) not in existing_dois:
            anchor_cand = fetch_openalex_by_doi(flora_doi_o)
            if anchor_cand:
                candidates = [anchor_cand] + candidates
                log.info("[%s] FLoRA anchor injected: %s", doi_r, flora_doi_o)
        anchor_note = (
            f"⚠ FLoRA ANCHOR: The FLoRA database has manually verified the original "
            f"study for this replication as DOI: {flora_doi_o} "
            f"(\"{flora.get('flora_study_o', '')}\"). "
            f"Evaluate this against the evidence — confirm it if supported, "
            f"override only if you find strong contradicting evidence."
        )

    # Combine anchor note with any user-supplied validation comment
    effective_note = "\n\n".join(filter(None, [anchor_note, validation_comment]))

    # ── Stage 3: Same-author / same-year disambiguation ──────────────────────
    stage3 = resolve_same_author_year(doi_r, study_r, abstract_r, candidates)

    if stage3["resolved"]:
        log.info("[%s] Resolved by same-author/year: %s", doi_r,
                 stage3["resolved_title_o"])
        return _build_output(doi_r, flora, cands_row, candidates,
                             stage3, {}, {}, {})

    # ── Stage 4: Early abstract-level LLM (multiple distinct patterns) ───────
    # If the abstract has 2+ distinct cited author-year patterns and we have
    # candidates, ask the LLM to pick using only the abstract (no PDF needed).
    abstract_patterns = extract_author_year_patterns(abstract_r, max_year=year_r)
    distinct_pairs    = {(p["surname"], p["year"]) for p in abstract_patterns}

    if len(distinct_pairs) >= 2 and candidates:
        log.info("[%s] Multiple abstract patterns — early abstract LLM", doi_r)
        llm4 = identify_original_with_llm(
            doi_r + "_abstract",   # separate cache key from full-text LLM
            study_r, abstract_r, pattern_r, candidates, {},
            validator_note=effective_note,
        )
        if llm4["resolved"]:
            log.info("[%s] Resolved by abstract LLM: %s", doi_r,
                     llm4["resolved_title_o"])
            return _build_output(doi_r, flora, cands_row, candidates,
                                 llm4, {}, {}, {})

    # ── Stage 5: PDF acquisition ─────────────────────────────────────────────
    pdf = acquire_pdf(doi_r, study_r)
    log.info("[%s] PDF: %s (%s)", doi_r, pdf["pdf_source"], pdf["pdf_url"])

    # ── Stage 6: GROBID ──────────────────────────────────────────────────────
    pdf_path  = Path(pdf["pdf_path"]) if pdf.get("pdf_path") else None
    grobid    = run_grobid(doi_r, pdf_path)
    sections  = grobid.get("sections", {})
    log.info("[%s] GROBID: %s (%d refs)", doi_r,
             grobid["grobid_status"], grobid["n_refs_parsed"])

    # ── Stage 7: LLM identification ──────────────────────────────────────────
    # Only run if we still have no resolved original
    llm = identify_original_with_llm(
        doi_r, study_r, abstract_r, pattern_r, candidates, sections,
        pdf_url        = pdf.get("pdf_url", "")   if not pdf.get("pdf_ok") else "",
        html_text      = pdf.get("html_text", ""),
        validator_note = effective_note,
    )
    log.info("[%s] LLM: resolved=%s source=%s", doi_r,
             llm["resolved"], llm["llm_source"])

    return _build_output(doi_r, flora, cands_row, candidates,
                         llm, pdf, grobid, sections)


# ── Output builder ────────────────────────────────────────────────────────────

def _build_output(doi_r:     str,
                  flora:     dict,
                  cands_row: dict,
                  candidates: list[dict],
                  resolution: dict,
                  pdf:        dict,
                  grobid:     dict,
                  sections:   dict) -> dict:
    """Assemble the flat output dict from all pipeline stage results."""
    import json

    return {
        # ── Input ─────────────────────────────────────────────────────────────
        "doi_r"                 : doi_r,

        # ── FLoRA sheet ───────────────────────────────────────────────────────
        **flora,

        # ── openalex_candidates pass-through ──────────────────────────────────
        **{c: cands_row.get(c, "") for c in _OA_PASSTHROUGH},

        # ── OpenAlex re-query ─────────────────────────────────────────────────
        "n_candidates"          : len(candidates),
        "all_candidates_json"   : json.dumps(candidates, ensure_ascii=False),

        # ── PDF ───────────────────────────────────────────────────────────────
        "pdf_url"               : pdf.get("pdf_url",    ""),
        "pdf_source"            : pdf.get("pdf_source", "none"),
        "pdf_path"              : pdf.get("pdf_path",   ""),
        "pdf_ok"                : bool(pdf.get("pdf_ok", False)),
        "pdf_url_tried"         : json.dumps(pdf.get("pdf_url_tried", []),
                                             ensure_ascii=False),
        "html_text"             : pdf.get("html_text", ""),

        # ── GROBID ────────────────────────────────────────────────────────────
        "grobid_status"         : grobid.get("grobid_status", "not_attempted"),
        "n_grobid_refs"         : grobid.get("n_refs_parsed",  0),
        "grobid_abstract"       : (sections.get("abstract", "") or "")[:1500],
        "grobid_intro"          : (sections.get("intro",    "") or "")[:1000],
        "grobid_methods"        : (sections.get("methods",  "") or "")[:700],
        "grobid_refs_json"      : json.dumps(
                                      (sections.get("references", []) or [])[:25],
                                      ensure_ascii=False),

        # ── Resolution ────────────────────────────────────────────────────────
        "resolution_method"     : resolution.get("resolution_method", "none"),
        "resolution_score"      : round(float(
                                      resolution.get("resolution_score", 0) or 0
                                  ), 4),
        "resolved_doi_o"        : resolution.get("resolved_doi_o",   ""),
        "resolved_title_o"      : resolution.get("resolved_title_o", ""),
        "resolved_year_o"       : resolution.get("resolved_year_o"),
        "resolved_author_o"     : resolution.get("resolved_author_o", ""),

        # ── LLM ───────────────────────────────────────────────────────────────
        "llm_source"            : resolution.get("llm_source",     ""),
        "llm_model"             : resolution.get("llm_model",      ""),
        "llm_confidence"        : resolution.get("llm_confidence", ""),
        "llm_evidence"          : resolution.get("llm_evidence",   ""),
        "llm_reasoning"         : resolution.get("llm_reasoning",  ""),
        "llm_prompt"            : resolution.get("llm_prompt",     ""),
        "llm_error"             : resolution.get("llm_error",      ""),
    }
