"""
run_extract.py — Stage 3 orchestrator.

For every row in filtered.csv:
  - false_positive → skipped (known non-replication; not written to extracted.csv)
  - replication/reproduction → classify match type, route to pipeline, write result

Each completed row is appended to data/extracted.csv immediately so that
Stage 4 validation can begin before the full run finishes.

Usage:
    python -m extract.run_extract
"""
import json
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from shared.config import (
    BASE_DIR, DATA_DIR, GEMINI_API_KEYS,
    OA_XML_CACHE_DIR, OPENAI_API_KEY, OPENROUTER_API_KEY, PARSE_CACHE_DIR,
    PDF_CACHE_DIR, log,
)
from shared import token_counter
from shared.llm_client import (
    _clean_study_numbers, classify_replication, screen_voters,
)
from shared.token_usage import TokenBudgetExhausted
from shared.openalex_client import OpenAlexQuotaExhausted
from shared.openalex_client import fetch_openalex_by_doi as _oa_by_doi
from shared.openalex_client import fetch_openalex_full_metadata as _oa_full_meta
from shared.openalex_client import _search_crossref_by_title, _search_openalex_by_title
from shared.pdf_parsing import (
    best_parse_result,
    outcome_text,
    parse_all as _parse_all,
    parse_result_is_empty,
    score_parse_result,
)
from shared.row_key import primary_key
from shared.doi_verify import keeps_no_doi, verify_and_correct
from shared.schema import (
    EXTRACTED_COLS,
    LINK_METHOD_VALUES,
    OUTCOME_CATEGORIES,
    RESOLVED_LINK_METHODS,
    make_pair_id,
)
from shared.utils import (bare_work_id, cache_key, clean_citation_title, clean_doi,
                          csv_lock, usable_title)
# Shared with csv_to_db so extraction and validation skip the same set (see shared/flora_skip.py)
from shared.flora_skip import (
    FLORA_VALIDATED_STATUSES,
    load_flora_skip_dois as _load_flora_skip_dois,
)
from extract.link_original import run_for_doi
from extract.code_outcome import extract_outcome, predict_outcome_keyword

def build_bibtex(
    authors: list,
    year: str,
    title: str,
    journal: str = "",
    volume: str = "",
    issue: str = "",
    first_page: str = "",
    last_page: str = "",
    doi: str = "",
    url: str = "",
) -> str:
    """Build a BibTeX entry string from metadata fields.

    Entry type is @article when a journal is present, @misc otherwise.
    Cite key: FirstAuthorSurname_Year (e.g. Smith_2021).
    Author format follows APA initials convention (Last, F. M.).
    """
    year_str = str(year or "")
    first_surname = (
        re.sub(r"[^A-Za-z0-9]", "", authors[0].split(",")[0].strip())
        if authors else "Unknown"
    )
    cite_key   = f"{first_surname}_{year_str}" if year_str else first_surname
    entry_type = "article" if journal else "misc"
    author_str = " and ".join(str(a) for a in authors) if authors else ""
    pages = (f"{first_page}--{last_page}" if first_page and last_page
             else (first_page or ""))
    doi_url = f"https://doi.org/{doi}" if doi else (url or "")

    fields: dict[str, str] = {"title": title or ""}
    if author_str:
        fields["author"] = author_str
    if journal:
        fields["journal"] = journal
    if volume:
        fields["volume"] = volume
    if issue:
        fields["number"] = issue
    if pages:
        fields["pages"] = pages
    if year_str:
        fields["year"] = year_str
    if doi:
        fields["doi"] = doi
    if doi_url:
        fields["url"] = doi_url

    body = ", ".join(f"{k}={{{v}}}" for k, v in fields.items())
    return f"@{entry_type}{{{cite_key}, {body}}}"


def _build_ref_o(doi_o: str, fallback_author: str = "",
                  fallback_year: str = "",
                  title_o: str = "") -> tuple[str, str, str]:
    """Build APA-style ref_o, authors_o, and bibtex_ref_o for a resolved original.

    Resolution order:
      1. DOI lookup via OpenAlex then CrossRef (fetch_openalex_full_metadata)
      2. Title search via CrossRef then OpenAlex (when DOI lookup fails and title_o given)
      3. Surname · Year fallback (when all API lookups fail)

    Returns (ref_o, authors_o, bibtex_ref_o).
    """
    meta: dict | None = None
    if doi_o:
        try:
            meta = _oa_full_meta(doi_o)
        except Exception:
            pass

    # Title-based fallback for no_metadata / hallucinated DOIs
    if meta is None and title_o:
        try:
            meta = (_search_crossref_by_title(title_o, fallback_year)
                    or _search_openalex_by_title(title_o, fallback_year))
            if meta:
                log.debug("[ref_o] title search hit for %r: %s", title_o[:60], meta.get("doi"))
        except Exception:
            pass

    if not meta:
        surname = str(fallback_author or "").split()[-1] if fallback_author else ""
        year    = str(fallback_year or "")
        ref     = " · ".join(s for s in [surname, year] if s)
        return ref, surname, ""

    authors    = meta.get("authors") or []
    year       = str(meta.get("year") or fallback_year or "")
    title      = meta.get("title") or ""
    journal    = meta.get("journal") or ""
    volume     = meta.get("volume") or ""
    issue      = meta.get("issue") or ""
    first_page = meta.get("first_page") or ""
    last_page  = meta.get("last_page") or ""
    doi_val    = meta.get("doi") or doi_o

    authors_o = "; ".join(authors)

    # APA author string
    if len(authors) == 1:
        auth_str = authors[0]
    elif len(authors) == 2:
        auth_str = f"{authors[0]}, & {authors[1]}"
    elif authors:
        auth_str = ", ".join(authors[:-1]) + f", & {authors[-1]}"
    else:
        auth_str = str(fallback_author or "")

    # Source segment: Journal, vol(issue), pages.
    source_seg = ""
    if journal:
        vol_issue = f", {volume}({issue})" if volume and issue else (f", {volume}" if volume else "")
        pages     = f", {first_page}–{last_page}" if first_page and last_page else (f", {first_page}" if first_page else "")
        source_seg = f"{journal}{vol_issue}{pages}."

    doi_url  = f"https://doi.org/{doi_val}" if doi_val else ""
    parts    = [f"{auth_str} ({year})." if auth_str else f"({year})."]
    if title:
        parts.append(f"{title}.")
    if source_seg:
        parts.append(source_seg)
    if doi_url:
        parts.append(doi_url)
    ref_o = " ".join(parts)

    bibtex_o = build_bibtex(
        authors, year, title, journal, volume, issue, first_page, last_page, doi_val,
    )
    return ref_o, authors_o, bibtex_o


# ── Internal → schema link_method mapping ────────────────────────────────────
# The five rule-based resolution methods pass through unchanged as their own public
# link_method values (they used to all collapse to "author_year_match"). They have
# very different reliability — single_candidate_after_requery auto-accepts a lone
# candidate at score 1.0 with no semantic check, whereas citation_context_match needs
# author+year+journal agreement — so downstream consumers must tell them apart. Only
# the internal LLM source labels are remapped to the public llm_* values.
_METHOD_MAP = {
    "citation_context_match":         "citation_context_match",
    "same_author_year_title_overlap": "same_author_year_title_overlap",
    "single_candidate_after_requery": "single_candidate_after_requery",
    "title_pattern_match":            "title_pattern_match",
    "grobid_ref_match":               "grobid_ref_match",
    # One entry per provider the ladder can answer from. Without the openrouter
    # rows, an OpenRouter-answered title search falls through _map_method's
    # startswith("llm_") catch-all to llm_fulltext — a resolved method — so a
    # provisional ~50%-precision link is coded, confidence-scored and imported
    # instead of quarantined.
    "llm_title_search_gemini":        "llm_title_search",
    "llm_title_search_openai":        "llm_title_search",
    "llm_title_search_openrouter":    "llm_title_search",
    "llm_gemini":                     "llm_fulltext",
    "llm_openai":                     "llm_fulltext",
    "llm_openrouter":                 "llm_fulltext",
    "llm_cited_candidates_gemini":            "llm_cited_candidates",
    "llm_cited_candidates_openai":            "llm_cited_candidates",
    "llm_cited_candidates_openrouter":        "llm_cited_candidates",
    # Resolved from the paper's own OpenAlex reference list, at high confidence
    # only — see the Stage 4.5 screen in link_original.run_for_doi.
    "llm_references":                 "llm_references",
    "llm_title_search_prepdf":        "llm_title_search",
    # The same screen concluded the paper is not a replication at all, so there is
    # no original to look for and no reason to fetch the PDF.
    "llm_not_a_replication":          "not_a_replication",
    # LLM ran successfully but concluded no identifiable original study exists.
    # Distinct from llm_failed (API errors) and llm_fulltext (original found).
    "llm_no_target":                  "no_original_found",
    # The merged target prompt saw several originals, so no single link may be
    # written; the row goes to the per-target adapter (see _resolve_and_code) and
    # only reaches this value when none of the targets could be matched to a record.
    "llm_multi_target":               "target_pending",
    "llm_failed":                     "target_pending",
    "llm_refscreen_declined":         "target_pending",
    # Only one of the two Q1 classifiers answered — no agreement can be read from a
    # single vote, so the row waits for a re-run rather than being escalated or
    # filed as a disagreement. Both classifiers failing is a plain API failure.
    "llm_refscreen_partial":          "target_pending",
    "llm_refscreen_failed":           "api_error",
    "no_candidates_found":            "target_pending",
    "needs_fulltext":                 "target_pending",
    "no_fulltext_available":          "target_pending",
    "none":                           "target_pending",
    "llm_none":                       "target_pending",
}

_VALID_OUTCOMES    = OUTCOME_CATEGORIES

# The reproduction outcome axes, carried through every row producer alongside the
# shared outcome block. Empty on a replication row.
_OUTCOME_AXIS_COLS = (
    "outcome_computation", "outcome_computational_quote",
    "out_quote_computational_source",
    "outcome_robustness", "outcome_robustness_quote", "out_quote_robust_source",
)

def _map_method(method: str) -> str:
    if method in _METHOD_MAP:
        return _METHOD_MAP[method]
    if method in LINK_METHOD_VALUES:
        return method
    if method == "llm_no_target":
        return "no_original_found"
    if method.startswith("llm_"):
        return "llm_fulltext"
    return "target_pending"


def _score_to_confidence(score) -> str:
    try:
        f = float(score or 0)
    except (TypeError, ValueError):
        return "low"
    return "high" if f >= 0.8 else "medium" if f >= 0.5 else "low"


def _link_confidence(link: dict) -> str:
    """Persisted link_confidence: LLM confidence if present, else derived from score.

    #51: single_candidate_after_requery auto-accepts a lone candidate at score 1.0
    with NO semantic check — "exactly one candidate came back" is not evidence it is
    the replication TARGET. Cap it at medium so validation prioritises these rows.

    A title-search link is always low: it is measured at roughly 50% precision and the
    score it carries is the search's title match, which says the DOI is the named paper
    — not that the named paper is the target (see LINK_METHOD_VALUES).
    """
    conf = (link["llm_confidence"]
            if link.get("llm_confidence") in {"high", "medium", "low"}
            else _score_to_confidence(link.get("resolution_score", 0)))
    if _map_method(str(link.get("resolution_method", ""))) == "llm_title_search":
        return "low"
    if link.get("resolution_method") == "single_candidate_after_requery" and conf == "high":
        return "medium"
    return conf


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


# ── BibTeX helper for the replication paper ──────────────────────────────────

def _build_bibtex_r(row: "pd.Series | dict") -> str:
    """Build a BibTeX entry for the replication paper from its row metadata.

    Uses the r-side columns already present in filtered.csv (doi_r, title_r,
    authors_r, year_r, journal_r, url_r). Volume/issue/pages are not tracked
    at Stage 1, so they are omitted here.
    """
    authors_raw = str(row.get("authors_r") or "").strip()
    authors = [a.strip() for a in authors_raw.split(";") if a.strip()]
    return build_bibtex(
        authors     = authors,
        year        = str(row.get("year_r")    or ""),
        title       = str(row.get("title_r")   or ""),
        journal     = str(row.get("journal_r") or ""),
        doi         = str(row.get("doi_r")     or ""),
        url         = str(row.get("url_r")     or ""),
    )


# ── Row merge helpers ─────────────────────────────────────────────────────────

def _record_type(filter_row: pd.Series, screen: "dict | None") -> str:
    """The paper type, which decides the outcome vocabulary: a reproduction is coded
    on the computation/robustness grid, not success/failure (see shared/schema.py).

    The front-door screen decides it — that is the call that read the abstract and
    said what the paper is. Stage 2's filter_status stands in when the screen
    proceeded without a qualifying vote, and is the whole answer on a --no-llm run
    where no screen ran at all.

    When neither has decided, the field is left EMPTY rather than defaulted: a paper
    nobody has classified is not a replication because replication is the commoner
    answer. Such a row still resolves an original and is still outcome-coded (on the
    replication vocabulary, the more general of the two grids), but it carries no
    type into the CSV and csv_to_db leaves it for a human.
    """
    if screen and screen.get("record_type"):
        return str(screen["record_type"])
    status = str(filter_row.get("filter_status", "")).strip().lower()
    if status in {"replication", "reproduction"}:
        return status
    return "" if screen else "replication"


def _screen_categories(screen: "dict | None") -> str:
    return "|".join((screen or {}).get("categories", []) or [])


def _base_row(filter_row: pd.Series, match_type: str, match_conf: str,
              classify_model: str, outcome: dict,
              screen: "dict | None" = None) -> dict:
    """The fields every written row carries, whichever producer built it.

    The three producers differ only in how they name the original and the link; the
    classification, the replication-side bibtex, the outcome block, the record type
    and the rank/count defaults are the same row for all of them.
    """
    row = filter_row.to_dict()
    # propagate study_r → title_r if title_r is absent (old seeded data uses study_r)
    if not row.get("title_r"):
        row["title_r"] = row.get("study_r", "")
    row.update({
        # The legacy seeded columns used study_r for a TITLE. Every producer sets the
        # real value explicitly below, so blanking it here is what stops a title
        # surviving into the study identifier.
        "study_r": "",
        "original_match_type":       match_type,
        "original_match_confidence": match_conf,
        "classify_llm_model":        classify_model,
        "bibtex_ref_r":      _build_bibtex_r(filter_row),
        "outcome":             outcome.get("outcome",             "cannot_be_determined"),
        "outcome_phrase":      outcome.get("outcome_phrase",      ""),
        "outcome_confidence":  outcome.get("outcome_confidence",  "low"),
        "out_quote_source":    outcome.get("out_quote_source",    ""),
        "outcome_reasoning":   outcome.get("outcome_reasoning",   ""),
        "outcome_llm_model":   str(outcome.get("llm_model",       "") or ""),
        **{col: outcome.get(col, "") for col in _OUTCOME_AXIS_COLS},
        "type":              _record_type(filter_row, screen),
        "screen_categories": _screen_categories(screen),
        # Blank unless a producer with a link fills them in: a row written without a
        # ladder result (front door, api_error) acquired and parsed nothing.
        "pdf_source":    "",
        "parse_method":  "",
        "original_rank": 1,
        "n_originals":   1,
    })
    return row


def _provenance(link: dict) -> dict:
    """The full-text provenance columns for a row built from *link*.

    Which acquisition tier supplied the document and which of the six parsers won —
    the two facts a reviewer needs to judge an `llm_fulltext` link and the two the
    2026-07 audit had to reconstruct from cache timestamps. Both blank when the
    ladder never got a document: "none" is the acquisition waterfall's word for a
    failed attempt, and on the row the column names a document or says nothing.
    """
    source = str(link.get("pdf_source", "") or "")
    return {
        "pdf_source":   "" if source in {"none", "unknown"} else source,
        "parse_method": str(link.get("parse_method", "") or ""),
    }


def _merge_row(filter_row: pd.Series, link: dict, outcome: dict,
               match_type: str, match_conf: str,
               rank: int, n: int, classify_model: str = "",
               screen: "dict | None" = None) -> dict:
    row = _base_row(filter_row, match_type, match_conf, classify_model, outcome, screen)
    doi_r_clean = clean_doi(str(filter_row.get("doi_r", "")))
    doi_o_clean = clean_doi(link.get("resolved_doi_o", "") or "")
    row.update({
        "pair_id":         make_pair_id(doi_r_clean, doi_o_clean),
        "doi_o":           doi_o_clean,
        "title_o":         str(link.get("resolved_title_o", "") or ""),
        "year_o":          str(link.get("resolved_year_o",  "") or ""),
        **dict(zip(("ref_o", "authors_o", "bibtex_ref_o"), _build_ref_o(
            doi_o_clean,
            str(link.get("resolved_author_o", "") or ""),
            str(link.get("resolved_year_o",   "") or ""),
            str(link.get("resolved_title_o",  "") or ""),
        ))),
        "study_o":         str(link.get("resolved_study_o", "") or ""),
        # Only on a link that actually resolved: study_r says which of this paper's
        # studies re-tests THAT original, so on a row with no original it would be a
        # study number attached to nothing. The ladder can carry a target list past an
        # unresolved exit, and its study numbers belong to those targets' rows.
        "study_r":         (str(link.get("resolved_study_r", "") or "")
                            if link.get("resolved") else ""),
        "link_method":     _map_method(link.get("resolution_method", "target_pending")),
        "link_evidence":   str(link.get("llm_evidence",     "") or ""),
        "link_confidence": _link_confidence(link),
        "link_llm_model":  str(link.get("llm_model",        "") or ""),
        **_provenance(link),
        "original_rank": rank,
        "n_originals":   n,
    })
    return row


def _merge_multi_row(filter_row: pd.Series, orig: dict, outcome: dict,
                     match_type: str, match_conf: str, n: int,
                     link_llm_model: str = "",
                     link_method: str = "llm_cited_candidates",
                     classify_model: str = "",
                     screen: "dict | None" = None) -> dict:
    row = _base_row(filter_row, match_type, match_conf, classify_model, outcome, screen)
    # Binary on the write side: "medium" was a value only the retired multi-original
    # writer produced. It stays legal in stored data and on every read path.
    conf_str = orig.get("confidence", "low")
    if conf_str not in {"high", "low"}:
        conf_str = "low"
    doi_r_clean  = clean_doi(str(filter_row.get("doi_r", "")))
    doi_o_clean  = clean_doi(orig.get("doi", "") or "")
    title_o      = str(orig.get("title", "") or "")
    row.update({
        "pair_id":         make_pair_id(doi_r_clean, doi_o_clean,
                                        str(orig.get("openalex_id", "") or ""),
                                        title_o),
        "doi_o":           doi_o_clean,
        "title_o":         title_o,
        "year_o":          str(orig.get("year",         "") or ""),
        **dict(zip(("ref_o", "authors_o", "bibtex_ref_o"), _build_ref_o(
            doi_o_clean,
            str(orig.get("first_author", "") or ""),
            str(orig.get("year",         "") or ""),
            title_o,
        ))),
        "study_o":         str(orig.get("study_number", "") or ""),
        "study_r":         str(orig.get("study_r",       "") or ""),
        "link_method":     link_method,
        "link_evidence":   str(orig.get("evidence",     "") or ""),
        "link_confidence": conf_str,
        "link_llm_model":  link_llm_model,
        "original_rank": orig.get("rank", 1),
        "n_originals":   n,
    })
    return row


def _empty_row(filter_row: pd.Series, match_type: str, match_conf: str,
               link_method: str = "api_error", classify_model: str = "",
               screen: "dict | None" = None) -> dict:
    """A row with no link. It still carries whatever the front door already decided:
    a paper the screen classified and categorised does not stop being categorised
    because the ladder found no original, and a reviewer reading the pending row
    needs the same evidence as one reading a resolved one."""
    doi_r_clean = clean_doi(str(filter_row.get("doi_r", "")))
    outcome = "api_error" if link_method == "api_error" else "pending"
    row = _base_row(filter_row, match_type, match_conf, classify_model,
                    {"outcome": outcome}, screen)
    row.update({
        "pair_id": make_pair_id(doi_r_clean, ""),
        "doi_o": "", "title_o": "", "year_o": "", "authors_o": "", "ref_o": "",
        "bibtex_ref_o": "",
        "link_method": link_method, "link_evidence": "", "link_confidence": "low",
        "link_llm_model": "",
    })
    if screen is None:
        # Nobody classified this row, and the replication default _record_type falls
        # back to would be a guess.
        row["type"] = ""
    return row


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has_document(doi_r: str, link: dict) -> bool:
    """True when there is something for the parsers to read.

    Every stage before Stage 5 returns with pdf={}, so pdf_source is "none" and no
    document was acquired. Parsing anyway wrote a cache of six empty results, and
    because the writers early-return on an existing file that empty cache then stood
    in for the real one on any later run that DID get the PDF (audit B4). A document
    cached by an earlier run still counts — that is what --recalibrate-outcomes reads.
    """
    if bool(link.get("pdf_ok")):
        return True
    if str(link.get("pdf_source", "none") or "none") not in {"", "none"}:
        return True
    key = cache_key(doi_r)
    return ((PDF_CACHE_DIR / f"{key}.pdf").exists()
            or (OA_XML_CACHE_DIR / f"oa_xml_{key}.json").exists())


def _read_parse_cache(doi_r: str) -> "dict | None":
    """Return the cached parse_all results for doi_r, or None on a miss.

    An all-empty cache counts as a miss: it is what a PDF-less run wrote, and reading
    it back would pin the paper to abstract-only coding forever (audit B4).
    """
    cache_file = PARSE_CACHE_DIR / f"parse_{cache_key(doi_r)}.json"
    if not cache_file.exists():
        return None
    try:
        with cache_file.open(encoding="utf-8") as fh:
            results = json.load(fh)
    except Exception:
        return None
    return None if parse_result_is_empty(results) else results


def _save_parse_cache(doi_r: str) -> None:
    """Run all PDF parsers for doi_r and cache results to PARSE_CACHE_DIR."""
    key      = cache_key(doi_r)
    out_file = PARSE_CACHE_DIR / f"parse_{key}.json"
    if _read_parse_cache(doi_r) is not None:
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
    if parse_result_is_empty(results):
        log.debug("[%s] parse produced no text — not caching", doi_r)
        return
    try:
        with out_file.open("w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.debug("[%s] _save_parse_cache write failed: %s", doi_r, exc)


# --resolved-only drops these, and they are never outcome-coded: there is no link.
_NO_LINK_METHODS = {"target_pending", "api_error", "no_original_found"}


def _outcome_without_coding(link_method: str, link: dict) -> "dict | None":
    """The outcome for a row that must not be outcome-coded, or None to code it.

    This is the single gate on outcome coding. Outcome extraction is the last LLM
    call and the only one that used to run on every row, including the 8,000-character
    full-text escalation — which fires precisely when the abstract was uninformative,
    the same rows that failed to resolve. A row whose link_method is not in
    RESOLVED_LINK_METHODS has no confirmed original to code an outcome against: it is
    either quarantined by sanity_check (not_a_replication, screen_disagreement,
    llm_title_search) or carries no link at all (target_pending, api_error,
    no_original_found). Coding it states a result for a comparison that may never
    have been made, and makes the row read as settled.
    """
    if link_method in RESOLVED_LINK_METHODS:
        return None

    # outcome_llm_model names the model whose verdict IS the outcome. Only
    # not_a_replication has one: the rest are placeholders for a verdict never made,
    # and stamping the link stage's model on them would read as an outcome coding.
    def _skip(outcome: str, confidence: str, source: str, reasoning: str,
              llm_model: str = "") -> dict:
        return {"outcome": outcome, "outcome_phrase": "",
                "outcome_confidence": confidence, "out_quote_source": source,
                "outcome_reasoning": reasoning,
                "llm_model": llm_model}

    if link_method == "api_error":
        return _skip("api_error", "low", "", str(link.get("llm_error", "") or ""))
    if link_method == "not_a_replication":
        # The classification screen already read the abstract and settled this; the
        # verdict IS the outcome, and sanity_check routes the row on it.
        return _skip("not_a_replication", "high", "abstract",
                     str(link.get("llm_reasoning", "") or ""),
                     str(link.get("llm_model", "") or ""))
    if link_method == "llm_title_search":
        return _skip("cannot_be_determined", "low", "",
                     "provisional link from a title search — outcome not coded "
                     "until the target is confirmed")
    if link_method == "screen_disagreement":
        return _skip("pending", "low", "",
                     "outcome not coded: the two classifiers disagreed on whether "
                     "this is a replication — set aside for review")
    return _skip("pending", "low", "",
                 f"outcome not coded: no resolved original link ({link_method})")


def _apply_outcome(row: dict, outcome: dict) -> dict:
    """Write the outcome fields onto an already-merged result row.

    The outcome step is also the first look the pipeline gets at the methods, so it
    can correct `type`: when the full-text pass reports the other vocabulary the row
    is re-coded under it, and the record type it was actually coded in is the one the
    row must carry.
    """
    row.update({
        "outcome":            outcome.get("outcome",            "cannot_be_determined"),
        "outcome_phrase":     outcome.get("outcome_phrase",     ""),
        "outcome_confidence": outcome.get("outcome_confidence", "low"),
        "out_quote_source":   outcome.get("out_quote_source",   ""),
        "outcome_reasoning":  outcome.get("outcome_reasoning",  ""),
        "outcome_llm_model":  str(outcome.get("llm_model",      "") or ""),
        **{col: outcome.get(col, "") for col in _OUTCOME_AXIS_COLS},
    })
    if outcome.get("record_type"):
        row["type"] = str(outcome["record_type"])
    return row


def _get_outcome(doi_r: str, row: pd.Series, link: dict, no_llm: bool = False,
                 screen: "dict | None" = None, *, original: "dict | None" = None,
                 multi_original: bool = False) -> dict:
    """The outcome for one (replication, original) pair.

    *original* names the original this call is about — the per-target adapter passes
    one entry per row, where the link carries a whole list; without it the link's own
    resolved_* fields are the original, as on the single-link path. *multi_original*
    tells the outcome prompt that the other originals are coded on their own rows.
    """
    abstract_r = str(row.get("abstract_r", ""))
    title_r    = str(row.get("title_r",    ""))

    # Prefer the best-scoring parse method from the parse cache so the outcome LLM
    # receives whichever parser extracted the richest text, not always GROBID —
    # narrowed to the discussion/conclusion, which is where FLoRA's rule says the
    # outcome is stated when the abstract does not state it.
    fulltext, provenance = _best_fulltext_from_cache(doi_r)
    if fulltext:
        fulltext = (f"[{_PROVENANCE_LABEL[provenance]}]\n\n{fulltext}")
    else:
        # Fallback: GROBID sections that run_for_doi already extracted. The intro is
        # in here for want of anything better; it is the section that most often
        # discusses OTHER studies' replication failures, and the prompt says so.
        fulltext = " ".join(filter(None, (
            str(link.get(key) or "") for key in
            ("grobid_abstract", "grobid_intro", "grobid_methods"))))
        if fulltext:
            fulltext = f"[{_PROVENANCE_LABEL['sections']}]\n\n{fulltext}"

    orig = original or {"title":        link.get("resolved_title_o"),
                        "first_author": link.get("resolved_author_o"),
                        "year":         link.get("resolved_year_o")}
    return extract_outcome(
        doi_r, abstract_r, fulltext, title_r, no_llm=no_llm,
        original_title=str(orig.get("title") or ""),
        original_authors=str(orig.get("first_author") or ""),
        original_year=str(orig.get("year") or ""),
        record_type=_record_type(row, screen),
        multi_original=multi_original,
    )


# Leaves room for the SOURCE label below under code_outcome._FULLTEXT_CAP, which
# truncates from the front — without the headroom the label would push the last
# few hundred characters of the discussion past the cap.
_OUTCOME_TEXT_CHARS = 7600

# Told to the model alongside the text, so it never attributes a quote to a
# section it was not shown.
_PROVENANCE_LABEL = {
    "discussion": "SOURCE: discussion / conclusion section of the paper",
    "tail":       "SOURCE: closing pages of the paper, before the reference list",
    "sections":   "SOURCE: abstract, introduction and methods only — the closing "
                  "sections could not be parsed. Statements about replication "
                  "failures in an introduction usually concern OTHER studies",
}


def _best_fulltext_from_cache(doi_r: str) -> tuple[str, str]:
    """The outcome-bearing text for doi_r, as (text, provenance).

    Reads the parse cache, and takes the raw text of the highest-scoring method
    that actually has raw text — the top-scoring method overall can be GROBID,
    which returns sections and no raw text at all, and falling straight back to
    its abstract + intro discarded a full parse another method had produced.

    `outcome_text()` then narrows that to the discussion/conclusion. Returns
    ("", "none") on a cache miss or an all-empty cache.
    """
    results = _read_parse_cache(doi_r)
    if not results:
        return "", "none"
    ranked = sorted((r for r in results.values() if isinstance(r, dict)),
                    key=score_parse_result, reverse=True)
    for result in ranked:
        raw = str(result.get("raw_text", "") or "").strip()
        if raw:
            return outcome_text(raw, max_chars=_OUTCOME_TEXT_CHARS)

    best = best_parse_result(results)
    if not best:
        return "", "none"
    joined = " ".join(filter(None, [
        str(best.get("abstract", "") or ""),
        str(best.get("intro",    "") or ""),
    ])).strip()
    return (joined, "sections") if joined else ("", "none")


# Verdicts that say something about the replication result. cannot_be_determined,
# uninformative, descriptive and not_a_replication do not, so they never outvote a
# study that reached a verdict when several studies are aggregated onto one row.
_SUBSTANTIVE_OUTCOMES = {"success", "failure", "mixed",
                         "statistically_successful_but_flawed"}


def _aggregate_outcomes(outcomes: list[str]) -> str:
    """One outcome for several studies replicated from the SAME original paper.

    A safety net rather than a normal path: the target prompt already returns one
    entry per original PAPER with the study numbers joined, and outcome coding runs
    AFTER the collapse and once per original — so entries reaching here carrying two
    different verdicts is a shape only a future producer could create. The rule stays
    implemented because FLoRA's is a rule about the database, not about this pipeline.

    FLoRA aggregates: "A replication study can have multiple studies but their results
    are aggregated … conflicting results, which we consider as mixed" (FLoRA FAQ,
    "What level is the database?"). So two substantive verdicts that disagree become
    mixed, one substantive verdict carries the row whatever the silent studies say,
    and a row with no substantive verdict at all falls back to the most informative
    non-verdict present.
    """
    substantive = [o for o in outcomes if o in _SUBSTANTIVE_OUTCOMES]
    if len(set(substantive)) > 1:
        return "mixed"
    if substantive:
        return substantive[0]
    for fallback in ("uninformative", "descriptive", "not_a_replication"):
        if fallback in outcomes:
            return fallback
    return "cannot_be_determined"


def _target_entry(target: dict, doi_r: str) -> "dict | None":
    """One confirmed target as the entry shape _collapse_same_paper_originals() and
    _merge_multi_row() read.

    None when the model could see a target but could not match it to a keyed record:
    there is no published record to write a row about, and the shortfall is reported
    on the rows that were written (see _per_target_rows).

    The DOI comes from the mapped record, never from the model. A reference parsed out
    of a PDF carries no DOI, so it is searched for once — the resulting link is
    provisional, at roughly 50% precision, exactly as on the single-link path.

    Such a reference also carries the raw citation line as its title, so the title is
    cleaned before anything is searched for or written, and a title that is still a
    citation fragment — like a paper with no DOI at all — makes the entry a low-
    confidence one: the row names an original nobody can look up.
    """
    record = target.get("record")
    if not (target.get("match_certain") and record):
        return None

    raw_title = str(record.get("title") or "")
    cleaned   = clean_citation_title(raw_title)
    # An unusable cleaning is no improvement: keep what was parsed so a reviewer sees
    # it, and let the guard reject the row on it.
    title = cleaned if usable_title(cleaned) else raw_title

    doi = clean_doi(str(record.get("doi") or ""))
    provisional = False
    if not doi and usable_title(title):
        from shared.doi_verify import resolve_doi_by_metadata
        hit = resolve_doi_by_metadata(title, record.get("first_author", ""),
                                      record.get("year"), exclude_doi=doi_r)
        if hit:
            doi = clean_doi(str(hit.get("doi", "") or ""))
            provisional = bool(doi)

    return {
        "rank":         0,     # renumbered over the written rows, after every drop
        "doi":          doi,
        "title":        title,
        "year":         record.get("year"),
        "first_author": str(record.get("first_author") or ""),
        "openalex_id":  str(record.get("openalex_id") or ""),
        "study_number": _clean_study_numbers(target.get("study_numbers", "")),
        "study_r":      _clean_study_numbers(target.get("replication_study_numbers", "")),
        "evidence":     str(target.get("evidence_quote") or ""),
        # match_certain is the acceptance gate, but confidence is about the RECORD the
        # key resolved to: with no DOI, or with a title that is a citation fragment,
        # there is nothing a validator can check and the row is not a confident one.
        "confidence":   "high" if doi and usable_title(title) else "low",
        "provisional":  provisional,
    }


def _collapse_same_paper_originals(originals: list[dict]) -> list[dict]:
    """Merge targeted studies that belong to the SAME original paper into one entry.

    FLoRA's coding level is one row per pair of *references*: several studies from one
    original paper stay one row, with their numbers in `study_o` ("1, 2"); several
    original papers are several rows. The LLM is asked for one entry per study, so the
    grouping happens here.

    It also removes a duplicate-key bug: `pair_id` is md5(doi_r|doi_o), so two entries
    sharing an original DOI produced two rows with the same pair_id — the identifier
    every other system joins on.

    Entries are grouped by resolved DOI; those without one fall back to their
    normalised title, so an unresolved paper's studies still group together. Order is
    preserved and ranks are renumbered 1..N over the groups.
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for orig in originals:
        doi = clean_doi(str(orig.get("doi", "") or ""))
        if doi:
            key = doi
        else:
            # Title alone collapses two distinct originals that share a generic
            # title ("Study 1"), so year and first author join the fallback key.
            title = " ".join(str(orig.get("title", "") or "").lower().split())
            year = str(orig.get("year", "") or "").strip()
            author = str(orig.get("first_author", "") or "").lower().strip()
            key = f"title:{title}|{year}|{author}" if title else ""
        if not key.strip():
            key = f"unkeyed:{len(order)}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(orig)

    collapsed = []
    for rank, key in enumerate(order, 1):
        members = groups[key]
        merged = dict(members[0])
        merged["rank"] = rank
        if len(members) > 1:
            numbers = [n for n in (str(m.get("study_number", "") or "") for m in members) if n]
            # Only meaningful when every member said which study it was: a partial list
            # would claim the replication targeted studies it never mentioned.
            merged["study_number"] = (", ".join(dict.fromkeys(numbers))
                                      if len(numbers) == len(members) else "")
            # study_r is a union, not a claim about every member: it says which studies
            # of THIS paper re-test the merged original, and a member that named none
            # does not make the ones that did untrue.
            merged["study_r"] = ", ".join(dict.fromkeys(
                part for m in members
                for part in str(m.get("study_r", "") or "").split(", ") if part))
            merged["outcome"] = _aggregate_outcomes(
                [str(m.get("outcome", "") or "") for m in members])
            merged["evidence"] = " ".join(
                dict.fromkeys(str(m.get("evidence", "") or "").strip() for m in members
                              if str(m.get("evidence", "") or "").strip()))
            merged["outcome_evidence"] = " ".join(
                dict.fromkeys(str(m.get("outcome_evidence", "") or "").strip() for m in members
                              if str(m.get("outcome_evidence", "") or "").strip()))
            # The row is only as good as its weakest member — it now stands for all of them.
            merged["confidence"] = min(
                (str(m.get("confidence", "low") or "low") for m in members),
                key=lambda c: {"high": 2, "medium": 1}.get(c, 0))
        collapsed.append(merged)
    return collapsed


# ── FLoRA entry sheet skip helper ────────────────────────────────────────────



# ── Main orchestrator ─────────────────────────────────────────────────────────

def _extract_row_key(row: "dict | pd.Series") -> str:
    """Resume key for a row — the shared doi → openalex_id → url → title chain.

    Prevents multiple no-DOI rows (e.g. from I4R / Replication Network) from
    collapsing to the same empty-string key and being skipped as already-processed.
    """
    return primary_key(row)


# Verdicts the classification screen reaches on its own, without ever seeing full text.
# They are the rows a changed voter pair or prompt would decide differently, so
# --rescreen reopens exactly these and nothing else.
SCREEN_SET_ASIDE_METHODS = {"not_a_replication", "screen_disagreement"}

# Where sanity_check parks the screen's own verdicts (see extract/sanity_check.py).
# A resumed run must read them too, or every screened-out paper is screened again.
SCREEN_SET_ASIDE_FILES = ("not_a_replication.csv", "screen_disagreement.csv")


def _screen_set_aside_keys(data_dir) -> set[str]:
    """Row keys of papers the screen already settled and sanity_check moved out."""
    keys: set[str] = set()
    for fname in SCREEN_SET_ASIDE_FILES:
        path = data_dir / fname
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        if df.empty:
            continue
        keys |= {k for k in df.apply(_extract_row_key, axis=1) if k}
    return keys


def _load_extracted_rows(out_path, rescreen: bool = False) -> tuple[dict[str, list[dict]], set[str]]:
    """Partition extracted.csv rows by resolution status for --resume mode.

    Also reads the screen's set-aside CSVs, which hold papers sanity_check moved out
    of extracted.csv; without them a resume re-screens every paper the screen already
    settled.

    False_positive rows in the existing file are dropped — they should never have
    been written to extracted.csv and will not be re-written on resume.

    rescreen — treat the screen's own set-aside verdicts (not_a_replication,
        screen_disagreement) as unresolved so the current voter pair decides them
        again. Off by default: a resumed run should reproduce its predecessor's
        decisions, not silently revisit them.

    Returns:
        resolved — row_key → list of rows that are fully resolved (link_method != target_pending)
        pending  — set of row_key where at least one row has link_method == target_pending
    """
    # An empty list of rows: the paper is settled, so it is skipped, but its row
    # stays in the set-aside CSV rather than being written back to extracted.csv.
    set_aside_keys = set() if rescreen else _screen_set_aside_keys(Path(out_path).parent)
    if not out_path.exists():
        return {k: [] for k in set_aside_keys}, set()
    df = pd.read_csv(out_path, dtype=str, encoding="utf-8-sig").fillna("")
    # Drop false_positive rows that were incorrectly written in a prior run.
    df = df[df["filter_status"] != "false_positive"]
    if rescreen and not df.empty:
        # Drop the paper, not just the row: the screen decides a whole paper, so a
        # multi-original paper with one set-aside row is re-screened as a unit.
        set_aside = (df["link_method"].isin(SCREEN_SET_ASIDE_METHODS)
                     | df["outcome"].isin(SCREEN_SET_ASIDE_METHODS))
        keys = df.apply(_extract_row_key, axis=1)
        df = df[~keys.isin(set(keys[set_aside]))]
    resolved: dict[str, list[dict]] = {}
    pending: set[str] = set()
    for row_key, group in df.groupby(df.apply(_extract_row_key, axis=1), sort=False):
        if not row_key:
            continue  # rows with no DOI, no OA ID, and no title — skip; can't dedup
        rows = group.to_dict("records")
        # Stale artifact: LLM method written before the no_original_found fix — these
        # have link_method=llm_fulltext/llm_cited_candidates but an empty doi_o. Re-process them.
        rows = [
            r for r in rows
            if not (r.get("link_method") in {"llm_fulltext", "llm_cited_candidates"}
                    and not r.get("doi_o", "").strip())
        ]
        if not rows:
            pending.add(row_key)
            continue
        is_pending = any(r.get("link_method") == "target_pending" for r in rows)
        if is_pending:
            pending.add(row_key)
        else:
            resolved[row_key] = rows
    for key in set_aside_keys - pending:
        resolved.setdefault(key, [])
    return resolved, pending


def _norm_title(t: str) -> str:
    """Lowercase, strip punctuation/whitespace — for comparing two titles for identity."""
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()


def _guard_original_link(row: dict) -> dict:
    """Reject self-links and recover a missing doi_o before the row is written.

    A validator needs a real original to compare against, so:

    1. A paper is never its own original. Matching doi_r/doi_o, or an identical
       title, means the linker looped back on itself — always rejected, however
       confident the LLM was.
    2. doi_o empty but title_o present → actively try to recover the DOI via
       CrossRef then OpenAlex title search before giving up.
    3. Still no DOI, but the title is substantive and distinct → KEEP the row and
       set doi_o_verification="no_doi". Plenty of genuine originals (old papers,
       book chapters, working papers) have no registered DOI; dropping them would
       discard valid links. Marked explicitly so it is never mistaken for verified.
       If the title search did return a work that OpenAlex indexes without a DOI,
       its work id becomes oa_work_id_o — that id is the row's only identity, and
       without it audit_extracted blocks the row.
    4. No DOI and no usable title → target_pending; there is nothing to validate.
       "Usable" is `usable_title()`: long enough, and not a fragment of a citation
       string ("[3] M. Moieni, M.R"), which names no paper however long it is.
    """
    # not_a_replication has no original by design — the reference screen concluded
    # the paper never replicated anything. Asking it for a doi_o would rewrite the
    # row to target_pending and --resolved-only would then discard the finding.
    if row.get("link_method") in {"target_pending", "api_error", "no_original_found",
                                  "not_a_replication", "screen_disagreement"}:
        return row

    doi_r = clean_doi(str(row.get("doi_r", "") or ""))
    doi_o = clean_doi(str(row.get("doi_o", "") or ""))
    title_r, title_o = str(row.get("title_r", "") or ""), str(row.get("title_o", "") or "")

    def _reject(reason: str) -> dict:
        log.info("[%s] original-link rejected (%s) — writing target_pending", doi_r, reason)
        row["link_method"] = "target_pending"
        # The original every one of these fields described is gone, so they describe
        # nothing — a rejected row carries the same empty original as _empty_row.
        # study_r goes with them: "which of our studies re-tests it" has no referent
        # once there is no "it".
        for column in ("doi_o", "study_o", "study_r", "title_o", "year_o", "authors_o",
                       "ref_o", "bibtex_ref_o", "oa_work_id_o"):
            row[column] = ""
        row["doi_o_verification"] = "skipped"
        row["link_confidence"] = "low"
        prior = str(row.get("link_evidence", "") or "")
        row["link_evidence"] = (f"{prior} | rejected: {reason}" if prior else f"rejected: {reason}")
        # The multi-original path merges the outcome before the guard runs, so a
        # demoted row would otherwise keep a coded outcome on an unresolved link.
        return _apply_outcome(row, _outcome_without_coding("target_pending", row) or {})

    work_id_r = bare_work_id(str(row.get("oa_work_id_r", "")
                                 or row.get("openalex_id_r", "") or ""))

    def _is_self(cand_doi: str, cand_work_id: str = "") -> str:
        if cand_doi and doi_r and cand_doi == doi_r:
            return "resolved original is the replication itself (same DOI)"
        if cand_work_id and work_id_r and cand_work_id == work_id_r:
            return "resolved original is the replication itself (same OpenAlex work)"
        if title_o and title_r and _norm_title(title_o) == _norm_title(title_r):
            return "resolved original has the same title as the replication"
        return ""

    reason = _is_self(doi_o)
    if reason:
        return _reject(reason)

    # 2. best-effort DOI recovery from the title
    meta: Optional[dict] = None
    if not doi_o and usable_title(title_o):
        year_o = str(row.get("year_o", "") or "")
        try:
            meta = (_search_crossref_by_title(title_o, year_o)
                    or _search_openalex_by_title(title_o, year_o))
        except Exception as exc:
            meta = None
            log.debug("[%s] doi_o title-recovery failed: %s", doi_r, exc)
        found = clean_doi(str((meta or {}).get("doi", "") or ""))
        if found:
            # The work id has to be compared too: OpenAlex can return the replication's
            # own work under a DOI that differs textually from doi_r (alternate or
            # canonical form), which the DOI comparison alone would wave through.
            reason = _is_self(found, bare_work_id(str((meta or {}).get("openalex_id", "") or "")))
            if reason:
                return _reject(f"recovered DOI is a self-link — {reason}")
            log.info("[%s] recovered doi_o=%s from title search", doi_r, found)
            row["doi_o"] = found
            row["pair_id"] = make_pair_id(doi_r, found)
            return row

    # 3/4. no DOI: keep only if the title is a usable, distinct original
    if not doi_o:
        if not usable_title(title_o):
            return _reject("no doi_o and no usable title_o")
        row["doi_o_verification"] = "no_doi"
        work_id_o = bare_work_id(str((meta or {}).get("openalex_id", "") or ""))
        if work_id_o:
            reason = _is_self("", work_id_o)
            if reason:
                return _reject(f"title-search hit is a self-link — {reason}")
            log.info("[%s] DOI-less original identified as %s", doi_r, work_id_o)
            # pair_id deliberately NOT recomputed: this is the single-original path,
            # where the oa: fallback buys no collision protection (one original per
            # row, and different replications already differ by doi_r) but would
            # re-key the existing DOI-less rows the validation DB holds under
            # md5("doi_r|") — a duplicate import. Only multi-original needs it.
            row["oa_work_id_o"] = work_id_o
        return row

    return row


def _verify_row(row: dict) -> dict:
    """Verify/correct doi_o in a finished result row before it is written.

    Keeps pair_id and ref_o consistent when the DOI changes, downgrades
    link_confidence on mismatch, and appends the verification note to
    link_evidence.
    """
    if row.get("link_method") in {"target_pending", "api_error"}:
        row["doi_o_verification"] = "skipped"
        return row

    old_doi = str(row.get("doi_o", "") or "")
    v = verify_and_correct(old_doi, str(row.get("title_o", "") or ""),
                           str(row.get("authors_o", "") or ""), row.get("year_o", ""),
                           exclude_doi=clean_doi(str(row.get("doi_r", ""))),
                           exclude_title=str(row.get("title_r", "")
                                             or row.get("study_r", "") or ""))
    if not keeps_no_doi(v["doi_o_verification"], str(row.get("doi_o_verification", "") or ""),
                        str(row.get("oa_work_id_o", "") or "")):
        row["doi_o_verification"] = v["doi_o_verification"]
    if v["doi_o"] != old_doi:
        row["doi_o"]   = v["doi_o"]
        row["pair_id"] = make_pair_id(clean_doi(str(row.get("doi_r", ""))), v["doi_o"])
        if v["doi_o"]:
            # The old work id was resolved from the old DOI (or from a title search
            # that produced it) and may describe a different work; _fill_work_ids
            # refills it from the corrected DOI, but only if the column is blank.
            row["oa_work_id_o"] = ""
        new_ref, new_authors, new_bibtex = _build_ref_o(v["doi_o"],
                                            str(row.get("authors_o", "") or ""),
                                            str(row.get("year_o",    "") or ""),
                                            str(row.get("title_o",   "") or ""))
        row["ref_o"]        = new_ref
        row["authors_o"]    = new_authors
        row["bibtex_ref_o"] = new_bibtex
    if v["doi_o_verification"] == "mismatch":
        # The DOI is registered but demonstrably describes a DIFFERENT paper, and
        # verify_and_correct found no better candidate. Keeping it sends validators
        # to the wrong original and yields a confidently wrong url_o, which is worse
        # than no link at all. Drop the DOI and everything derived from it; the
        # title/author/year claim is retained so the row can still be reviewed.
        row["doi_o"] = ""
        row["bibtex_ref_o"] = ""
        # Any oa_work_id_o on a row that had a doi_o was resolved from that DOI, which
        # has just been shown to describe a different paper — so it goes too, and the
        # pair_id keys on the DOI pair alone rather than on a discredited work id.
        row["oa_work_id_o"] = ""
        row["pair_id"] = make_pair_id(clean_doi(str(row.get("doi_r", ""))), "")
        row["link_confidence"] = "low"
    if v["evidence_note"]:
        existing = str(row.get("link_evidence", "") or "")
        # resume mode re-verifies carried-forward rows — don't append the same note twice
        if v["evidence_note"] not in existing:
            row["link_evidence"] = f"{existing} | {v['evidence_note']}".strip(" |")
    return row


def _fill_work_ids(row: dict) -> dict:
    """Populate oa_work_id_r / oa_work_id_o (bare OpenAlex W-ids) on a finished row.

    The r-side is free — Stage 1 already carries openalex_id_r as a URL — so the API is
    only touched for rows that arrived without one, and for the o-side. Must run *after*
    _verify_row, which can replace doi_o and would otherwise leave a stale o-side id.
    """
    if not row.get("oa_work_id_r"):
        row["oa_work_id_r"] = bare_work_id(str(row.get("openalex_id_r", "") or ""))

    for col, doi_col in (("oa_work_id_r", "doi_r"), ("oa_work_id_o", "doi_o")):
        if row.get(col):
            continue
        doi = clean_doi(str(row.get(doi_col, "") or ""))
        row[col] = bare_work_id((_oa_by_doi(doi) or {}).get("openalex_id", "")) if doi else ""
    return row


def _append_row(out_path, result_row: dict, first: bool) -> None:
    """Write one result row to the output CSV immediately after processing.

    first=True  → open with mode='w' (creates / truncates the file) and write header.
    first=False → open with mode='a' (append) and skip header.
    """
    result_row = _fill_work_ids(_verify_row(result_row))

    # Sanitize row data to handle problematic characters
    for key, val in result_row.items():
        if isinstance(val, str):
            # Replace control characters and problematic whitespace
            # but preserve newlines within fields (they'll be quoted)
            result_row[key] = val.replace('\x00', '').replace('\r', ' ')

    row_df = pd.DataFrame([result_row])
    for col in EXTRACTED_COLS:
        if col not in row_df.columns:
            row_df[col] = ""

    try:
        # Hold the shared CSV lock per row so promote_test's full-file rewrite cannot
        # clobber this append (and vice versa) when both run against extracted.csv (#49).
        with csv_lock(out_path):
            row_df[EXTRACTED_COLS].to_csv(
                out_path, mode="w" if first else "a",
                index=False, encoding="utf-8-sig", header=first,
                quoting=1,  # csv.QUOTE_ALL to quote fields with special characters
                quotechar='"',
            )
    except Exception as e:
        log.error("Failed to write row for DOI %s: %s",
                  result_row.get("doi_r", "unknown"), str(e))
        raise


# Read at call time, so a run picks up the keys config actually loaded.
_SCREEN_KEYS = {
    "GEMINI_API_KEY":     lambda: GEMINI_API_KEYS[0] if GEMINI_API_KEYS else "",
    "OPENAI_API_KEY":     lambda: OPENAI_API_KEY,
    "OPENROUTER_API_KEY": lambda: OPENROUTER_API_KEY,
}


def _check_screen_providers(no_llm: bool) -> None:
    """Refuse to start when the front-door screen cannot get its second vote.

    The screen decides whether a paper is a replication at all by voting two
    providers against each other. With one provider configured every screened row
    returns a single vote, which the pipeline can only treat as an incomplete
    screen — the whole run would produce target_pending rows and discard nothing.

    Which key each voter needs comes from screen_voters(), the single place the pair
    is configured — so a changed voter changes the required key with it.
    """
    if no_llm:
        return
    missing = [env for _, _, env in screen_voters() if not _SCREEN_KEYS[env]()]
    if missing:
        raise RuntimeError(
            f"Stage 3 needs both reference-screen providers; missing: {', '.join(missing)}. "
            "Set them in .env, or run with --no-llm to skip every LLM stage."
        )


def _merge_duplicate_originals(rows: list[dict], doi_r: str) -> list[dict]:
    """Merge written rows that turned out to name the SAME original.

    _collapse_same_paper_originals groups on what the model said; the guard then
    recovers a DOI for a target that had none, so two entries that looked distinct —
    a bare title and a keyed record, say — can arrive at one doi_o afterwards. Two
    rows sharing a doi_o share a pair_id, the identifier every other system joins on.

    This is the case _aggregate_outcomes exists for: the members were coded
    separately, so their verdicts have to be reconciled by FLoRA's rule rather than
    by taking the first. Rows without a doi_o are left alone — an empty DOI is not
    evidence of identity.
    """
    groups: dict[str, list[dict]] = {}
    order:  list[str] = []
    for result_row in rows:
        doi = clean_doi(str(result_row.get("doi_o", "") or ""))
        key = doi or f"unkeyed:{len(order)}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(result_row)

    merged: list[dict] = []
    for key in order:
        members = groups[key]
        first   = members[0]
        if len(members) > 1:
            log.info("[%s] %d target rows resolved to the same original (%s) — "
                     "merging into one", doi_r, len(members), key)
            for column in ("study_o", "study_r"):
                first[column] = ", ".join(dict.fromkeys(
                    part for m in members
                    for part in str(m.get(column, "") or "").split(", ") if part))
            first["outcome"] = _aggregate_outcomes(
                [str(m.get("outcome", "") or "") for m in members])
        merged.append(first)
    return merged


def _per_target_rows(row: pd.Series, doi_r: str, link: dict, screen: "dict | None",
                     no_llm: bool, no_pdf: bool, resolved_only: bool,
                     recalibrate_outcomes: bool) -> list[dict]:
    """One row per original PAPER the merged target prompt named.

    The order is the single-original path's: resolve, merge, guard, --resolved-only,
    and only THEN the outcome — the guard can demote a target to target_pending, and
    coding it first would spend an LLM call on a row about to be dropped. Several
    studies of ONE original are one row (FLoRA's coding level), so the collapse runs
    before any outcome call rather than after.

    Targets the model saw but could not match to a keyed record get no row: there is
    no published record to write one about. The shortfall is reported in the
    link_evidence of every row that WAS written, so it cannot vanish silently.
    """
    targets = link.get("targets") or []
    entries = [e for e in (_target_entry(t, doi_r) for t in targets) if e]
    missing = [str(t.get("target_as_named", "") or "") for t in targets
               if not (t.get("match_certain") and t.get("record"))]
    shortfall = len(missing) + int(link.get("unidentified_count") or 0)
    if not entries:
        return []

    n_studies = len(entries)
    entries   = _collapse_same_paper_originals(entries)
    if len(entries) < n_studies:
        log.info("[%s] %d targeted studies → %d original paper(s) "
                 "(FLoRA coding level: one row per reference pair)",
                 doi_r, n_studies, len(entries))

    # Once for the paper, not once per target: the outcome escalation reads the parse
    # cache, and the retired multi path never populated it — which is why its rows
    # were coded from the abstract alone however much full text had been acquired.
    if (not no_pdf or recalibrate_outcomes) and _has_document(doi_r, link):
        _save_parse_cache(doi_r)

    # An observation, not a prediction: the row count IS the match type.
    match_type = "multiple_original" if len(entries) > 1 else "single_original"
    link_model = str(link.get("llm_model", "") or "")

    rows: list[dict] = []
    for entry in entries:
        # A DOI the pipeline had to search for is provisional at ~50% precision — the
        # same rule _link_confidence applies on the single path, applied here rather
        # than writing a constant "high" onto a link nobody confirmed.
        link_method = ("llm_title_search" if entry["provisional"]
                       else _map_method(str(link.get("target_stage") or "llm_fulltext")))
        entry = {**entry, "confidence": ("low" if entry["provisional"]
                                         else entry["confidence"])}
        result_row = _guard_original_link(
            {**_merge_multi_row(row, entry, {}, match_type, "high", len(entries),
                                link_model, link_method=link_method,
                                classify_model="", screen=screen),
             # Every row of the group read the same document, so the provenance is
             # the paper's, not the target's.
             **_provenance(link)})
        if shortfall:
            note = (f"identified {len(entries)} of {len(entries) + shortfall} targets; "
                    f"unidentified: {'; '.join(filter(None, missing)) or 'not named'}")
            prior = str(result_row.get("link_evidence", "") or "")
            result_row["link_evidence"] = f"{prior} | {note}" if prior else note

        method = str(result_row["link_method"])
        if resolved_only and method in _NO_LINK_METHODS:
            log.debug("[%s] --resolved-only: skipping %s target row", doi_r, method)
            continue

        outcome = _outcome_without_coding(method, link)
        if outcome is None:
            outcome = _get_outcome(doi_r, row, link,
                                   no_llm=no_llm and not recalibrate_outcomes,
                                   screen=screen, original=entry,
                                   multi_original=len(entries) > 1)
        rows.append(_apply_outcome(result_row, outcome))

    rows = _merge_duplicate_originals(rows, doi_r)

    # Everything that describes the GROUP is settled here, after the guard's demotions
    # and --resolved-only's drops: a paper whose second target was rejected is not a
    # multiple_original paper, and a row demoted to target_pending is not a
    # high-confidence match to anything. audit_extracted also requires ranks 1..n over
    # the rows that actually reach the CSV, with n_originals equal to the group size.
    for i, result_row in enumerate(rows, 1):
        result_row["original_rank"] = i
        result_row["n_originals"]   = len(rows)
        result_row["original_match_type"] = ("multiple_original" if len(rows) > 1
                                             else "single_original")
        result_row["original_match_confidence"] = (
            "high" if str(result_row.get("link_method", "")) in RESOLVED_LINK_METHODS
            else "low")
    return rows


def _resolve_and_code(doi_r: str, row: pd.Series, screen: "dict | None",
                      no_llm: bool, no_pdf: bool, resolved_only: bool,
                      recalibrate_outcomes: bool) -> list[dict]:
    """Run the resolution ladder for one row and code the outcome of what it found.

    The order is deliberate: resolve, merge, guard, --resolved-only, and only THEN
    the outcome. The guard can demote a link to target_pending (self-link, no usable
    original), and --resolved-only discards the row outright — running the outcome
    LLM before either would spend the pipeline's last call on a row that is about to
    be dropped. Returns [] when nothing is to be written.

    A ladder that named targets without accepting one of them goes to the per-target
    adapter: that is a paper the target prompt read as re-testing several originals
    (or one it declined to link), and keeping a single link for it would silently drop
    N-1 originals. A resolved single link takes the merge path below unchanged.
    """
    link = run_for_doi(doi_r, cands_df=_build_cands_df(row),
                       no_llm=no_llm, no_pdf=no_pdf, classification=screen)

    if link.get("targets") and not link.get("resolved"):
        n_targets = int(link.get("n_targets") or 0)
        log.info("[%s] target prompt named %d original(s) without a single accepted "
                 "link — writing one row per target", doi_r, n_targets)
        rows = _per_target_rows(row, doi_r, link, screen, no_llm, no_pdf,
                                resolved_only, recalibrate_outcomes)
        if rows:
            return rows
        if link.get("multi_target"):
            pending = _empty_row(row, "multiple_original", "high",
                                 link_method="target_pending", screen=screen)
            pending["link_evidence"] = (f"target prompt named {n_targets} originals; "
                                        "none could be matched to a record")
            return [] if resolved_only else [pending]

    # original_match_confidence is now an observation about the answer, not a
    # prediction made before it: high when the ladder settled on one original, low
    # when the row is written without one.
    result_row = _guard_original_link(
        _merge_row(row, link, {}, "single_original",
                   "high" if link.get("resolved") else "low", 1, 1, "",
                   screen=screen))
    link_method = str(result_row.get("link_method", ""))
    if resolved_only and link_method in _NO_LINK_METHODS:
        log.debug("[%s] --resolved-only: skipping %s row", doi_r, link_method)
        return []

    outcome = _outcome_without_coding(link_method, link)
    if outcome is None:
        if (not no_pdf or recalibrate_outcomes) and _has_document(doi_r, link):
            _save_parse_cache(doi_r)
        outcome = _get_outcome(doi_r, row, link,
                               no_llm=no_llm and not recalibrate_outcomes,
                               screen=screen)
    return [_apply_outcome(result_row, outcome)]


def _front_door_row(filter_row: pd.Series, screen: dict) -> "dict | None":
    """The row to write when the classification screen ends the paper, else None.

    Two endings, with the semantics the screen used to reach from inside the ladder,
    after the match-type call and often a PDF had already been paid for:

      incomplete — one vote is not a verdict: target_pending so a re-run can decide
                   the row once the provider is back; no votes at all is api_error.
      discard    — screen_gate() says the two votes settle it: not_a_replication.

    A gate "proceed" returns None and goes down the ladder. There is no
    disagreement ending: a confident "none" against a confident qualifying answer
    proceeds, because a false inclusion costs a ladder run and a false discard
    costs the paper.
    """
    method = str(screen.get("resolution_method", ""))
    if method not in {"llm_refscreen_partial", "llm_refscreen_failed"}:
        if screen.get("screen_verdict") != "discard":
            return None
        method = "llm_not_a_replication"

    link = {**screen, "resolution_method": method}
    if method == "llm_not_a_replication":
        # A discarded row must record who said what — the verdict alone is not
        # something a reviewer can act on.
        verdicts = "; ".join(
            f"{v['provider']}={v['classification']}/"
            f"{'confident' if v['confident'] else 'unconfident'}"
            for v in screen.get("votes", []))
        link["llm_evidence"] = "; ".join(filter(None, [
            f"screen discard: {verdicts}" if verdicts else "screen discard",
            str(screen.get("llm_evidence", "") or ""),
        ]))

    link_method = _map_method(method)
    return _merge_row(filter_row, link,
                      _outcome_without_coding(link_method, link),
                      "single_original", "low", 1, 1, screen=screen)


# filtered.csv reaches 4.9 GB in production, so Stage 3 reads it in chunks like
# Stages 1 and 2 and never holds more than one chunk in memory.
_CHUNK_ROWS = 50_000


def _apply_filters(chunk: pd.DataFrame,
                   from_year:         "int | None"       = None,
                   to_year:           "int | None"       = None,
                   predicted_outcome: "str | None"       = None,
                   source:            "str | None"       = None,
                   doi_r_filter:      "list[str] | None" = None) -> pd.DataFrame:
    """Apply the CLI row filters to one chunk of filtered.csv.

    Every filter is a per-row predicate, so a chunk can be filtered on its own and
    the run never needs the whole file. Pure dataframe work: no API call, no row
    written, and no logging — the applied filters are reported once by the caller
    rather than once per chunk.
    """
    if from_year is not None or to_year is not None:
        def _year_int(v: str) -> "int | None":
            try:
                return int(v)
            except (ValueError, TypeError):
                return None
        years = chunk["year_r"].apply(_year_int)
        mask = pd.Series(True, index=chunk.index)
        if from_year is not None:
            mask &= years.apply(lambda y: y is not None and y >= from_year)
        if to_year is not None:
            mask &= years.apply(lambda y: y is not None and y <= to_year)
        chunk = chunk[mask]

    # "other" means any outcome that is not failure (success / mixed / descriptive /
    # cannot_be_determined). Keyword-only, on title + abstract — no LLM, no API calls,
    # and so a lexical sample rather than a sample of papers with that outcome.
    if predicted_outcome and not chunk.empty:
        want = predicted_outcome.lower()
        def _keep(row: pd.Series) -> bool:
            pred = predict_outcome_keyword(
                str(row.get("title_r", "")), str(row.get("abstract_r", ""))
            )
            return pred != "failure" if want == "other" else pred == want
        chunk = chunk[chunk.apply(_keep, axis=1)]

    if source is not None:
        chunk = chunk[chunk["source"].str.lower() == source.lower()]

    if doi_r_filter:
        target_set = {clean_doi(d) for d in doi_r_filter}
        chunk = chunk[chunk["doi_r"].apply(clean_doi).isin(target_set)]

    return chunk


def _iter_filtered_rows(filtered_path,
                        from_year:         "int | None"       = None,
                        to_year:           "int | None"       = None,
                        predicted_outcome: "str | None"       = None,
                        source:            "str | None"       = None,
                        doi_r_filter:      "list[str] | None" = None):
    """Yield filtered.csv rows, abstract-bearing ones first, without loading the file.

    Rows without an abstract are deferred to the end so they do not hold up the ones
    the pipeline can actually work on. That ordering used to come from a concat of
    two slices of the whole frame; here it is two passes over the same chunked read,
    which costs a second scan of the file and no memory.
    """
    for line in (f"--year filter {from_year or 'any'}–{to_year or 'any'}" if
                 (from_year is not None or to_year is not None) else "",
                 f"--predicted-outcome {predicted_outcome!r} (keyword-selected, not a "
                 "representative sample)" if predicted_outcome else "",
                 f"--source filter {source!r}" if source is not None else "",
                 "--doi-r filter" if doi_r_filter else ""):
        if line:
            log.info("%s applied during chunked read", line)

    for with_abstract in (True, False):
        n_read = n_yielded = 0
        for chunk in pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig",
                                 chunksize=_CHUNK_ROWS):
            n_read += len(chunk)
            chunk = _apply_filters(chunk.fillna(""), from_year, to_year,
                                   predicted_outcome, source, doi_r_filter)
            if chunk.empty:
                continue
            has_abstract = chunk["abstract_r"].astype(str).str.strip() != ""
            chunk = chunk[has_abstract] if with_abstract else chunk[~has_abstract]
            if not with_abstract and n_yielded == 0 and not chunk.empty:
                log.info("Deferring candidates without abstracts — processing will "
                         "continue but at lower priority")
            n_yielded += len(chunk)
            for _, row in chunk.iterrows():
                yield row
        if with_abstract:
            log.info("Stage 3: %d rows in %s, %d with an abstract",
                     n_read, Path(filtered_path).name, n_yielded)


def _should_skip(row: pd.Series, row_key: str, doi_r_clean: str,
                 flora_skip: "set[str]", resolved_rows: dict,
                 resolved_main: dict, doi_r_targets: "set[str]",
                 only_reproductions: bool) -> "str | None":
    """Why this row is not processed, or None to process it.

    Ordered by cost: the memory-set checks first, and --only-reproductions ahead of
    the caller's --limit counter, so `--limit N` counts reproductions rather than
    the replications it scanned past.
    """
    if doi_r_clean in flora_skip:
        return "flora"
    if row_key and row_key in resolved_rows:
        return "resumed"
    # --extracted-test: skip DOIs already resolved in the production extracted.csv.
    # Exception: --doi-r targets are always processed (explicit re-run request).
    if row_key and row_key in resolved_main and doi_r_clean not in doi_r_targets:
        return "already in extracted.csv"
    # False positives are excluded from Stage 3 — only replications and reproductions proceed.
    if row.get("filter_status") == "false_positive":
        return "false_positive"
    if only_reproductions and str(row.get("filter_status", "")).strip().lower() != "reproduction":
        return "not a reproduction"
    return None


def _process_row(row: pd.Series, doi_r: str, no_llm: bool, no_pdf: bool,
                 no_reproductions: bool,
                 resolved_only: bool, recalibrate_outcomes: bool) -> list[dict]:
    """Every row the pipeline writes for one filtered.csv row.

    Front door, then the resolution ladder — there is no router in front of it any
    more: how many originals a paper targets is what the target prompt answers, not
    something a cheaper call predicts from the abstract. An empty list means the row
    is not written at all — either a flag suppressed it or --resolved-only discarded
    it.
    """
    # Ahead of the front door: a run that is not coding reproductions should not pay
    # to screen them either. The type this reads is Stage 2's, the only one there is
    # before the screen speaks.
    if no_reproductions and str(row.get("filter_status", "")) == "reproduction":
        log.info("[%s] --no-reproductions: writing target_pending", doi_r)
        return [_empty_row(row, "single_original", "low",
                           link_method="target_pending")]

    # ── Front door: is this a replication at all? ────────────────────────
    # 58% of the rows that reach the classification screen are discarded there.
    # Asking first costs nothing — the two votes were already being made — and
    # spares those rows the heavy-model match-type call, the resolution ladder,
    # PDF acquisition and outcome coding. The verdict is threaded into
    # run_for_doi so Stage 4.5 picks a target without voting again.
    screen = None
    if not no_llm:
        token_counter.set_stage("extract_refscreen")
        screen = classify_replication(doi_r, str(row.get("title_r", "") or ""),
                                      str(row.get("abstract_r", "")))
        done = _front_door_row(row, screen)
        if done is not None:
            log.info("[%s] front-door screen: %s", doi_r, done["link_method"])
            return [done]
        # filter_status is the paper-type field (issue #93), so a screen that
        # said what the paper is overwrites it. A gate that proceeded without a
        # qualifying vote (unclear/unclear, or an unconfident none against an
        # unconfident qualifying answer) said nothing, and the row keeps whatever
        # Stage 2 left — a needs_review row stays needs_review, waits for a human
        # on the check page, and is not imported by csv_to_db.
        if screen.get("record_type"):
            row["filter_status"] = screen["record_type"]
            row["filter_method"] = "screen"   # the screen decided the type

    try:
        return _resolve_and_code(
            doi_r, row, screen=screen, no_llm=no_llm, no_pdf=no_pdf,
            resolved_only=resolved_only,
            recalibrate_outcomes=recalibrate_outcomes)
    except (OpenAlexQuotaExhausted, TokenBudgetExhausted):
        # Not a per-row failure: the row was never examined, and writing it as
        # api_error would bury the reason the rest of the run stops too.
        raise
    except Exception as e:
        log.error("[%s] extraction failed: %s", doi_r, e)
        return [_empty_row(row, "single_original", "low")]


def run_extract(no_llm: bool = False,
                limit: "int | None" = None,
                no_pdf: bool = False,
                no_reproductions: bool = False,
                only_reproductions: bool = False,
                skip_flora_validated: bool = True,
                resume: bool = False,
                resolved_only: bool = False,
                from_year: "int | None" = None,
                to_year: "int | None" = None,
                predicted_outcome: "str | None" = None,
                out_path: "Path | None" = None,
                source: "str | None" = None,
                doi_r_filter: "list[str] | None" = None,
                recalibrate_outcomes: bool = False,
                rescreen: bool = False) -> pd.DataFrame:
    """
    Run Stage 3 and stream results to data/extracted.csv.

    no_llm              — skip all LLM calls (rule-based only).
    limit               — process only the first N non-false-positive rows.
    no_pdf              — skip PDF download; abstract-only LLM resolution only.
    no_reproductions    — skip rows with filter_status=reproduction (write as target_pending).
    only_reproductions  — process ONLY filter_status=reproduction rows; others are
                          skipped entirely (not written at all).
    skip_flora_validated — skip DOIs already in FLoRA: validated entry-sheet rows
                           (FLORA_VALIDATED_STATUSES) plus every row in flora.csv.
                           ON by default; disable with --no-skip-flora-validated.
    resume              — carry forward already-resolved rows from extracted.csv unchanged;
                           re-run only rows with link_method == "target_pending".
    resolved_only       — only write rows that are fully resolved (any rule-based or
                           llm_cited_candidates / llm_fulltext link_method with a non-empty doi_o).
                           target_pending / api_error / no_original_found rows are silently skipped.
                           Use with --no-llm --no-pdf for a fast rule-based-only pass, then
                           follow up with --resume for the LLM pass on remaining rows.
    rescreen            — reopen the rows a previous run set aside on the classification screen's
                           own verdict (not_a_replication, screen_disagreement) so the current
                           voter pair decides them again. Without it those rows are carried
                           forward untouched, which freezes an old pair's verdicts in place.
    recalibrate_outcomes — run the full outcome pipeline (PDF download + LLM) even when --no-pdf
                           or --no-llm are set. Only the outcome step is affected; link resolution
                           still respects those flags. Useful for a fast --no-llm --no-pdf pass
                           that still gets proper outcomes.
    """
    _check_screen_providers(no_llm)

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

    flora_skip: set[str] = set()
    if skip_flora_validated:
        flora_skip = _load_flora_skip_dois(
            DATA_DIR / "FLoRA entry sheet - replication list.csv",
            DATA_DIR / "flora.csv",
        )

    prod_path = DATA_DIR / "extracted.csv"
    if out_path is None:
        out_path = prod_path
    test_mode = (str(out_path.resolve()) != str(prod_path.resolve()))

    output_rows: list[dict] = []
    first_write = True
    processed = 0

    # In test mode: always load the production CSV to identify which DOIs to skip
    # (those already resolved in extracted.csv should not be re-processed or written
    #  to extracted-test.csv, even without --resume).
    resolved_main: dict[str, list[dict]] = {}
    if test_mode:
        resolved_main, _ = _load_extracted_rows(prod_path, rescreen=rescreen)
        log.info(
            "--extracted-test: %d DOIs already resolved in extracted.csv will be skipped",
            len(resolved_main),
        )

    resolved_rows: dict[str, list[dict]] = {}
    if resume:
        resolved_rows, pending_dois = _load_extracted_rows(out_path, rescreen=rescreen)
        n_resolved_rows = sum(len(v) for v in resolved_rows.values())
        log.info(
            "--resume: %d DOIs already resolved (%d rows), %d pending re-processing",
            len(resolved_rows), n_resolved_rows, len(pending_dois),
        )
        # Write ALL resolved rows to the output file immediately, before processing
        # any pending rows. This prevents data loss if the run is interrupted later —
        # resolved work is committed up front, not interleaved with slow PDF/LLM calls.
        for rows in resolved_rows.values():
            for result_row in rows:
                _append_row(out_path, result_row, first=first_write)
                first_write = False
                output_rows.append(result_row)
        log.info("--resume: wrote %d resolved rows to %s (safe to interrupt)",
                 len(output_rows), out_path.name)

    doi_r_targets = {clean_doi(d) for d in (doi_r_filter or [])}
    flora_skip_count = 0

    for row in _iter_filtered_rows(filtered_path, from_year, to_year,
                                   predicted_outcome, source, doi_r_filter):
        doi_r_clean = clean_doi(str(row.get("doi_r", "")))
        skip = _should_skip(row, _extract_row_key(row), doi_r_clean, flora_skip,
                            resolved_rows, resolved_main, doi_r_targets,
                            only_reproductions)
        if skip is not None:
            log.debug("[%s] skipping — %s", doi_r_clean, skip)
            if skip == "flora":
                flora_skip_count += 1
            continue

        if limit is not None and processed >= limit:
            break
        processed += 1

        # If DOI is missing, try to resolve one from the URL before processing.
        # This lets I4R / Replication Network rows participate in the full pipeline.
        doi_r = doi_r_clean
        if not doi_r:
            url_r = str(row.get("url_r", "") or "").strip()
            if url_r:
                from shared.openalex_client import resolve_doi_from_url
                resolved = resolve_doi_from_url(url_r)
                if resolved:
                    log.info("[url:%s] resolved DOI %s — using for extraction", url_r[:60], resolved)
                    doi_r = resolved
                    row = row.copy()
                    row["doi_r"] = doi_r
                else:
                    log.info("[url:%s] could not resolve DOI — will extract from URL/abstract only", url_r[:60])

        result_rows = _process_row(
            row, doi_r, no_llm=no_llm, no_pdf=no_pdf,
            no_reproductions=no_reproductions, resolved_only=resolved_only,
            recalibrate_outcomes=recalibrate_outcomes)

        # Self-links and unrecoverable doi_o are already rejected by
        # _guard_original_link at each producer, ahead of outcome coding.
        for result_row in result_rows:
            if resolved_only and result_row.get("link_method") in _NO_LINK_METHODS:
                log.debug("[%s] --resolved-only: skipping %s row",
                          doi_r, result_row.get("link_method"))
                continue
            _append_row(out_path, result_row, first=first_write)
            first_write = False
            output_rows.append(result_row)
            log.info("Streamed %d rows → %s", len(output_rows), out_path.name)

    log.info("Stage 3 complete: %d rows → %s", len(output_rows), out_path)

    # Print summary
    print("\n" + "=" * 70)
    print("STAGE 3 SUMMARY")
    print("=" * 70)
    print(f"  Rows processed:           {processed}")
    print(f"  Rows output:              {len(output_rows)}")
    print(f"  Flora-validated skipped:  {flora_skip_count}")
    if skip_flora_validated:
        print(f"    (from flora_entry_sheet.csv + flora.csv)")
    if limit:
        print(f"  Limit reached:            {processed >= limit}")
    print("=" * 70 + "\n")

    out_df = pd.DataFrame(output_rows)
    return out_df.reindex(columns=EXTRACTED_COLS, fill_value="")


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
            record_type=(str(row.get("type", "")).strip().lower()
                         or ("reproduction"
                             if str(row.get("filter_status", "")).strip().lower()
                             == "reproduction" else "replication")),
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
    parser.add_argument(
        "--outcome-only", action="store_true",
        help="Classify outcome only -> data/outcome_only.csv",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Process only the first N non-false-positive rows.",
    )
    parser.add_argument(
        "--no-pdf", action="store_true",
        help="Skip PDF download; use abstract-only for LLM resolution.",
    )
    parser.add_argument(
        "--no-reproductions", action="store_true",
        help="Skip reproduction rows (write as target_pending).",
    )
    parser.add_argument(
        "--only-reproductions", action="store_true",
        help="Process ONLY reproduction rows (filter_status=reproduction); skip "
             "replications entirely without writing them.",
    )
    parser.add_argument(
        "--skip-flora-validated", action=argparse.BooleanOptionalAction, default=True,
        help="Skip DOIs already in FLoRA — validated entry-sheet rows "
             "('validated - unchanged' | 'validated - changed' | 'validated - chosen') "
             "plus every row in flora.csv. ON by default; pass "
             "--no-skip-flora-validated to re-extract them anyway.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Carry forward already-resolved rows from extracted.csv; "
             "re-run only rows with link_method == 'target_pending'.",
    )
    parser.add_argument(
        "--rescreen", action="store_true",
        help="With --resume (or --extracted-test): also re-process rows a previous run "
             "set aside on the classification screen's verdict (not_a_replication, "
             "screen_disagreement), so a changed voter pair or prompt decides them again. "
             "Rows already moved into data/not_a_replication.csv or "
             "data/screen_disagreement.csv by sanity_check are re-processed anyway, since "
             "they are no longer in extracted.csv.",
    )
    parser.add_argument(
        "--resolved-only", action="store_true",
        help="Only write rows that are fully resolved (any rule-based method or llm_cited_candidates / llm_fulltext). "
             "target_pending / api_error / no_original_found rows are silently skipped. "
             "Combine with --no-llm --no-pdf for a fast rule-based pass, then use --resume "
             "for the LLM pass on the remaining rows.",
    )
    parser.add_argument(
        "--from-year", type=int, default=None, metavar="YYYY",
        help="Only process rows from filtered.csv where year_r >= YYYY.",
    )
    parser.add_argument(
        "--to-year", type=int, default=None, metavar="YYYY",
        help="Only process rows from filtered.csv where year_r <= YYYY.",
    )
    parser.add_argument(
        "--predicted-outcome",
        choices=["failure", "success", "mixed", "descriptive", "cannot_be_determined", "other"],
        default=None,
        metavar="OUTCOME",
        help=(
            "Pre-screen rows using keyword-only outcome prediction on title + abstract "
            "before running any API calls. 'other' means any predicted outcome except failure. "
            "Choices: failure | success | mixed | descriptive | cannot_be_determined | other."
        ),
    )
    parser.add_argument(
        "--extracted-test", action="store_true",
        help=(
            "Write output to data/extracted-test.csv instead of extracted.csv. "
            "Skips DOIs already resolved in extracted.csv; re-runs target_pending "
            "rows and new rows not yet present in either CSV."
        ),
    )
    parser.add_argument(
        "--source", type=str, default=None, metavar="SOURCE",
        help=(
            "Only process rows from this source "
            "(openalex | bob_reed | i4r | semantic_scholar | …). "
            "Case-insensitive. Matches the 'source' column in filtered.csv."
        ),
    )
    parser.add_argument(
        "--doi-r", type=str, default=None, metavar="DOIS",
        help=(
            "Comma-separated list of replication DOIs (doi_r) to process. "
            "All other rows are skipped. Useful for re-running specific rows."
        ),
    )
    parser.add_argument(
        "--recalibrate-outcomes", action="store_true",
        help=(
            "Run the full outcome pipeline (PDF download + LLM) even when --no-pdf "
            "or --no-llm are set. Only the outcome step is affected; link resolution "
            "still respects those flags. Useful with --no-llm --no-pdf --resume to "
            "carry forward links but upgrade cannot_be_determined outcomes."
        ),
    )
    args = parser.parse_args()

    try:
        if args.outcome_only:
            run_outcome_only(no_llm=args.no_llm, limit=args.limit)
        else:
            doi_r_list = (
                [d.strip() for d in args.doi_r.split(",") if d.strip()]
                if args.doi_r else None
            )
            run_extract(
                no_llm=args.no_llm,
                limit=args.limit,
                no_pdf=args.no_pdf,
                no_reproductions=args.no_reproductions,
                only_reproductions=args.only_reproductions,
                skip_flora_validated=args.skip_flora_validated,
                resume=args.resume,
                resolved_only=args.resolved_only,
                from_year=args.from_year,
                to_year=args.to_year,
                predicted_outcome=args.predicted_outcome,
                out_path=(DATA_DIR / "extracted-test.csv") if args.extracted_test else None,
                source=args.source,
                doi_r_filter=doi_r_list,
                recalibrate_outcomes=args.recalibrate_outcomes,
                rescreen=args.rescreen,
            )
    except (OpenAlexQuotaExhausted, TokenBudgetExhausted) as exc:
        log.error("%s", exc)
        log.error("Rows written before the stop are intact; re-run with --resume to continue.")
    finally:
        token_counter.print_summary()
        # Sanity pass over whatever was written — runs on normal completion AND on
        # Ctrl-C. Skipped for the outcome-only mode (different output file).
        if not args.outcome_only:
            from extract.sanity_check import run_sanity_check
            run_sanity_check(DATA_DIR / ("extracted-test.csv" if args.extracted_test
                                         else "extracted.csv"))
        from shared.dashboard_cache import refresh as _dc_refresh
        _dc_refresh("extracted-test" if args.extracted_test else "extracted")
