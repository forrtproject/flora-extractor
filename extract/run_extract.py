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
import re
import time

import pandas as pd

from shared.config import (
    BASE_DIR, DATA_DIR, GEMINI_LIGHT_MODEL, LLM_CACHE_DIR, LLM_RATE_SEC,
    OA_XML_CACHE_DIR, PARSE_CACHE_DIR, PDF_CACHE_DIR, log,
)
from shared.llm_client import call_llm
from shared.openalex_client import extract_author_year_patterns, find_all_candidates
from shared.pdf_parsing import parse_all as _parse_all
from shared.schema import EXTRACTED_COLS, make_pair_id
from shared.utils import cache_key, clean_doi
from extract.link_original import run_for_doi
from extract.multi_original import run_multi_original_for_doi
from extract.code_outcome import extract_outcome

# ── Internal → schema link_method mapping ────────────────────────────────────
_METHOD_MAP = {
    "citation_context_match":         "author_year_match",
    "same_author_year_title_overlap": "author_year_match",
    "single_candidate_after_requery": "author_year_match",
    "title_pattern_match":            "author_year_match",
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
_VALID_OUTCOMES    = {"success", "failure", "mixed", "uninformative", "descriptive"}

# ── Rule-based multi-original detection ──────────────────────────────────────
# These patterns catch papers whose title or abstract unambiguously declares
# that N independent original studies are being replicated (Many Labs, RRR, etc).
# They run BEFORE the LLM and BEFORE the cache so they cannot be overridden by
# a stale cached single_original result.

_MULTI_TITLE_RE = re.compile(
    r"\bmany\s+labs\b"
    r"|\bregistered\s+replication\s+report\b"
    r"|\bmany\s+analysts\b"
    r"|\breplicat(?:ion|ions?)\s+of\s+\d+\b",
    re.IGNORECASE,
)

# Each pattern must capture the count of studies in group 1.
_MULTI_ABSTRACT_RES: list[re.Pattern] = [
    # "replications of 28"  /  "replication of 10 studies"
    re.compile(r"\breplicat(?:ion|ions?)\s+of\s+(\d+)\b", re.IGNORECASE),
    # "replicated 28 original findings"  /  "replicating 10 classic studies"
    re.compile(
        r"\b(?:replicated?|replicating)\s+(?:a\s+total\s+of\s+)?(\d+)\s*"
        r"(?:original|independent|published|classic|contemporary|distinct|previous)?"
        r"\s*(?:studi(?:es)?|findings?|experiments?|effects?|papers?)\b",
        re.IGNORECASE,
    ),
    # "28 classic and contemporary findings"  /  "27 independent studies"
    re.compile(
        r"\b(\d+)\s+(?:original|independent|published|classic|contemporary|distinct)"
        r"(?:\s+and\s+\w+(?:\s+\w+)?)?\s+(?:studi(?:es)?|findings?|experiments?|effects?)\b",
        re.IGNORECASE,
    ),
]

_MULTI_N_MIN = 3  # counts < 3 might be multiple_match, not multiple_original


def _rule_classify_multi_original(title_r: str, abstract_r: str) -> "dict | None":
    """
    Return a classification dict if title or abstract contains unambiguous signals
    that the paper replicates N ≥ 3 independent original studies. Returns None
    when no rule fires (caller should fall through to LLM).
    """
    if _MULTI_TITLE_RE.search(title_r):
        return {
            "original_match_type":       "multiple_original",
            "original_match_confidence": "high",
            "rule_fired":                True,
            "reasoning": "Title matches a known multi-target replication project or explicit 'replication of N' pattern.",
        }
    for pattern in _MULTI_ABSTRACT_RES:
        m = pattern.search(abstract_r)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except (IndexError, ValueError, TypeError):
            continue
        if n >= _MULTI_N_MIN:
            return {
                "original_match_type":       "multiple_original",
                "original_match_confidence": "high",
                "rule_fired":                True,
                "reasoning": f"Abstract explicitly states replication of {n} studies.",
            }
    return None


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

def classify_match_type(row: dict, no_llm: bool = False) -> dict:
    """
    Classify original_match_type for a filtered.csv row.

    Steps:
      0. Rule-based pre-screening (title/abstract patterns) — fires before cache
      1. Extract author-year citation patterns from abstract_r
      2. Fetch referenced works from OpenAlex, match against patterns
      3. Call LLM with: title, abstract, matched candidates, count of distinct patterns
      4. Return {"original_match_type": ..., "original_match_confidence": ...}

    no_llm=True: rules only; returns single_original default when no rule fires.
    Rules run BEFORE the cache so a stale single_original result from a prior LLM
    call cannot override a deterministic rule match (e.g. Many Labs, RRR papers).
    LLM results are cached as cache_key(doi_r + "_match_type"). On OpenAlex failure,
    defaults to single_original (logs a warning, does not crash).
    """
    doi_r      = clean_doi(str(row.get("doi_r", "")))
    title_r    = str(row.get("title_r",    ""))
    abstract_r = str(row.get("abstract_r", ""))
    oa_id_r    = str(row.get("openalex_id_r", ""))
    year_r_str = str(row.get("year_r", ""))

    # Step 0: deterministic rules — catch Many Labs / RRR / "replications of N" papers
    # without an LLM call and without being overridden by a cached LLM result.
    rule = _rule_classify_multi_original(title_r, abstract_r)
    if rule:
        log.info("[%s] classify_match_type: rule fired → %s", doi_r, rule["original_match_type"])
        return rule

    if no_llm:
        return {"original_match_type": "single_original",
                "original_match_confidence": "low", "rule_fired": False}

    cache_file = LLM_CACHE_DIR / f"match_type_{cache_key(doi_r + '_match_type')}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

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
        "- multiple_match: 2–5 candidates share the SAME author/year; paper targets ONE"
        " original but disambiguation is needed (e.g. two papers by Smith 2005)\n"
        "- multiple_original: paper explicitly replicates SEVERAL INDEPENDENT original"
        " studies as its stated goal (will produce N output rows, one per original)\n\n"
        "Key rules:\n"
        "1. Merely citing many background studies is NOT multiple_original.\n"
        "2. A large candidate list from OpenAlex does NOT mean multiple_original —"
        " it may just reflect many citations.\n"
        "3. STRONG signals for multiple_original: explicit count in abstract"
        " (e.g. 'replications of 28 studies'), project names like Many Labs or"
        " Registered Replication Report, a table of target studies each with its own protocol.\n"
        "4. multiple_match applies when ONE study is targeted but there are 2–5 candidates"
        " with the identical author/year — not when there are many different author/year pairs.\n\n"
        'Respond with ONLY this JSON:\n'
        '{"original_match_type": "<single_original|multiple_match|multiple_original>", '
        '"original_match_confidence": "<high|medium|low>", "reasoning": "<brief>"}'
    )

    result, model_used, _ = call_llm(prompt, gemini_model=GEMINI_LIGHT_MODEL)
    if result:
        time.sleep(LLM_RATE_SEC)
        mtype = result.get("original_match_type", "single_original")
        conf  = result.get("original_match_confidence", "low")
        if mtype not in _VALID_MATCH_TYPES:
            mtype = "single_original"
        if conf not in {"high", "medium", "low"}:
            conf = "low"
        return {
            "original_match_type":       mtype,
            "original_match_confidence": conf,
            "classify_llm_model":        model_used,
            "reasoning":                 str(result.get("reasoning", "") or ""),
        }

    log.warning("[%s] classify_match_type: LLM failed — defaulting to single_original", doi_r)
    return {"original_match_type": "single_original", "original_match_confidence": "low",
            "classify_llm_model": ""}


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
    doi_r_clean = clean_doi(str(filter_row.get("doi_r", "")))
    doi_o_clean = clean_doi(link.get("resolved_doi_o", "") or "")
    row.update({
        "pair_id":           make_pair_id(doi_r_clean, doi_o_clean),
        "original_match_type":       match_type,
        "original_match_confidence": match_conf,
        "doi_o":           doi_o_clean,
        "title_o":         str(link.get("resolved_title_o", "") or ""),
        "year_o":          str(link.get("resolved_year_o",  "") or ""),
        "authors_o":       str(link.get("resolved_author_o","") or ""),
        "link_method":     _map_method(link.get("resolution_method", "target_pending")),
        "link_evidence":   str(link.get("llm_evidence",     "") or ""),
        "link_confidence": (link["llm_confidence"]
                            if link.get("llm_confidence") in {"high", "medium", "low"}
                            else _score_to_confidence(link.get("resolution_score", 0))),
        "link_llm_model":  str(link.get("llm_model",        "") or ""),
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
                     match_type: str, match_conf: str, n: int,
                     link_llm_model: str = "") -> dict:
    row = filter_row.to_dict()
    if not row.get("title_r"):
        row["title_r"] = row.get("study_r", "")
    conf_str = orig.get("confidence", "low")
    if conf_str not in {"high", "medium", "low"}:
        conf_str = "low"
    doi_r_clean  = clean_doi(str(filter_row.get("doi_r", "")))
    doi_o_clean  = clean_doi(orig.get("doi", "") or "")
    row.update({
        "pair_id":           make_pair_id(doi_r_clean, doi_o_clean),
        "original_match_type":       match_type,
        "original_match_confidence": match_conf,
        "doi_o":           doi_o_clean,
        "title_o":         str(orig.get("title",        "") or ""),
        "year_o":          str(orig.get("year",         "") or ""),
        "authors_o":       str(orig.get("first_author", "") or ""),
        "link_method":     "llm_abstract",
        "link_evidence":   str(orig.get("evidence",     "") or ""),
        "link_confidence": conf_str,
        "link_llm_model":  link_llm_model,
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
    doi_r_clean = clean_doi(str(filter_row.get("doi_r", "")))
    row.update({
        "pair_id": make_pair_id(doi_r_clean, ""),
        "original_match_type":       match_type,
        "original_match_confidence": match_conf,
        "doi_o": "", "title_o": "", "year_o": "", "authors_o": "",
        "link_method": "api_error", "link_evidence": "", "link_confidence": "low",
        "link_llm_model": "",
        "outcome": "api_error", "outcome_phrase": "",
        "outcome_confidence": "low", "out_quote_source": "",
        "type": "", "original_rank": 1, "n_originals": 1,
    })
    return row


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_parse_cache(doi_r: str) -> None:
    """Run all PDF parsers for doi_r and cache results to PARSE_CACHE_DIR."""
    key      = cache_key(doi_r)
    out_file = PARSE_CACHE_DIR / f"parse_{key}.json"
    if out_file.exists():
        return

    pdf_path = PDF_CACHE_DIR / f"{key}.pdf"
    if not pdf_path.exists():
        pdf_path = None  # type: ignore[assignment]

    oa_xml_file = OA_XML_CACHE_DIR / f"oa_xml_{key}.json"
    oa_xml: dict | None = None
    if oa_xml_file.exists():
        try:
            with oa_xml_file.open(encoding="utf-8") as fh:
                oa_xml = json.load(fh)
        except Exception:
            pass

    results = _parse_all(doi_r, pdf_path, oa_xml=oa_xml)
    try:
        with out_file.open("w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.debug("[%s] _save_parse_cache write failed: %s", doi_r, exc)


def _get_outcome(doi_r: str, row: pd.Series, link: dict, no_llm: bool = False) -> dict:
    abstract_r = str(row.get("abstract_r", ""))
    title_r    = str(row.get("title_r",    ""))
    # Combine all parsed sections for richer keyword coverage (~3200 chars vs. previous 1000)
    fulltext = " ".join(filter(None, [
        str(link.get("grobid_abstract", "") or ""),
        str(link.get("grobid_intro",    "") or ""),
        str(link.get("grobid_methods",  "") or ""),
        str(link.get("html_text",       "") or ""),
    ]))
    return extract_outcome(doi_r, abstract_r, fulltext, title_r, no_llm=no_llm)


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


def run_extract(no_llm: bool = False,
                limit: "int | None" = None) -> pd.DataFrame:
    """
    Run Stage 3 and stream results to data/extracted.csv.

    no_llm=True: skip all LLM calls (rule-based only).
    limit: process only the first N non-false-positive rows.
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
    first_write = True
    processed = 0

    for _, row in df.iterrows():
        result_rows: list[dict] = []

        # False positives are excluded from extracted.csv entirely
        if row.get("filter_status") == "false_positive":
            log.debug("[%s] false_positive — skipped", clean_doi(str(row.get("doi_r", ""))))
            continue
        else:
            if limit is not None and processed >= limit:
                break
            processed += 1

            doi_r = clean_doi(str(row.get("doi_r", "")))
            match = classify_match_type(row.to_dict(), no_llm=no_llm)
            match_type = match["original_match_type"]
            match_conf = match["original_match_confidence"]
            log.info("[%s] match_type=%s conf=%s", doi_r, match_type, match_conf)

            try:
                if match_type == "multiple_original":
                    rule_fired = bool(match.get("rule_fired", False))
                    result    = run_multi_original_for_doi(
                        doi_r, _build_rep_df(row), force_multi=rule_fired
                    )
                    originals = _parse_originals(result)
                    if not originals:
                        if rule_fired:
                            log.warning(
                                "[%s] rule_fired=True but LLM returned no originals — "
                                "writing target_pending (NOT single_original)", doi_r
                            )
                            result_rows.append(_empty_row(row, "multiple_original", match_conf))
                        else:
                            link    = run_for_doi(doi_r, cands_df=_build_cands_df(row),
                                                  no_llm=no_llm)
                            _save_parse_cache(doi_r)
                            outcome = _get_outcome(doi_r, row, link, no_llm=no_llm)
                            result_rows.append(
                                _merge_row(row, link, outcome, "single_original", match_conf, 1, 1)
                            )
                    else:
                        multi_llm_model = str(result.get("llm_model", "") or "")
                        for orig in originals:
                            raw_out = str(orig.get("outcome", "uninformative") or "uninformative").lower()
                            if raw_out not in _VALID_OUTCOMES:
                                raw_out = "uninformative"
                            outcome = {
                                "outcome":            raw_out,
                                "outcome_phrase":     str(orig.get("outcome_evidence", "") or ""),
                                "outcome_confidence": str(orig.get("confidence", "low") or "low"),
                                "out_quote_source":   "llm_multi",
                            }
                            result_rows.append(
                                _merge_multi_row(row, orig, outcome, match_type, match_conf,
                                                 len(originals), multi_llm_model)
                            )
                else:
                    link    = run_for_doi(doi_r, cands_df=_build_cands_df(row), no_llm=no_llm)
                    _save_parse_cache(doi_r)
                    outcome = _get_outcome(doi_r, row, link, no_llm=no_llm)
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
            log.info("Streamed %d rows → %s", len(output_rows), out_path.name)

    log.info("Stage 3 complete: %d rows → %s", len(output_rows), out_path)
    return pd.DataFrame(output_rows)


def run_match_type_only(no_llm: bool = False,
                        limit: "int | None" = None) -> pd.DataFrame:
    """
    Read filtered.csv, classify match type per row, write data/match_type_only.csv.
    Useful for evaluating match-type classification in isolation.
    """
    filtered_path = DATA_DIR / "filtered.csv"
    if not filtered_path.exists():
        filtered_path = BASE_DIR / "misc" / "sample_filtered.csv"
    df = pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig").fillna("")
    eligible = df[df["filter_status"] != "false_positive"]
    if limit is not None:
        eligible = eligible.head(limit)

    rows = []
    for _, row in eligible.iterrows():
        doi_r = clean_doi(str(row.get("doi_r", "")))
        match = classify_match_type(row.to_dict(), no_llm=no_llm)
        rows.append({
            "doi_r":         doi_r,
            "title_r":       str(row.get("title_r", "")),
            "filter_status": str(row.get("filter_status", "")),
            "match_type":    match["original_match_type"],
            "match_conf":    match["original_match_confidence"],
            "rule_fired":    str(match.get("rule_fired", False)),
            "reasoning":     str(match.get("reasoning", "")),
        })

    out = pd.DataFrame(rows)
    out_path = DATA_DIR / "match_type_only.csv"
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("match-type-only: %d rows → %s", len(out), out_path)
    return out


def run_outcome_only(no_llm: bool = False,
                     limit: "int | None" = None) -> pd.DataFrame:
    """
    Read filtered.csv, classify outcome per row, write data/outcome_only.csv.
    Useful for evaluating outcome classification in isolation.
    """
    filtered_path = DATA_DIR / "filtered.csv"
    if not filtered_path.exists():
        filtered_path = BASE_DIR / "misc" / "sample_filtered.csv"
    df = pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig").fillna("")
    eligible = df[df["filter_status"] != "false_positive"]
    if limit is not None:
        eligible = eligible.head(limit)

    rows = []
    for _, row in eligible.iterrows():
        doi_r   = clean_doi(str(row.get("doi_r", "")))
        outcome = extract_outcome(
            doi_r,
            str(row.get("abstract_r", "")),
            fulltext="",
            title_r=str(row.get("title_r", "")),
            no_llm=no_llm,
        )
        rows.append({
            "doi_r":              doi_r,
            "title_r":            str(row.get("title_r", "")),
            "outcome":            outcome["outcome"],
            "outcome_phrase":     outcome["outcome_phrase"],
            "outcome_confidence": outcome["outcome_confidence"],
            "out_quote_source":   outcome["out_quote_source"],
        })

    out = pd.DataFrame(rows)
    out_path = DATA_DIR / "outcome_only.csv"
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("outcome-only: %d rows → %s", len(out), out_path)
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Stage 3 Extract pipeline")
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Skip all LLM calls. Rule-based only.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--match-type-only", action="store_true",
        help="Classify match type only -> data/match_type_only.csv",
    )
    group.add_argument(
        "--outcome-only", action="store_true",
        help="Classify outcome only -> data/outcome_only.csv",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Process only the first N non-false-positive rows.",
    )
    args = parser.parse_args()

    if args.match_type_only:
        run_match_type_only(no_llm=args.no_llm, limit=args.limit)
    elif args.outcome_only:
        run_outcome_only(no_llm=args.no_llm, limit=args.limit)
    else:
        run_extract(no_llm=args.no_llm, limit=args.limit)
