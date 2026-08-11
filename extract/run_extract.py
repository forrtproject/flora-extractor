"""
run_extract.py — Stage 3's per-row pipeline, as a library. There is no runner here.

One function is the whole entry point: `_process_row(row, doi_r, …)` takes one input
row — the FILTERED_COLS + SCREEN_COLS shape Stage 2's handoff produces — and returns
every `EXTRACTED_COLS` row the pipeline writes for it. Front door, resolution ladder,
per-target adapter, outcome coding, the original-link guard, DOI verification. Its
one caller is `extract/tier.py`'s judge.

**Nothing in this file writes `data/extracted.csv`.** That file has exactly one
writer, `python -m extract.export`, which renders it from the permanent verdict rows
the extract tier stores. Stage 3 runs as that claimed tier:

    python -m extract.tier --run     # claim, extract, write verdicts
    python -m extract.export         # verdicts → data/extracted.csv

The CSV runner that used to live here — the chunked read of `data/filtered.csv`, the
worker pool, the file-based resume with `--fresh`/`--rescreen`, the appending writer,
the input-manifest handshake, `--screen-here` — is retired. Its checkpoint was the
output file, the tier's is a row in the state authority, and running both meant two
answers to "has this paper been extracted". It is parked on the `wip/csv-runner`
branch, with a WIP.md saying what a revival would have to satisfy.

The functions below are ordered as a row moves through them, and every one of them
is a pure-ish per-row step: no file is opened for writing, no run state is held, and
concurrency belongs to whoever calls them (the tier runs `EXTRACT_WORKERS` works at
once through the engine's generic spine).
"""
import json
import re
from typing import Optional

import pandas as pd

from shared.config import (
    OA_XML_CACHE_DIR, PARSE_CACHE_DIR, PDF_CACHE_DIR, log,
)
from shared.cache import write_json
from shared.llm_client import _clean_study_numbers, provider_for
from shared.token_usage import TokenBudgetExhausted
from shared.openalex_client import OpenAlexQuotaExhausted
from shared.openalex_client import fetch_openalex_by_doi as _oa_by_doi
from shared.openalex_client import fetch_openalex_full_metadata as _oa_full_meta
from shared.openalex_client import (
    _TitleSearchUnavailable, _search_crossref_by_title, _search_openalex_by_title,
)
from shared.pdf_sources import (cached_pdf, openalex_xml_has_content,
                                verified_cached_document)
from shared.pdf_parsing import (
    best_parse_result,
    outcome_text,
    parse_all as _parse_all,
    parse_result_has_transient_failure,
    parse_result_is_empty,
    parse_cache_path as _parse_cache_path,
    read_parse_cache as _read_parse_cache_shared,
    score_parse_result,
)
from shared.prompts import OUTCOME_TEXT_CHARS
from shared.doi_verify import keeps_no_doi, verify_and_correct
from shared.row_key import primary_key
from shared.schema import (
    EXTRACTED_COLS,
    LINK_METHOD_VALUES,
    OUTCOME_CATEGORIES,
    RESOLVED_LINK_METHODS,
    RESOLVED_LINK_METHODS,
    VERIFICATION_SKIP_LINK_METHODS,
    assert_no_float_years,
    make_pair_id,
    year_str,
)
from shared.utils import (bare_work_id, cache_key, citation_fragment,
                          clean_citation_title, clean_doi, non_article_doi,
                          psyctests_doi, usable_title)
from extract.link_original import run_for_doi
from extract.code_outcome import extract_outcome

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
    # The two catches below turn a lookup failure into the surname·year fallback,
    # which is the right answer for a genuinely unfindable original. They must NOT
    # swallow the signals the API layer raises deliberately: OpenAlexQuotaExhausted
    # means every later row will fail the same way and the run has to stop
    # (run_extract's main loop is what stops it), and _TitleSearchUnavailable means
    # the search never happened. Caught here, both would quietly degrade ref_o on
    # every remaining row of a run nobody knew had lost its quota.
    meta: dict | None = None
    if doi_o:
        try:
            meta = _oa_full_meta(doi_o)
        except (OpenAlexQuotaExhausted, _TitleSearchUnavailable):
            raise
        except Exception as exc:
            log.debug("[ref_o] DOI lookup failed for %s: %s", doi_o, exc)

    # Title-based fallback for no_metadata / hallucinated DOIs
    if meta is None and title_o:
        try:
            meta = (_search_crossref_by_title(title_o, fallback_year)
                    or _search_openalex_by_title(title_o, fallback_year))
            if meta:
                log.debug("[ref_o] title search hit for %r: %s", title_o[:60], meta.get("doi"))
        except (OpenAlexQuotaExhausted, _TitleSearchUnavailable):
            raise
        except Exception as exc:
            log.debug("[ref_o] title search failed for %r: %s", title_o[:60], exc)

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


def _match_confidence(result_row: dict) -> str:
    """`original_match_confidence` for a finished row — one rule for every producer.

    Both halves have to hold: a resolved link_method says the ladder finished, and
    link_confidence (`_link_confidence`) says the record it finished on is checkable.
    The single-link path used to write "high" for any resolved method, which promoted
    exactly the links `_link_confidence` caps at medium — `single_candidate_after_requery`
    auto-accepts a lone candidate with no semantic check — so the same evidence read as
    high on one path and low on the other.
    """
    return ("high" if (str(result_row.get("link_method", "")) in RESOLVED_LINK_METHODS
                       and str(result_row.get("link_confidence", "")) == "high")
            else "low")


def _link_confidence(link: dict) -> str:
    """Persisted link_confidence: LLM confidence if present, else derived from score.

    #51: single_candidate_after_requery auto-accepts a lone candidate at score 1.0
    with NO semantic check — "exactly one candidate came back" is not evidence it is
    the replication TARGET. Cap it at medium so validation prioritises these rows.

    A title-search link is always low: the score it carries is the search's title
    match, which says the DOI is the named paper — not that the named paper is the
    target. The class imports at 98-99% measured, but low is what routes validator
    attention to the rows whose link no reference list corroborates
    (see LINK_METHOD_VALUES).
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
        "year_r":                year_str(row.get("year_r")),
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
        year        = year_str(row.get("year_r")),
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
    type into the CSV, and the validation import leaves it for a human.
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
        # The input row's year arrives however pandas typed the column it was read
        # from — a float where anything in that chunk was missing (#140).
        "year_r": year_str(row.get("year_r")),
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
        "year_o":          year_str(link.get("resolved_year_o")),
        **dict(zip(("ref_o", "authors_o", "bibtex_ref_o"), _build_ref_o(
            doi_o_clean,
            str(link.get("resolved_author_o", "") or ""),
            year_str(link.get("resolved_year_o")),
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
        # The record's OWN work id, kept rather than used for the pair_id and thrown
        # away. An original that came from an OpenAlex reference record has an
        # OpenAlex id whether or not it has a DOI, and that id was its identity; the
        # row then reached `_guard_original_link` carrying nothing, which spent a
        # title search re-finding what it had been handed. `_fill_work_ids` only ever
        # derives this column FROM doi_o, so for a DOI-less original nothing else
        # would have filled it.
        "oa_work_id_o":    bare_work_id(str(orig.get("openalex_id", "") or "")),
        "title_o":         title_o,
        "year_o":          year_str(orig.get("year")),
        **dict(zip(("ref_o", "authors_o", "bibtex_ref_o"), _build_ref_o(
            doi_o_clean,
            str(orig.get("first_author", "") or ""),
            year_str(orig.get("year")),
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
               link_method: str, classify_model: str = "",
               screen: "dict | None" = None, error: str = "") -> dict:
    """A row with no link. It still carries whatever the front door already decided:
    a paper the screen classified and categorised does not stop being categorised
    because the ladder found no original, and a reviewer reading the pending row
    needs the same evidence as one reading a resolved one.

    *error* states what stopped the row, and is what makes an api_error readable
    after the run's log is gone: it lands in link_evidence, where a reader looks,
    and in outcome_reasoning through _outcome_without_coding's api_error branch.

    link_method has no default. It used to default to api_error, which is how the
    catch-all in _process_row came to write a row that said only that something
    failed — the silence was by omission, so every caller now states its intent.
    """
    doi_r_clean = clean_doi(str(filter_row.get("doi_r", "")))
    row = _base_row(filter_row, match_type, match_conf, classify_model,
                    _outcome_without_coding(link_method, {"llm_error": error}) or {},
                    screen)
    row.update({
        "pair_id": make_pair_id(doi_r_clean, ""),
        "doi_o": "", "title_o": "", "year_o": "", "authors_o": "", "ref_o": "",
        "bibtex_ref_o": "",
        "link_method": link_method, "link_evidence": error,
        "link_confidence": "low", "link_llm_model": "",
    })
    if screen is None:
        # Nobody classified this row, and the replication default _record_type falls
        # back to would be a guess.
        row["type"] = ""
    return row


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cached_oa_xml(openalex_id: str) -> "dict | None":
    """The cached OpenAlex GROBID-XML result for *openalex_id*, or None when it is
    no document.

    The argument is the OpenAlex WORK ID, because that is what
    `get_openalex_fulltext()` files the entry under. This reader used to be handed
    `cache_key(doi_r)` instead, which named a file the writer never creates: across
    the 285 rows of the 2026-08-06 `extracted.csv`, the DOI-derived name matched 0
    entries on disk and the id-derived name matched every one that had been fetched.
    So `_has_document()` reported "nothing to read" for a row whose only document was
    OpenAlex XML, and `_save_parse_cache()` re-parsed with `oa_xml=None`.

    A content-free shell (`openalex_xml_has_content()`) is not a document — the ladder
    applies that test before it will read one, and so must every reader of the same
    cache file, or the shell counts as full text here while ending the row at
    `no_fulltext_available` there.
    """
    if not openalex_id:
        return None
    oa_id = openalex_id.strip()
    if not oa_id.startswith("W"):
        oa_id = f"W{oa_id}"
    cache_file = OA_XML_CACHE_DIR / f"oa_xml_{cache_key(oa_id)}.json"
    if not cache_file.exists():
        return None
    try:
        with cache_file.open(encoding="utf-8") as fh:
            cached = json.load(fh)
    except Exception:
        return None
    return cached if openalex_xml_has_content(cached) else None


def _has_document(doi_r: str, link: dict, openalex_id: str = "") -> bool:
    """True when there is something for the parsers to read.

    Every stage before Stage 5 returns with pdf={}, so pdf_source is "none" and no
    document was acquired. Parsing anyway wrote a cache of six empty results, and
    because the writers early-return on an existing file that empty cache then stood
    in for the real one on any later run that DID get the PDF (audit B4). A document
    cached by an earlier run still counts — that is what --recalibrate-outcomes reads.

    The document is looked up by DOI because that is what `download_pdf()` keys it on;
    the XML by OpenAlex id, for the same reason (`_cached_oa_xml`). A DOI-less row
    simply has no document to find here, which is what it had before too. `cached_pdf`
    does the looking, so a Word file counts as much as a PDF.
    """
    if bool(link.get("pdf_ok")):
        return True
    if str(link.get("pdf_source", "none") or "none") not in {"", "none"}:
        return True
    if doi_r and cached_pdf(doi_r, cache_dir=PDF_CACHE_DIR) is not None:
        return True
    return _cached_oa_xml(openalex_id) is not None


def _cache_id(row: pd.Series, doi_r: str = "") -> str:
    """This row's identity for the on-disk parse cache.

    The DOI when there is one, so every entry written before this existed still
    matches; otherwise `primary_key()`'s next-strongest identifier (`oa:` → `url:` →
    `title:`). Verified against the 2026-08-06 `extracted.csv`: all 278 rows with a
    DOI keep the key they already had on disk, and the 7 without stop sharing one.
    """
    return doi_r or primary_key(row)


def _read_parse_cache(cache_id: str) -> "dict | None":
    """The cached parse_all results for *cache_id*, or None on a miss.

    The reader itself lives in shared/pdf_parsing.py: the ladder reads the same files
    before it parses (link_original Stage 6), and it cannot import this module.
    """
    return _read_parse_cache_shared(cache_id, PARSE_CACHE_DIR)


def _save_parse_cache(cache_id: str, doi_r: str = "", openalex_id: str = "",
                      title_r: str = "") -> None:
    """Run all PDF parsers for *cache_id*'s document and cache the results.

    *cache_id* names the cache entry (see `parse_cache_path`); *doi_r* and
    *openalex_id* say where the document itself is, which are different keys.

    The document is taken by identifier, not from what acquire_pdf just returned, so
    a row that resolved above the acquisition rung parses whatever an earlier run
    left on disk — including a file the server mis-served. *title_r* is what says it
    is the right paper.
    """
    out_file = _parse_cache_path(cache_id, PARSE_CACHE_DIR)
    if _read_parse_cache(cache_id) is not None:
        return

    pdf_path = (verified_cached_document(doi_r, title_r, cache_dir=PDF_CACHE_DIR)
                if doi_r else None)

    results = _parse_all(doi_r, pdf_path, oa_xml=_cached_oa_xml(openalex_id))
    if parse_result_is_empty(results):
        log.debug("[%s] parse produced no text — not caching", cache_id)
        return
    if parse_result_has_transient_failure(results):
        # One method never got an answer (the reference extractor while its provider
        # was unreachable, say). The others' text is real, but the whole dict is what
        # goes to disk and what comes back, so caching now would make an outage this
        # paper's permanent answer about its references. Nothing is cached until every
        # method has answered; the text costs a re-parse, which is local.
        log.info("[%s] parse carries a transient failure — not caching", cache_id)
        return
    try:
        write_json(out_file, results, indent=2)
    except Exception as exc:
        log.debug("[%s] _save_parse_cache write failed: %s", cache_id, exc)


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
    keyed_link_disputed) or carries no link at all (target_pending, api_error,
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
    if link_method == "prescreen_discard":
        # The cheap tier's own verdict, and the only thing that ever read this paper.
        # Confidence is low whatever the models said: two 3B-class answers are not the
        # validated pair, which is exactly why the row is quarantined separately.
        return _skip("not_a_replication", "low", "abstract",
                     str(link.get("llm_reasoning", "") or ""),
                     str(link.get("llm_model", "") or ""))
    if link_method == "screen_disagreement":
        return _skip("pending", "low", "",
                     "outcome not coded: the two classifiers disagreed on whether "
                     "this is a replication — set aside for review")
    return _skip("pending", "low", "",
                 f"outcome not coded: no resolved original link ({link_method})")


def _apply_outcome(row: dict, outcome: dict) -> dict:
    """Write the outcome fields onto an already-merged result row.

    The outcome step is also the first look the pipeline gets at the methods, so it
    can correct `type`: when the model reports the other vocabulary the row is re-coded
    under it, and the record type it was actually coded in is the one the row must
    carry.

    target_check is the standalone coder's verdict on the link an earlier stage
    asserted. "no_original" is a veto, handled where record_type_check == "neither" is
    (in normalise_outcome_block, which sets not_a_replication). "other_original" is
    not: the paper does re-test something, just not this — which is a link the row
    should carry at low confidence with the disagreement written down, for a human to
    settle, rather than a row silently dropped.
    """
    if outcome.get("target_check") == "other_original":
        row["link_confidence"] = "low"
        note = "target check: text describes re-testing a different original"
        prior = str(row.get("link_evidence", "") or "")
        row["link_evidence"] = f"{prior} | {note}" if prior else note
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
                 screen: "dict | None" = None, *,
                 original: "dict | None" = None) -> dict:
    """The outcome for one (replication, original) pair, coded on its own.

    Only rows whose link no LLM chose reach here — a deterministic rule resolved them,
    so nothing has read the paper and checked the original. Both passages the parse
    yielded are sent, each named, together with the evidence the rule matched on: the
    model checks the link (target_check) as well as coding the verdict.

    *original* names the original this call is about — the per-target adapter passes
    one entry per row, where the link carries a whole list; without it the link's own
    resolved_* fields are the original, as on the single-link path.
    """
    abstract_r = str(row.get("abstract_r", ""))
    title_r    = str(row.get("title_r",    ""))

    # Prefer the best-scoring parse method from the parse cache so the outcome LLM
    # receives whichever parser extracted the richest text, not always GROBID —
    # narrowed to the discussion/conclusion, which is where FLoRA's rule says the
    # outcome is stated when the abstract does not state it.
    fulltext, provenance, intro = _best_fulltext_from_cache(_cache_id(row, doi_r))
    if not fulltext:
        # Fallback: sections that run_for_doi already extracted. The intro is in here
        # for want of anything better; it is the section that most often discusses
        # OTHER studies' replication failures, and the prompt says so.
        fulltext = " ".join(filter(None, (
            str(link.get(key) or "") for key in
            ("grobid_abstract", "grobid_intro", "grobid_methods"))))
        provenance = "sections" if fulltext else "none"
    if not intro:
        intro = str(link.get("grobid_intro") or "")

    orig = original or {"title":        link.get("resolved_title_o"),
                        "first_author": link.get("resolved_author_o"),
                        "year":         link.get("resolved_year_o")}
    return extract_outcome(
        doi_r, abstract_r, fulltext, title_r, no_llm=no_llm,
        original_title=str(orig.get("title") or ""),
        original_authors=str(orig.get("first_author") or ""),
        original_year=str(orig.get("year") or ""),
        record_type=_record_type(row, screen),
        intro_text=intro,
        original_evidence=str(link.get("llm_evidence") or ""),
        fulltext_provenance=provenance,
    )


def _best_fulltext_from_cache(cache_id: str) -> tuple[str, str, str]:
    """The outcome-bearing text for *cache_id*, as (closing text, provenance, intro).

    Reads the parse cache, and takes the raw text of the highest-scoring method
    that actually has raw text — the top-scoring method overall can be GROBID,
    which returns sections and no raw text at all, and falling straight back to
    its abstract + intro discarded a full parse another method had produced.

    `outcome_text()` then narrows that to the discussion/conclusion. The intro is
    returned beside it rather than folded into it: the two are different evidence and
    the prompt names them separately, so a quote can be attributed to the right one.
    Returns ("", "none", "") on a cache miss or an all-empty cache.
    """
    results = _read_parse_cache(cache_id)
    if not results:
        return "", "none", ""
    ranked = sorted((r for r in results.values() if isinstance(r, dict)),
                    key=score_parse_result, reverse=True)
    intro = next((str(r.get("intro", "") or "").strip() for r in ranked
                  if str(r.get("intro", "") or "").strip()), "")
    for result in ranked:
        raw = str(result.get("raw_text", "") or "").strip()
        if raw:
            text, provenance = outcome_text(raw, max_chars=OUTCOME_TEXT_CHARS)
            return text, provenance, intro

    best = best_parse_result(results)
    if not best:
        return "", "none", intro
    joined = " ".join(filter(None, [
        str(best.get("abstract", "") or ""),
        str(best.get("intro",    "") or ""),
    ])).strip()
    return (joined, "sections", intro) if joined else ("", "none", intro)


# Verdicts that say something about the replication result. cannot_be_determined,
# uninformative, descriptive and not_a_replication do not, so they never outvote a
# study that reached a verdict when several studies are aggregated onto one row.
_SUBSTANTIVE_OUTCOMES = {"successful", "failed", "mixed",
                         "statistically successful but flawed"}


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
    for fallback in ("uninformative", "descriptive only", "not_a_replication"):
        if fallback in outcomes:
            return fallback
    return "cannot_be_determined"


# Which value wins when two studies of one original were coded differently on an axis.
# Disagreement is itself the finding, and FLoRA's rule for a conflict is the value that
# records it: a robustness axis that held in one re-analysis and not in another IS a
# robustness challenge, and numbers that came out in one and not the other ARE
# computational issues.
_AXIS_CONFLICT = {"outcome_computation": "computational issues",
                  "outcome_robustness":  "robustness challenges"}

_AXIS_QUOTES = {"outcome_computation": ("outcome_computational_quote",
                                        "out_quote_computational_source"),
                "outcome_robustness":  ("outcome_robustness_quote",
                                        "out_quote_robust_source")}


def _aggregate_axes(members: list[dict]) -> dict:
    """The reproduction axes for several studies of ONE original, merged.

    Per axis: one settled value carries it, two settled values that disagree become the
    conflict value in _AXIS_CONFLICT, and an axis nothing settled stays unsettled. The
    quotes and their sources are joined with " | " in matching order, so a validator can
    still see which passage supported which member's verdict.
    """
    merged: dict = {}
    for axis, conflict in _AXIS_CONFLICT.items():
        values = [str(m.get(axis, "") or "") for m in members]
        settled = [v for v in values if v and v != "cannot_be_determined"]
        merged[axis] = (conflict if len(set(settled)) > 1
                        else settled[0] if settled
                        else next((v for v in values if v), ""))
        quote_col, source_col = _AXIS_QUOTES[axis]
        kept = [(str(m.get(quote_col, "") or "").strip(),
                 str(m.get(source_col, "") or "").strip())
                for m in members if str(m.get(quote_col, "") or "").strip()]
        merged[quote_col]  = " | ".join(q for q, _ in kept)
        merged[source_col] = " | ".join(src for _, src in kept)
    return merged


_CITED_NAME_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_CITED_NAME_STOPWORDS = {"and", "the", "et", "al", "study", "studies", "experiment",
                         "experiments", "claim"}


def _cited_surnames(pattern: dict) -> list[str]:
    """Every author surname the citation named, most significant first.

    `extract_author_year_patterns` reports ONE surname per match, and for a
    multi-author citation that is a run-on of all of them ("kaufmann,weber,andhaisley")
    or just the first ("jones" for "Jones and Macken (1995)"). Either way the other
    names are thrown away, and they are what makes a shortlist usable: measured
    2026-08-07, "jones 1995" matches 8,348 OpenAlex works and "jones AND macken 1995"
    matches 7, with the right paper third.

    Read off the matched span rather than the reported surname, because the span is
    the citation as the paper wrote it.
    """
    raw = str(pattern.get("raw") or pattern.get("surname") or "")
    # Only the part before the parenthesis: what follows the year inside it is the
    # venue or the study number — "Wilson et al. (2017, JPSP)" — and reading "jpsp" as
    # an author name would AND a word no author list carries into the query.
    names = [n.lower() for n in _CITED_NAME_RE.findall(raw.split("(")[0] or raw)]
    seen: list[str] = []
    for name in names:
        if name in _CITED_NAME_STOPWORDS or name in seen:
            continue
        seen.append(name)
    return seen or [str(pattern.get("surname") or "")]


def _title_searched_entry(target: dict, doi_r: str, context: dict) -> "dict | None":
    """An entry for a target the model NAMED but no keyed record could match.

    ONE pooled candidate list per target, from every search that can say anything
    about it, and ONE decision over that pool by the linking model.

    The two searches used to be exclusive: a description with a title in it went to
    the title search, one without went to the author-and-year query, and each
    mechanically dropped what its own guards doubted. That is the expensive way round.
    A candidate the model never sees can be neither confirmed nor disconfirmed, and a
    target left unresolved sends the whole work back to a worklist that pays for the
    ladder again — the abstract call, the reference list, the PDF. Asking the model to
    reject one bad candidate costs one field of one answer. So the pool is wide and
    the model, not a metadata rule, decides; the rules survive as FLAGS on the
    candidates they doubt.

    Measured across the dev iterations: switching a work between the two routes gained
    links and lost them in roughly equal numbers, because each route saw only its own
    half of what was findable.
    """
    named = str(target.get("target_as_named") or "").strip()
    cleaned = clean_citation_title(named)

    from extract.link_original import (citation_without_title, strip_citation_prefix,
                                       title_search_candidates)
    from shared.openalex_client import (author_year_candidates,
                                        extract_author_year_patterns)
    from shared.llm_client import pick_author_year_original

    patterns      = extract_author_year_patterns(named)
    cited_year    = str(patterns[0]["year"]) if patterns else ""
    cited_surnames = _cited_surnames(patterns[0]) if patterns else []

    pool: list[dict] = []
    unavailable = False
    asked: list[str] = []
    # How many works the searches matched, against how many the model was shown. The
    # author-and-year query is the only one that reports a population; a title search
    # returns its hits and nothing else, so on a title-only pool the two are equal.
    total = 0

    # The title search, whenever there is something that could be a title. A citation
    # with no title in it — "Ramscar et al. (2010)" — has nothing to search on, and
    # asking anyway costs two free-text queries at 10x a filter query each.
    if usable_title(cleaned) and not citation_without_title(named):
        # The replication's own title, not "": `title_search_candidates` refuses a hit
        # whose title is the replication's at Jaccard 0.9, and that check has never
        # once run because this call has never supplied one. Holdout wrong-settle
        # 2266446612 is exactly that class — a repository record titled all but
        # identically to the replication itself.
        hits, hits_unavailable = title_search_candidates(
            doi_r, named, str(context.get("title_r") or ""), cited_year,
            "|".join(cited_surnames))
        unavailable = unavailable or hits_unavailable
        asked.append(f"title:{strip_citation_prefix(named)[:50]!r}")
        pool.extend(hits)

    # The author-and-year query, whenever the citation names an author and a year AND
    # the title search has not already returned a candidate nothing doubts. Widening
    # the net where the net came up empty or flagged is the point; widening it over a
    # clean title hit buys a sibling-paper distractor and a second free-text query at
    # 10x a filter query. A 100-work sandbox run that asked both of everything
    # exhausted the OpenAlex daily budget outright (2026-08-07).
    clean_hit = any(not c.get("flags") for c in pool)
    if cited_surnames and cited_year and not clean_hit:
        found, oa_total, oa_unavailable = author_year_candidates(
            cited_surnames, int(cited_year),
            topic=f"{context.get('title_r') or ''} {context.get('abstract_r') or ''}")
        unavailable = unavailable or oa_unavailable
        total = max(total, oa_total)
        asked.append(f"authoryear:{'+'.join(cited_surnames)} {cited_year}")
        seen = {c.get("doi") or c.get("openalex_id") for c in pool}
        for c in found:
            # Filtered HERE rather than where the shortlist is fetched, because the
            # shortlist is cached: a rule applied at fetch time leaves every list
            # already on disk carrying what the rule now excludes, and only a cache
            # shape bump — which re-pays every query — would clear them.
            if non_article_doi(str(c.get("doi") or "")):
                continue
            if (c.get("doi") or c.get("openalex_id")) not in seen:
                # Same flag the title-hit builder carries (link_original): a
                # measure record by the right authors is usually not the article a
                # replication re-tests, but FLoRA's curated data does link a few, so
                # the model judges it rather than a rule dropping it.
                doubt = (["a PsycTESTS measure record, not the article itself"]
                         if psyctests_doi(str(c.get("doi") or "")) else [])
                pool.append({**c, "source": "openalex_authoryear", "flags": doubt})

    # The named string leads, then what was asked of it: the evidence line is what an
    # adjudication reads, and "authoryear:ramscar 2010" does not say which citation in
    # the paper it came from.
    attempt = {"named": named,
               "query": f"{named} -> {'; '.join(asked)}" if asked else named,
               "candidates": pool, "candidates_total": max(total, len(pool))}
    target["_search_attempt"] = attempt

    if not asked:
        attempt["outcome"] = "unsearchable"
        return None
    if unavailable:
        # A provider was silent, so the answer is incomplete however good the part in
        # hand. The row is written api_error, which a re-run reopens; settling on
        # incomplete evidence is not reversible and a re-run is nearly free.
        attempt["outcome"] = "unavailable"
        found = "; ".join(f"{c['source']}: {c['doi']}" for c in pool)
        return {"rank": 0, "doi": "", "title": "", "year": None, "first_author": "",
                "openalex_id": "", "study_number": "", "study_r": "",
                "evidence": ("the searches for the named target did not reach every "
                             "provider" + (f"; what did answer: {found}" if found else "")),
                "confidence": "low", "provisional": False, "outcome_block": {},
                "search_unavailable": True, "title_search_candidates": pool}
    if not pool:
        attempt["outcome"] = "no_candidates"
        return None

    verdict = pick_author_year_original(
        doi_r, str(context.get("title_r") or ""), str(context.get("abstract_r") or ""),
        named, str(target.get("evidence_quote") or ""), pool,
        attempt["candidates_total"])
    if verdict["llm_error"]:
        attempt["outcome"] = "unavailable"
        return {"rank": 0, "doi": "", "title": "", "year": None, "first_author": "",
                "openalex_id": "", "study_number": "", "study_r": "",
                "evidence": f"the pick over {len(pool)} candidates failed: "
                            f"{verdict['llm_error']}",
                "confidence": "low", "provisional": False, "outcome_block": {},
                "search_unavailable": True, "title_search_candidates": pool}

    pick = verdict["pick"]
    attempt["reasoning"] = verdict["reasoning"]
    if not pick or not verdict["confident"]:
        attempt["outcome"] = ("declined" if pick is None else "unconfident")
        return None

    attempt["outcome"] = "resolved"
    # Which search found the chosen candidate is what the row is filed under, so the
    # two resolvers' precision stays measurable apart even though one call decides.
    method = ("llm_author_year_search" if pick.get("source") == "openalex_authoryear"
              else "llm_title_search")
    evidence = str(target.get("evidence_quote") or "")
    note = (f"target named but unmatched to any record; picked by "
            f"{verdict['llm_model']} from {len(pool)} candidate(s) "
            f"({', '.join(sorted({str(c.get('source')) for c in pool}))}): "
            f"{verdict['reasoning']}. All considered: "
            + "; ".join(f"{c.get('source')}: {c.get('doi') or c.get('openalex_id')}"
                        for c in pool))
    return {
        "rank":         0,
        "doi":          pick.get("doi", ""),
        "title":        pick.get("title", ""),
        "year":         pick.get("year"),
        "first_author": pick.get("first_author", ""),
        "openalex_id":  pick.get("openalex_id", ""),
        "study_number": _clean_study_numbers(target.get("study_numbers", "")),
        "study_r":      _clean_study_numbers(target.get("replication_study_numbers", "")),
        "evidence":     f"{evidence} | {note}" if evidence else note,
        "confidence":   "low",
        "provisional":  True,
        "provisional_method": method,
        "outcome_block": target.get("outcome_block") or {},
        # Kept whole so a later evaluation can read what the alternatives were, not
        # just that there were some.
        "title_search_candidates": pool,
    }


def _target_entry(target: dict, doi_r: str, context: dict) -> "dict | None":
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
    if not record:
        # No record at ALL — the key namespace was empty, so the model could not have
        # matched anything however plainly it named the target. That is every OSF
        # registration and every URL-only row: OpenAlex returns no candidates and no
        # reference list for them. Dropping the target here wrote the work
        # `no_original_found`, which SETTLES, so a work whose original the model had
        # named in plain text ("Conceptual replication of Hyman & Sheatsley (1950)")
        # was closed permanently as though none existed. Search the name instead.
        #
        # Not reached when a record WAS offered and the model declined it: that is the
        # model judging the evidence, and second-guessing it by search is how a paper
        # gets linked to a landmark it merely cites.
        return _title_searched_entry(target, doi_r, context)
    if not target.get("match_certain"):
        return None

    raw_title = str(record.get("title") or "")
    cleaned   = clean_citation_title(raw_title)
    # Cleaning down to a fragment is no improvement: keep what the record carried so a
    # reviewer sees it, and let the guard reject the row on it. A short cleaned title
    # ("Nudge") is a title and is kept.
    title = raw_title if citation_fragment(cleaned) else cleaned

    doi = clean_doi(str(record.get("doi") or ""))
    provisional = False
    if not doi and usable_title(title):
        from shared.doi_verify import resolve_doi_by_metadata
        hit = resolve_doi_by_metadata(title, record.get("first_author", ""),
                                      record.get("year"), exclude_doi=doi_r)
        if hit:
            doi = clean_doi(str(hit.get("doi", "") or ""))
            provisional = bool(doi)
    if not doi and not record.get("openalex_id"):
        # Still nothing, and the record has no id of its own. This is a reference
        # GROBID parsed out of the PDF, and its title is routinely mangled — "Report of
        # an jnternship c:gndyctp1 lithe MemQrial Unjvmj'Y". `resolve_doi_by_metadata`
        # scores its candidates by TITLE SIMILARITY, so on a mangled title it cannot
        # succeed however well the paper is indexed. The author and the year survive
        # OCR better than the title does, so the pooled search gets a turn: it asks
        # CrossRef for that author in that year and lets the model judge the subject.
        named = " ".join(str(x) for x in (record.get("first_author"),
                                          f"({record.get('year')})" if record.get("year")
                                          else "", title) if x).strip()
        recovered = _title_searched_entry({**target, "target_as_named": named,
                                           "record": None}, doi_r, context)
        if recovered and recovered.get("doi"):
            return {**recovered,
                    "study_number": _clean_study_numbers(target.get("study_numbers", "")),
                    "study_r": _clean_study_numbers(
                        target.get("replication_study_numbers", "")),
                    "outcome_block": target.get("outcome_block") or {}}

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
        # A DOI settles identity whatever the title's length, so "Nudge" with a DOI
        # stays high — only the shape rule and a missing DOI demote.
        "confidence":   "high" if doi and not citation_fragment(title) else "low",
        "provisional":  provisional,
        # The outcome the call that named this target coded for it, in the same
        # reading. {} when the target came from a producer that codes no outcome.
        "outcome_block": target.get("outcome_block") or {},
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
            blocks = [m.get("outcome_block") or {} for m in members]
            if any(blocks):
                merged["outcome_block"] = {
                    **blocks[0],
                    "outcome": _aggregate_outcomes(
                        [str(b.get("outcome", "") or "") for b in blocks]),
                    **_aggregate_axes(blocks),
                }
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
                                  "not_a_replication", "screen_disagreement",
                                  "prescreen_discard"}:
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
        except (OpenAlexQuotaExhausted, _TitleSearchUnavailable):
            # Deliberate signals, not failures to absorb. Swallowed, they make this
            # guard read "no DOI exists for this title" from a search that never
            # ran — and step 3 below then writes no_doi (or target_pending) on a row
            # whose link was resolved, permanently, because nothing re-runs a row
            # that got a verdict. Let them reach the run loop.
            raise
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
            # The work id the row arrived with came from the REFERENCE RECORD, and the
            # DOI just recovered came from a title search of that record's title. They
            # need not describe the same work, and a row exposing a DOI for one and an
            # OpenAlex id for another sends a validator to two different papers.
            # `_fill_work_ids` refills the column from the DOI, but only when blank.
            row["oa_work_id_o"] = ""
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
        elif str(row.get("link_method", "")) in RESOLVED_LINK_METHODS:
            # No DOI, no OpenAlex id: the original has no identity anything downstream
            # can use. `resolved` asserts an IDENTIFIED original and its rows are the
            # ones the validation import takes — and a pair cannot be keyed on a title.
            # So the row keeps its link and stops claiming to be resolved.
            #
            # This is not a similarity test, deliberately. Measured over 277 correct
            # links on 2026-08-08, title Jaccard between an original and its
            # replication reaches 0.73 ("Rurality in England and Wales 1981" against
            # "…1991: A Replication and Extension"), while the two real self-links in
            # the samples scored 0.50 and 0.17 — one of them because OCR had mangled
            # the title it copied. Similarity does not separate the two classes at any
            # threshold. Identifiability does: the self-link that got through was a
            # full-text rung matching an OCR-garbled copy of the paper's own title,
            # with no DOI and no work id behind it.
            log.info("[%s] named original has neither a DOI nor a work id — kept, "
                     "flagged unidentified_original", doi_r)
            row["link_method"] = "unidentified_original"
            row["link_confidence"] = "low"
            note = ("the named original has no DOI and no OpenAlex id: kept for review "
                    "as an unidentified original rather than written resolved")
            prior = str(row.get("link_evidence", "") or "")
            row["link_evidence"] = f"{prior} | {note}" if prior else note
        return row

    return row


def _verify_row(row: dict) -> dict:
    """Verify/correct doi_o in a finished result row before it is written.

    Keeps pair_id and ref_o consistent when the DOI changes, downgrades
    link_confidence on mismatch, and appends the verification note to
    link_evidence.
    """
    if row.get("link_method") in VERIFICATION_SKIP_LINK_METHODS:
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
        # verify_and_correct found no better candidate IN A COMPLETED SEARCH — a
        # search that could not reach CrossRef or OpenAlex comes back "api_error"
        # instead and never reaches this branch, because a registry being down is
        # not evidence that no better candidate exists and must not cost the row its
        # DOI. Keeping it sends validators
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


def _sanitise_row(result_row: dict) -> dict:
    """Make a row's text safe to write. Free — no API call.

    This is the last point every written row passes through, so it is where the
    assertion that no float year escapes belongs (#140). The row builders normalise
    with year_str(); a raise here means a new write path bypassed them.
    """
    for key, val in result_row.items():
        if isinstance(val, str):
            # Replace control characters and problematic whitespace
            # but preserve newlines within fields (they'll be quoted)
            result_row[key] = val.replace('\x00', '').replace('\r', ' ')
    assert_no_float_years(result_row)
    return result_row


# The link methods the keyed-record check covers: an LLM accepted a keyed record.
# Rule resolutions get the standalone coder's target_check, and the pooled search
# picks were already adjudicated cold by pick_author_year_original — a second
# same-model pass over them was measured at zero value (0 flags on 200 fresh rows,
# both real wrongs passed; analysis/stage3_eval/model_triage_2026-08-08.md), so they
# are deliberately not re-checked here.
_KEYED_CONFIRM_METHODS = {"llm_fulltext", "llm_references", "llm_cited_candidates"}

# What llm_evidence appends after the quote itself, on "; " — see
# resolve_targets_and_outcomes' evidence_notes. Later run notes join on " | ".
_EVIDENCE_NOTE_RE = re.compile(r";\s*(unidentified=\d+|stated_count=.*)$")


def evidence_quote(link_evidence: str) -> str:
    """The quoted evidence out of a row's link_evidence, run notes stripped.

    Public because analysis/stage3_eval/keyed_confirm_eval.py rebuilds the check's
    inputs from stored rows with it — the measurement and the ladder must send the
    same prompt or the measurement is of something else.
    """
    quote = str(link_evidence or "").split(" | ")[0]
    return _EVIDENCE_NOTE_RE.sub("", quote).strip()


def _confirm_keyed_row(row: dict) -> dict:
    """Issue #186's Shape 1 on the keyed-record path: before an LLM-accepted keyed
    link is written, a separate call adjudicates the record cold against the study's
    abstract and the quoted evidence.

    The wrong entry picked from the right list is invisible to every other guard —
    `_verify_row` checks DOI-against-title, never title-against-target (work
    3124119366, linked to a margin-squeeze paper while its own evidence named
    Abel-Koch). The verdict decides:

    - plausible                → the row passes unchanged; nothing is written on it.
    - not plausible, confident → demoted to `keyed_link_disputed`: the link, the
      outcome and the check's reasoning are all KEPT, quarantined for a human —
      the disagreement is between two LLM readings, so neither answer is dropped.
    - not plausible, unsure    → flagged: confidence low, note on the evidence,
      link kept. An unconfident "no" removes nothing.
    - no answer                → `api_error`: an unchecked link must not settle
      permanently on a transient failure, and a re-run is near-free off the caches.

    Runs after `_verify_row` so it judges the record as corrected, and it is
    downstream of the targetoutcome cache — editing the confirm prompt re-decides
    rows without disturbing the pick's cached answer. It also runs after
    `--resolved-only`'s drops, which is fine where it matters: the tier — the one
    live caller — never sets that flag (`extract/tier.py` passes False), and a
    demotion here still carries a link, while `api_error` un-settling the work is
    correct whatever was filtered.

    Measured before wiring (analysis/stage3_eval/keyed_confirm_eval.py,
    2026-08-08): over all 63 keyed link rows in the four evaluation batches and
    the live re-extraction, the one known-wrong link was flagged, confidently, and
    none of the 62 correct ones — including the preprint-vs-published year gaps that
    sank the mechanical author/year check. That is adjudication against Crossref,
    not the issue's human-confirmed precision, and it is a first exercise, not a
    guarantee.
    """
    if str(row.get("link_method") or "") not in _KEYED_CONFIRM_METHODS:
        return row
    doi_o = clean_doi(str(row.get("doi_o") or ""))
    if not doi_o and not row.get("oa_work_id_o"):
        # Nothing identifiable to dispute; the unidentified_original path owns these.
        return row

    from shared.llm_client import confirm_keyed_original
    verdict = confirm_keyed_original(
        clean_doi(str(row.get("doi_r") or "")),
        str(row.get("title_r") or ""), str(row.get("abstract_r") or ""),
        evidence_quote(str(row.get("link_evidence") or "")),
        {"doi": doi_o, "title": str(row.get("title_o") or ""),
         "first_author": str(row.get("authors_o") or ""),
         "year": str(row.get("year_o") or ""),
         "openalex_id": str(row.get("oa_work_id_o") or "")})

    def _note(text: str) -> None:
        prior = str(row.get("link_evidence", "") or "")
        row["link_evidence"] = f"{prior} | {text}" if prior else text

    if verdict["plausible"] is None:
        log.warning("[%s] keyed-record check got no answer: %s",
                    row.get("doi_r"), verdict["llm_error"])
        row["link_method"] = "api_error"
        _note(f"keyed-record check got no answer ({verdict['llm_error']}); "
              "re-run decides")
        return row
    if verdict["plausible"]:
        return row
    if verdict["confident"]:
        log.info("[%s] keyed link disputed by %s: %s", row.get("doi_r"),
                 verdict["llm_model"], verdict["reasoning"])
        row["link_method"] = "keyed_link_disputed"
        row["link_confidence"] = "low"
        _note(f"keyed-record check ({verdict['llm_model']}): the linked record was "
              f"judged not to be the named target — {verdict['reasoning']}")
    else:
        row["link_confidence"] = "low"
        _note(f"keyed-record check ({verdict['llm_model']}), unconfident: "
              f"{verdict['reasoning']} — link kept, flagged")
    return row


def _finalise_row(result_row: dict) -> dict:
    """Verify doi_o, check an LLM-accepted keyed link, fill the work ids, and make
    the row's text safe to write.

    Split out of the write because it is the part that calls APIs: two lookups plus
    one short confirm call per row, which the workers must make on their own time
    rather than while holding the write lock the whole pool queues on.
    """
    return _sanitise_row(_fill_work_ids(_confirm_keyed_row(_verify_row(result_row))))




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
            # The axes are the reproduction half of the same verdict, and leaving the
            # first member's in place beside an aggregated `outcome` made the row
            # disagree with itself.
            first.update(_aggregate_axes(members))
        merged.append(first)
    return merged


def _per_target_rows(row: pd.Series, doi_r: str, link: dict, screen: "dict | None",
                     no_llm: bool, no_pdf: bool, resolved_only: bool) -> list[dict]:
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
    targets  = link.get("targets") or []
    # What the paper is ABOUT, for the resolvers that need to judge a candidate's
    # subject matter rather than match its title.
    context  = {"title_r": str(row.get("title_r", "") or ""),
                "abstract_r": str(row.get("abstract_r", "") or "")}
    recovered = link.get("_recovered_entry")
    resolved = ([(targets[0], recovered)] if recovered and len(targets) == 1
                else [(t, _target_entry(t, doi_r, context)) for t in targets])
    entries  = [e for _, e in resolved if e]
    # A target the title search recovered is no longer missing: it has a row, so
    # reporting it as unidentified would contradict the row written next to it.
    missing  = [str(t.get("target_as_named", "") or "") for t, e in resolved if not e]
    shortfall = len(missing) + int(link.get("unidentified_count") or 0)
    # Every title search this work ran, and what it got — kept whether or not any
    # target resolved, so a later resolver can be evaluated off stored rows.
    attempts = "; ".join(
        f"{a['outcome']}({a['query'][:60]!r})"
        for a in (t.get("_search_attempt") for t, _ in resolved) if a)
    link["search_attempts"] = attempts
    if attempts:
        # Onto the LINK, not just the rows written below: when NO target resolves,
        # this function returns [] and the work is written by the single-row path
        # further up, which never sees `resolved`. That is precisely the case worth
        # recording — the work settles `no_original_found` and the only way to tell
        # later whether a better resolver would have found it is to know what was
        # searched and that both providers disowned it.
        prior = str(link.get("llm_evidence", "") or "")
        note = f"title searches: {attempts}"
        link["llm_evidence"] = f"{prior} | {note}" if prior else note
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
    oa_id_r = str(row.get("openalex_id_r", "") or "")
    if not no_pdf and _has_document(doi_r, link, oa_id_r):
        _save_parse_cache(_cache_id(row, doi_r), doi_r, oa_id_r,
                          str(row.get("title_r", "") or ""))

    # An observation, not a prediction: the row count IS the match type.
    match_type = "multiple_original" if len(entries) > 1 else "single_original"
    link_model = str(link.get("llm_model", "") or "")

    rows: list[dict] = []
    for entry in entries:
        # A DOI the pipeline had to search for is provisional at ~50% precision — the
        # same rule _link_confidence applies on the single path, applied here rather
        # than writing a constant "high" onto a link nobody confirmed.
        link_method = ("api_error" if entry.get("search_unavailable")
                       else entry.get("provisional_method", "llm_title_search")
                       if entry["provisional"]
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
            if attempts:
                note += f" | searches: {attempts}"
            prior = str(result_row.get("link_evidence", "") or "")
            result_row["link_evidence"] = f"{prior} | {note}" if prior else note

        method = str(result_row["link_method"])
        if resolved_only and method in _NO_LINK_METHODS:
            log.debug("[%s] --resolved-only: skipping %s target row", doi_r, method)
            continue

        outcome = _outcome_without_coding(method, link)
        if outcome is None:
            # The call that named this target coded its outcome in the same reading;
            # a second call about the same evidence would only cost money to re-answer.
            outcome = entry.get("outcome_block") or _get_outcome(
                doi_r, row, link,
                no_llm=no_llm,
                screen=screen, original=entry)
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
        result_row["original_match_confidence"] = _match_confidence(result_row)
    return rows


def _observe_link(observed: "dict | None", link: dict) -> None:
    """Record what the ladder did with this row, for a caller that keeps a report.

    Everything here is already on the row or in the log; what it is not is
    *addressable*. The extract tier stores one result row per work in the state
    authority and its payload has to be self-sufficient — rebuildable with no
    network, no cache and no pool — and these are the facts about the RUN, as opposed
    to about the original, that the written rows do not carry: which rung answered,
    whether it accepted a single link, how many targets it named and how many it could
    not identify.

    An out-parameter rather than a return value because every producer below already
    returns the rows it wrote, and threading a second value through all of them would
    change five signatures to serve one caller. `None` — every caller but the tier —
    records nothing.
    """
    if observed is None:
        return
    observed.update({
        "link_method": _map_method(str(link.get("resolution_method", "") or "")),
        "target_stage": str(link.get("target_stage", "") or ""),
        "resolved": bool(link.get("resolved", False)),
        "n_targets": int(link.get("n_targets") or 0),
        "stated_count": link.get("stated_count"),
        "unidentified_count": int(link.get("unidentified_count") or 0),
        "link_llm_model": str(link.get("llm_model", "") or ""),
        "pdf_source": str(link.get("pdf_source", "") or ""),
        "parse_method": str(link.get("parse_method", "") or ""),
        "link_evidence": str(link.get("llm_evidence", "") or ""),
        "grobid_discussion": bool(link.get("grobid_discussion")),
        "discussion_provenance": str(link.get("discussion_provenance", "") or ""),
        "error": str(link.get("llm_error", "") or ""),
    })


def _resolve_and_code(doi_r: str, row: pd.Series, screen: "dict | None",
                      no_llm: bool, no_pdf: bool, resolved_only: bool,
                      observed: "dict | None" = None) -> list[dict]:
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
                       no_llm=no_llm, no_pdf=no_pdf, classification=screen,
                       record_type=_record_type(row, screen),
                       cache_id=_cache_id(row, doi_r))
    _observe_link(observed, link)

    if link.get("targets") and not link.get("resolved"):
        n_targets = int(link.get("n_targets") or 0)
        log.info("[%s] target prompt named %d original(s) without a single accepted "
                 "link — writing one row per target", doi_r, n_targets)
        rows = _per_target_rows(row, doi_r, link, screen, no_llm, no_pdf, resolved_only)
        # Re-observed: the title searches happen INSIDE _per_target_rows, so the
        # observation taken above predates them and would report the run without the
        # one thing that says what was tried for a work that resolved nothing.
        _observe_link(observed, link)
        if rows:
            return rows
        # Named, and not identified. That is not "this paper replicates nothing" — it
        # is "we know which paper it replicates and could not look it up", and the two
        # must not share an ending: `no_original_found` SETTLES and closes the work for
        # good. Falling through to the single-row path wrote exactly that, because the
        # ladder's resolution_method is still `llm_no_target`. Measured on the frozen
        # dev sample 2026-08-07: 24 of 100 works closed this way, every one of them
        # naming an author and a year that identify a single published paper.
        #
        # It covers every reason the identification failed — no record in the key
        # namespace, a title search both providers answered nothing to, a target
        # description with no searchable title in it — because none of them is
        # evidence about the original's existence.
        match_type = "multiple_original" if link.get("multi_target") else "single_original"
        pending = _empty_row(row, match_type, "low",
                             link_method="target_pending", screen=screen)
        # The facts about the RUN survive the ending, exactly as they did when this
        # row came out of the single-row path: which model named the targets, and
        # which tier and parser supplied the document it read them from. A reviewer
        # cannot judge a pending row whose provenance columns are blank, and a blank
        # pdf_source next to a full-text rung reads as a contradiction.
        searches = str(link.get("search_attempts") or "")
        note = (f"target prompt named {n_targets} original(s); "
                "none could be matched to a record"
                + (f" | searches: {searches}" if searches else ""))
        prior = str(link.get("llm_evidence", "") or "")
        pending.update({
            **_provenance(link),
            "link_llm_model": str(link.get("llm_model", "") or ""),
            "link_evidence": f"{prior} | {note}" if prior else note,
        })
        return [] if resolved_only else [pending]

    # `no_original_found` claims the paper re-tests nothing identifiable, and it CLOSES
    # the work. Before making that claim on a paper whose own text cites somebody, the
    # citation is looked up and the model is asked about it. Not vetoed — a veto would
    # hold open every paper that says "we replicate X because Smith (2010) argued
    # replications matter", where Smith is an aside and not the target. Retrieved and
    # CHECKED: if the model recognises the cited work as the original, the paper has a
    # link; if it declines, `no_original_found` stands and is now supported by an
    # answer rather than by one reader's silence.
    #
    # Holdout 3, work 6925248538: closed `no_original_found` with "the study by Sela et
    # al. investigating the effect of assortment size on option choice" in its own
    # abstract, having named no target at all.
    if (not link.get("targets")
            and _map_method(str(link.get("resolution_method", "") or ""))
                == "no_original_found"):
        from shared.openalex_client import extract_author_year_patterns
        cited = extract_author_year_patterns(
            f"{row.get('title_r', '')}\n{row.get('abstract_r', '')}")
        if cited:
            context = {"title_r": str(row.get("title_r", "") or ""),
                       "abstract_r": str(row.get("abstract_r", "") or "")}
            probe = {"key": None, "match_certain": True, "record": None,
                     "study_numbers": "", "replication_study_numbers": "",
                     "target_as_named": cited[0]["raw"],
                     "evidence_quote": str(link.get("llm_evidence", "") or "")}
            entry = _title_searched_entry(probe, doi_r, context)
            attempt = probe.get("_search_attempt") or {}
            if entry and entry.get("doi"):
                log.info("[%s] the target prompt named nothing; %s was looked up and "
                         "the model accepted it", doi_r, cited[0]["raw"])
                link = {**link, "resolved": False, "targets": [{**probe, "record": None}],
                        "n_targets": 1, "multi_target": False,
                        "_recovered_entry": entry}
                rows = _per_target_rows(row, doi_r, link, screen, no_llm, no_pdf,
                                        resolved_only)
                if rows:
                    return rows
            else:
                # Asked and answered: the model was shown what the paper cites and did
                # not recognise it as the original. The verdict stands, and the row now
                # records the question rather than only the silence.
                link = {**link, "llm_evidence": " | ".join(filter(None, [
                    str(link.get("llm_evidence", "") or ""),
                    f"named no target; the paper cites {cited[0]['raw']}, looked up and "
                    f"not accepted ({attempt.get('outcome', 'no answer')})"]))}

    # original_match_confidence is an observation about the answer, not a prediction
    # made before it, and it is settled AFTER the guard by the same `_match_confidence`
    # rule the per-target path uses — the value passed into _merge_row here is only a
    # placeholder the line below overwrites.
    result_row = _guard_original_link(
        _merge_row(row, link, {}, "single_original", "low", 1, 1, "",
                   screen=screen))
    result_row["original_match_confidence"] = _match_confidence(result_row)
    link_method = str(result_row.get("link_method", ""))
    if resolved_only and link_method in _NO_LINK_METHODS:
        log.debug("[%s] --resolved-only: skipping %s row", doi_r, link_method)
        return []

    outcome = _outcome_without_coding(link_method, link)
    if outcome is None:
        # An LLM rung that accepted this link coded its outcome in the same reading.
        # Only a link no LLM chose — a deterministic rule's — is coded on its own.
        outcome = link.get("outcome_block") or {}
        if not outcome:
            oa_id_r = str(row.get("openalex_id_r", "") or "")
            if not no_pdf and _has_document(doi_r, link, oa_id_r):
                _save_parse_cache(_cache_id(row, doi_r), doi_r, oa_id_r,
                                  str(row.get("title_r", "") or ""))
            outcome = _get_outcome(doi_r, row, link,
                                   no_llm=no_llm,
                                   screen=screen)
    return [_apply_outcome(result_row, outcome)]


def _screen_from_row(filter_row: pd.Series) -> "dict | None":
    """The screen's verdict as `classify_replication()` shaped it, from the CSV.

    The two-voter front door runs in Stage 2 (`filter/engine/tiers.py`), and its
    answer arrives on the row in `SCREEN_COLS`. This rebuilds the dict the rest of
    Stage 3 already speaks, so nothing below this line knows the screen moved:
    `_record_type()` reads `record_type`, `_front_door_row()` reads
    `screen_verdict` and the votes, `screen_references_with_llm()` reads
    `screen_classification` to decide whether to pick a target at all, and the
    pre-PDF title-search rung reads each voter's classification AND confidence.

    Returns None when the row carries no verdict — a blank `screen_verdict` — which
    is a different statement from a verdict of "proceed" and is answered separately
    by the caller.

    `screen_votes` is `<model>=<classification>/<confident|unconfident>`, |-joined
    in call order. The model, not the provider, because the model is what the
    verdict rows record and what the generation is keyed on; the provider is
    derived from it for the evidence strings a reviewer reads.
    """
    verdict = str(filter_row.get("screen_verdict", "") or "").strip()
    if not verdict:
        return None

    votes: list[dict] = []
    for part in str(filter_row.get("screen_votes", "") or "").split("|"):
        if "=" not in part:
            continue
        model, _, answer = part.partition("=")
        classification, _, confidence = answer.rpartition("/")
        votes.append({
            "provider":       provider_for(model.strip()),
            "model":          model.strip(),
            "classification": classification.strip().lower(),
            "confident":      confidence.strip() == "confident",
            "categories":     [],
            "reasoning":      "",
        })

    record_type = str(filter_row.get("screen_record_type", "") or "").strip()
    labels = {v["classification"] for v in votes}
    categories = [c for c in str(filter_row.get("screen_categories", "") or "").split("|")
                  if c]
    return {
        "resolution_method": "llm_refscreen_declined",
        "screen_verdict": verdict,
        "screen_classification": (record_type or
                                  (labels.pop() if len(labels) == 1 else "unclear")),
        "record_type": record_type,
        "categories": categories,
        "votes": votes,
        "llm_source": "+".join(v["provider"] for v in votes),
        "llm_model": "+".join(v["model"] for v in votes),
        "llm_evidence": str(filter_row.get("screen_evidence", "") or ""),
        "llm_reasoning": str(filter_row.get("screen_reasoning", "") or ""),
        "llm_prompt": "", "llm_error": "",
    }


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


def _resolve_missing_doi(row: pd.Series, doi_r: str) -> "tuple[pd.Series, str]":
    """A DOI for a row that arrived without one, resolved from its URL.

    Pool rows are admitted on their OpenAlex id, and 416 of the 2026-08-05
    handoff's 1,614 rows carry no DOI — repository handles and working-paper URLs
    instead. Everything below (candidate lookups, PDF acquisition, the caches, the
    self-link guard) keys on the DOI, so a resolvable one is worth one lookup
    before the row is processed. Both runners go through here: the CSV runner
    (`_run_row`) and the extract tier's judge, which otherwise skipped it and sent
    blank-DOI rows down the whole ladder.
    """
    if doi_r:
        return row, doi_r
    url_r = str(row.get("url_r", "") or "").strip()
    if not url_r:
        return row, doi_r
    from shared.openalex_client import resolve_doi_from_url
    resolved = resolve_doi_from_url(url_r)
    if resolved:
        log.info("[url:%s] resolved DOI %s — using for extraction", url_r[:60], resolved)
        row = row.copy()
        row["doi_r"] = resolved
        return row, resolved
    log.info("[url:%s] could not resolve DOI — will extract from URL/abstract only",
             url_r[:60])
    return row, doi_r


def _process_row(row: pd.Series, doi_r: str, no_llm: bool, no_pdf: bool,
                 no_reproductions: bool,
                 resolved_only: bool,
                 observed: "dict | None" = None) -> list[dict]:
    """Every row the pipeline writes for one input row.

    Front door, then the resolution ladder — there is no router in front of it any
    more: how many originals a paper targets is what the target prompt answers, not
    something a cheaper call predicts from the abstract. An empty list means the row
    is not written at all — either a flag suppressed it or --resolved-only discarded
    it.

    *observed* is an optional dict this fills in with what the RUN did — which rung
    answered, whether it accepted a single link, how many targets it named, what
    stopped it. Nothing here reads it and `None` records nothing; it exists because
    `extract/tier.py` stores one result row per work and its payload has to say what
    happened, not only what was written (`_observe_link`).
    """
    if observed is not None:
        # An exit that never reaches the ladder still has to say so, so the fields
        # exist before the first `return` rather than only where one is filled in.
        observed.setdefault("link_method", "")
        observed.setdefault("target_stage", "")
        observed.setdefault("resolved", False)
        observed.setdefault("n_targets", 0)
        observed.setdefault("error", "")
    # Ahead of the front door: a run that is not coding reproductions should not pay
    # to screen them either. The type this reads is Stage 2's, the only one there is
    # before the screen speaks.
    if no_reproductions and str(row.get("filter_status", "")) == "reproduction":
        log.info("[%s] --no-reproductions: writing target_pending", doi_r)
        return [_empty_row(row, "single_original", "low",
                           link_method="target_pending")]

    # No cheap pre-screen here. That tier is Stage 2's `screen_cheap` pile
    # (filter/engine/tiers.py): which rows get it is decided by the rule book, so a
    # row arriving here has already been routed past it. Gating it globally from
    # Stage 3 would have re-applied the cheap gate to rows the rule book sent to the
    # expensive tier.

    # ── Front door: is this a replication at all? ────────────────────────
    # READ, not asked. The two-voter screen is Stage 2's `screen_expensive` tier and
    # its verdict arrives on the row (SCREEN_COLS). It used to run here too, on the
    # same cache key — free on the second pass, but two copies of one decision, and
    # only one of them could be claimed, budget-gated or recorded as evidence.
    #
    # 58% of screened rows are discarded, and the handoff leaves those out entirely,
    # so most rows arriving here read "proceed". The verdict is still threaded into
    # run_for_doi, where Stage 4.5 picks a target without voting.
    screen = _screen_from_row(row)
    if screen is None and not no_llm:
        # The row carries no verdict. It is not extractable: nothing has said this is
        # a replication, and guessing is what the screen exists to stop.
        # target_pending, because a re-run after Stage 2 has screened the work decides
        # it — the same ending an incomplete screen gets. There is no in-Stage-3
        # fallback screen any more: a work reaches this function through the extract
        # tier, whose worklist only offers works a live screen verdict admitted.
        log.info("[%s] no screen verdict on the input row — writing target_pending "
                 "(screen it in Stage 2)", doi_r)
        return [_empty_row(row, "single_original", "low",
                           link_method="target_pending",
                           error="no screen verdict: the row carries no "
                                 "screen_verdict column value")]
    if screen is not None:
        done = _front_door_row(row, screen)
        if done is not None:
            log.info("[%s] front-door screen: %s", doi_r, done["link_method"])
            return [done]
        # filter_status is the paper-type field (issue #93), so a screen that
        # said what the paper is overwrites it. A gate that proceeded without a
        # qualifying vote (unclear/unclear, or an unconfident none against an
        # unconfident qualifying answer) said nothing, and the row keeps whatever
        # Stage 2 left — a needs_review row stays needs_review, waits for a human
        # on the check page, and is not pushed for validation.
        if screen.get("record_type"):
            row["filter_status"] = screen["record_type"]
            row["filter_method"] = "screen"   # the screen decided the type

    try:
        return _resolve_and_code(
            doi_r, row, screen=screen, no_llm=no_llm, no_pdf=no_pdf,
            resolved_only=resolved_only,
            observed=observed)
    except (OpenAlexQuotaExhausted, TokenBudgetExhausted):
        # Not a per-row failure: the row was never examined, and writing it as
        # api_error would bury the reason the rest of the run stops too.
        raise
    except Exception as e:
        # log.exception, not log.error: the message alone does not say which call
        # raised, and the traceback is the only thing that does.
        log.exception("[%s] extraction failed: %s", doi_r, e)
        if observed is not None:
            observed["error"] = f"{type(e).__name__}: {e}"
        # The screen already ran and was paid for — the row keeps the type and the
        # categories it bought, and the exception is written onto the row rather
        # than only into a log that will not outlive the run.
        return [_empty_row(row, "single_original", "low",
                           link_method="api_error", screen=screen,
                           error=f"extraction failed: {type(e).__name__}: {e}")]




def main() -> int:
    """The retirement notice. `python -m extract.run_extract` used to be Stage 3."""
    print(
        "the CSV runner is retired: use python -m extract.tier "
        "(see docs/cli-reference.md); the legacy runner is parked on wip/csv-runner"
    )
    return 2


if __name__ == "__main__":
    import sys

    sys.exit(main())
