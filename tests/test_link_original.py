"""Tests for citation-context extraction and Stage 4.5 screen routing in
extract/link_original.py."""
from unittest.mock import patch

import pandas as pd

import extract.link_original as link_original
from extract.link_original import _extract_cit_contexts, run_for_doi
from extract.run_extract import _map_method


class TestExtractCitContexts:
    def test_narrative_citation_detected(self):
        """The old local regex only matched fully-parenthetical citations like
        '(Antle, 2010)' and missed narrative citations like 'Kim et al. (2014)' —
        this was the confirmed root cause of the aepp.13320 wrong-original-link bug."""
        text = "In this paper, we replicate Kim et al. (2014) who study downside risk."
        results = _extract_cit_contexts(text)
        assert any(r["surnames"] == ["kim"] and r["year"] == 2014 for r in results)

    def test_parenthetical_citation_still_detected(self):
        text = "We compare our results to the partial moments model (Antle, 2010)."
        results = _extract_cit_contexts(text)
        assert any(r["surnames"] == ["antle"] and r["year"] == 2010 for r in results)

    def test_both_narrative_and_parenthetical_present(self):
        """Reconstructs the real aepp.13320 case: the true target is cited
        narratively, a secondary comparison is cited parenthetically — both must
        be extractable so the resolver can score and pick the right one."""
        text = (
            "we replicate Kim et al. (2014) who perform a quantile moments-based "
            "analysis. We compare to the partial moments model (Antle, 2010)."
        )
        results = _extract_cit_contexts(text)
        surnames_years = {(tuple(r["surnames"]), r["year"]) for r in results}
        assert (("kim",), 2014) in surnames_years
        assert (("antle",), 2010) in surnames_years

    def test_journal_hint_extracted_from_parenthetical(self):
        text = ("This builds on prior work (Antle, 2010, American Journal of "
                 "Agricultural Economics).")
        results = _extract_cit_contexts(text)
        match = next(r for r in results if r["surnames"] == ["antle"])
        assert "American Journal of Agricultural Economics" in match["journal"]

    def test_no_journal_when_absent(self):
        text = "We compare our results to the partial moments model (Antle, 2010)."
        results = _extract_cit_contexts(text)
        match = next(r for r in results if r["surnames"] == ["antle"])
        assert match["journal"] == ""

    def test_multi_author_surnames_all_preserved(self):
        text = "Jones and Smith (2015) found similar effects in a related domain."
        results = _extract_cit_contexts(text)
        match = next(r for r in results if r["year"] == 2015)
        assert set(match["surnames"]) == {"jones", "smith"}

    def test_no_citation_returns_empty(self):
        assert _extract_cit_contexts("No citations appear in this sentence at all.") == []


# ── Stage 4.5 screen routing ─────────────────────────────────────────────────

def _screen_result(**over) -> dict:
    base = {
        "resolved": False, "resolution_method": "llm_refscreen_declined",
        "resolved_doi_o": "", "resolved_title_o": "", "resolved_year_o": None,
        "resolved_author_o": "", "resolution_score": 0.0,
        "is_replication": "unclear", "models_agree": False, "votes": [],
        "llm_confidence": "", "classification_confidence": "",
        "target_description": "",
        "llm_source": "", "llm_model": "", "llm_evidence": "",
        "llm_reasoning": "", "llm_prompt": "", "llm_error": "",
    }
    base.update(over)
    return base


def _run_to_screen(screen: dict) -> dict:
    """Drive run_for_doi to the Stage 4.5 screen and return its output row.

    The abstract carries no author-year citation, so stages 2.5-4 all decline and
    the screen is the first thing that can fire.
    """
    cands_df = pd.DataFrame([{
        "doi_r": "10.1/rep", "study_r": "A study", "abstract_r": "No citations here.",
        "year_r": "2020", "openalex_id_r": "W1", "url_r": "",
        "author_year_pattern_r": "",
    }])
    with patch.object(link_original, "find_all_candidates", return_value=[]), \
         patch.object(link_original, "fetch_referenced_works_metadata", return_value=[]), \
         patch.object(link_original, "fetch_opencitations_references", return_value=[]), \
         patch.object(link_original, "screen_references_with_llm", return_value=screen), \
         patch.object(link_original, "acquire_pdf",
                      side_effect=AssertionError("must not reach the PDF stage")):
        return run_for_doi("10.1/rep", cands_df=cands_df)


class TestScreenRouting:
    def test_one_vote_is_target_pending_not_a_disagreement(self):
        """A single surviving vote is a provider outage. Filing it as a two-model
        disagreement both misroutes the row and inflates the disagreement rate."""
        row = _run_to_screen(_screen_result(
            resolution_method="llm_refscreen_partial",
            llm_error="classifier failed: openai",
            llm_model="gemini-light",
            votes=[{"provider": "gemini", "is_replication": "no",
                    "confidence": "high", "reasoning": "r"}]))

        assert row["resolution_method"] == "llm_refscreen_partial"
        assert _map_method(row["resolution_method"]) == "target_pending"
        assert row["llm_model"] == "gemini-light"
        assert "openai" in row["llm_error"]

    def test_no_votes_is_an_api_error(self):
        row = _run_to_screen(_screen_result(
            resolution_method="llm_refscreen_failed",
            llm_error="classifier failed: gemini, openai"))

        assert row["resolution_method"] == "llm_refscreen_failed"
        assert _map_method(row["resolution_method"]) == "api_error"

    def test_discarded_row_carries_the_screen_attribution(self):
        """not_a_replication rows are quarantined for review, so they must record
        which models decided it, on what evidence, with what reasoning."""
        row = _run_to_screen(_screen_result(
            is_replication="no", models_agree=True, classification_confidence="high",
            llm_model="gemini-light+gpt-mini", llm_source="gemini+openai",
            llm_evidence="not a replication of anything",
            llm_reasoning="gemini: unrelated | openai: unrelated",
            votes=[{"provider": "gemini", "is_replication": "no",
                    "confidence": "high", "reasoning": "unrelated"},
                   {"provider": "openai", "is_replication": "no",
                    "confidence": "high", "reasoning": "unrelated"}]))

        assert _map_method(row["resolution_method"]) == "not_a_replication"
        assert row["llm_model"] == "gemini-light+gpt-mini"
        assert row["llm_evidence"] == "not a replication of anything"
        assert "gemini: unrelated" in row["llm_reasoning"]

    def test_disagreement_row_carries_the_screen_attribution(self):
        row = _run_to_screen(_screen_result(
            is_replication="unclear", models_agree=False,
            llm_model="gemini-light+gpt-mini", llm_source="gemini+openai",
            llm_evidence="the abstract says replication",
            llm_reasoning="gemini: yes | openai: no",
            votes=[{"provider": "gemini", "is_replication": "yes",
                    "confidence": "high", "reasoning": "yes"},
                   {"provider": "openai", "is_replication": "no",
                    "confidence": "high", "reasoning": "no"}]))

        assert _map_method(row["resolution_method"]) == "screen_disagreement"
        assert row["llm_model"] == "gemini-light+gpt-mini"
        assert "gemini=yes/high" in row["llm_evidence"]
        assert "openai=no/high" in row["llm_evidence"]
        assert "the abstract says replication" in row["llm_evidence"]


# ── Stage 4.6 title-search gate (audit D2) ───────────────────────────────────

def _run_to_title_search(screen: dict, hit: "dict | None" = None) -> tuple[dict, object]:
    """Drive run_for_doi past the screen with the title search stubbed.

    acquire_pdf returns an empty acquisition so a row that gets past the screen
    without a title-search hit ends at no_fulltext_available instead of exploding.
    """
    cands_df = pd.DataFrame([{
        "doi_r": "10.1/rep", "study_r": "A study", "abstract_r": "No citations here.",
        "year_r": "2020", "openalex_id_r": "W1", "url_r": "",
        "author_year_pattern_r": "",
    }])
    with patch.object(link_original, "find_all_candidates", return_value=[]), \
         patch.object(link_original, "fetch_referenced_works_metadata", return_value=[]), \
         patch.object(link_original, "fetch_opencitations_references", return_value=[]), \
         patch.object(link_original, "screen_references_with_llm", return_value=screen), \
         patch.object(link_original, "_search_title_for_original",
                      return_value=hit) as search, \
         patch.object(link_original, "acquire_pdf",
                      return_value={"pdf_path": None, "openalex_xml": None,
                                    "pdf_source": "none", "pdf_url": "",
                                    "pdf_ok": False, "pdf_url_tried": []}):
        return run_for_doi("10.1/rep", cands_df=cands_df), search


def _yes_screen(confidence: str) -> dict:
    return _screen_result(
        is_replication="yes", models_agree=True,
        classification_confidence=confidence,
        target_description="Smith (2010), Time flies from left to right",
        votes=[{"provider": "gemini", "is_replication": "yes",
                "confidence": confidence, "reasoning": "r"},
               {"provider": "openai", "is_replication": "yes",
                "confidence": confidence, "reasoning": "r"}])


_HIT = {
    "resolved": True, "resolution_method": "llm_title_search_prepdf",
    "resolved_doi_o": "10.9/orig", "resolved_title_o": "Time flies from left to right",
    "resolved_year_o": 2010, "resolved_author_o": "Smith", "resolution_score": 1.0,
}


class TestTitleSearchGate:
    """The title search is the one resolver that matches against the whole
    literature rather than a supplied candidate list, at ~50% measured precision.
    It may only spend its two searches on a screen both models called high."""

    def test_high_confidence_screen_runs_the_search(self):
        row, search = _run_to_title_search(_yes_screen("high"), hit=_HIT)
        assert search.called
        assert row["resolution_method"] == "llm_title_search_prepdf"
        assert row["resolved_doi_o"] == "10.9/orig"

    def test_medium_confidence_screen_does_not_search(self):
        row, search = _run_to_title_search(_yes_screen("medium"), hit=_HIT)
        assert not search.called
        assert row["resolved_doi_o"] == ""
        assert _map_method(row["resolution_method"]) == "target_pending"

    def test_low_confidence_screen_does_not_search(self):
        _, search = _run_to_title_search(_yes_screen("low"), hit=_HIT)
        assert not search.called

    def test_missing_target_description_does_not_search(self):
        screen = _yes_screen("high")
        screen["target_description"] = ""
        _, search = _run_to_title_search(screen, hit=_HIT)
        assert not search.called


# ── Abstract-stage LLM: self-link exclusion (audit B9) ───────────────────────

class TestAbstractStageExcludeDoi:
    def test_abstract_llm_receives_the_real_doi_r(self):
        """identify_original_with_llm uses its doi_r argument as exclude_doi.

        A suffixed key ("<doi>_abstract") never equals a real DOI, so the "never
        link a paper to itself" exclusion could not fire on this path at all.
        """
        cands_df = pd.DataFrame([{
            "doi_r": "10.1/rep", "study_r": "A replication",
            "abstract_r": "We replicate Smith (2010).",
            "year_r": "2020", "openalex_id_r": "W1", "url_r": "",
            "author_year_pattern_r": "",
        }])
        candidates = [{"doi": "10.9/orig", "title": "Original", "year": 2010,
                       "first_author": "Smith"}]
        with patch.object(link_original, "find_all_candidates", return_value=candidates), \
             patch.object(link_original, "_resolve_by_title_pattern", return_value=None), \
             patch.object(link_original, "_resolve_rule_based",
                          return_value={"resolved": False,
                                        "resolution_method": "needs_fulltext"}), \
             patch.object(link_original, "identify_original_with_llm",
                          return_value={"resolved": False,
                                        "resolution_method": "llm_no_target",
                                        "llm_source": "gemini"}) as llm, \
             patch.object(link_original, "fetch_referenced_works_metadata", return_value=[]), \
             patch.object(link_original, "fetch_opencitations_references", return_value=[]), \
             patch.object(link_original, "screen_references_with_llm",
                          return_value=_screen_result()), \
             patch.object(link_original, "acquire_pdf",
                          return_value={"pdf_path": None, "openalex_xml": None,
                                        "pdf_source": "none", "pdf_url": "",
                                        "pdf_ok": False, "pdf_url_tried": []}):
            run_for_doi("10.1/rep", cands_df=cands_df)

        assert llm.called
        assert llm.call_args_list[0].args[0] == "10.1/rep"
