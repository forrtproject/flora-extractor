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

import html
import json
import re
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from shared.config import GROBID_CACHE_DIR, LLM_CACHE_DIR, OA_CACHE_DIR, PARSE_CACHE_DIR, RESEARCHER_EMAIL, log
from shared.disambiguation import is_umbrella_paper, jaccard_similarity
from shared.llm_client import identify_original_with_llm
from shared.pdf_parsing import parse_all as _parse_all
from shared.openalex_client import author_matches, extract_author_year_patterns, find_all_candidates, fetch_openalex_by_doi
from shared.pdf_sources import acquire_pdf
from shared.utils import cache_key, clean_doi

# ── Unified rule-based resolver (runs before any LLM call) ───────────────────
# Combines citation-context scoring (journal-qualified) with same-author/year
# title-Jaccard fallback into a single function so both paths share one code path.
#
# Path A — journal hint present in abstract citation:
#   Scores by author(+2) + year(+2) + journal Jaccard(+3/+1.5) + title Jaccard(+≤1).
#   Resolves when best ≥ 4.0 AND gap ≥ 2.0.  Strict because the journal
#   contributes 3 points, making the winner unambiguous.
#
# Path B — no journal hint, but all candidates share same author+year:
#   Falls back to title-Jaccard relative threshold (best > 0.05, best ≥ second×1.5).
#   Same logic as the old resolve_same_author_year() in shared/disambiguation.py.

_CITATION_YEAR  = r"(?:19|20)\d{2}"
_CITATION_NAME  = r"[A-Z][A-Za-z\-\xc0-ɏ]{1,}(?:\s+[A-Z][A-Za-z\-\xc0-ɏ]{1,})*"
_CITATION_RE    = re.compile(
    r"\("
    r"(?P<authors>" + _CITATION_NAME +
    r"(?:\s*(?:,|&|and|\bet\s+al\.?)\s*" + _CITATION_NAME + r")*)"
    r"\s*,\s*"
    r"(?P<year>" + _CITATION_YEAR + r")"
    r"(?:\s*,\s*(?P<journal>[A-Z][A-Za-z\s&:]+?))?"
    r"\s*\)",
    re.UNICODE,
)
_STOP_SURNAMES = {"and", "van", "von", "der", "den", "del", "the", "for"}

# ── Title-pattern resolver ─────────────────────────────────────────────────────
# Patterns that extract the original study name from a replication paper's title.
# Order matters: more specific patterns come first.

_TITLE_PATS: list[re.Pattern] = [
    # "A Direct Replication of TARGET" / "Failed Replication of TARGET" / "Replication Study of TARGET"
    re.compile(
        r"^(?:a\s+)?(?:direct\s+|close\s+|failed\s+|conceptual\s+)?replication"
        r"(?:\s+study)?\s+of\s+(.+)",
        re.IGNORECASE,
    ),
    # "Replicating TARGET"
    re.compile(r"^replicating\s+(.+)", re.IGNORECASE),
    # "A Reproduction of TARGET" / "Reproducing TARGET"
    re.compile(r"^(?:a\s+)?reproduction\s+of\s+(.+)", re.IGNORECASE),
    re.compile(r"^reproducing\s+(.+)", re.IGNORECASE),
    # "Revisiting TARGET" / "Re-examining TARGET" / "Reconsidering TARGET"
    re.compile(r"^(?:re-?examining|revisiting|reconsidering)\s+(.+)", re.IGNORECASE),
    # "Can we replicate TARGET?" / "Does TARGET replicate?"
    re.compile(r"^can\s+we\s+replicate\s+(.+?)[\?\.]*$", re.IGNORECASE),
    re.compile(r"^does\s+(.+?)\s+replicate[\?\.]*$", re.IGNORECASE),
    # "Testing the replicability of TARGET"
    re.compile(r"^testing\s+the\s+replicability\s+of\s+(.+)", re.IGNORECASE),
    # "TARGET: A Replication" / "TARGET: Replication and Extension"
    re.compile(
        r"^(.+?)\s*:\s*(?:a\s+)?(?:direct\s+)?replication(?:\s+and\s+extension)?[\?\.]*$",
        re.IGNORECASE,
    ),
]

_TITLE_TARGET_MIN_LEN = 8   # shorter targets are noise (e.g. "Revisiting X" or "Trust")


def _extract_title_target(title_r: str) -> "str | None":
    """
    Extract the original study target from a replication paper's title.
    Returns the target substring or None if no pattern matches / target too short.
    """
    title_r = title_r.strip()
    for pat in _TITLE_PATS:
        m = pat.match(title_r)
        if m:
            target = m.group(1).strip().rstrip("?:.,;\"'")
            if len(target) >= _TITLE_TARGET_MIN_LEN:
                return target
    return None


def _resolve_by_title_pattern(
    doi_r:      str,
    study_r:    str,
    candidates: list[dict],
) -> "dict | None":
    """
    Try to resolve the original study by matching the replication paper's title
    against candidate titles using Jaccard similarity.

    Returns:
      - dict with resolved=True when a single confident match exists
      - dict with resolved=False + title_pattern_hint when multiple plausible matches
      - None when no pattern matches or no candidates score above minimum threshold
    """
    target = _extract_title_target(study_r)
    if not target or not candidates:
        return None

    scored = sorted(
        candidates,
        key=lambda c: jaccard_similarity(c.get("title", ""), target),
        reverse=True,
    )

    best      = scored[0]
    best_score = jaccard_similarity(best.get("title", ""), target)
    sec_score  = jaccard_similarity(scored[1].get("title", ""), target) if len(scored) > 1 else 0.0

    if best_score < 0.3:
        return None

    base = {
        "resolved":             False,
        "resolution_method":    "needs_fulltext",
        "resolved_doi_o":       "",
        "resolved_title_o":     "",
        "resolved_year_o":      None,
        "resolved_author_o":    "",
        "resolution_score":     0.0,
        "title_pattern_target": target,
    }

    if best_score >= 0.4 and best_score >= sec_score * 1.5:
        log.info("[%s] title_pattern resolved: %s (score=%.3f target=%r)",
                 doi_r, best.get("doi"), best_score, target)
        return {
            **base,
            "resolved":          True,
            "resolution_method": "title_pattern_match",
            "resolved_doi_o":    best.get("doi", ""),
            "resolved_title_o":  best.get("title", ""),
            "resolved_year_o":   best.get("year"),
            "resolved_author_o": best.get("first_author", ""),
            "resolution_score":  round(best_score, 4),
        }

    hint_titles = [
        c.get("title", "") for c in scored[:3]
        if jaccard_similarity(c.get("title", ""), target) >= 0.3
    ]
    return {**base, "title_pattern_hint": hint_titles}


def _extract_cit_contexts(text: str) -> list[dict]:
    """Return list of {surnames, year, journal} from all parenthetical citations."""
    results: list[dict] = []
    seen: set[tuple] = set()
    for m in _CITATION_RE.finditer(text):
        surnames = [
            t.lower()
            for t in re.findall(r"[A-Z][A-Za-z\-\xc0-ɏ]{2,}", m.group("authors"))
            if t.lower() not in _STOP_SURNAMES
        ]
        try:
            year = int(m.group("year"))
        except ValueError:
            continue
        key = (tuple(sorted(surnames)), year)
        if key in seen:
            continue
        seen.add(key)
        journal = (m.group("journal") or "").strip().rstrip(",;.:")
        results.append({"surnames": surnames, "year": year, "journal": journal, "raw": m.group(0)})
    return results


def _journal_token_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta = {t.lower() for t in re.findall(r"\b\w{2,}\b", a)}
    tb = {t.lower() for t in re.findall(r"\b\w{2,}\b", b)}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _fetch_journal_cached(doi: str) -> str:
    """Return the journal display name for a DOI from OpenAlex. Result cached."""
    doi = clean_doi(doi)
    if not doi:
        return ""
    cache_path = OA_CACHE_DIR / f"journal_{cache_key(doi)}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8")).get("journal", "")
        except Exception:
            pass
    try:
        r = requests.get(
            "https://api.openalex.org/works",
            params={"filter": f"doi:{doi}",
                    "select": "id,primary_location",
                    "mailto": RESEARCHER_EMAIL},
            headers={"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        journal = ""
        if data and data.get("results"):
            loc = (data["results"][0].get("primary_location") or {})
            src = (loc.get("source") or {})
            journal = (src.get("display_name") or "").strip()
    except Exception:
        journal = ""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"journal": journal}), encoding="utf-8")
    time.sleep(0.12)
    return journal


def _resolve_rule_based(
    doi_r:      str,
    abstract_r: str,
    candidates: list[dict],
    year_r:     int,
    study_r:    str = "",
) -> dict:
    """
    Unified pre-LLM resolver covering both citation-context and same-author/year cases.

    Returns the same shape dict as identify_original_with_llm().
    """
    base: dict = {
        "resolved":           False,
        "resolution_method":  "needs_fulltext",
        "resolved_doi_o":     "",
        "resolved_title_o":   "",
        "resolved_year_o":    None,
        "resolved_author_o":  "",
        "resolution_score":   0.0,
    }

    if not candidates:
        base["resolution_method"] = "no_candidates_found"
        return base

    # Single unambiguous candidate
    if len(candidates) == 1:
        c = candidates[0]
        if is_umbrella_paper(c.get("title", "")):
            return base
        return {**base,
                "resolved":          True,
                "resolution_method": "single_candidate_after_requery",
                "resolved_doi_o":    c.get("doi",          ""),
                "resolved_title_o":  c.get("title",        ""),
                "resolved_year_o":   c.get("year"),
                "resolved_author_o": c.get("first_author", ""),
                "resolution_score":  1.0}

    decoded    = html.unescape(abstract_r or "")
    citations  = [c for c in _extract_cit_contexts(decoded) if c["year"] <= year_r]
    has_journal = any(cit["journal"] for cit in citations)

    # ── Path A: citation scoring (author + year + optional journal) ───────────
    if citations:
        scored: list[dict] = []
        for cand in candidates:
            cand_doi    = cand.get("doi", "")
            cand_title  = cand.get("title", "") or ""
            cand_year   = int(cand.get("year") or 0)
            cand_snames = [s.lower() for s in (cand.get("all_authors") or []) if s]
            if not cand_snames:
                fa = (cand.get("first_author") or "").lower()
                if fa:
                    cand_snames = [fa]

            best_base = 0.0
            best_cit: dict | None = None
            for cit in citations:
                auth_sc = 2.0 if any(author_matches(sn, cand_snames) for sn in cit["surnames"]) else 0.0
                yr_sc   = 2.0 if cit["year"] == cand_year else (1.0 if abs(cit["year"] - cand_year) == 1 else 0.0)
                if auth_sc == 0.0 and yr_sc == 0.0:
                    continue
                if auth_sc + yr_sc > best_base:
                    best_base = auth_sc + yr_sc
                    best_cit  = cit

            if best_cit is None or best_base < 2.0:
                continue
            scored.append({"cand": cand, "citation": best_cit, "base_score": best_base,
                           "cand_doi": cand_doi, "cand_title": cand_title,
                           "cand_year": cand_year, "cand_snames": cand_snames})

        # Enrich with journal info when a journal hint is present
        if has_journal:
            for entry in scored:
                cit = entry["citation"]
                if not cit.get("journal") or not entry["cand_doi"]:
                    continue
                cand_journal = _fetch_journal_cached(entry["cand_doi"])
                if cand_journal:
                    jsim = _journal_token_sim(cit["journal"], cand_journal)
                    entry["base_score"] += 3.0 if jsim >= 0.6 else (1.5 if jsim >= 0.3 else 0.0)

        for entry in scored:
            entry["total"] = round(
                entry["base_score"] + jaccard_similarity(entry["cand_title"], decoded), 4)

        scored.sort(key=lambda x: x["total"], reverse=True)

        if scored:
            best   = scored[0]
            second = scored[1]["total"] if len(scored) > 1 else 0.0
            gap    = best["total"] - second
            if best["total"] >= 4.0 and gap >= 2.0:
                log.info("[%s] rule_based resolved (citation-context): %s score=%.2f gap=%.2f",
                         doi_r, best["cand_doi"], best["total"], gap)
                return {**base,
                        "resolved":          True,
                        "resolution_method": "citation_context_match",
                        "resolved_doi_o":    best["cand_doi"],
                        "resolved_title_o":  best["cand_title"],
                        "resolved_year_o":   best["cand_year"],
                        "resolved_author_o": best["cand_snames"][0] if best["cand_snames"] else "",
                        "resolution_score":  round(min(best["total"] / 8.0, 1.0), 4)}

    # ── Path B: same-author/year cluster — title Jaccard relative threshold ───
    # Fires when all candidates share one surname and one year but no journal hint
    # was present (or Path A's strict threshold was not met).
    surnames = {(c.get("first_author") or "").lower().split()[-1] for c in candidates if c.get("first_author")}
    years    = {c.get("year") for c in candidates}
    if len(surnames) == 1 and len(years) == 1:
        context = decoded + " " + (study_r or "")
        by_title = sorted(candidates,
                          key=lambda c: jaccard_similarity(c.get("title", ""), context),
                          reverse=True)
        best_sc  = jaccard_similarity(by_title[0].get("title", ""), context)
        sec_sc   = jaccard_similarity(by_title[1].get("title", ""), context) if len(by_title) > 1 else 0.0
        if best_sc > 0.05 and best_sc >= sec_sc * 1.5:
            c = by_title[0]
            log.info("[%s] rule_based resolved (same-author/year Jaccard): %s score=%.4f",
                     doi_r, c.get("doi"), best_sc)
            return {**base,
                    "resolved":          True,
                    "resolution_method": "same_author_year_title_overlap",
                    "resolved_doi_o":    c.get("doi",          ""),
                    "resolved_title_o":  c.get("title",        ""),
                    "resolved_year_o":   c.get("year"),
                    "resolved_author_o": c.get("first_author", ""),
                    "resolution_score":  round(best_sc, 4)}

    return base


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


def _write_parse_cache(doi_r: str, parse_results: dict) -> None:
    """Persist parse_all results to PARSE_CACHE_DIR so run_extract._save_parse_cache() skips re-parsing."""
    out_file = PARSE_CACHE_DIR / f"parse_{cache_key(doi_r)}.json"
    if out_file.exists():
        return
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with out_file.open("w", encoding="utf-8") as fh:
            json.dump(parse_results, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.debug("[%s] _write_parse_cache failed: %s", doi_r, exc)


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


def _best_parse_result(parse_results: dict[str, dict]) -> dict:
    """Return the parse result with the richest content.

    Scoring: each reference is worth 500 points (references are the most
    useful input for the LLM linking step); abstract and intro contribute
    their character count.  Methods that errored score -1 and are skipped
    unless all methods errored, in which case we fall back to the grobid
    result (or the first available result).
    """
    def _score(r: dict) -> int:
        if r.get("error"):
            return -1
        refs     = r.get("references") or []
        abstract = r.get("abstract")   or ""
        intro    = r.get("intro")      or ""
        return len(refs) * 500 + len(abstract) + len(intro)

    scored = [(m, _score(r), r) for m, r in parse_results.items()]
    valid  = [(m, s, r) for m, s, r in scored if s >= 0]
    if valid:
        return max(valid, key=lambda x: x[1])[2]
    return parse_results.get("grobid", next(iter(parse_results.values())))


def run_for_doi(doi_r:              str,
                flora_df:           Optional[pd.DataFrame] = None,
                cands_df:           Optional[pd.DataFrame] = None,
                force:              bool = False,
                validation_comment: str  = "",
                no_llm:             bool = False,
                no_pdf:             bool = False) -> dict:
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

    # ── Stage 2.5: Title-pattern resolver ─────────────────────────────────────
    # Runs before citation scoring and before any LLM call.
    title_pat = _resolve_by_title_pattern(doi_r, study_r, candidates)
    if title_pat and title_pat.get("resolved"):
        return _build_output(doi_r, flora, cands_row, candidates,
                             title_pat, {}, {}, {})
    if title_pat and title_pat.get("title_pattern_hint"):
        hint_note = (
            f"TITLE PATTERN HINT: The replication paper's title contains a pattern "
            f"suggesting the original is \"{title_pat['title_pattern_target']}\". "
            f"Top candidate matches by title: "
            + ", ".join(f"\"{t}\"" for t in title_pat["title_pattern_hint"])
        )
        effective_note = "\n\n".join(filter(None, [effective_note, hint_note]))

    # ── Stage 3: Rule-based resolver (citation-context + same-author/year) ──────
    stage3 = _resolve_rule_based(doi_r, abstract_r, candidates, year_r, study_r)
    if stage3["resolved"]:
        log.info("[%s] Resolved rule-based (%s): %s", doi_r,
                 stage3["resolution_method"], stage3["resolved_title_o"])
        return _build_output(doi_r, flora, cands_row, candidates,
                             stage3, {}, {}, {})

    # ── Stage 4: Abstract-level LLM ──────────────────────────────────────────
    if not no_llm:
        abstract_patterns = extract_author_year_patterns(abstract_r, max_year=year_r)
        distinct_pairs    = {(p["surname"], p["year"]) for p in abstract_patterns}

        if abstract_r and distinct_pairs:
            log.info("[%s] Abstract has %d author-year patterns — early abstract LLM", doi_r, len(distinct_pairs))
            llm4 = identify_original_with_llm(
                doi_r + "_abstract",
                study_r, abstract_r, pattern_r, candidates, {},
                validator_note=effective_note,
            )
            if llm4["resolved"]:
                log.info("[%s] Resolved by abstract LLM: %s", doi_r,
                         llm4["resolved_title_o"])
                return _build_output(doi_r, flora, cands_row, candidates,
                                     llm4, {}, {}, {})

    # ── Stage 5: PDF acquisition ─────────────────────────────────────────────
    if no_pdf:
        pdf = {"pdf_source": "skipped", "pdf_url": "", "pdf_path": None, "openalex_xml": None}
    else:
        pdf = acquire_pdf(doi_r, study_r, openalex_id=oa_id_r)
    log.info("[%s] PDF: %s (%s)", doi_r, pdf["pdf_source"], pdf["pdf_url"])

    # ── Stage 6: Parse all — pick richest result to send to LLM ─────────────
    pdf_path       = Path(pdf["pdf_path"]) if pdf.get("pdf_path") else None
    oa_xml_content = pdf.get("openalex_xml")
    parse_results  = _parse_all(doi_r, pdf_path, oa_xml=oa_xml_content, no_llm=no_llm)
    _write_parse_cache(doi_r, parse_results)

    for method, r in parse_results.items():
        log.debug("[%s]   parse:%s refs=%d abstract=%d intro=%d error=%s",
                  doi_r, method, len(r.get("references") or []),
                  len(r.get("abstract") or ""), len(r.get("intro") or ""),
                  r.get("error"))

    best     = _best_parse_result(parse_results)
    best_src = best.get("source", "unknown")
    best_refs = best.get("references") or []
    log.info("[%s] parse_all best=%s refs=%d abstract=%d intro=%d",
             doi_r, best_src, len(best_refs),
             len(best.get("abstract") or ""), len(best.get("intro") or ""))

    sections = {
        "abstract":   best.get("abstract") or "",
        "intro":      best.get("intro")    or "",
        "methods":    "",
        "references": best_refs,
    }
    grobid = {
        "grobid_status": f"parse_all:{best_src}",
        "n_refs_parsed": len(best_refs),
        "sections":      sections,
    }

    # ── Stage 7: LLM identification ──────────────────────────────────────────
    if no_llm:
        log.info("[%s] no_llm mode — skipping LLM, writing target_pending", doi_r)
        return _build_output(doi_r, flora, cands_row, candidates, {
            "resolved":          False,
            "resolution_method": "none",
            "resolved_doi_o":    "",
            "resolved_title_o":  "",
            "resolved_year_o":   None,
            "resolved_author_o": "",
            "resolution_score":  0.0,
            "llm_error":         "no_llm mode",
        }, pdf, grobid, sections)

    # Guard: refuse to call the LLM when it would have nothing to reason from.
    _has_context = (
        abstract_r
        or candidates
        or (sections.get("intro") or "")
        or (sections.get("references") or [])
    )
    if not _has_context:
        log.warning("[%s] No context — skipping LLM, writing target_pending", doi_r)
        return _build_output(doi_r, flora, cands_row, candidates, {
            "resolved":          False,
            "resolution_method": "no_context",
            "resolved_doi_o":    "",
            "resolved_title_o":  "",
            "resolved_year_o":   None,
            "resolved_author_o": "",
            "resolution_score":  0.0,
            "llm_error":         "no_context: abstract missing, PDF unavailable, no refs",
        }, pdf, grobid, sections)

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
