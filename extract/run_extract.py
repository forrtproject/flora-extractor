"""
run_extract.py — Stage 3 orchestrator.

For every row in filtered.csv:
  - false_positive → pass through unchanged (no extraction)
  - replication/reproduction → classify match type, route to pipeline, write result

Each completed row is appended to data/extracted.csv immediately so that
Stage 4 validation can begin before the full run finishes.

Usage:
    python -m extract.run_extract
"""
import json
import time

import pandas as pd

from shared.config import BASE_DIR, DATA_DIR, LLM_CACHE_DIR, LLM_RATE_SEC, log
from shared.llm_client import call_gemini, call_openai
from shared.openalex_client import extract_author_year_patterns, find_all_candidates
from shared.schema import EXTRACTED_COLS
from shared.utils import cache_key, clean_doi
from extract.link_original import run_for_doi
from extract.multi_original import run_multi_original_for_doi
from extract.code_outcome import extract_outcome

# ── Internal → schema link_method mapping ────────────────────────────────────
_METHOD_MAP = {
    "same_author_year_title_overlap": "author_year_match",
    "single_candidate_after_requery": "author_year_match",
    "grobid_ref_match":               "author_year_match",
    "llm_gemini":                     "llm_fulltext",
    "llm_openai":                     "llm_fulltext",
    "llm_abstract_gemini":            "llm_abstract",
    "llm_abstract_openai":            "llm_abstract",
    "llm_failed":                     "target_pending",
    "no_candidates_found":            "target_pending",
    "needs_fulltext":                 "target_pending",
    "none":                           "target_pending",
    "llm_none":                       "target_pending",
}

_VALID_MATCH_TYPES = {"single_original", "multiple_match", "multiple_original"}


def _map_method(method: str) -> str:
    if method in _METHOD_MAP:
        return _METHOD_MAP[method]
    if method in {"author_year_match", "llm_abstract", "llm_fulltext",
                  "target_pending", "api_error"}:
        return method
    if method.startswith("llm_"):
        return "llm_fulltext"
    return "target_pending"


def _score_to_confidence(score) -> str:
    try:
        f = float(score or 0)
    except (TypeError, ValueError):
        return "low"
    return "high" if f >= 0.8 else "medium" if f >= 0.5 else "low"


# ── Match-type classification (Issue 8) ──────────────────────────────────────

def classify_match_type(row: dict) -> dict:
    """
    Classify original_match_type for a filtered.csv row.

    Steps:
      1. Extract author-year citation patterns from abstract_r
      2. Fetch referenced works from OpenAlex, match against patterns
      3. Call LLM with: title, abstract, matched candidates, count of distinct patterns
      4. Return {"original_match_type": ..., "original_match_confidence": ...}

    Cached as cache_key(doi_r + "_match_type"). On OpenAlex failure, defaults
    to single_original (logs a warning, does not crash).
    """
    doi_r      = clean_doi(str(row.get("doi_r", "")))
    cache_file = LLM_CACHE_DIR / f"match_type_{cache_key(doi_r + '_match_type')}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    abstract_r = str(row.get("abstract_r", ""))
    title_r    = str(row.get("title_r",    ""))
    oa_id_r    = str(row.get("openalex_id_r", ""))
    year_r_str = str(row.get("year_r", ""))
    try:
        year_r = int(year_r_str) if year_r_str else 2099
    except (ValueError, TypeError):
        year_r = 2099

    # Step 1: extract author-year citation patterns from abstract and title
    # extract_author_year_patterns() always returns a list, so we can concatenate results immediately.
    # This steps repeats in find_all_candidates, but we need the patterns here to feed into the LLM prompt
    patterns = extract_author_year_patterns(title_r, max_year=year_r) + extract_author_year_patterns(abstract_r, max_year=year_r)
    distinct_pairs = {(p["surname"], p["year"]) for p in patterns}

    # Step 2: fetch OpenAlex referenced works and match against patterns
    try:
        candidates = find_all_candidates(doi_r, oa_id_r, title_r, abstract_r, year_r, "")
    except Exception as e:
        log.warning("[%s] classify_match_type: OpenAlex failed: %s — defaulting to single_original",
                    doi_r, e)
        return {"original_match_type": "single_original", "original_match_confidence": "low"}

    # Step 3: call LLM
    result = _llm_classify_match_type(doi_r, title_r, abstract_r, distinct_pairs, candidates)

    with cache_file.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)

    return result


def _llm_classify_match_type(doi_r: str,
                              title_r: str,
                              abstract_r: str,
                              distinct_pairs: set,
                              candidates: list) -> dict:
    """LLM call to classify original_match_type. Returns a dict with both fields."""
    abstract_snip = (abstract_r[:800] + "…") if len(abstract_r) > 800 else abstract_r
    pattern_lines = "\n".join(
        f"- {s} ({y})" for s, y in sorted(distinct_pairs)
    ) or "(none found)"
    cand_lines = "\n".join(
        f"{i+1}. \"{c.get('title','?')}\" ({c.get('year','?')}) — {c.get('first_author','?')}"
        for i, c in enumerate(candidates[:15])
    ) or "(none found)"

    prompt = (
        "Classify how many original studies this replication paper targets.\n\n"
        f"TITLE: {title_r}\n"
        f"ABSTRACT: {abstract_snip or '(not available)'}\n\n"
        f"CITED AUTHOR-YEAR PATTERNS IN ABSTRACT ({len(distinct_pairs)} distinct):\n"
        f"{pattern_lines}\n\n"
        f"CANDIDATE ORIGINALS FROM OPENALEX ({len(candidates)} found):\n"
        f"{cand_lines}\n\n"
        "Classify as ONE of:\n"
        "- single_original: paper targets one specific original study\n"
        "- multiple_match: 2–5 candidates share same author/year; paper targets ONE original"
        " but disambiguation is needed\n"
        "- multiple_original: paper explicitly replicates SEVERAL INDEPENDENT original"
        " studies (will produce N output rows)\n\n"
        "Key rule: citing multiple background studies is NOT multiple_original. Only choose\n"
        "multiple_original if the paper's stated goal is to replicate EACH of several independent studies.\n\n"
        'Respond with ONLY this JSON:\n'
        '{"original_match_type": "<single_original|multiple_match|multiple_original>", '
        '"original_match_confidence": "<high|medium|low>", "reasoning": "<brief>"}'
    )

    result, _ = call_gemini(prompt)
    if not result:
        result, _ = call_openai(prompt)
    if result:
        time.sleep(LLM_RATE_SEC)
        mtype = result.get("original_match_type", "single_original")
        conf  = result.get("original_match_confidence", "low")
        if mtype not in _VALID_MATCH_TYPES:
            mtype = "single_original"
        if conf not in {"high", "medium", "low"}:
            conf = "low"
        return {"original_match_type": mtype, "original_match_confidence": conf}

    log.warning("[%s] classify_match_type: LLM failed — defaulting to single_original", doi_r)
    return {"original_match_type": "single_original", "original_match_confidence": "low"}


# ── Data adapters ─────────────────────────────────────────────────────────────

def _build_cands_df(row: pd.Series) -> pd.DataFrame:
    """Build a minimal cands_df for link_original.run_for_doi from a filtered.csv row."""
    return pd.DataFrame([{
        "doi_r":                 str(row.get("doi_r", "")),
        "study_r":               str(row.get("title_r", row.get("study_r", ""))),
        "abstract_r":            str(row.get("abstract_r", "")),
        "year_r":                str(row.get("year_r",    "")),
        "openalex_id_r":         str(row.get("openalex_id_r", "")),
        "url_r":                 str(row.get("url_r",    "")),
        "author_year_pattern_r": "",
    }])


def _build_rep_df(row: pd.Series) -> pd.DataFrame:
    """Build a minimal all_rep_df for multi_original.run_multi_original_for_doi."""
    return pd.DataFrame([{
        "doi_r":                 str(row.get("doi_r", "")),
        "study_r":               str(row.get("title_r", row.get("study_r", ""))),
        "abstract_r":            str(row.get("abstract_r", "")),
        "year_r":                str(row.get("year_r",    "")),
        "url_r":                 str(row.get("url_r",    "")),
        "openalex_id_r":         str(row.get("openalex_id_r", "")),
        "author_year_pattern_r": "",
    }])


# ── Row merge helpers ─────────────────────────────────────────────────────────

def _merge_row(filter_row: pd.Series, link: dict, outcome: dict,
               match_type: str, match_conf: str,
               rank: int, n: int) -> dict:
    row = filter_row.to_dict()
    # propagate study_r → title_r if title_r is absent (old seeded data uses study_r)
    if not row.get("title_r"):
        row["title_r"] = row.get("study_r", "")
    row.update({
        "original_match_type":       match_type,
        "original_match_confidence": match_conf,
        "doi_o":           clean_doi(link.get("resolved_doi_o",   "") or ""),
        "title_o":         str(link.get("resolved_title_o", "") or ""),
        "year_o":          str(link.get("resolved_year_o",  "") or ""),
        "authors_o":       str(link.get("resolved_author_o","") or ""),
        "link_method":     _map_method(link.get("resolution_method", "target_pending")),
        "link_evidence":   str(link.get("llm_evidence",     "") or ""),
        "link_confidence": _score_to_confidence(link.get("resolution_score", 0)),
        "outcome":             outcome.get("outcome",             "uninformative"),
        "outcome_phrase":      outcome.get("outcome_phrase",      ""),
        "outcome_confidence":  outcome.get("outcome_confidence",  "low"),
        "out_quote_source":    outcome.get("out_quote_source",    ""),
        "type":          "reproduction"
                         if str(filter_row.get("filter_status", "")) == "reproduction"
                         else "replication",
        "original_rank": rank,
        "n_originals":   n,
    })
    return row


def _merge_multi_row(filter_row: pd.Series, orig: dict, outcome: dict,
                     match_type: str, match_conf: str, n: int) -> dict:
    row = filter_row.to_dict()
    if not row.get("title_r"):
        row["title_r"] = row.get("study_r", "")
    conf_str = orig.get("confidence", "low")
    if conf_str not in {"high", "medium", "low"}:
        conf_str = "low"
    row.update({
        "original_match_type":       match_type,
        "original_match_confidence": match_conf,
        "doi_o":           clean_doi(orig.get("doi",          "") or ""),
        "title_o":         str(orig.get("title",        "") or ""),
        "year_o":          str(orig.get("year",         "") or ""),
        "authors_o":       str(orig.get("first_author", "") or ""),
        "link_method":     "llm_abstract",
        "link_evidence":   str(orig.get("evidence",     "") or ""),
        "link_confidence": conf_str,
        "outcome":             outcome.get("outcome",             "uninformative"),
        "outcome_phrase":      outcome.get("outcome_phrase",      ""),
        "outcome_confidence":  outcome.get("outcome_confidence",  "low"),
        "out_quote_source":    outcome.get("out_quote_source",    ""),
        "type":          "replication",
        "original_rank": orig.get("rank", 1),
        "n_originals":   n,
    })
    return row


def _empty_row(filter_row: pd.Series, match_type: str, match_conf: str) -> dict:
    row = filter_row.to_dict()
    row.update({
        "original_match_type":       match_type,
        "original_match_confidence": match_conf,
        "doi_o": "", "title_o": "", "year_o": "", "authors_o": "",
        "link_method": "api_error", "link_evidence": "", "link_confidence": "low",
        "outcome": "api_error", "outcome_phrase": "",
        "outcome_confidence": "low", "out_quote_source": "",
        "type": "", "original_rank": 1, "n_originals": 1,
    })
    return row


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_outcome(doi_r: str, row: pd.Series, link: dict) -> dict:
    abstract_r = str(row.get("abstract_r", ""))
    title_r    = str(row.get("title_r",    ""))
    fulltext   = str(link.get("grobid_intro", "") or link.get("html_text", "") or "")
    return extract_outcome(doi_r, abstract_r, fulltext, title_r)


def _parse_originals(result: dict) -> list[dict]:
    """Extract originals list from run_multi_original_for_doi result."""
    raw = result.get("originals")
    if isinstance(raw, list) and raw:
        return raw
    json_str = result.get("originals_json", "[]")
    if isinstance(json_str, str):
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return []
    return []


# ── Main orchestrator ─────────────────────────────────────────────────────────

def _append_row(out_path, result_row: dict, first: bool) -> None:
    """Write one result row to the output CSV immediately after processing.

    first=True  → open with mode='w' (creates / truncates the file) and write header.
    first=False → open with mode='a' (append) and skip header.
    """
    row_df = pd.DataFrame([result_row])
    for col in EXTRACTED_COLS:
        if col not in row_df.columns:
            row_df[col] = ""
    row_df[EXTRACTED_COLS].to_csv(
        out_path, mode="w" if first else "a",
        index=False, encoding="utf-8-sig", header=first,
    )


def run_extract() -> pd.DataFrame:
    """
    Run Stage 3 and stream results to data/extracted.csv.

    Each completed row is written immediately so Stage 4 validation can begin
    before the full run finishes.
    """
    filtered_path = DATA_DIR / "filtered.csv"
    if not filtered_path.exists():
        sample_path = BASE_DIR / "misc" / "sample_filtered.csv"
        if sample_path.exists():
            log.info("data/filtered.csv not found — using misc/sample_filtered.csv")
            filtered_path = sample_path
        else:
            raise FileNotFoundError(
                f"filtered.csv not found at {filtered_path}. Run Stage 2 first."
            )

    df = pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Stage 3: loaded %d rows from %s", len(df), filtered_path.name)

    out_path = DATA_DIR / "extracted.csv"
    output_rows: list[dict] = []
    first_write = True  # write CSV header on the first row

    for _, row in df.iterrows():
        result_rows: list[dict] = []

        # False positives pass through unchanged — no extraction
        if row.get("filter_status") == "false_positive":
            result_rows.append(row.to_dict())
        else:
            doi_r = clean_doi(str(row.get("doi_r", "")))
            match = classify_match_type(row.to_dict())
            match_type = match["original_match_type"]
            match_conf = match["original_match_confidence"]
            log.info("[%s] match_type=%s conf=%s", doi_r, match_type, match_conf)

            try:
                if match_type == "multiple_original":
                    result    = run_multi_original_for_doi(doi_r, _build_rep_df(row))
                    originals = _parse_originals(result)
                    if result.get("is_false_positive") or not originals:
                        link    = run_for_doi(doi_r, cands_df=_build_cands_df(row))
                        outcome = _get_outcome(doi_r, row, link)
                        result_rows.append(
                            _merge_row(row, link, outcome, "single_original", match_conf, 1, 1)
                        )
                    else:
                        for orig in originals:
                            outcome = _get_outcome(doi_r, row, {})
                            result_rows.append(
                                _merge_multi_row(row, orig, outcome, match_type, match_conf,
                                                 len(originals))
                            )
                else:
                    link    = run_for_doi(doi_r, cands_df=_build_cands_df(row))
                    outcome = _get_outcome(doi_r, row, link)
                    result_rows.append(
                        _merge_row(row, link, outcome, match_type, match_conf, 1, 1)
                    )

            except Exception as e:
                log.error("[%s] extraction failed: %s", doi_r, e)
                result_rows.append(_empty_row(row, match_type, match_conf))

        for result_row in result_rows:
            _append_row(out_path, result_row, first=first_write)
            first_write = False
            output_rows.append(result_row)
            log.info("Streamed %d/%d rows → %s", len(output_rows), len(df), out_path.name)

    log.info("Stage 3 complete: %d rows → %s", len(output_rows), out_path)
    return pd.DataFrame(output_rows)


if __name__ == "__main__":
    run_extract()
