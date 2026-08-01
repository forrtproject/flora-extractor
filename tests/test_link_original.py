"""Tests for citation-context extraction and Stage 4.5 screen routing in
extract/link_original.py."""
import json
from unittest.mock import patch

import pandas as pd

import extract.link_original as link_original
from extract.link_original import _extract_cit_contexts, run_for_doi
from extract.run_extract import _map_method
from shared.prompts import TARGET_INTRO_CHARS, build_target_prompt


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
        "screen_verdict": "proceed", "screen_classification": "unclear",
        "record_type": "", "categories": [], "votes": [],
        "llm_confidence": "", "target_description": "",
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
            screen_verdict="",
            votes=[{"provider": "gemini", "classification": "none",
                    "confident": True, "categories": [], "reasoning": "r"}]))

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
            screen_verdict="discard", screen_classification="none",
            llm_model="gemini-light+gpt-mini", llm_source="gemini+openai",
            llm_evidence="not a replication of anything",
            llm_reasoning="gemini: unrelated | openai: unrelated",
            votes=[{"provider": "gemini", "classification": "none",
                    "confident": True, "categories": [], "reasoning": "unrelated"},
                   {"provider": "openai", "classification": "none",
                    "confident": True, "categories": [], "reasoning": "unrelated"}]))

        assert _map_method(row["resolution_method"]) == "not_a_replication"
        assert row["llm_model"] == "gemini-light+gpt-mini"
        assert "gemini=none/confident" in row["llm_evidence"]
        assert "openai=none/confident" in row["llm_evidence"]
        assert "not a replication of anything" in row["llm_evidence"]
        assert "gemini: unrelated" in row["llm_reasoning"]

    def test_a_confident_split_proceeds_instead_of_being_set_aside(self):
        """There is no screen_disagreement terminal state any more: a confident
        none against a confident qualifying answer goes down the ladder."""
        row, _ = _run_to_title_search(_screen_result(
            screen_verdict="proceed", screen_classification="replication",
            record_type="replication",
            votes=[{"provider": "gemini", "classification": "replication",
                    "confident": True, "categories": [], "reasoning": "yes"},
                   {"provider": "openai", "classification": "none",
                    "confident": True, "categories": [], "reasoning": "no"}]))

        # It escalated past the screen instead of terminating there.
        assert _map_method(row["resolution_method"]) != "screen_disagreement"
        assert _map_method(row["resolution_method"]) == "target_pending"


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


def _yes_screen(v1_confident: bool = True, v2_confident: bool = True,
                v2_class: str = "replication") -> dict:
    return _screen_result(
        screen_verdict="proceed", screen_classification="replication",
        record_type="replication",
        target_description="Smith (2010), Time flies from left to right",
        votes=[{"provider": "gemini", "classification": "replication",
                "confident": v1_confident, "categories": [], "reasoning": "r"},
               {"provider": "openai", "classification": v2_class,
                "confident": v2_confident, "categories": [], "reasoning": "r"}])


_HIT = {
    "resolved": True, "resolution_method": "llm_title_search_prepdf",
    "resolved_doi_o": "10.9/orig", "resolved_title_o": "Time flies from left to right",
    "resolved_year_o": 2010, "resolved_author_o": "Smith", "resolution_score": 1.0,
}


class TestTitleSearchGate:
    """The title search is the one resolver that matches against the whole
    literature rather than a supplied candidate list, at ~50% measured precision.
    It may only spend its two searches when BOTH voters gave a qualifying answer
    and BOTH stood behind it."""

    def test_both_voters_qualifying_and_confident_runs_the_search(self):
        row, search = _run_to_title_search(_yes_screen(), hit=_HIT)
        assert search.called
        assert row["resolution_method"] == "llm_title_search_prepdf"
        assert row["resolved_doi_o"] == "10.9/orig"

    def test_an_unconfident_voter_does_not_search(self):
        row, search = _run_to_title_search(_yes_screen(v2_confident=False), hit=_HIT)
        assert not search.called
        assert row["resolved_doi_o"] == ""
        assert _map_method(row["resolution_method"]) == "target_pending"

    def test_an_unconfident_first_voter_does_not_search(self):
        _, search = _run_to_title_search(_yes_screen(v1_confident=False), hit=_HIT)
        assert not search.called

    def test_a_non_qualifying_voter_does_not_search(self):
        _, search = _run_to_title_search(_yes_screen(v2_class="unclear"), hit=_HIT)
        assert not search.called

    def test_missing_target_description_does_not_search(self):
        screen = _yes_screen()
        screen["target_description"] = ""
        _, search = _run_to_title_search(screen, hit=_HIT)
        assert not search.called


# ── Abstract-stage LLM: self-link exclusion (audit B9) ───────────────────────

class TestAbstractStageExcludeDoi:
    def test_abstract_llm_receives_the_real_doi_r(self):
        """identify_targets_with_llm uses its doi_r argument as exclude_doi.

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
             patch.object(link_original, "identify_targets_with_llm",
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


def _run_to_fulltext(abstract_r: str, parsed: dict) -> tuple[dict, dict]:
    """Drive run_for_doi to the full-text rung and return (row, LLM kwargs).

    The parsers' output is *parsed*; the returned kwargs are what run_for_doi handed
    to identify_targets_with_llm, which passes them straight to build_target_prompt —
    so a test can render the prompt the model actually got.
    """
    cands_df = pd.DataFrame([{
        "doi_r": "10.1/rep", "study_r": "A study", "abstract_r": abstract_r,
        "year_r": "2020", "openalex_id_r": "W1", "url_r": "",
        "author_year_pattern_r": "",
    }])
    with patch.object(link_original, "find_all_candidates", return_value=[]), \
         patch.object(link_original, "fetch_referenced_works_metadata", return_value=[]), \
         patch.object(link_original, "fetch_opencitations_references", return_value=[]), \
         patch.object(link_original, "screen_references_with_llm",
                      return_value=_screen_result(screen_verdict="proceed",
                                                  screen_classification="replication",
                                                  record_type="replication")), \
         patch.object(link_original, "acquire_pdf",
                      return_value={"pdf_path": "/tmp/x.pdf", "openalex_xml": None,
                                    "pdf_source": "unpaywall", "pdf_url": "u",
                                    "pdf_ok": True, "pdf_url_tried": []}), \
         patch.object(link_original, "_parse_all", return_value={"grobid": parsed}), \
         patch.object(link_original, "_write_parse_cache"), \
         patch.object(link_original, "identify_targets_with_llm",
                      return_value={"resolved": False,
                                    "resolution_method": "llm_no_target",
                                    "llm_source": "gemini"}) as llm:
        row = run_for_doi("10.1/rep", cands_df=cands_df)
    return row, llm.call_args.kwargs


class TestStoredEvidenceMatchesThePrompt:
    """The row is what a reviewer reads instead of the paper. Storing more than the
    model was sent claimed an abstract the answer cannot rest on, and storing less
    hid the evidence behind it."""

    _PARSED = {"source": "grobid", "abstract": "PDF abstract. Extra sentence.",
               "intro": "i" * 5000, "references": []}

    def test_sections_are_stored_as_the_prompt_carries_them(self):
        row, kwargs = _run_to_fulltext("OpenAlex abstract.", self._PARSED)
        prompt = build_target_prompt("A study", "OpenAlex abstract.", [], **kwargs)
        assert len(row["grobid_intro"]) == TARGET_INTRO_CHARS
        # Not just the right length — the same text the prompt carries. The abstract
        # is stored as the tail _abstract_tail sends, not as the section it came from.
        assert row["grobid_intro"] in prompt
        assert row["grobid_abstract"]
        assert row["grobid_abstract"] in prompt

    def test_an_abstract_the_model_never_saw_is_not_stored(self):
        """The PDF abstract is sent only where it goes beyond the OpenAlex one. When
        they agree the model reads none of it, and the row must say so."""
        parsed = dict(self._PARSED, abstract="OpenAlex abstract.")
        row, _ = _run_to_fulltext("OpenAlex abstract.", parsed)
        assert row["grobid_abstract"] == ""

    def test_reference_count_records_what_the_prompt_renders(self):
        """assign_target_keys drops what the reference block cannot show: an entry
        with neither title nor DOI, and a work already listed as a candidate."""
        refs = ([{"title": f"Ref {i}", "year": 2000, "authors": ["A"]}
                 for i in range(40)]
                + [{"title": "", "doi": "", "year": 2001, "authors": ["B"]}])
        candidates = [{"doi": "10.5/c", "title": "Ref 0", "year": 2000,
                       "first_author": "A"}]
        out = link_original._build_output(
            "10.1/rep", {}, {}, candidates, {}, {}, {}, {"references": refs})
        assert len(json.loads(out["grobid_refs_json"])) == 25
        assert out["n_references_sent"] == 39


# ── The may-not-short-circuit gate (WP1) ─────────────────────────────────────

class TestMayStopAtARule:
    """A first-success ladder ends the row at the first deterministic hit. That is
    only safe when the paper's own text rules out a second target — otherwise the
    remaining N-1 originals are dropped without anything ever enumerating them."""

    def test_one_author_year_pair_and_no_count_may_stop(self):
        assert link_original.may_stop_at_a_rule(
            "A replication of Smith (2009)",
            "We re-tested the effect reported by Smith (2009).", 2020) is True

    def test_two_distinct_pairs_may_not_stop(self):
        assert link_original.may_stop_at_a_rule(
            "A replication of Smith (2009)",
            "We re-tested Smith (2009) and Jones (2011).", 2020) is False

    def test_a_stated_study_count_may_not_stop(self):
        assert link_original.may_stop_at_a_rule(
            "Many Labs 2",
            "We report replications of 28 classic studies, following Smith (2009).",
            2020) is False

    def test_a_year_is_not_a_study_count(self):
        assert link_original.may_stop_at_a_rule(
            "Replication of 2019 findings",
            "We re-tested the effect reported by Smith (2009).", 2020) is True


def _answer(**over) -> dict:
    """What identify_targets_with_llm returns. target_stage is what tells an answer
    apart from a provider failure, so it is on every answer and on no failure."""
    base = {"resolved": False, "resolution_method": "llm_no_target",
            "llm_source": "gemini", "llm_model": "gemini-heavy", "llm_error": "",
            "target_stage": "llm_gemini", "targets": [], "multi_target": False,
            "unidentified_count": 0, "resolved_study_r": "", "llm_evidence": "",
            "llm_reasoning": "no second target"}
    base.update(over)
    return base


def _failed_answer(error: str = "quota exhausted") -> dict:
    """A provider failure: no target_stage, and an llm_error the row must keep."""
    return {"resolved": False, "resolution_method": "llm_failed", "llm_source": "none",
            "llm_model": "", "llm_error": error, "target_stage": "", "targets": [],
            "multi_target": False, "llm_reasoning": ""}


def _gate_target(doi: str, title: str, author: str = "Smith", year: int = 2010,
                 **over) -> dict:
    target = {"key": f"@{author.lower()}{year}", "match_certain": True,
              "target_as_named": title, "study_numbers": "",
              "replication_study_numbers": "", "evidence_quote": "q",
              "record": {"doi": doi, "title": title, "first_author": author,
                         "year": year, "openalex_id": ""}}
    target.update(over)
    return target


def _run_gate(title_r: str, abstract_r: str, candidates: list,
              llm_answer: "dict | None" = None,
              abstract_answer: "dict | None" = None,
              screen: "dict | None" = None,
              pdf_ok: bool = True, no_llm: bool = False, no_pdf: bool = False) -> dict:
    """Drive run_for_doi with the title-pattern rule able to fire.

    *abstract_answer* is what the Stage 4 abstract call returns and *llm_answer* what
    the Stage 7 full-text call returns; pdf_ok=False stops the ladder at the
    no-document exit, and no_llm / no_pdf stop it earlier still.
    """
    cands_df = pd.DataFrame([{
        "doi_r": "10.1/rep", "study_r": title_r, "abstract_r": abstract_r,
        "year_r": "2020", "openalex_id_r": "W1", "url_r": "",
        "author_year_pattern_r": "",
    }])
    pdf = ({"pdf_path": "/tmp/x.pdf", "openalex_xml": None, "pdf_source": "unpaywall",
            "pdf_url": "u", "pdf_ok": True, "pdf_url_tried": []} if pdf_ok else
           {"pdf_path": None, "openalex_xml": None, "pdf_source": "none",
            "pdf_url": "", "pdf_ok": False, "pdf_url_tried": []})
    answers = [abstract_answer if abstract_answer is not None else _answer(),
               llm_answer if llm_answer is not None else _answer()]

    def _identify(*a, **k):
        return answers.pop(0) if len(answers) > 1 else answers[0]

    with patch.object(link_original, "find_all_candidates", return_value=candidates), \
         patch.object(link_original, "fetch_referenced_works_metadata", return_value=[]), \
         patch.object(link_original, "fetch_opencitations_references", return_value=[]), \
         patch.object(link_original, "screen_references_with_llm",
                      return_value=screen or _screen_result(
                          screen_verdict="proceed",
                          screen_classification="replication",
                          record_type="replication")), \
         patch.object(link_original, "acquire_pdf", return_value=pdf), \
         patch.object(link_original, "_parse_all",
                      return_value={"grobid": {"source": "grobid", "abstract": "",
                                               "intro": "i", "references": []}}), \
         patch.object(link_original, "_write_parse_cache"), \
         patch.object(link_original, "identify_targets_with_llm", side_effect=_identify):
        return run_for_doi("10.1/rep", cands_df=cands_df, no_llm=no_llm, no_pdf=no_pdf)


_GATE_CANDS = [{"title": "Time flies from left to right", "year": 2010,
                "first_author": "Smith", "all_authors": ["Smith"],
                "doi": "10.9/orig", "openalex_id": "W9"}]

_GATE_TITLE = "A replication of Time flies from left to right"
_ONE_PAIR   = "We re-tested Smith (2010)."
_TWO_PAIRS  = "We re-tested Smith (2010) and Jones (2011)."


class TestGateInTheLadder:
    def test_an_ungated_title_pattern_hit_ends_the_row(self):
        row = _run_gate(_GATE_TITLE, _ONE_PAIR, _GATE_CANDS)
        assert row["resolution_method"] == "title_pattern_match"
        assert row["resolved_doi_o"] == "10.9/orig"

    def test_a_withheld_pick_is_restored_when_the_target_call_names_nothing(self):
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS)
        assert row["resolution_method"] == "title_pattern_match"
        assert "no second target" in row["llm_reasoning"]

    def test_a_withheld_pick_is_restored_when_one_target_names_the_same_work(self):
        """Agreement is on the mapped record's DOI — the call confirmed the rule."""
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS,
                        llm_answer=_answer(targets=[_gate_target(
                            "10.9/orig", "Time flies from left to right")]))
        assert row["resolution_method"] == "title_pattern_match"
        assert row["resolved_doi_o"] == "10.9/orig"

    def test_a_withheld_pick_is_not_restored_when_the_one_target_is_another_work(self):
        """The call the gate waited for named a DIFFERENT original. Restoring here
        wrote the withheld pick at high confidence over the model's contradiction."""
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS,
                        llm_answer=_answer(targets=[_gate_target(
                            "10.9/other", "A different original", author="Jones",
                            year=2011)]))
        assert row["resolution_method"] != "title_pattern_match"
        assert row["n_targets"] == 1

    def test_a_withheld_pick_is_not_restored_when_two_targets_were_found(self):
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS,
                        llm_answer=_answer(
                            resolution_method="llm_multi_target", multi_target=True,
                            targets=[_gate_target("10.9/a", "A"),
                                     _gate_target("10.9/b", "B", author="Jones",
                                                  year=2011)]))
        assert row["resolution_method"] == "llm_multi_target"
        assert row["n_targets"] == 2

    def test_an_api_failure_is_not_an_answer_and_keeps_its_error(self):
        """A 429 is not "the model saw no second target". Restoring on it would freeze
        an unconfirmed rule pick into a resolved row a re-run never revisits, and the
        emitted row must still carry the error."""
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS,
                        llm_answer=_failed_answer("quota exhausted"))
        assert row["resolution_method"] == "llm_failed"
        assert row["llm_error"] == "quota exhausted"


class TestGateRestoresWhenNothingEnumerates:
    """The gate withholds a pick UNTIL something that can enumerate targets speaks.
    When nothing ever does, the pick stands — every one of these exits returned it
    before the gate existed, so dropping it is a lost resolution, not a caution."""

    def test_the_no_document_exit_restores_it(self):
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS, pdf_ok=False,
                        abstract_answer=_failed_answer())
        assert row["resolution_method"] == "title_pattern_match"
        assert row["resolved_doi_o"] == "10.9/orig"

    def test_no_llm_mode_never_withholds_at_all(self):
        """--no-llm runs no enumerating call, so withholding buys no information and
        would cost a PDF download per rule-resolved row: the rule stops the ladder."""
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS, no_llm=True)
        assert row["resolution_method"] == "title_pattern_match"
        # Returned at the rule: every stage past it emits with the acquired pdf block.
        assert row["pdf_source"] == "none"

    def test_no_pdf_mode_restores_it(self):
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS, no_pdf=True,
                        abstract_answer=_failed_answer())
        assert row["resolution_method"] == "title_pattern_match"

    def test_an_incomplete_screen_restores_it_and_keeps_the_error(self):
        """One surviving vote is a provider outage. Turning it into a lost
        deterministic resolution is exactly what the error-handling rule forbids."""
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS,
                        abstract_answer=_failed_answer(),
                        screen=_screen_result(
                            resolution_method="llm_refscreen_partial",
                            llm_error="classifier failed: openai"))
        assert row["resolution_method"] == "title_pattern_match"
        assert row["llm_error"] == "classifier failed: openai"

    def test_a_screen_discard_still_wins_over_the_pick(self):
        """A discard is a verdict about the paper, not a failure to look."""
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS,
                        abstract_answer=_failed_answer(),
                        screen=_screen_result(screen_verdict="discard",
                                              screen_classification="none"))
        assert row["resolution_method"] == "llm_not_a_replication"


class TestTheRichestAnswerSurvives:
    """Two rungs read different evidence, so the later one is not the newer truth —
    it may simply have been shown a shorter reference list."""

    def test_a_later_thinner_answer_does_not_replace_a_richer_one(self):
        row = _run_gate(
            "A study", _TWO_PAIRS, [],
            abstract_answer=_answer(
                resolution_method="llm_multi_target", multi_target=True,
                target_stage="llm_cited_candidates_gemini",
                targets=[_gate_target("10.9/a", "A"),
                         _gate_target("10.9/b", "B", author="Jones", year=2011)]),
            llm_answer=_answer(targets=[_gate_target("10.9/a", "A")]))
        assert row["n_targets"] == 2
        assert row["target_stage"] == "llm_cited_candidates_gemini"

    def test_a_later_single_resolution_does_not_drop_the_earlier_originals(self):
        """The reference rung settled on ONE original for a paper the abstract rung
        already saw two in. Returning that link silently dropped the other."""
        row = _run_gate(
            "A study", _TWO_PAIRS, [],
            abstract_answer=_answer(
                resolution_method="llm_multi_target", multi_target=True,
                targets=[_gate_target("10.9/a", "A"),
                         _gate_target("10.9/b", "B", author="Jones", year=2011)]),
            llm_answer=_answer(resolved=True, resolution_method="llm_gemini",
                               resolved_doi_o="10.9/c", resolved_title_o="C",
                               resolved_author_o="Kim", resolved_year_o=2012,
                               targets=[_gate_target("10.9/c", "C", author="Kim",
                                                     year=2012)]))
        assert row["resolution_method"] == "llm_multi_target"
        assert row["n_targets"] == 3
        assert row["resolved_doi_o"] == ""

    def test_the_union_does_not_write_one_original_twice(self):
        """The same work reached through two rungs carries two keys — the namespaces
        are per call — and two rows for it would share one pair_id."""
        row = _run_gate(
            "A study", _TWO_PAIRS, [],
            abstract_answer=_answer(
                resolution_method="llm_multi_target", multi_target=True,
                targets=[_gate_target("10.9/a", "A"),
                         _gate_target("10.9/b", "B", author="Jones", year=2011)]),
            llm_answer=_answer(resolved=True, resolution_method="llm_gemini",
                               resolved_doi_o="10.9/a", resolved_title_o="A",
                               targets=[_gate_target("10.9/a", "A",
                                                     key="@smith2010_again")]))
        assert row["n_targets"] == 2


def test_targets_found_at_the_reference_rung_survive_a_no_document_exit():
    """Stage 4.5 can name several targets and decline a single link. The list used to
    die with the stage, so the row was written target_pending with nothing on it."""
    targets = [{"key": "@a", "match_certain": True, "record": {"doi": "10.9/a"}},
               {"key": "@b", "match_certain": True, "record": {"doi": "10.9/b"}}]
    screen = _screen_result(screen_verdict="proceed",
                            screen_classification="replication",
                            record_type="replication",
                            targets=targets, multi_target=True,
                            target_stage="llm_references",
                            unidentified_count=1, resolved_study_r="2")
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
                      return_value={"pdf_path": None, "openalex_xml": None,
                                    "pdf_source": "none", "pdf_url": "",
                                    "pdf_ok": False, "pdf_url_tried": []}):
        row = run_for_doi("10.1/rep", cands_df=cands_df)

    assert row["resolution_method"] == "no_fulltext_available"
    assert row["n_targets"] == 2
    assert row["target_stage"] == "llm_references"
    assert row["unidentified_count"] == 1
    assert row["resolved_study_r"] == "2"
