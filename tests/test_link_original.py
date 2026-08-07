"""Tests for citation-context extraction, title-pattern resolution and Stage 4.5
screen routing in extract/link_original.py."""
import json
from unittest.mock import patch

import pandas as pd
import pytest

import extract.link_original as link_original
from extract.link_original import (
    _extract_cit_contexts, _extract_title_target, _resolve_by_title_pattern,
    run_for_doi,
)
from extract.run_extract import _map_method
from shared.prompts import TARGET_INTRO_CHARS, build_target_outcome_prompt


class TestExtractCitContexts:
    def test_both_narrative_and_parenthetical_present(self):
        """Reconstructs the real aepp.13320 case: the true target is cited
        narratively ('Kim et al. (2014)'), a secondary comparison parenthetically.

        The old local regex matched only the parenthetical form, which was the
        confirmed root cause of the aepp.13320 wrong-original-link bug. Both forms
        must be extractable — with every surname of a multi-author citation kept —
        so the resolver can score them and pick the right one.
        """
        text = (
            "we replicate Kim et al. (2014) who perform a quantile moments-based "
            "analysis. We compare to the partial moments model (Antle, 2010). "
            "Jones and Smith (2015) found similar effects."
        )
        results = _extract_cit_contexts(text)
        surnames_years = {(tuple(r["surnames"]), r["year"]) for r in results}
        assert (("kim",), 2014) in surnames_years
        assert (("antle",), 2010) in surnames_years
        assert (("jones", "smith"), 2015) in surnames_years


# ── Stage 2.5 title-pattern resolver ─────────────────────────────────────────

class TestExtractTitleTarget:
    @pytest.mark.parametrize("title,expected_contains", [
        ("Replication of the ego depletion effect",        "ego depletion effect"),
        ("A Direct Replication of the pen-in-mouth effect","pen-in-mouth effect"),
        ("Failed Replication of the IAT effect",           "IAT effect"),
        ("Replicating Milgram's obedience study",           "Milgram"),
        ("Revisiting the weapons effect",                  "weapons effect"),
        ("Re-examining the anchoring and adjustment effect","anchoring and adjustment effect"),
        ("Reconsidering ego depletion",                    "ego depletion"),
        ("The pen-in-mouth effect: A Replication",         "pen-in-mouth effect"),
        ("The pen-in-mouth effect: Replication and Extension","pen-in-mouth effect"),
        ("Does power posing increase testosterone? Replication attempt",  None),
        ("Can we replicate the Mozart effect?",             "Mozart effect"),
        ("Testing the replicability of social priming",     "social priming"),
        ("A Reproduction of the embodied cognition effect", "embodied cognition effect"),
        # The two patterns the table above never reached.
        ("Reproducing the analyses of Smith and Jones (2009)", "Smith and Jones"),
        ("Does the facial feedback effect replicate?",       "facial feedback effect"),
    ])
    def test_extract_target(self, title, expected_contains):
        result = _extract_title_target(title)
        if expected_contains is None:
            assert result is None or len(result) < 15
        else:
            assert result is not None, f"Expected match for: {title!r}"
            assert expected_contains.lower() in result.lower(), (
                f"Expected {expected_contains!r} in {result!r} for title {title!r}"
            )

    def test_no_match_returns_none(self):
        assert _extract_title_target("A meta-analysis of social priming effects") is None

    def test_generic_title_returns_none(self):
        assert _extract_title_target("Many Labs 2: Investigating Variation in Replicability") is None


class TestResolveByTitlePattern:
    _CANDIDATES = [
        {"doi": "10.1037/ego", "title": "Ego depletion: Is the active self a limited resource?",
         "year": 1998, "first_author": "Baumeister"},
        {"doi": "10.1037/sleep", "title": "Sleep deprivation and cognitive performance",
         "year": 2005, "first_author": "Harrison"},
        {"doi": "10.1037/social", "title": "Social facilitation effects in competitive tasks",
         "year": 2003, "first_author": "Zajonc"},
    ]

    def test_resolves_when_single_strong_match(self):
        result = _resolve_by_title_pattern(
            "10.1234/rep",
            "Replication of the ego depletion effect: Is the active self a limited resource?",
            self._CANDIDATES,
        )
        assert result is not None
        assert result["resolved"] is True
        assert result["resolved_doi_o"] == "10.1037/ego"
        assert result["resolution_method"] == "title_pattern_match"

    def test_returns_none_when_no_pattern_in_title(self):
        result = _resolve_by_title_pattern(
            "10.1234/rep",
            "A meta-analysis of sleep deprivation studies",
            self._CANDIDATES,
        )
        assert result is None

    def test_returns_none_when_no_candidates(self):
        result = _resolve_by_title_pattern(
            "10.1234/rep", "Replication of ego depletion", [])
        assert result is None


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

    @pytest.mark.parametrize("broken", [
        "second_voter_unconfident", "first_voter_unconfident",
        "second_voter_non_qualifying", "no_target_description",
    ])
    def test_any_missing_conjunct_blocks_the_search(self, broken):
        screen = {
            "second_voter_unconfident": lambda: _yes_screen(v2_confident=False),
            "first_voter_unconfident": lambda: _yes_screen(v1_confident=False),
            "second_voter_non_qualifying": lambda: _yes_screen(v2_class="unclear"),
        }.get(broken, _yes_screen)()
        if broken == "no_target_description":
            screen["target_description"] = ""

        row, search = _run_to_title_search(screen, hit=_HIT)

        assert not search.called
        assert row["resolved_doi_o"] == ""
        assert _map_method(row["resolution_method"]) == "target_pending"


# ── Abstract-stage LLM: self-link exclusion (audit B9) ───────────────────────

class TestAbstractStageExcludeDoi:
    def test_abstract_llm_receives_the_real_doi_r(self):
        """resolve_targets_and_outcomes uses its doi_r argument as exclude_doi.

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
             patch.object(link_original, "resolve_targets_and_outcomes",
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

    The parsers' output is *parsed*; the returned kwargs are the evidence blocks
    run_for_doi handed to resolve_targets_and_outcomes, which passes them straight to
    build_target_outcome_prompt — so a test can render the prompt the model actually
    got. record_type and rung select the builder rather than describing evidence, so
    they are dropped here.
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
         patch.object(link_original, "resolve_targets_and_outcomes",
                      return_value={"resolved": False,
                                    "resolution_method": "llm_no_target",
                                    "llm_source": "gemini"}) as llm:
        row = run_for_doi("10.1/rep", cands_df=cands_df)
    evidence = {k: v for k, v in llm.call_args.kwargs.items()
                if k not in ("record_type", "rung")}
    return row, evidence


class TestStoredEvidenceMatchesThePrompt:
    """The row is what a reviewer reads instead of the paper. Storing more than the
    model was sent claimed an abstract the answer cannot rest on, and storing less
    hid the evidence behind it."""

    _PARSED = {"source": "grobid", "abstract": "PDF abstract. Extra sentence.",
               "intro": "i" * 5000, "references": []}

    def test_sections_are_stored_as_the_prompt_carries_them(self):
        row, kwargs = _run_to_fulltext("OpenAlex abstract.", self._PARSED)
        prompt = build_target_outcome_prompt("A study", "OpenAlex abstract.", [], **kwargs)
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
            "10.1/rep", {}, candidates, {}, {}, {}, {"references": refs})
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

    def test_a_spelled_out_count_may_not_stop(self):
        """Abstracts write small counts in words at least as often as in digits, and
        two originals is already one too many for a deterministic rung to settle."""
        assert link_original.may_stop_at_a_rule(
            "A replication study",
            "We replicate two classic studies, starting from Smith (2009).",
            2020) is False

    def test_a_year_is_not_a_study_count(self):
        assert link_original.may_stop_at_a_rule(
            "Replication of 2019 findings",
            "We re-tested the effect reported by Smith (2009).", 2020) is True

    def test_a_year_earlier_in_the_text_does_not_shadow_a_later_count(self):
        """Both hits come from the same pattern; checking only the first match read
        the year, decided "no count", and let the rung stop on a multi-target paper."""
        assert link_original.may_stop_at_a_rule(
            "Replications of 2019 studies",
            "We report replications of three studies, starting from Smith (2009).",
            2020) is False


# A verdict the ladder may stop on, and one it must read on for. Every answer below
# carries the settled one unless a test says otherwise: the descent is a separate
# behaviour from the gate, and the gate tests are about the gate.
_SETTLED   = {"outcome": "failure", "outcome_phrase": "did not replicate"}
_UNSETTLED = {"outcome": "cannot_be_determined", "outcome_phrase": ""}


def _answer(**over) -> dict:
    """What resolve_targets_and_outcomes returns. target_stage is what tells an answer
    apart from a provider failure, so it is on every answer and on no failure."""
    base = {"resolved": False, "resolution_method": "llm_no_target",
            "llm_source": "gemini", "llm_model": "gemini-heavy", "llm_error": "",
            "target_stage": "llm_gemini", "targets": [], "multi_target": False,
            "unidentified_count": 0, "resolved_study_r": "", "llm_evidence": "",
            "llm_reasoning": "no second target", "outcome_block": dict(_SETTLED)}
    base.update(over)
    return base


def _failed_answer(error: str = "quota exhausted") -> dict:
    """A provider failure: no target_stage, and an llm_error the row must keep."""
    return {"resolved": False, "resolution_method": "llm_failed", "llm_source": "none",
            "llm_model": "", "llm_error": error, "target_stage": "", "targets": [],
            "multi_target": False, "llm_reasoning": "", "outcome_block": {}}


def _gate_target(doi: str, title: str, author: str = "Smith", year: int = 2010,
                 **over) -> dict:
    target = {"key": f"@{author.lower()}{year}", "match_certain": True,
              "target_as_named": title, "study_numbers": "",
              "replication_study_numbers": "", "evidence_quote": "q",
              "outcome_block": dict(_SETTLED),
              "record": {"doi": doi, "title": title, "first_author": author,
                         "year": year, "openalex_id": ""}}
    target.update(over)
    return target


def _run_gate(title_r: str, abstract_r: str, candidates: list,
              llm_answer: "dict | None" = None,
              abstract_answer: "dict | None" = None,
              screen: "dict | None" = None,
              pdf_ok: bool = True, no_llm: bool = False, no_pdf: bool = False,
              oa_xml: "dict | None" = None, parse: "dict | None" = None,
              identify=None, record_type: str = "replication") -> dict:
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
           {"pdf_path": None, "openalex_xml": oa_xml,
            "pdf_source": "openalex_xml" if oa_xml else "none",
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
                      return_value=parse or {"grobid": {"source": "grobid",
                                                        "abstract": "", "intro": "i",
                                                        "references": []}}), \
         patch.object(link_original, "_write_parse_cache"), \
         patch.object(link_original, "resolve_targets_and_outcomes",
                      side_effect=identify or _identify):
        return run_for_doi("10.1/rep", cands_df=cands_df, no_llm=no_llm,
                           no_pdf=no_pdf, record_type=record_type)


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

    def test_a_withheld_pick_is_not_restored_when_the_one_target_is_unmatchable(self):
        """An enumerator named ONE target the key map could not match to a record.
        We cannot show it is the held work, so restoring would overrule the call on
        no evidence — the pick is dropped, not written at rule confidence."""
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS,
                        llm_answer=_answer(targets=[_gate_target(
                            "", "Some stated original", match_certain=False,
                            record=None)]))
        assert row["resolution_method"] != "title_pattern_match"

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

    def test_openalex_body_text_reaches_the_llm_instead_of_no_context(self):
        """A parse with body text and nothing else must still be read.

        OpenAlex's TEI has no <head> elements, so parse_tei_sections cannot split it
        and returns the body whole in raw_text. Before raw_text was carried into
        sections, build_target_outcome_prompt saw abstract/intro/methods only: a document with
        text but no abstract and no references passed the "we have a document" guard
        and was then dropped as no_context, with the text never sent anywhere."""
        body = ("INTRODUCTION\nWe re-test the finding reported by Smith (2010) that "
                "time flies from left to right. " * 20)
        oa_xml = {"source": "openalex_xml", "xml_url": "u",
                  "sections": {"abstract": "", "intro": "", "methods": "",
                               "references": [], "raw_text": body}}
        parse = {"openalex_xml": {"source": "openalex_xml", "abstract": "",
                                  "intro": "", "references": [], "raw_text": body}}
        seen: dict = {}

        def _capture(*a, **k):
            seen.update(k)
            return _answer()

        row = _run_gate("An unrelated title", "", [], pdf_ok=False, oa_xml=oa_xml,
                        parse=parse, identify=_capture)
        assert row["resolution_method"] != "no_context"
        assert seen["intro"].startswith("INTRODUCTION")
        # Sent and stored at the same size, so the row shows what the model read.
        assert len(seen["intro"]) <= TARGET_INTRO_CHARS
        assert row["grobid_intro"] == seen["intro"]

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

    def test_an_incomplete_screen_does_not_settle_the_pick(self):
        """The failure is what stopped the reference-list target pick from running, so
        nothing has earned the right to settle a withheld pick — restoring it here would
        read "we never asked" as "we asked and nothing contradicted it". Deferring costs
        one re-run of a free rule; settling an unconfirmed pick during an outage is
        permanent."""
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS,
                        abstract_answer=_failed_answer(),
                        screen=_screen_result(
                            resolution_method="llm_refscreen_partial",
                            llm_error="classifier failed: openai"))
        assert row["resolution_method"] == "llm_refscreen_partial"
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


_LONE_CAND  = [{"title": "Time flies from left to right", "year": 2010,
                "first_author": "Smith", "all_authors": ["Smith"],
                "doi": "10.9/orig", "openalex_id": "W9"}]

# Two candidates sharing one surname and one year: Path A cannot break the tie (both
# score author+year alike, so the gap stays under 2.0) and Path B's title overlap does.
_SAME_AUTHOR_CANDS = _LONE_CAND + [
    {"title": "Wombat physiology in captivity", "year": 2010,
     "first_author": "Smith", "all_authors": ["Smith"],
     "doi": "10.9/other", "openalex_id": "W8"}]

_ONE_PAIR_TITLED = "We re-tested Smith (2010): time flies from left to right."


class TestWeakRulePicksAreOnlyHeld:
    """The lone-candidate branch and Path B carry no semantic check — the first accepts
    whatever the re-query left standing, the second breaks on a ≥0.05 token overlap the
    tie Path A refused. 28 of the 29 rule links in data/extracted.csv came from the
    first. An abstract citing exactly one author-year that is NOT the target passes
    may_stop_at_a_rule, so ending the row there links the paper to whatever it cited."""

    def test_a_lone_candidate_does_not_end_the_row(self):
        """may_stop is true (one pair, no count) and the pick is still held: the
        abstract LLM runs, and only then is the pick restored at the exit."""
        row = _run_gate("An unrelated title", _ONE_PAIR, _LONE_CAND)
        assert row["resolution_method"] == "single_candidate_after_requery"
        assert row["resolved_doi_o"] == "10.9/orig"
        # Restored, not returned at the rule: the reasoning of the call that declined
        # to contradict it travels with the row.
        assert "no second target" in row["llm_reasoning"]

    def test_the_abstract_llm_overrules_a_lone_candidate(self):
        row = _run_gate("An unrelated title", _ONE_PAIR, _LONE_CAND,
                        abstract_answer=_answer(
                            resolved=True, resolution_method="llm_gemini",
                            resolved_doi_o="10.9/other",
                            resolved_title_o="A different original",
                            targets=[_gate_target("10.9/other",
                                                  "A different original")]))
        assert row["resolution_method"] == "llm_gemini"
        assert row["resolved_doi_o"] == "10.9/other"

    def test_a_lone_candidate_confirmed_by_one_target_keeps_the_rule_method(self):
        row = _run_gate("An unrelated title", _ONE_PAIR, _LONE_CAND,
                        abstract_answer=_answer(targets=[_gate_target(
                            "10.9/orig", "Time flies from left to right")]))
        assert row["resolution_method"] == "single_candidate_after_requery"
        assert row["resolved_doi_o"] == "10.9/orig"

    def test_a_lone_candidate_still_ends_the_row_under_no_llm(self):
        """--no-llm runs nothing that could enumerate, so holding would only buy a PDF
        download per rule-resolved row."""
        row = _run_gate("An unrelated title", _ONE_PAIR, _LONE_CAND, no_llm=True)
        assert row["resolution_method"] == "single_candidate_after_requery"
        assert row["pdf_source"] == "none"

    def test_path_b_does_not_end_the_row(self):
        row = _run_gate("An unrelated title", _ONE_PAIR_TITLED, _SAME_AUTHOR_CANDS)
        assert row["resolution_method"] == "same_author_year_title_overlap"
        assert row["resolved_doi_o"] == "10.9/orig"
        assert "no second target" in row["llm_reasoning"]

    def test_path_b_is_overruled_when_the_llm_names_another_work(self):
        row = _run_gate("An unrelated title", _ONE_PAIR_TITLED, _SAME_AUTHOR_CANDS,
                        abstract_answer=_answer(targets=[_gate_target(
                            "10.9/other", "Wombat physiology in captivity")]))
        assert row["resolution_method"] != "same_author_year_title_overlap"
        assert row["n_targets"] == 1


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


class TestLadderReadsTheParseCache:
    """Stage 6 wrote the parse cache and never read it: every run re-ran all six
    parsers over a document whose parse was already on disk."""

    # What is on disk, and what a fresh parse_all would return — different winners, so
    # the row's parse_method says which one the ladder used.
    _CACHED = {"markitdown": {"source": "markitdown", "abstract": "cached abstract",
                              "intro": "cached intro", "references": [{"title": "r"}],
                              "raw_text": "body", "error": None}}
    _FRESH  = {"pdfminer": {"source": "pdfminer", "abstract": "", "intro": "fresh intro",
                            "references": [], "raw_text": "body", "error": None}}

    def _cache(self, tmp_path, monkeypatch, results):
        from shared.utils import cache_key
        monkeypatch.setattr(link_original, "PARSE_CACHE_DIR", tmp_path)
        (tmp_path / f"parse_{cache_key('10.1/rep')}.json").write_text(
            json.dumps(results), encoding="utf-8")

    def test_a_cache_hit_skips_the_six_parsers(self, tmp_path, monkeypatch):
        self._cache(tmp_path, monkeypatch, self._CACHED)
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS, parse=self._FRESH)
        assert row["parse_method"] == "markitdown"
        assert "cached intro" in row["grobid_intro"]

    def test_an_empty_cache_is_still_a_miss(self, tmp_path, monkeypatch):
        """An all-empty parse is what a PDF-less run wrote; reading it back would pin
        the paper to abstract-only coding forever (audit B4)."""
        self._cache(tmp_path, monkeypatch,
                    {"markitdown": {"source": "markitdown", "abstract": "", "intro": "",
                                    "references": [], "raw_text": "", "error": None}})
        row = _run_gate(_GATE_TITLE, _TWO_PAIRS, _GATE_CANDS, parse=self._FRESH)
        assert row["parse_method"] == "pdfminer"


# ── OUTCOME_DESCENT: an unsettled verdict keeps the ladder going ─────────────
# The escalation this replaces lived in code_outcome and could never fire: a row
# resolved from the abstract never acquired a document, so 5 of 285 rows in
# data/extracted.csv carry any pdf_source. Reading on is the ladder's job.

class TestOutcomeDescent:
    _CANDS = [{"title": "Time flies from left to right", "year": 2010,
               "first_author": "Smith", "all_authors": ["Smith"],
               "doi": "10.9/orig", "openalex_id": "W9"}]

    @staticmethod
    def _resolved(outcome_block, **over) -> dict:
        return _answer(**{"resolved": True, "resolution_method": "llm_gemini",
                          "resolved_doi_o": "10.9/orig",
                          "resolved_title_o": "Original", "resolved_year_o": 2010,
                          "resolved_author_o": "Smith", "resolution_score": 1.0,
                          "llm_confidence": "high", "outcome_block": outcome_block,
                          **over})

    def test_a_settled_outcome_ends_the_row_at_the_abstract_rung(self):
        """Nothing is read that the verdict does not need: the abstract named the
        target AND said how it came out, so no PDF is acquired."""
        acquired: list = []
        with patch.object(link_original, "acquire_pdf",
                          side_effect=lambda *a, **k: acquired.append(1) or {
                              "pdf_path": None, "openalex_xml": None,
                              "pdf_source": "none", "pdf_url": "", "pdf_ok": False,
                              "pdf_url_tried": []}):
            row = _run_gate("A study", _ONE_PAIR, self._CANDS,
                            abstract_answer=self._resolved(dict(_SETTLED)))
        assert row["resolved"] is True
        assert row["outcome_block"]["outcome"] == "failure"
        assert acquired == [], "a settled row must not pay for a document"

    def test_an_unsettled_outcome_descends_to_the_full_text(self):
        """The link stands; what is missing is the verdict, and the closing sections
        state it. The full-text call's reading replaces the carried one."""
        row = _run_gate("A study", _ONE_PAIR, self._CANDS,
                        abstract_answer=self._resolved(dict(_UNSETTLED)),
                        llm_answer=self._resolved(dict(_SETTLED),
                                                  resolution_method="llm_fulltext"))
        assert row["resolution_method"] == "llm_fulltext"
        assert row["outcome_block"]["outcome"] == "failure"

    def test_a_carried_resolution_survives_a_no_document_exit(self):
        """No document means nothing further can settle the verdict — but the link was
        ACCEPTED, and dropping it for target_pending would lose a resolution the
        pipeline already paid for."""
        row = _run_gate("A study", _ONE_PAIR, self._CANDS,
                        abstract_answer=self._resolved(dict(_UNSETTLED)),
                        pdf_ok=False)
        assert row["resolved"] is True
        assert row["resolved_doi_o"] == "10.9/orig"
        assert row["outcome_block"]["outcome"] == "cannot_be_determined"

    def test_a_full_text_provider_failure_keeps_the_carried_resolution(self):
        """A failure is not an answer. The row keeps the accepted link and records the
        error, rather than reading "we never asked" as target_pending."""
        row = _run_gate("A study", _ONE_PAIR, self._CANDS,
                        abstract_answer=self._resolved(dict(_UNSETTLED)),
                        llm_answer=_failed_answer("quota exhausted"))
        assert row["resolved"] is True
        assert row["resolved_doi_o"] == "10.9/orig"
        assert "quota exhausted" in row["llm_error"]

    def test_a_settled_outcome_is_not_overwritten_by_a_later_unsettled_one(self):
        """Two readings of one work: the later rung read different evidence, not better
        evidence, about a verdict the earlier one already stated plainly."""
        early = _gate_target("10.9/orig", "Original")
        late = _gate_target("10.9/orig", "Original")
        late["outcome_block"] = dict(_UNSETTLED)
        late["outcome_stage"] = "llm_fulltext"
        merged = link_original._union_targets([early], [late])
        assert len(merged) == 1
        assert merged[0]["outcome_block"]["outcome"] == "failure"

    def test_a_later_settled_outcome_does_win(self):
        early = _gate_target("10.9/orig", "Original")
        early["outcome_block"] = dict(_UNSETTLED)
        late = _gate_target("10.9/orig", "Original")
        late["outcome_block"] = {"outcome": "success"}
        merged = link_original._union_targets([early], [late])
        assert merged[0]["outcome_block"]["outcome"] == "success"

    def test_an_unsettled_reproduction_axis_is_enough_to_descend(self):
        """Either axis unresolved is a reason to read on: half a verdict must not stop
        the ladder as though the paper had been read to the end."""
        half = {"outcome_computation": "computationally reproducible",
                "outcome_robustness": "cannot_be_determined"}
        both = {"outcome_computation": "computationally reproducible",
                "outcome_robustness": "robust"}
        row = _run_gate("A study", _ONE_PAIR, self._CANDS,
                        abstract_answer=self._resolved(half),
                        llm_answer=self._resolved(both,
                                                  resolution_method="llm_fulltext"),
                        record_type="reproduction")
        assert row["resolution_method"] == "llm_fulltext"
        assert row["outcome_block"]["outcome_robustness"] == "robust"

    def test_the_full_text_rung_is_sent_the_closing_sections(self):
        """The block that makes the outcome answerable, with the provenance that says
        what it is — a real discussion heading or simply the end of the paper."""
        paper = ("Introduction. " + "x" * 400 + " Discussion\n"
                 + "We conclude the effect did not replicate. " + "y" * 300)
        seen: dict = {}

        def _identify(*a, **k):
            seen.update(k)
            return _answer()

        row = _run_gate("A study", _ONE_PAIR, [],
                        parse={"markitdown": {"source": "markitdown", "abstract": "",
                                              "intro": "", "raw_text": paper,
                                              "references": []}},
                        identify=_identify)
        assert seen["rung"] == "fulltext"
        assert "did not replicate" in seen["discussion"]
        assert seen["discussion_provenance"] in ("discussion", "tail")
        assert row["grobid_discussion"] == seen["discussion"]


class TestTheAbstractRungReadsTheTitleToo:
    """An OSF registration's abstract is boilerplate — "Stage 1 IPA at PCI RR" — and
    its original is in the paper's own title. Reading the abstract alone left 18 of
    100 works on the frozen dev sample with no rung ever naming a target: this gate
    closed, the reference rung had no references to pick from, and no document was
    acquired."""

    @staticmethod
    def _run(title_r: str, abstract_r: str):
        cands_df = pd.DataFrame([{
            "doi_r": "10.1/rep", "study_r": title_r, "abstract_r": abstract_r,
            "year_r": "2020", "openalex_id_r": "", "url_r": "",
            "author_year_pattern_r": "",
        }])
        with patch.object(link_original, "find_all_candidates", return_value=[]), \
             patch.object(link_original, "_resolve_by_title_pattern", return_value=None), \
             patch.object(link_original, "_resolve_rule_based",
                          return_value={"resolved": False,
                                        "resolution_method": "needs_fulltext"}), \
             patch.object(link_original, "resolve_targets_and_outcomes",
                          return_value={"resolved": False,
                                        "resolution_method": "llm_no_target",
                                        "llm_source": "openai"}) as llm, \
             patch.object(link_original, "fetch_referenced_works_metadata", return_value=[]), \
             patch.object(link_original, "fetch_opencitations_references", return_value=[]), \
             patch.object(link_original, "screen_references_with_llm",
                          return_value=_screen_result()), \
             patch.object(link_original, "acquire_pdf",
                          return_value={"pdf_path": None, "openalex_xml": None,
                                        "pdf_source": "none", "pdf_url": "",
                                        "pdf_ok": False, "pdf_url_tried": []}):
            run_for_doi("10.1/rep", cands_df=cands_df)
        return [c for c in llm.call_args_list if c.kwargs.get("rung") == "abstract"]

    def test_a_citation_in_the_title_alone_opens_the_rung(self):
        calls = self._run("A multilab investigation into the N2pc: Direct replication "
                          "of Eimer (1996)", "Stage 1 IPA at PCI RR")
        assert calls, "the abstract rung never ran for a title that names its original"

    def test_a_paper_that_names_nobody_still_does_not_open_it(self):
        """The gate is there so the call is not made with nothing to reason from."""
        assert not self._run("A replication study in Senegal",
                             "This study aims to replicate a previous intervention.")


class TestTheTitleSearchIsGivenTheCitedYear:
    """Both searches reject a hit more than two years from the year they are given,
    and `title_search_candidates` was called with none, so the check never ran.
    "Bem (2011)" matched a 1965 paper called "Personality and social psychology" —
    the target string carried the journal name and the title index matched that."""

    @staticmethod
    def _search(seen):
        def search(title, year, raise_on_unavailable=False):
            seen.append((title, year))
            return None
        return search

    def test_the_year_reaches_both_providers(self):
        seen: list = []
        with patch.object(link_original, "_search_crossref_by_title",
                          side_effect=self._search(seen)), \
             patch.object(link_original, "_search_openalex_by_title",
                          side_effect=self._search(seen)):
            link_original.title_search_candidates("10.1/rep", "Bem (2011) precognition",
                                                  "", "2011")
        assert [y for _, y in seen] == ["2011", "2011"]

    def test_no_year_in_the_citation_still_searches(self):
        """A target with no year is not a reason to skip the search — it is a reason
        not to filter on one."""
        seen: list = []
        with patch.object(link_original, "_search_crossref_by_title",
                          side_effect=self._search(seen)), \
             patch.object(link_original, "_search_openalex_by_title",
                          side_effect=self._search(seen)):
            link_original.title_search_candidates("10.1/rep", "A named study", "")
        assert [y for _, y in seen] == ["", ""]


class TestATitleHitMustCarryTheCitedAuthor:
    """A citation names an author, and a paper that does not have that author is not
    that paper however well its title scored. Both title-search failures left on the
    frozen dev sample after the year filter were this."""

    @staticmethod
    def _run(hit, surname):
        with patch.object(link_original, "_search_crossref_by_title", return_value=hit), \
             patch.object(link_original, "_search_openalex_by_title", return_value=None):
            return link_original.title_search_candidates(
                "10.1/rep", "Bem (2011) precognition", "", "2011", surname)[0]

    _CHAPTER = {"doi": "10.1093/oxfordhb/9780195398991.013.0001",
                "title": "Personality and Social Psychology",
                "year": 2012, "authors": ["Snyder, M.", "Deaux, K."]}

    def test_a_hit_by_other_authors_is_dropped(self):
        assert self._run(self._CHAPTER, "bem") == []

    def test_front_matter_with_no_author_at_all_is_dropped(self):
        """The Svensson case: a journal's front matter has nobody on it, and a record
        with nobody on it cannot be the paper a citation names."""
        assert self._run({**self._CHAPTER, "authors": []}, "svensson") == []

    def test_the_named_author_passes(self):
        assert self._run({**self._CHAPTER, "authors": ["Bem, D. J."]}, "bem")

    def test_a_citation_with_no_author_filters_nothing(self):
        assert self._run(self._CHAPTER, "")

    def test_a_multi_author_citation_matches_on_any_of_its_names(self):
        """extract_author_year_patterns returns "Kaufmann, Weber, and Haisley (2013)"
        as one run-on token. Matching that as a word dropped the right paper."""
        assert self._run({**self._CHAPTER,
                          "authors": ["Kaufmann, C.", "Weber, M.", "Haisley, E."]},
                         "kaufmann,weber,andhaisley")
