"""
Tests for Stage 3 (extract).

Unit tests mock all external API calls.
Run:  python -m pytest tests/test_extract.py -v
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from shared.schema import (
    EXTRACTED_COLS,
    LINK_METHOD_VALUES,
    OUTCOME_CATEGORIES,
    RESOLVED_LINK_METHODS,
    make_pair_id,
)
from shared.cache import content_key, read_cache
import extract.code_outcome as code_outcome
import extract.run_extract as run_extract
from extract.code_outcome import extract_outcome, _keyword_scan, _expand_to_sentences
from extract.run_extract import (
    classify_match_type,
    _llm_classify_match_type,
    _map_method,
    _merge_row,
    _merge_multi_row,
    _rule_classify_multi_original,
    _score_to_confidence,
)


# ── Sentence expansion unit tests ────────────────────────────────────────────

class TestExpandToSentences:
    def test_returns_target_sentence(self):
        text = "First sentence. We replicated the effect. Third sentence."
        result = _expand_to_sentences(text, text.index("We replicated"), text.index("We replicated") + 5, n_context=0)
        assert "We replicated the effect" in result

    def test_includes_one_sentence_before(self):
        text = "First sentence. We replicated the effect. Third sentence."
        start = text.index("We replicated")
        result = _expand_to_sentences(text, start, start + 5, n_context=1)
        assert "First sentence" in result
        assert "We replicated the effect" in result

    def test_includes_one_sentence_after(self):
        text = "First sentence. We replicated the effect. Third sentence."
        start = text.index("We replicated")
        result = _expand_to_sentences(text, start, start + 5, n_context=1)
        assert "We replicated the effect" in result
        assert "Third sentence" in result

    def test_single_sentence_no_error(self):
        text = "We replicated the effect."
        result = _expand_to_sentences(text, 3, 15, n_context=1)
        assert "We replicated the effect" in result

    def test_match_at_start_clamps(self):
        text = "Failed to replicate. Second sentence. Third sentence."
        result = _expand_to_sentences(text, 0, 18, n_context=1)
        assert "Failed to replicate" in result
        assert "Second sentence" in result

    def test_et_al_not_split(self):
        text = "Smith et al. found an effect. The replication failed."
        start = text.index("The replication")
        result = _expand_to_sentences(text, start, start + 20, n_context=1)
        assert "Smith et al" in result
        assert "replication failed" in result

    def test_empty_text_returns_empty(self):
        result = _expand_to_sentences("", 0, 0)
        assert result == ""


# ── Keyword scan unit tests ───────────────────────────────────────────────────

class TestKeywordScan:
    @pytest.mark.parametrize("text,expected", [
        ("we found no evidence of ego depletion", "failure"),
        ("failed to replicate the original finding", "failure"),
        ("null result for the predicted effect", "failure"),
        ("the three-item CRT was successfully replicated", "success"),
        ("effect was robustly replicated across three samples", "success"),
        ("IAT demonstrated strong psychometric properties consistent with original reports", "success"),
        ("partially replicated with some but not all findings held", "mixed"),
        ("No evidence was found for precognition in any experiment", "failure"),
        ("adapted the procedure in a different cultural population", "descriptive"),
    ])
    def test_keyword_scan(self, text, expected):
        hit = _keyword_scan(text, "abstract")
        assert hit is not None, f"No match for: {text!r}"
        assert hit["outcome"] == expected, (
            f"Expected {expected}, got {hit['outcome']} for: {text!r}"
        )

    @pytest.mark.parametrize("text", [
        "significant but smaller effect than the original study reported",
        "the replication produced a reduced effect magnitude",
    ])
    def test_reduced_effect_size_alone_is_not_a_keyword_hit(self, text):
        """Effect size alone must not decide the outcome.

        `mixed` requires the authors to present their own evidence as partly
        supporting and partly not; a supported-but-smaller effect is a success.
        Neither is decidable from these phrases, so the keyword pass declines and
        the row goes to the LLM rather than being coded on magnitude alone.
        """
        assert _keyword_scan(text, "abstract") is None

    def test_no_match_returns_none(self):
        hit = _keyword_scan("we attempted this study across multiple sites", "abstract")
        assert hit is None

    def test_failure_beats_success_keyword(self):
        hit = _keyword_scan("we failed to replicate the originally replicated finding", "abstract")
        assert hit["outcome"] == "failure"

    def test_success_in_the_same_sentence_vetoes_a_failure_keyword(self):
        """A failure phrase does not decide a sentence that also reports success."""
        text = ("The effect did not replicate in Study 1, but was successfully "
                "replicated in Study 2.")
        hit = _keyword_scan(text, "abstract")
        assert hit is not None
        assert hit["outcome"] != "failure"

    def test_success_elsewhere_does_not_veto_a_failure_sentence(self):
        """The veto is per sentence — a success claim about a different result is not one."""
        text = ("The manipulation check was successfully replicated. "
                "The focal effect failed to replicate.")
        hit = _keyword_scan(text, "abstract")
        assert hit is not None
        assert hit["outcome"] == "failure"

    @pytest.mark.parametrize("text", [
        "We found no significant difference between our estimate and the original, "
        "consistent with the original finding.",
        "There was no evidence of a difference from the original effect; the "
        "replication was successful.",
    ])
    def test_weak_failure_phrases_lose_to_an_explicit_success(self, text):
        """"No significant difference" describes the test, not the verdict.

        In a successful replication it is how the comparison against the original
        is reported, so an explicit success claim in the same abstract wins.
        """
        hit = _keyword_scan(text, "abstract")
        assert hit is not None
        assert hit["outcome"] == "success"

    def test_weak_failure_phrase_alone_still_codes_failure_at_medium(self):
        hit = _keyword_scan("We found no evidence of ego depletion.", "abstract")
        assert hit["outcome"] == "failure"
        assert hit["outcome_confidence"] == "medium"

    def test_returns_source_correctly(self):
        hit = _keyword_scan("successfully replicated", "fulltext")
        assert hit["out_quote_source"] == "fulltext"

    def test_outcome_phrase_is_not_bare_keyword(self):
        text = "We ran three experiments. The results failed to replicate. Further analysis confirmed this."
        hit = _keyword_scan(text, "abstract")
        assert hit is not None
        assert len(hit["outcome_phrase"]) > len("failed to replicate")

    def test_outcome_phrase_contains_surrounding_sentence(self):
        text = "Prior work found a large effect. We failed to replicate this effect in our sample. Our power was 0.95."
        hit = _keyword_scan(text, "abstract")
        assert hit is not None
        assert "Prior work" in hit["outcome_phrase"] or "power was" in hit["outcome_phrase"]


# ── extract_outcome unit tests ────────────────────────────────────────────────

class TestExtractOutcome:
    def test_keyword_hit_still_routes_through_llm_when_available(self, tmp_path):
        """#70: even a clear keyword hit must be seen by the LLM (is_genuine_attempt
        veto) when the LLM is available — the keyword short-circuit is no_llm-only."""
        mock = {"outcome": "failure", "outcome_phrase": "no effect", "is_genuine_attempt": True,
                "confidence": "high", "out_quote_source": "abstract"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            result = extract_outcome(
                "10.1234/test",
                abstract_r="we found no evidence of the original effect",
                title_r="A Replication Study",
            )
        mock_llm.assert_called()  # keyword hit no longer skips the LLM
        assert result["outcome"] == "failure"

    def test_keyword_hit_vetoed_as_not_a_replication(self, tmp_path):
        """#70: a 'failed to replicate' abstract that the LLM judges is_genuine_attempt
        =false becomes not_a_replication instead of a coded failure."""
        mock = {"outcome": "failure", "outcome_phrase": "background prose",
                "is_genuine_attempt": False, "confidence": "high",
                "out_quote_source": "abstract"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")), \
             patch("extract.code_outcome.time.sleep"):
            result = extract_outcome(
                "10.1234/veto",
                abstract_r="prior work failed to replicate the effect, we do something else",
                title_r="Not actually a replication",
            )
        assert result["outcome"] == "not_a_replication"

    def test_keyword_hit_skips_llm_in_no_llm_mode(self):
        """The keyword fast-path is preserved when the LLM is off."""
        with patch("extract.code_outcome.call_llm") as mock_llm:
            result = extract_outcome(
                "10.1234/test",
                abstract_r="we found no evidence of the original effect",
                title_r="A Replication Study",
                no_llm=True,
            )
        mock_llm.assert_not_called()
        assert result["outcome"] == "failure"
        assert result["out_quote_source"] == "abstract"

    def test_uninformative_triggers_llm(self):
        """No keyword match should fall through to LLM."""
        mock_llm_result = {"outcome": "mixed", "outcome_phrase": "partial support",
                           "confidence": "medium", "out_quote_source": "abstract"}
        with patch("extract.code_outcome.call_llm", return_value=(mock_llm_result, "gemini-model", "")), \
             patch("extract.code_outcome.time.sleep"):
            result = extract_outcome(
                "10.1234/test2",
                abstract_r="we conducted this study with different participants",
                title_r="A New Study",
            )
        assert result["outcome"] == "mixed"

    def test_single_candidate_confidence_capped_at_medium(self):
        """#51: a lone candidate auto-accepted at score 1.0 must not read as high."""
        from extract.run_extract import _link_confidence
        assert _link_confidence(
            {"resolution_method": "single_candidate_after_requery", "resolution_score": 1.0}
        ) == "medium"
        # other methods at 1.0 are unaffected
        assert _link_confidence(
            {"resolution_method": "citation_context_match", "resolution_score": 1.0}
        ) == "high"
        # an explicit LLM confidence still flows through for non-capped methods
        assert _link_confidence(
            {"resolution_method": "llm_cited_candidates", "llm_confidence": "high"}
        ) == "high"

    def test_llm_failure_returns_api_error(self, tmp_path):
        """Exhausting every provider is an api_error, not a cannot_be_determined verdict."""
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm", return_value=(None, "", "quota | error")):
            result = extract_outcome("10.1234/fail", abstract_r="ambiguous text")
        assert result["outcome"] == "api_error"
        assert result["outcome_confidence"] == "low"
        # An API failure must not be cached — a re-run has to be able to code the row.
        assert not list(tmp_path.glob("*.json"))

    def test_llm_failure_backs_off_between_retries(self, tmp_path):
        """call_llm reports failure by returning None, so the backoff must run on that path."""
        sleeps: list = []
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.time.sleep", side_effect=sleeps.append), \
             patch("extract.code_outcome.call_llm", return_value=(None, "", "quota")):
            extract_outcome("10.1234/fail", abstract_r="ambiguous text")
        assert sleeps == [1, 2]

    def test_llm_result_cached(self, tmp_path):
        """LLM result should be written to cache and reused."""
        mock_result = {"outcome": "success", "outcome_phrase": "replicated",
                       "confidence": "high", "out_quote_source": "abstract"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock_result, "gemini-model", "")), \
             patch("extract.code_outcome.time.sleep"):
            r1 = extract_outcome("10.1234/cache", abstract_r="ambiguous text")
            with patch("extract.code_outcome.call_llm") as mock2:
                r2 = extract_outcome("10.1234/cache", abstract_r="ambiguous text")
                mock2.assert_not_called()
        assert r1["outcome"] == r2["outcome"] == "success"

    def test_invalid_llm_outcome_normalised(self, tmp_path):
        """LLM returning an unexpected outcome value should become cannot_be_determined."""
        mock_result = {"outcome": "uncertain", "outcome_phrase": "",
                       "confidence": "low", "out_quote_source": ""}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock_result, "gemini-model", "")), \
             patch("extract.code_outcome.time.sleep"):
            result = extract_outcome("10.1234/bad", abstract_r="ambiguous text")
        assert result["outcome"] == "cannot_be_determined"


# ── LLM outcome prompt tests ─────────────────────────────────────────────────

class TestLLMOutcomePrompt:
    """Verify the enriched _llm_outcome() prompt and new extract_outcome() params."""

    def _run_llm(self, tmp_path, abstract_r="ambiguous text",
                 original_title="", original_authors="", original_year="",
                 llm_return=None):
        if llm_return is None:
            llm_return = {"outcome": "success", "outcome_phrase": "We confirmed the effect.",
                          "confidence": "high", "out_quote_source": "abstract",
                          "outcome_reasoning": "All effects replicated."}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(llm_return, "gemini-model", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            result = extract_outcome(
                "10.1234/test", abstract_r=abstract_r, title_r="A Study",
                original_title=original_title, original_authors=original_authors,
                original_year=original_year,
            )
        return result, mock_llm

    def test_original_citation_appears_in_prompt_when_provided(self, tmp_path):
        _, mock_llm = self._run_llm(
            tmp_path, original_title="The Original", original_authors="Smith", original_year="2010"
        )
        prompt = mock_llm.call_args[0][0]
        assert "This paper replicates" in prompt
        assert "The Original" in prompt
        assert "Smith" in prompt
        assert "2010" in prompt

    def test_no_original_block_when_title_empty(self, tmp_path):
        _, mock_llm = self._run_llm(tmp_path)
        prompt = mock_llm.call_args[0][0]
        assert "This paper replicates" not in prompt

    def test_fulltext_not_in_abstract_prompt(self, tmp_path):
        """#61 abstract-first: the FIRST call must be abstract-only. Fulltext is held
        in reserve for escalation, so it must not appear in the abstract prompt."""
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(
                 {"outcome": "success", "outcome_phrase": "x", "outcome_confidence": "high",
                  "out_quote_source": "abstract", "outcome_reasoning": ""},
                 "gemini-model", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            extract_outcome("10.1234/ft", abstract_r="ambiguous text", fulltext="UNIQUE_FULLTEXT_MARKER")
        first_prompt = mock_llm.call_args_list[0][0][0]
        assert "UNIQUE_FULLTEXT_MARKER" not in first_prompt
        # A decisive abstract answer means no escalation call at all.
        assert mock_llm.call_count == 1

    def test_outcome_reasoning_returned_from_llm(self, tmp_path):
        result, _ = self._run_llm(tmp_path)
        assert "outcome_reasoning" in result
        assert result["outcome_reasoning"] == "All effects replicated."

    def test_outcome_reasoning_empty_on_keyword_hit(self):
        # #70: keyword short-circuit is no_llm-only now; with the LLM off a keyword hit
        # still returns a keyword result with empty reasoning.
        result = extract_outcome(
            "10.1234/kw", abstract_r="we failed to replicate the original finding",
            no_llm=True,
        )
        assert result["outcome"] == "failure"
        assert result.get("outcome_reasoning", "") == ""

    def test_outcome_reasoning_empty_on_llm_failure(self):
        with patch("extract.code_outcome.call_llm", return_value=(None, "", "")):
            result = extract_outcome("10.1234/fail2", abstract_r="ambiguous")
        assert result.get("outcome_reasoning", "") == ""

    def test_prompt_asks_for_is_genuine_attempt(self, tmp_path):
        _, mock_llm = self._run_llm(tmp_path)
        prompt = mock_llm.call_args[0][0]
        assert "is_genuine_attempt" in prompt

    def test_not_a_genuine_attempt_forces_not_a_replication_outcome(self, tmp_path):
        llm_return = {
            "is_genuine_attempt": False,
            "outcome": "success",
            "outcome_phrase": "unrelated colloquial use of the word replication",
            "outcome_confidence": "high",
            "out_quote_source": "abstract",
            "outcome_reasoning": "The text uses 'replication' metaphorically and never "
                                 "engages with the named original study.",
        }
        result, _ = self._run_llm(tmp_path, llm_return=llm_return)
        assert result["outcome"] == "not_a_replication"

    def test_genuine_attempt_true_keeps_model_outcome(self, tmp_path):
        llm_return = {
            "is_genuine_attempt": True,
            "outcome": "failure",
            "outcome_phrase": "We did not find support for the original effect.",
            "outcome_confidence": "high",
            "out_quote_source": "abstract",
            "outcome_reasoning": "Authors explicitly state the effect did not replicate.",
        }
        result, _ = self._run_llm(tmp_path, llm_return=llm_return)
        assert result["outcome"] == "failure"

    def test_missing_is_genuine_attempt_field_defaults_to_true(self, tmp_path):
        """Backward compatibility: a model response without the new field (e.g. from
        stale test doubles) must not be treated as a false positive by default."""
        llm_return = {
            "outcome": "success",
            "outcome_phrase": "We confirmed the effect.",
            "outcome_confidence": "high",
            "out_quote_source": "abstract",
            "outcome_reasoning": "All effects replicated.",
        }
        result, _ = self._run_llm(tmp_path, llm_return=llm_return)
        assert result["outcome"] == "success"


# ── Outcome-coding unification tests ─────────────────────────────────────────

class TestOutcomeEnumSingleSource:
    """The outcome enum is defined once in schema and imported everywhere."""

    def test_code_outcome_valid_is_schema_categories(self):
        assert code_outcome._VALID_OUTCOMES is OUTCOME_CATEGORIES

    def test_run_extract_valid_is_schema_categories(self):
        assert run_extract._VALID_OUTCOMES is OUTCOME_CATEGORIES

    def test_cannot_be_determined_present(self):
        assert "cannot_be_determined" in OUTCOME_CATEGORIES

    def test_categories_are_exact(self):
        # not_a_replication is a genuine classifier output (is_genuine_attempt=false),
        # so it belongs in the category enum. uninformative and
        # statistically_successful_but_flawed are FLoRA codebook categories restored
        # in the rule-alignment pass — see shared/schema.py.
        assert OUTCOME_CATEGORIES == {
            "success", "failure", "mixed", "descriptive",
            "statistically_successful_but_flawed", "uninformative",
            "cannot_be_determined", "not_a_replication",
        }

    def test_uninformative_is_a_live_category_not_a_legacy_value(self):
        """FLoRA's 'the authors say their study is uninformative' is a coding, not a
        historical artefact — and is distinct from 'we could not tell'."""
        from shared.schema import OUTCOME_LEGACY_VALUES, OUTCOME_VALUES
        assert "uninformative" in OUTCOME_CATEGORIES
        assert "uninformative" in OUTCOME_VALUES
        assert "uninformative" not in OUTCOME_LEGACY_VALUES

    def test_flawed_success_is_distinguishable_from_success(self):
        assert "statistically_successful_but_flawed" in OUTCOME_CATEGORIES


class TestKeywordScanNoFulltext:
    """The fulltext keyword scan was removed — only title + abstract are scanned."""

    def test_fulltext_only_signal_does_not_fire_keyword(self, tmp_path):
        # A clear failure phrase lives ONLY in the fulltext (background prose about
        # another study). With no_llm and no abstract/title signal, the result must
        # not be classified as failure from the fulltext.
        result = extract_outcome(
            "10.1234/ftkw",
            abstract_r="",
            fulltext="Prior work by Jones failed to replicate the classic effect.",
            title_r="",
            no_llm=True,
        )
        assert result["outcome"] == "cannot_be_determined"

    def test_abstract_signal_still_fires(self):
        result = extract_outcome(
            "10.1234/abskw",
            abstract_r="We failed to replicate the original finding.",
            no_llm=True,
        )
        assert result["outcome"] == "failure"
        assert result["out_quote_source"] == "abstract"


class TestFulltextEscalation:
    _ABS_CBD = {"outcome": "cannot_be_determined", "outcome_phrase": "",
                "outcome_confidence": "low", "out_quote_source": "abstract",
                "outcome_reasoning": "abstract too thin"}
    _FT_FAIL = {"outcome": "failure", "outcome_phrase": "The effect did not replicate.",
                "outcome_confidence": "high", "out_quote_source": "fulltext",
                "outcome_reasoning": "results section is explicit"}

    def test_escalation_fires_on_cannot_be_determined(self, tmp_path):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(self._ABS_CBD, "m", ""), (self._FT_FAIL, "m", "")]) as mock_llm:
            result = extract_outcome(
                "10.1234/esc", abstract_r="ambiguous abstract",
                fulltext="RESULTS: the effect did not replicate.", title_r="A Study",
            )
        assert mock_llm.call_count == 2
        # Second (escalation) prompt must contain the parsed fulltext.
        assert "did not replicate" in mock_llm.call_args_list[1][0][0]
        assert result["outcome"] == "failure"
        assert result["out_quote_source"] == "fulltext"

    def test_no_escalation_when_flag_off(self, tmp_path):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", False), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(self._ABS_CBD, "m", ""), (self._FT_FAIL, "m", "")]) as mock_llm:
            result = extract_outcome(
                "10.1234/noesc", abstract_r="ambiguous abstract",
                fulltext="RESULTS: the effect did not replicate.", title_r="A Study",
            )
        assert mock_llm.call_count == 1
        assert result["outcome"] == "cannot_be_determined"

    def test_no_escalation_when_no_fulltext(self, tmp_path):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(self._ABS_CBD, "m", "")]) as mock_llm:
            result = extract_outcome(
                "10.1234/noft", abstract_r="ambiguous abstract",
                fulltext="", title_r="A Study",
            )
        assert mock_llm.call_count == 1
        assert result["outcome"] == "cannot_be_determined"

    def test_escalation_fires_on_empty_abstract(self, tmp_path):
        # No abstract → escalate even though the abstract call did not return cbd.
        abs_success = {"outcome": "success", "outcome_phrase": "", "outcome_confidence": "low",
                       "out_quote_source": "title", "outcome_reasoning": ""}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(abs_success, "m", ""), (self._FT_FAIL, "m", "")]) as mock_llm:
            result = extract_outcome(
                "10.1234/emptyabs", abstract_r="",
                fulltext="RESULTS: the effect did not replicate.", title_r="A Study",
            )
        assert mock_llm.call_count == 2
        assert result["outcome"] == "failure"

    def test_failed_escalation_is_not_cached(self, tmp_path):
        """Caching the abstract's cannot_be_determined after the fulltext call died
        would retire the escalation permanently."""
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome._call_outcome_llm",
                   side_effect=[(self._ABS_CBD, "m"), (None, "")]) as mock_llm:
            result = extract_outcome(
                "10.1234/escfail", abstract_r="ambiguous abstract",
                fulltext="RESULTS: the effect did not replicate.", title_r="A Study",
            )
        assert mock_llm.call_count == 2
        assert result["outcome"] == "cannot_be_determined"
        assert list(tmp_path.glob("outcome_*.json")) == []

        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome._call_outcome_llm",
                   side_effect=[(self._ABS_CBD, "m"), (self._FT_FAIL, "m")]) as retry:
            result = extract_outcome(
                "10.1234/escfail", abstract_r="ambiguous abstract",
                fulltext="RESULTS: the effect did not replicate.", title_r="A Study",
            )
        assert retry.call_count == 2
        assert result["outcome"] == "failure"


class TestOutcomePromptContent:
    def _prompt(self, tmp_path, **kw):
        ret = {"outcome": "success", "outcome_phrase": "x", "confidence": "high",
               "out_quote_source": "abstract", "outcome_reasoning": ""}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(ret, "m", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            extract_outcome("10.1234/pr", abstract_r="ambiguous abstract", title_r="T", **kw)
        return mock_llm.call_args_list[0][0][0]

    def test_example_one_relabelled_descriptive(self, tmp_path):
        prompt = self._prompt(tmp_path)
        assert "1. DESCRIPTIVE" in prompt

    def test_uninformative_and_flawed_success_are_offered(self, tmp_path):
        """Both FLoRA categories must appear in the rules AND in the JSON enum — a
        category defined in prose but absent from the enum is silently coerced away."""
        prompt = self._prompt(tmp_path)
        assert "- uninformative:" in prompt
        assert "- statistically_successful_but_flawed:" in prompt
        assert ('"outcome": "<success|failure|mixed|descriptive|'
                'statistically_successful_but_flawed|uninformative|'
                'cannot_be_determined>"') in prompt

    def test_no_default_to_cannot_be_determined_line(self, tmp_path):
        prompt = self._prompt(tmp_path)
        assert "rather than 'uninformative'" not in prompt

    def test_abstract_prompt_quote_source_excludes_fulltext(self, tmp_path):
        prompt = self._prompt(tmp_path)
        assert '"out_quote_source": "<abstract|title>"' in prompt

    def test_abstract_truncated_at_3000(self, tmp_path):
        long_abstract = ("A" * 2999) + "MARKER_INSIDE" + ("B" * 3000) + "MARKER_OUTSIDE"
        ret = {"outcome": "success", "outcome_phrase": "x", "confidence": "high",
               "out_quote_source": "abstract", "outcome_reasoning": ""}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(ret, "m", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            extract_outcome("10.1234/trunc", abstract_r=long_abstract, title_r="T")
        prompt = mock_llm.call_args_list[0][0][0]
        assert "MARKER_OUTSIDE" not in prompt
        assert "MARKER_INSIDE" not in prompt  # sits just past the 3000-char cap
        assert "…" in prompt


class TestOutcomeCacheKey:
    """One content-keyed entry per outcome answer — no legacy DOI-only key."""

    _RET = {"outcome": "success", "outcome_phrase": "x", "confidence": "high",
            "out_quote_source": "abstract", "outcome_reasoning": ""}

    def _run(self, tmp_path, **kwargs):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(self._RET, "m", "")) as mock, \
             patch("extract.code_outcome.time.sleep"):
            extract_outcome(kwargs.pop("doi", "10.1234/key"), **kwargs)
        return mock

    def test_single_entry_written(self, tmp_path):
        self._run(tmp_path, abstract_r="ambiguous abstract", title_r="T")
        assert len(list(tmp_path.glob("outcome_*.json"))) == 1

    def test_same_inputs_hit_cache(self, tmp_path):
        self._run(tmp_path, abstract_r="ambiguous abstract", title_r="T")
        mock = self._run(tmp_path, abstract_r="ambiguous abstract", title_r="T")
        assert mock.call_count == 0

    def test_changed_abstract_misses(self, tmp_path):
        self._run(tmp_path, abstract_r="one abstract", title_r="T")
        mock = self._run(tmp_path, abstract_r="another abstract", title_r="T")
        assert mock.call_count == 1
        assert len(list(tmp_path.glob("outcome_*.json"))) == 2

    def test_changed_record_type_misses(self, tmp_path):
        self._run(tmp_path, abstract_r="a", title_r="T")
        mock = self._run(tmp_path, abstract_r="a", title_r="T", record_type="reproduction")
        assert mock.call_count == 1

    def test_changed_fulltext_misses(self, tmp_path):
        """The escalation reads the fulltext, so a re-parsed PDF must not replay the
        verdict reached without it."""
        self._run(tmp_path, abstract_r="a", title_r="T", fulltext="old text")
        mock = self._run(tmp_path, abstract_r="a", title_r="T", fulltext="new text")
        assert mock.call_count == 1

    def test_prompt_version_in_key(self, tmp_path, monkeypatch):
        from shared import prompts
        self._run(tmp_path, abstract_r="a", title_r="T")
        monkeypatch.setattr(prompts, "OUTCOME_RULES", prompts.OUTCOME_RULES + " EDIT")
        prompts.prompt_version.cache_clear()
        try:
            mock = self._run(tmp_path, abstract_r="a", title_r="T")
        finally:
            prompts.prompt_version.cache_clear()
        assert mock.call_count == 1

    def test_accumulate_env_var_is_gone(self):
        import shared.cache as cache_mod
        import shared.config as config_mod
        assert not hasattr(cache_mod, "read_dual_cache")
        assert not hasattr(cache_mod, "write_dual_cache")
        assert not hasattr(config_mod, "LLM_CACHE_READ")

    def test_content_key_shape_allows_per_doi_glob(self, tmp_path):
        from shared.utils import cache_key as _ck
        key = content_key("outcome", "10.1/x", "a", "b")
        assert key.startswith(f"outcome_{_ck('10.1/x')}_")
        assert content_key("outcome", "10.1/x", "a", "c") != key


def write_cache_json(cache_dir, key, data):
    import json as _json
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(_json.dumps(data), encoding="utf-8")


# ── classify_match_type unit tests (Issue 8) ─────────────────────────────────

# Two distinct author-year pairs: below that the LLM match-type call is gated off
# (audit E1) and classify_match_type returns single_original without asking.
_ROW = {
    "doi_r": "10.1000/test",
    "title_r": "A Replication Study",
    "abstract_r": "We replicated Smith (2010) and Jones (2012) and found "
                  "consistent results.",
    "year_r": "2020",
    "openalex_id_r": "W999",
}

_CAND_SINGLE = [{"title": "Smith Study", "year": 2010, "first_author": "Smith",
                 "doi": "10.999/smith", "openalex_id": "W111", "all_authors": ["Smith"]}]
_CAND_MULTI  = [
    {"title": "Smith Study A", "year": 2010, "first_author": "Smith",
     "doi": "10.999/a", "openalex_id": "W111", "all_authors": ["Smith"]},
    {"title": "Smith Study B", "year": 2010, "first_author": "Smith",
     "doi": "10.999/b", "openalex_id": "W222", "all_authors": ["Smith"]},
]


class TestClassifyMatchType:
    """Issue 8 — unit tests for classify_match_type.

    All external calls (OpenAlex, LLM) are mocked.
    """

    def _classify(self, tmp_path, oa_result, llm_result, row=None):
        """Helper: run classify_match_type with mocked OpenAlex + LLM."""
        row = row or _ROW
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates", return_value=oa_result), \
             patch("extract.run_extract.call_llm", return_value=(llm_result, "gemini-model", "")):
            return classify_match_type(row)

    def test_returns_single_original(self, tmp_path):
        llm = {"original_match_type": "single_original",
               "confidence": "high", "reasoning": "one clear target"}
        result = self._classify(tmp_path, _CAND_SINGLE, llm)
        assert result["original_match_type"] == "single_original"
        assert result["original_match_confidence"] == "high"

    def test_returns_multiple_match(self, tmp_path):
        llm = {"original_match_type": "multiple_match",
               "confidence": "high", "reasoning": "same author/year"}
        result = self._classify(tmp_path, _CAND_MULTI, llm)
        assert result["original_match_type"] == "multiple_match"

    def test_returns_multiple_original(self, tmp_path):
        row = dict(_ROW, abstract_r="We replicated Smith (2010) and Jones (2012).")
        llm = {"original_match_type": "multiple_original",
               "confidence": "medium", "reasoning": "two independent targets"}
        result = self._classify(tmp_path, _CAND_MULTI, llm, row=row)
        assert result["original_match_type"] == "multiple_original"
        assert result["original_match_confidence"] == "medium"

    def test_openalex_failure_defaults_to_single_original(self, tmp_path):
        """OpenAlex exception should return single_original without crashing."""
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates",
                   side_effect=ConnectionError("timeout")):
            result = classify_match_type(_ROW)
        assert result["original_match_type"] == "single_original"
        assert result["original_match_confidence"] == "low"

    def test_llm_failure_defaults_to_single_original(self, tmp_path):
        """LLM failure should return single_original without crashing."""
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates", return_value=_CAND_SINGLE), \
             patch("extract.run_extract.call_llm", return_value=(None, "", "quota | error")):
            result = classify_match_type(_ROW)
        assert result["original_match_type"] == "single_original"
        assert result["original_match_confidence"] == "low"

    def test_result_cached_on_second_call(self, tmp_path):
        """Second call with the same inputs must not repeat the LLM call.

        The candidate list is part of the key, so the (disk-cached) OpenAlex lookup
        runs first and is expected to be called both times.
        """
        llm = {"original_match_type": "single_original",
               "confidence": "high", "reasoning": "cached"}
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates",
                   return_value=_CAND_SINGLE), \
             patch("extract.run_extract.call_llm", return_value=(llm, "gemini-model", "")) as mock_llm:
            classify_match_type(_ROW)  # first call — populates cache
            classify_match_type(_ROW)  # second call — should use cache
        assert mock_llm.call_count == 1

    def test_changed_candidates_miss_the_cache(self, tmp_path):
        """A different candidate list is a different question — it must be re-asked."""
        llm = {"original_match_type": "single_original", "confidence": "high"}
        other = [dict(_CAND_SINGLE[0], title="A completely different paper")]
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.call_llm", return_value=(llm, "m", "")) as mock_llm:
            with patch("extract.run_extract.find_all_candidates", return_value=_CAND_SINGLE):
                classify_match_type(_ROW)
            with patch("extract.run_extract.find_all_candidates", return_value=other):
                classify_match_type(_ROW)
        assert mock_llm.call_count == 2

    def test_invalid_llm_match_type_normalised(self, tmp_path):
        """LLM returning an unknown match_type value should become single_original."""
        llm = {"original_match_type": "unknown_value", "confidence": "high"}
        result = self._classify(tmp_path, _CAND_SINGLE, llm)
        assert result["original_match_type"] == "single_original"

    def test_prompt_includes_pattern_count_and_candidates(self, tmp_path):
        """The LLM prompt must include distinct pattern count and candidate list."""
        captured_prompt = []
        def fake_llm(prompt, gemini_model=""):
            captured_prompt.append(prompt)
            return ({"original_match_type": "single_original",
                     "confidence": "high"}, "gemini-model", "")

        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates", return_value=_CAND_SINGLE), \
             patch("extract.run_extract.call_llm", side_effect=fake_llm):
            classify_match_type(_ROW)

        prompt = captured_prompt[0]
        assert "distinct" in prompt.lower()
        assert "smith" in prompt.lower()          # candidate first_author
        assert "Smith Study" in prompt             # candidate title


# ── run_extract orchestration tests ──────────────────────────────────────────

_MOCK_LINK = {
    "resolution_method": "same_author_year_title_overlap",
    "resolution_score": 0.95,
    "resolved_doi_o": "10.1037/h0054651",
    "resolved_title_o": "The Original Study",
    "resolved_year_o": 1935,
    "resolved_author_o": "Smith",
    "llm_evidence": "Smith (1935)",
    "grobid_intro": "",
    "html_text": "",
}
_MOCK_OUTCOME = {
    "outcome": "success", "outcome_phrase": "replicated",
    "outcome_confidence": "high", "out_quote_source": "abstract",
    "outcome_reasoning": "", "llm_model": "gemini-outcome",
}
_MOCK_MULTI = {
    "is_false_positive": False,
    "n_originals": 2,
    "originals": [
        {"rank": 1, "title": "Study A", "doi": "10.1000/a", "first_author": "Jones",
         "year": 2000, "evidence": "Jones et al. (2000)", "confidence": "high"},
        {"rank": 2, "title": "Study B", "doi": "10.1000/b", "first_author": "Kim",
         "year": 2001, "evidence": "Kim et al. (2001)", "confidence": "medium"},
    ],
    "originals_json": "[]",
}
_MOCK_MATCH = {"original_match_type": "single_original", "original_match_confidence": "high"}

# Stage 3's front door: both classifiers agree the paper is a replication, so the
# row goes down the ladder exactly as it did before the screen moved to the front.
_YES_SCREEN = {
    "resolution_method": "llm_refscreen_declined", "screen_verdict": "proceed",
    "screen_classification": "replication", "record_type": "replication",
    "categories": ["clearly_declared", "context_transfer"],
    "votes": [{"provider": "gemini", "classification": "replication",
               "confident": True, "categories": ["clearly_declared"], "reasoning": "r"},
              {"provider": "openai", "classification": "replication",
               "confident": True, "categories": ["context_transfer"], "reasoning": "r"}],
    "llm_source": "gemini+openai", "llm_model": "flash-lite+gpt-5.4-mini",
    "llm_evidence": "", "llm_reasoning": "", "llm_prompt": "", "llm_error": "",
}


def _vote(provider, classification, confident=True, categories=()):
    return {"provider": provider, "classification": classification,
            "confident": confident, "categories": list(categories), "reasoning": "r"}


class TestRunExtract:
    @pytest.fixture(autouse=True)
    def _screen_providers_configured(self, monkeypatch):
        """run_extract refuses to start unless both Q1 screen providers are configured.
        These tests mock every LLM call, so satisfy the check rather than bypass it."""
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["test-key"])
        monkeypatch.setattr(run_extract, "OPENROUTER_API_KEY", "test-key")

    def _run(self, filtered_csv: str, mock_multi=None, mock_match=None, screen=None,
             **run_kwargs):
        """Helper: write a temp CSV, run extract with mocked APIs, return result DataFrame."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                        delete=False, encoding="utf-8-sig") as f:
            f.write(filtered_csv)
            tmp = Path(f.name)

        with patch("extract.run_extract.classify_replication",
                   return_value=screen or _YES_SCREEN), \
             patch("extract.run_extract.classify_match_type",
                   return_value=mock_match or _MOCK_MATCH), \
             patch("extract.run_extract.run_for_doi", return_value=_MOCK_LINK), \
             patch("extract.run_extract.run_multi_original_for_doi",
                   return_value=mock_multi or {
                       "is_false_positive": False, "n_originals": 0,
                       "originals": [], "originals_json": "[]"}), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME), \
             patch("extract.run_extract.verify_and_correct",
                   side_effect=lambda doi, *a, **k: {"doi_o": doi,
                                                     "doi_o_verification": "skipped",
                                                     "evidence_note": ""}), \
             patch("extract.run_extract._oa_by_doi", return_value=None), \
             patch("extract.run_extract.DATA_DIR", tmp.parent), \
             patch("extract.run_extract.BASE_DIR", tmp.parent):
            filtered_path = tmp.parent / "filtered.csv"
            if not filtered_path.exists():
                filtered_path.write_text(tmp.read_text(encoding="utf-8-sig"),
                                         encoding="utf-8-sig")
            from extract.run_extract import run_extract
            result = run_extract(**run_kwargs)

        tmp.unlink(missing_ok=True)
        (tmp.parent / "filtered.csv").unlink(missing_ok=True)
        (tmp.parent / "extracted.csv").unlink(missing_ok=True)
        return result

    def test_output_has_all_schema_columns(self):
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/test,Test Paper,Abstract text,2020,Smith,J. Psych,,W999,openalex,"
            "replication,rule_based,direct replication,high\n"
        )
        result = self._run(csv)
        missing = [c for c in EXTRACTED_COLS if c not in result.columns]
        assert not missing, f"Missing: {missing}"

    def test_false_positives_are_skipped(self):
        """False positives must appear in output without calling classify_match_type."""
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/fp,False Pos,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "false_positive,rule_based,not a replication,high\n"
            "10.1000/rep,Real Rep,Abstract,2020,Jones,J. Psych,,W2,openalex,"
            "replication,rule_based,direct replication,high\n"
        )
        mock_classify = MagicMock(return_value=_MOCK_MATCH)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                         delete=False, encoding="utf-8-sig") as f:
            f.write(csv)
            tmp = Path(f.name)

        with patch("extract.run_extract.classify_replication", return_value=_YES_SCREEN), \
             patch("extract.run_extract.classify_match_type", mock_classify), \
             patch("extract.run_extract.run_for_doi", return_value=_MOCK_LINK), \
             patch("extract.run_extract.run_multi_original_for_doi",
                   return_value={"is_false_positive": False, "n_originals": 0,
                                 "originals": [], "originals_json": "[]"}), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME), \
             patch("extract.run_extract.verify_and_correct",
                   side_effect=lambda doi, *a, **k: {"doi_o": doi,
                                                     "doi_o_verification": "skipped",
                                                     "evidence_note": ""}), \
             patch("extract.run_extract._oa_by_doi", return_value=None), \
             patch("extract.run_extract.DATA_DIR", tmp.parent), \
             patch("extract.run_extract.BASE_DIR", tmp.parent):
            fp_path = tmp.parent / "filtered.csv"
            fp_path.write_text(tmp.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
            from extract.run_extract import run_extract
            result = run_extract()

        tmp.unlink(missing_ok=True)
        fp_path.unlink(missing_ok=True)
        (tmp.parent / "extracted.csv").unlink(missing_ok=True)

        # false_positive rows are skipped, not written (run_extract.py:1021-1023;
        # they are known non-replications and must not enter extracted.csv / Stage 4).
        assert len(result) == 1
        doi_set = set(result["doi_r"])
        assert "10.1000/fp" not in doi_set
        assert "10.1000/rep" in doi_set
        # classify_match_type called only for the replication row, not the false positive
        assert mock_classify.call_count == 1
        called_doi = mock_classify.call_args[0][0].get("doi_r") or ""
        assert "fp" not in called_doi

    def test_link_confidence_is_categorical(self):
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/test,Test,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "replication,rule_based,direct replication,high\n"
        )
        result = self._run(csv)
        assert result.iloc[0]["link_confidence"] in {"high", "medium", "low"}

    def test_multiple_original_expands_rows(self):
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/multi,Multi-target,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "replication,rule_based,direct replication,high\n"
        )
        result = self._run(csv,
                           mock_multi=_MOCK_MULTI,
                           mock_match={"original_match_type": "multiple_original",
                                       "original_match_confidence": "high"})
        assert len(result) == 2
        assert list(result["original_rank"].astype(int)) == [1, 2]
        assert list(result["n_originals"].astype(int)) == [2, 2]

    _TYPE_CSV = (
        "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
        "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
        "10.1000/rep,Rep Paper,Abstract,2020,Smith,J. Psych,,W1,openalex,"
        "replication,rule_based,direct replication,high\n"
        "10.1000/repro,Repro Paper,Abstract,2020,Jones,J. Psych,,W2,openalex,"
        "reproduction,rule_based,reproduction study,high\n"
    )

    def test_type_column_comes_from_the_screen_not_filter_status(self):
        """The screen read the abstract and said what the paper is, so its verdict
        overrides Stage 2's guess for both rows."""
        result = self._run(self._TYPE_CSV,
                           screen={**_YES_SCREEN, "record_type": "reproduction"})
        assert set(result["type"]) == {"reproduction"}

    def test_type_column_falls_back_to_filter_status_without_an_llm(self):
        """--no-llm runs no screen, so Stage 2's filter_status is all there is."""
        result = self._run(self._TYPE_CSV, no_llm=True)
        types = dict(zip(result["doi_r"], result["type"]))
        assert types["10.1000/rep"] == "replication"
        assert types["10.1000/repro"] == "reproduction"
        assert set(result["screen_categories"]) == {""}

    def test_classify_not_called_for_false_positives(self):
        """Routing test: false_positive must bypass classify_match_type entirely."""
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/fp,FP,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "false_positive,rule_based,meta-discussion,high\n"
        )
        mock_classify = MagicMock(return_value=_MOCK_MATCH)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                         delete=False, encoding="utf-8-sig") as f:
            f.write(csv)
            tmp = Path(f.name)

        with patch("extract.run_extract.classify_replication", return_value=_YES_SCREEN), \
             patch("extract.run_extract.classify_match_type", mock_classify), \
             patch("extract.run_extract.run_for_doi", return_value=_MOCK_LINK), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME), \
             patch("extract.run_extract.verify_and_correct",
                   side_effect=lambda doi, *a, **k: {"doi_o": doi,
                                                     "doi_o_verification": "skipped",
                                                     "evidence_note": ""}), \
             patch("extract.run_extract._oa_by_doi", return_value=None), \
             patch("extract.run_extract.DATA_DIR", tmp.parent), \
             patch("extract.run_extract.BASE_DIR", tmp.parent):
            fp_path = tmp.parent / "filtered.csv"
            fp_path.write_text(tmp.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
            from extract.run_extract import run_extract
            run_extract()

        tmp.unlink(missing_ok=True)
        fp_path.unlink(missing_ok=True)
        (tmp.parent / "extracted.csv").unlink(missing_ok=True)

        mock_classify.assert_not_called()


    def test_api_error_passthrough(self):
        """When extraction throws an exception, link_method and outcome must be
        'api_error' and the row must still appear in the output."""
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/fail,Fail Paper,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "replication,rule_based,direct replication,high\n"
        )
        result = self._run(csv)  # run_for_doi is mocked to return _MOCK_LINK by default
        # Now run again but force run_for_doi to raise
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                         delete=False, encoding="utf-8-sig") as f:
            f.write(csv)
            tmp = Path(f.name)

        with patch("extract.run_extract.classify_replication", return_value=_YES_SCREEN), \
             patch("extract.run_extract.classify_match_type", return_value=_MOCK_MATCH), \
             patch("extract.run_extract.run_for_doi", side_effect=Exception("API timeout")), \
             patch("extract.run_extract.run_multi_original_for_doi",
                   return_value={"is_false_positive": False, "n_originals": 0,
                                 "originals": [], "originals_json": "[]"}), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME), \
             patch("extract.run_extract.verify_and_correct",
                   side_effect=lambda doi, *a, **k: {"doi_o": doi,
                                                     "doi_o_verification": "skipped",
                                                     "evidence_note": ""}), \
             patch("extract.run_extract._oa_by_doi", return_value=None), \
             patch("extract.run_extract.DATA_DIR", tmp.parent), \
             patch("extract.run_extract.BASE_DIR", tmp.parent):
            fp_path = tmp.parent / "filtered.csv"
            fp_path.write_text(tmp.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
            from extract.run_extract import run_extract
            result = run_extract()

        tmp.unlink(missing_ok=True)
        fp_path.unlink(missing_ok=True)
        (tmp.parent / "extracted.csv").unlink(missing_ok=True)

        assert len(result) == 1, "Row must not be dropped on extraction failure"
        assert result.iloc[0]["link_method"] == "api_error"
        assert result.iloc[0]["outcome"] == "api_error"

    def test_get_outcome_receives_original_study_info(self):
        """_get_outcome must pass resolved_title_o/author_o/year_o to extract_outcome."""
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/rep,Rep Paper,Abstract,2020,Jones,J. Psych,,W2,openalex,"
            "replication,rule_based,direct replication,high\n"
        )
        with patch("extract.run_extract.classify_replication", return_value=_YES_SCREEN), \
             patch("extract.run_extract.classify_match_type", return_value=_MOCK_MATCH), \
             patch("extract.run_extract.run_for_doi", return_value=_MOCK_LINK), \
             patch("extract.run_extract.run_multi_original_for_doi",
                   return_value={"is_false_positive": False, "n_originals": 0,
                                 "originals": [], "originals_json": "[]"}), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME) as mock_eo, \
             patch("extract.run_extract.DATA_DIR", Path(tempfile.gettempdir())), \
             patch("extract.run_extract.BASE_DIR", Path(tempfile.gettempdir())):
            fp = Path(tempfile.gettempdir()) / "filtered.csv"
            fp.write_text(csv, encoding="utf-8-sig")
            out = Path(tempfile.gettempdir()) / "extracted.csv"
            from extract.run_extract import run_extract
            run_extract()
            fp.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

        call_kwargs = mock_eo.call_args[1]
        assert call_kwargs.get("original_title") == "The Original Study"
        assert call_kwargs.get("original_authors") == "Smith"
        assert call_kwargs.get("original_year") == "1935"


# ── Granular link_method labels ──────────────────────────────────────────────

class TestGranularLinkMethods:
    """The five rule-based resolution methods must pass through as distinct public
    link_method values instead of collapsing to author_year_match."""

    GRANULAR = [
        "citation_context_match",
        "same_author_year_title_overlap",
        "single_candidate_after_requery",
        "title_pattern_match",
        "grobid_ref_match",
    ]

    @pytest.mark.parametrize("method", GRANULAR)
    def test_map_method_passes_through(self, method):
        assert _map_method(method) == method

    def test_no_method_maps_to_author_year_match(self):
        for method in self.GRANULAR:
            assert _map_method(method) != "author_year_match"

    @pytest.mark.parametrize("method", GRANULAR)
    def test_merge_row_emits_granular_label(self, method):
        link = {
            "resolution_method": method,
            "resolved_doi_o": "10.1/orig", "resolved_title_o": "Original",
            "resolved_year_o": 2000, "resolved_author_o": "Smith",
            "resolution_score": 1.0, "llm_confidence": "high",
        }
        filter_row = pd.Series({"doi_r": "10.1/rep", "title_r": "Rep",
                                "filter_status": "replication"})
        with patch("extract.run_extract._build_ref_o", return_value=("ref", "auth")):
            row = _merge_row(filter_row, link, _MOCK_OUTCOME,
                             "single_original", "high", 1, 1)
        assert row["link_method"] == method


# ── pair_id identity: stability for DOI rows, distinctness without a DOI ──────

class TestMakePairId:
    def test_doi_pair_hashes_are_frozen(self):
        """pair_id is the identity key the validation DB already holds, and csv_to_db
        skips pair_ids it has seen. If the hash of a row with a doi_o ever changes,
        every imported record re-imports as a duplicate — so these literals are the
        pre-fallback md5("doi_r|doi_o") values and must never move."""
        assert (make_pair_id("10.1/rep", "10.2/orig")
                == "cdb1325243087bf3f8292ff737cf69cc")
        assert (make_pair_id("10.25669/9kzj-tc3j", "10.1037/0022-3514.51.6.1173")
                == "22e94c46165158b30a740f3e66114c82")
        assert make_pair_id("", "") == "b99834bc19bbad24580b3adfa04fb947"

    def test_identifierless_row_keeps_its_legacy_hash(self):
        """extracted.csv holds 129 single-original rows with a blank doi_o AND a blank
        oa_work_id_o, already keyed on md5("doi_r|") in the validation DB. Nothing may
        re-key them, which is why the single-original writer never passes title_o —
        this literal is the real pair_id of one of those rows."""
        assert (make_pair_id("10.34917/4332616", "", "")
                == "422738f9134f6255828b6088979c7ae3")

    def test_extra_arguments_are_ignored_when_doi_o_is_set(self):
        assert (make_pair_id("10.1/rep", "10.2/orig", "W123", "A Title")
                == make_pair_id("10.1/rep", "10.2/orig"))

    def test_returns_32_char_hex(self):
        pid = make_pair_id("10.1/rep", "", "W2003152982")
        assert len(pid) == 32 and all(c in "0123456789abcdef" for c in pid)

    def test_two_doi_less_originals_of_one_replication_are_distinct(self):
        """The collision this fallback exists to fix: without it both originals
        hash to "doi_r|" and csv_to_db silently drops one of them."""
        a = make_pair_id("10.1/rep", "", "W1")
        b = make_pair_id("10.1/rep", "", "W2")
        c = make_pair_id("10.1/rep", "", "", "Gender Advertisements")
        d = make_pair_id("10.1/rep", "", "", "Frame Analysis")
        assert len({a, b, c, d, make_pair_id("10.1/rep", "")}) == 5

    def test_work_id_wins_over_title(self):
        assert (make_pair_id("10.1/rep", "", "W1", "Some Title")
                == make_pair_id("10.1/rep", "", "W1", "Another Title"))

    def test_work_id_form_and_title_whitespace_are_normalised(self):
        assert (make_pair_id("10.1/rep", "", "https://openalex.org/W1")
                == make_pair_id("10.1/rep", "", "w1"))
        assert (make_pair_id("10.1/rep", "", "", "  Gender   Advertisements ")
                == make_pair_id("10.1/rep", "", "", "Gender Advertisements"))

    def test_non_work_openalex_id_falls_through_to_title(self):
        assert (make_pair_id("10.1/rep", "", "A5023888391", "T")
                == make_pair_id("10.1/rep", "", "", "T"))


# ── Multi-original pair_id uniqueness + truthful link_method ──────────────────

class TestMergeMultiRow:
    _FILTER_ROW = pd.Series({"doi_r": "10.1/rep", "title_r": "Rep Paper",
                             "filter_status": "replication"})
    _OUTCOME = {"outcome": "success", "outcome_phrase": "",
                "outcome_confidence": "high", "out_quote_source": "llm_multi"}

    def _merge(self, orig, link_method="llm_fulltext"):
        with patch("extract.run_extract._build_ref_o", return_value=("", "")):
            return _merge_multi_row(self._FILTER_ROW, orig, self._OUTCOME,
                                    "multiple_original", "high", 2,
                                    link_method=link_method)

    def test_two_unresolved_originals_get_distinct_pair_ids(self):
        """Empty doi_o must not collapse every original to the same pair_id."""
        r1 = self._merge({"rank": 1, "doi": "", "title": "Original One",
                          "first_author": "A", "year": 2001, "confidence": "high"})
        r2 = self._merge({"rank": 2, "doi": "", "title": "Original Two",
                          "first_author": "B", "year": 2002, "confidence": "high"})
        assert r1["pair_id"] != r2["pair_id"]
        # And neither equals the naive make_pair_id(doi_r, "") that used to collide.
        collide = make_pair_id("10.1/rep", "")
        assert r1["pair_id"] != collide
        assert r2["pair_id"] != collide

    def test_resolved_doi_pair_id_is_deterministic(self):
        r = self._merge({"rank": 1, "doi": "10.1/x", "title": "X",
                         "first_author": "A", "year": 2001, "confidence": "high"})
        assert r["pair_id"] == make_pair_id("10.1/rep", "10.1/x")

    def test_two_doi_less_originals_with_openalex_ids_are_distinct(self):
        r1 = self._merge({"rank": 1, "doi": "", "title": "A Book",
                          "openalex_id": "W1", "confidence": "high"})
        r2 = self._merge({"rank": 2, "doi": "", "title": "A Book",
                          "openalex_id": "W2", "confidence": "high"})
        assert r1["pair_id"] != r2["pair_id"]

    def test_link_method_label_is_passed_through(self):
        r = self._merge({"rank": 1, "doi": "10.1/x", "title": "X",
                         "first_author": "A", "year": 2001, "confidence": "high"},
                        link_method="llm_cited_candidates")
        assert r["link_method"] == "llm_cited_candidates"


# ── Multi-original count regex bound ──────────────────────────────────────────

class TestMultiOriginalCountBound:
    """3 ≤ N < 1900 — a captured year is not a study count."""

    def test_year_in_title_not_treated_as_count(self):
        assert _rule_classify_multi_original("Replication of 2019 findings", "") is None

    def test_year_in_abstract_not_treated_as_count(self):
        assert _rule_classify_multi_original(
            "A paper", "We report replications of 2019 studies conducted earlier."
        ) is None

    def test_valid_count_in_title_routes_to_multiple_original(self):
        r = _rule_classify_multi_original("Replication of 12 studies", "")
        assert r is not None
        assert r["original_match_type"] == "multiple_original"

    def test_valid_count_in_abstract_routes_to_multiple_original(self):
        r = _rule_classify_multi_original(
            "A paper", "We replicated 28 classic studies across many labs."
        )
        assert r is not None
        assert r["original_match_type"] == "multiple_original"

    def test_count_below_minimum_does_not_route(self):
        assert _rule_classify_multi_original("Replication of 2 studies", "") is None

    def test_known_project_name_still_routes(self):
        r = _rule_classify_multi_original("Many Labs 2: replicating effects", "")
        assert r is not None
        assert r["original_match_type"] == "multiple_original"


# ── Schema integration test ───────────────────────────────────────────────────

def test_sample_extracted_schema():
    """sample_extracted.csv must contain all EXTRACTED_COLS."""
    df = pd.read_csv("misc/sample_extracted.csv", dtype=str,
                     on_bad_lines="skip").fillna("")
    missing = [c for c in EXTRACTED_COLS if c not in df.columns]
    assert not missing, f"Missing columns in sample_extracted.csv: {missing}"


# ── FLoRA skip-list (entry sheet + flora.csv) ────────────────────────────────

class TestFloraSkipDois:
    """Stage 3 must never re-extract a replication already in FLoRA.

    Two sources: the entry sheet (rows already validated) and flora.csv (the
    published database — every row is by definition already in FLoRA).
    """

    def _sheet(self, tmp_path, rows):
        p = tmp_path / "sheet.csv"
        pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
        return p

    def _flora(self, tmp_path, rows):
        p = tmp_path / "flora.csv"
        pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
        return p

    def test_validated_chosen_is_skipped(self, tmp_path):
        """Regression: 'validated - chosen' was omitted, so those rows leaked
        through to extraction and on to validation (e.g. 10.1037/per0000041)."""
        sheet = self._sheet(tmp_path, [
            {"doi_r": "10.1/chosen",    "validation_status": "validated - chosen"},
            {"doi_r": "10.1/unchanged", "validation_status": "validated - unchanged"},
            {"doi_r": "10.1/changed",   "validation_status": "validated - changed"},
        ])
        got = run_extract._load_flora_skip_dois(sheet, None)
        assert got == {"10.1/chosen", "10.1/unchanged", "10.1/changed"}

    def test_unvalidated_statuses_not_skipped(self, tmp_path):
        sheet = self._sheet(tmp_path, [
            {"doi_r": "10.1/blank",    "validation_status": ""},
            {"doi_r": "10.1/help",     "validation_status": "help needed"},
            {"doi_r": "10.1/hold",     "validation_status": "on hold"},
            {"doi_r": "10.1/awaiting", "validation_status": "awaiting validation"},
        ])
        assert run_extract._load_flora_skip_dois(sheet, None) == set()

    def test_flora_csv_skipped_wholesale(self, tmp_path):
        """flora.csv has no validation_status — everything in it is already in FLoRA."""
        flora = self._flora(tmp_path, [
            {"doi_r": "10.2/a", "doi_r_alt": ""},
            {"doi_r": "10.2/b", "doi_r_alt": "10.2/b-alt"},
            {"doi_r": "",       "doi_r_alt": ""},
        ])
        got = run_extract._load_flora_skip_dois(None, flora)
        assert got == {"10.2/a", "10.2/b", "10.2/b-alt"}

    def test_both_sources_are_unioned(self, tmp_path):
        sheet = self._sheet(tmp_path, [
            {"doi_r": "10.1/chosen", "validation_status": "validated - chosen"},
            {"doi_r": "10.1/blank",  "validation_status": ""},
        ])
        flora = self._flora(tmp_path, [{"doi_r": "10.2/a", "doi_r_alt": ""}])
        assert run_extract._load_flora_skip_dois(sheet, flora) == {"10.1/chosen", "10.2/a"}

    def test_missing_files_are_non_fatal(self, tmp_path):
        assert run_extract._load_flora_skip_dois(
            tmp_path / "nope.csv", tmp_path / "also-nope.csv") == set()

    def test_skip_is_on_by_default(self):
        import inspect
        sig = inspect.signature(run_extract.run_extract)
        assert sig.parameters["skip_flora_validated"].default is True


# ── Reproduction outcome coding (3x3 computation/robustness grid) ────────────

class TestReproductionOutcome:
    """Reproductions use a different vocabulary from replications; the row's
    type must select it, or every reproduction verdict is coerced away."""

    def test_grid_has_nine_values(self):
        from shared.schema import REPRODUCTION_OUTCOME_CATEGORIES as R
        assert len(R) == 9
        for comp in ("computationally successful", "computational issues",
                     "computation not checked"):
            for rob in ("robust", "robustness challenges", "robustness not checked"):
                assert f"{comp}, {rob}" in R

    def test_vocabulary_selected_by_type(self):
        from shared.schema import outcome_categories_for
        repro = outcome_categories_for("reproduction")
        repl = outcome_categories_for("replication")
        assert "computationally successful, robust" in repro
        assert "computationally successful, robust" not in repl
        assert "success" in repl and "success" not in repro
        assert "cannot_be_determined" in repro and "cannot_be_determined" in repl

    def test_repro_outcome_survives_normalisation(self, tmp_path):
        """A valid grid value must be kept, not coerced to cannot_be_determined."""
        mock = {"outcome": "computational issues, robustness challenges",
                "outcome_phrase": "x" * 400, "confidence": "high",
                "out_quote_source": "abstract", "outcome_reasoning": "r"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")), \
             patch("extract.code_outcome.time.sleep"):
            res = extract_outcome("10.1/repro", abstract_r="we re-ran their code",
                                  record_type="reproduction")
        assert res["outcome"] == "computational issues, robustness challenges"

    def test_replication_value_rejected_for_reproduction(self, tmp_path):
        """If the LLM answers with the replication vocabulary for a reproduction,
        it must NOT be accepted silently."""
        mock = {"outcome": "success", "outcome_phrase": "q", "confidence": "high",
                "out_quote_source": "abstract", "outcome_reasoning": "r"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")), \
             patch("extract.code_outcome.time.sleep"):
            res = extract_outcome("10.1/repro2", abstract_r="re-analysis",
                                  record_type="reproduction")
        assert res["outcome"] == "cannot_be_determined"

    def test_reproduction_skips_replication_keyword_scan(self, tmp_path):
        """'failed to replicate' in a reproduction abstract must not shortcut to
        the replication enum — it must reach the reproduction LLM prompt."""
        mock = {"outcome": "computational issues, robustness not checked",
                "outcome_phrase": "q", "confidence": "high",
                "out_quote_source": "abstract", "outcome_reasoning": "r"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            res = extract_outcome("10.1/repro3",
                                  abstract_r="We failed to replicate the reported numbers.",
                                  record_type="reproduction")
        assert mock_llm.called, "reproduction must not be short-circuited by keyword scan"
        assert res["outcome"] == "computational issues, robustness not checked"
        assert "REPRODUCTION" in mock_llm.call_args[0][0]
        assert "computationally successful, robust" in mock_llm.call_args[0][0]

    def test_replication_uses_keyword_scan_only_in_no_llm(self):
        # Replications use the keyword scan as the no_llm fallback; reproductions never do.
        with patch("extract.code_outcome.call_llm") as mock_llm:
            res = extract_outcome("10.1/repl", abstract_r="we found no evidence of the effect",
                                  record_type="replication", no_llm=True)
        mock_llm.assert_not_called()
        assert res["outcome"] == "failure"

    def test_prompts_ask_for_long_untrimmed_quotes(self):
        from shared.prompts import (build_outcome_abstract_prompt,
                                    build_repro_abstract_prompt)
        for p in (build_outcome_abstract_prompt("t", "a", ""),
                  build_repro_abstract_prompt("t", "a", "")):
            assert "COMPLETE sentences" in p
            assert "1200" in p
            assert "Never truncate" in p


# ── Original-link guard: self-links, DOI recovery, graceful empties ──────────

class TestGuardOriginalLink:
    def _row(self, **kw):
        r = {"doi_r": "10.1/repl", "title_r": "A Study of Things",
             "doi_o": "10.2/orig", "title_o": "The Original Work",
             "link_method": "llm_fulltext", "link_confidence": "high",
             "pair_id": "p", "doi_o_verification": "verified", "ref_o": "x"}
        r.update(kw); return r

    def test_self_link_by_doi_rejected(self):
        out = run_extract._guard_original_link(self._row(doi_o="10.1/repl"))
        assert out["link_method"] == "target_pending"
        assert out["doi_o"] == ""

    def test_self_link_by_title_rejected(self):
        out = run_extract._guard_original_link(
            self._row(doi_o="", title_o="A Study of Things"))
        assert out["link_method"] == "target_pending"

    def test_self_link_title_match_ignores_case_and_punctuation(self):
        out = run_extract._guard_original_link(
            self._row(doi_o="", title_o="  a study of THINGS.  "))
        assert out["link_method"] == "target_pending"

    def test_demotion_clears_a_merged_outcome(self):
        """The multi-original path merges the outcome before the guard runs, so a
        rejected row would otherwise carry a coded outcome on an unresolved link."""
        out = run_extract._guard_original_link(self._row(
            doi_o="10.1/repl", outcome="success", outcome_phrase="we replicated it",
            outcome_confidence="high", out_quote_source="llm_multi"))
        assert out["link_method"] == "target_pending"
        assert out["outcome"] == "pending"
        assert out["outcome_phrase"] == ""
        assert out["outcome_confidence"] == "low"
        assert out["out_quote_source"] == ""

    def test_good_link_untouched(self):
        out = run_extract._guard_original_link(self._row())
        assert out["link_method"] == "llm_fulltext"
        assert out["doi_o"] == "10.2/orig"

    def test_missing_doi_recovered_from_title(self):
        with patch("extract.run_extract._search_crossref_by_title",
                   return_value={"doi": "10.9/found"}):
            out = run_extract._guard_original_link(self._row(doi_o=""))
        assert out["doi_o"] == "10.9/found"
        assert out["link_method"] == "llm_fulltext"

    def test_recovered_doi_that_is_a_self_link_is_rejected(self):
        """Recovery must not resurrect the replication itself as its own original."""
        with patch("extract.run_extract._search_crossref_by_title",
                   return_value={"doi": "10.1/repl"}):
            out = run_extract._guard_original_link(self._row(doi_o=""))
        assert out["link_method"] == "target_pending"

    def test_genuinely_empty_doi_with_real_title_is_kept(self):
        """No DOI anywhere, but a substantive distinct title -> keep the row and
        mark it explicitly rather than dropping a valid original."""
        with patch("extract.run_extract._search_crossref_by_title", return_value=None), \
             patch("extract.run_extract._search_openalex_by_title", return_value=None):
            out = run_extract._guard_original_link(self._row(doi_o=""))
        assert out["link_method"] == "llm_fulltext"
        assert out["doi_o"] == ""
        assert out["doi_o_verification"] == "no_doi"

    def test_no_doi_and_no_usable_title_is_pending(self):
        with patch("extract.run_extract._search_crossref_by_title", return_value=None), \
             patch("extract.run_extract._search_openalex_by_title", return_value=None):
            out = run_extract._guard_original_link(self._row(doi_o="", title_o="n/a"))
        assert out["link_method"] == "target_pending"

    def test_doi_less_original_keeps_the_openalex_work_id(self):
        """A book or pre-DOI-era original OpenAlex indexes without a DOI: the work
        id is the row's only identity, so it must reach oa_work_id_o."""
        hit = {"doi": "", "openalex_id": "W123", "title": "The Original Work"}
        with patch("extract.run_extract._search_crossref_by_title", return_value=None), \
             patch("extract.run_extract._search_openalex_by_title", return_value=hit):
            out = run_extract._guard_original_link(self._row(doi_o=""))
        assert out["link_method"] == "llm_fulltext"
        assert out["doi_o"] == ""
        assert out["doi_o_verification"] == "no_doi"
        assert out["oa_work_id_o"] == "W123"

    def test_legacy_doi_less_row_keeps_its_md5_doi_r_pipe_pair_id(self):
        """REGRESSION: 129 rows already live in the validation DB keyed on
        md5("doi_r|"). Re-extracting one and now finding a DOI-less OpenAlex work
        must stamp oa_work_id_o WITHOUT re-keying pair_id — a changed pair_id is a
        duplicate import, and the oa: fallback buys nothing on the single-original
        path anyway."""
        legacy = make_pair_id("10.1/repl", "")
        hit = {"doi": "", "openalex_id": "W123", "title": "The Original Work"}
        with patch("extract.run_extract._search_crossref_by_title", return_value=None), \
             patch("extract.run_extract._search_openalex_by_title", return_value=hit):
            out = run_extract._guard_original_link(
                self._row(doi_o="", oa_work_id_o="", pair_id=legacy))
        assert out["oa_work_id_o"] == "W123"
        assert out["pair_id"] == legacy
        assert out["pair_id"] != make_pair_id("10.1/repl", "", "W123")

    def test_step2_doi_hit_whose_work_id_is_the_replication_is_rejected(self):
        """OpenAlex can return the replication's own work under an alternate DOI
        string; the DOI comparison alone would let it through."""
        hit = {"doi": "10.1/REPL.v2", "openalex_id": "https://openalex.org/W999"}
        with patch("extract.run_extract._search_crossref_by_title", return_value=hit):
            out = run_extract._guard_original_link(
                self._row(doi_o="", oa_work_id_r="W999"))
        assert out["link_method"] == "target_pending"
        assert out["doi_o"] == ""

    def test_doi_less_original_without_a_work_id_is_left_alone(self):
        with patch("extract.run_extract._search_crossref_by_title", return_value=None), \
             patch("extract.run_extract._search_openalex_by_title",
                   return_value={"doi": "", "openalex_id": ""}):
            out = run_extract._guard_original_link(self._row(doi_o=""))
        assert out["doi_o_verification"] == "no_doi"
        assert out.get("oa_work_id_o", "") == ""
        assert out["pair_id"] == "p", "no identifier found — pair_id must not be re-keyed"

    def test_work_id_matching_the_replication_is_a_self_link(self):
        hit = {"doi": "", "openalex_id": "W999", "title": "The Original Work"}
        with patch("extract.run_extract._search_crossref_by_title", return_value=None), \
             patch("extract.run_extract._search_openalex_by_title", return_value=hit):
            out = run_extract._guard_original_link(
                self._row(doi_o="", openalex_id_r="https://openalex.org/W999"))
        assert out["link_method"] == "target_pending"
        assert out.get("oa_work_id_o", "") == ""

    def test_recovered_doi_keeps_the_legacy_two_argument_pair_id(self):
        """pair_ids already imported into the validation DB key on md5("doi_r|doi_o"),
        so a DOI-recovering hit must not fold the work id into the hash."""
        hit = {"doi": "10.9/found", "openalex_id": "W123"}
        with patch("extract.run_extract._search_crossref_by_title", return_value=hit):
            out = run_extract._guard_original_link(self._row(doi_o=""))
        assert out["doi_o"] == "10.9/found"
        assert out["pair_id"] == make_pair_id("10.1/repl", "10.9/found")
        assert out.get("oa_work_id_o", "") == ""


class TestNoDoiWorkIdSurvivesVerification:
    """The guard sets oa_work_id_o before _verify_row and _fill_work_ids run."""

    def _row(self):
        return {"doi_r": "10.1/repl", "title_r": "Repl", "doi_o": "",
                "title_o": "Gender Advertisements", "year_o": "1979",
                "authors_o": "Goffman", "link_method": "llm_fulltext",
                "link_confidence": "high", "oa_work_id_o": "W123",
                "oa_work_id_r": "W555",
                "doi_o_verification": "no_doi",
                "pair_id": make_pair_id("10.1/repl", "")}

    def test_verify_row_keeps_no_doi_and_the_pair_id(self):
        v = {"doi_o_verification": "not_found", "doi_o": "",
             "evidence_note": "No DOI found for resolved title/author"}
        with patch("extract.run_extract.verify_and_correct", return_value=v):
            out = run_extract._verify_row(self._row())
        assert out["doi_o_verification"] == "no_doi"
        assert out["oa_work_id_o"] == "W123"
        assert out["pair_id"] == make_pair_id("10.1/repl", "")

    def test_fill_work_ids_leaves_the_guard_set_id_alone(self):
        with patch("extract.run_extract._oa_by_doi") as by_doi:
            out = run_extract._fill_work_ids(self._row())
        assert out["oa_work_id_o"] == "W123"
        by_doi.assert_not_called()

    def test_a_found_doi_clears_the_stale_work_id(self):
        """W123 was resolved before the DOI was known; once verification supplies a
        real doi_o the id must be refilled from it, not carried over."""
        v = {"doi_o_verification": "corrected", "doi_o": "10.2/right",
             "evidence_note": "DOI filled from metadata search"}
        with patch("extract.run_extract.verify_and_correct", return_value=v), \
             patch("extract.run_extract._build_ref_o", return_value=("r", "a", "b")):
            out = run_extract._verify_row(self._row())
        assert out["doi_o"] == "10.2/right"
        assert out["oa_work_id_o"] == "", "a stale o-side id blocks _fill_work_ids"
        assert out["pair_id"] == make_pair_id("10.1/repl", "10.2/right")

        with patch("extract.run_extract._oa_by_doi",
                   return_value={"openalex_id": "https://openalex.org/W222"}):
            filled = run_extract._fill_work_ids(out)
        assert filled["oa_work_id_o"] == "W222"


# ── Mismatched doi_o must not survive into the row (fix 1) ───────────────────

class TestMismatchClearsDoi:
    def _row(self):
        return {"doi_r": "10.1/repl", "title_r": "Repl", "doi_o": "10.2/wrong",
                "title_o": "The Original Work", "year_o": "2010", "authors_o": "Smith",
                "link_method": "llm_fulltext", "link_confidence": "high",
                "pair_id": "p", "ref_o": "old ref", "bibtex_ref_o": "@article{old}"}

    def test_mismatch_clears_doi_but_keeps_title(self):
        v = {"doi_o_verification": "mismatch", "doi_o": "10.2/wrong",
             "evidence_note": "DOI mismatch: points to another paper"}
        with patch("extract.run_extract.verify_and_correct", return_value=v):
            out = run_extract._verify_row(self._row())
        assert out["doi_o"] == "", "a known-wrong DOI must not be kept"
        assert out["bibtex_ref_o"] == "", "bibtex derived from the wrong DOI must go too"
        assert out["title_o"] == "The Original Work", "the title claim is retained"
        assert out["doi_o_verification"] == "mismatch"
        assert out["link_confidence"] == "low"

    def test_mismatch_clears_a_work_id_resolved_from_the_wrong_doi(self):
        v = {"doi_o_verification": "mismatch", "doi_o": "10.2/wrong",
             "evidence_note": "DOI mismatch: points to another paper"}
        row = {**self._row(), "oa_work_id_o": "W999"}
        with patch("extract.run_extract.verify_and_correct", return_value=v):
            out = run_extract._verify_row(row)
        assert out["oa_work_id_o"] == ""
        assert out["pair_id"] == make_pair_id("10.1/repl", "")

    def test_verified_doi_untouched(self):
        v = {"doi_o_verification": "verified", "doi_o": "10.2/wrong", "evidence_note": ""}
        with patch("extract.run_extract.verify_and_correct", return_value=v):
            out = run_extract._verify_row(self._row())
        assert out["doi_o"] == "10.2/wrong"
        assert out["bibtex_ref_o"] == "@article{old}"


# ── OpenAlex work ids on every written row (issue #69) ───────────────────────

class TestFillWorkIds:
    def test_r_side_comes_from_stage1_url_without_an_api_call(self):
        row = {"openalex_id_r": "https://openalex.org/W111", "doi_r": "10.1/r", "doi_o": ""}
        with patch("extract.run_extract._oa_by_doi") as oa:
            out = run_extract._fill_work_ids(row)
        assert out["oa_work_id_r"] == "W111"
        oa.assert_not_called(), "openalex_id_r already carries the id — no lookup needed"

    def test_o_side_resolved_from_doi_o(self):
        row = {"openalex_id_r": "https://openalex.org/W111", "doi_r": "10.1/r",
               "doi_o": "10.2/o"}
        with patch("extract.run_extract._oa_by_doi",
                   return_value={"openalex_id": "https://openalex.org/W222"}):
            out = run_extract._fill_work_ids(row)
        assert out["oa_work_id_o"] == "W222"

    def test_r_side_falls_back_to_doi_lookup(self):
        row = {"openalex_id_r": "", "doi_r": "10.1/r", "doi_o": ""}
        with patch("extract.run_extract._oa_by_doi",
                   return_value={"openalex_id": "https://openalex.org/W333"}):
            out = run_extract._fill_work_ids(row)
        assert out["oa_work_id_r"] == "W333"

    def test_unresolvable_ids_are_blank_not_missing(self):
        row = {"openalex_id_r": "", "doi_r": "", "doi_o": ""}
        with patch("extract.run_extract._oa_by_doi", return_value=None):
            out = run_extract._fill_work_ids(row)
        assert out["oa_work_id_r"] == ""
        assert out["oa_work_id_o"] == ""

    def test_runs_after_verification_so_a_corrected_doi_o_wins(self):
        """_verify_row can replace doi_o; the o-side id must describe the DOI that
        actually got written, not the one the LLM originally proposed."""
        row = {"doi_r": "10.1/r", "title_r": "R", "doi_o": "10.2/wrong",
               "title_o": "Orig", "year_o": "2010", "authors_o": "Smith",
               "link_method": "llm_fulltext", "link_confidence": "high",
               "openalex_id_r": "https://openalex.org/W111", "pair_id": "p"}
        v = {"doi_o_verification": "corrected", "doi_o": "10.2/right",
             "evidence_note": "corrected"}
        by_doi = {"10.2/wrong": "https://openalex.org/W999",
                  "10.2/right": "https://openalex.org/W222"}
        with patch("extract.run_extract.verify_and_correct", return_value=v), \
             patch("extract.run_extract._build_ref_o", return_value=("r", "a", "b")), \
             patch("extract.run_extract._oa_by_doi") as oa:
            oa.side_effect = lambda d: {"openalex_id": by_doi.get(d, "")}
            out = run_extract._fill_work_ids(run_extract._verify_row(row))
        assert out["doi_o"] == "10.2/right"
        assert out["oa_work_id_o"] == "W222", "the id must follow the corrected DOI"

    def test_schema_declares_both_columns(self):
        from shared.schema import EXTRACTED_COLS
        assert "oa_work_id_r" in EXTRACTED_COLS
        assert "oa_work_id_o" in EXTRACTED_COLS


# ── Title-search provenance is visible in link_method (fix 2) ────────────────

class TestTitleSearchProvenance:
    def test_schema_knows_the_method(self):
        from shared.schema import LINK_METHOD_VALUES, RESOLVED_LINK_METHODS
        assert "llm_title_search" in LINK_METHOD_VALUES
        # Provisional, not resolved: ~50% measured precision and the failure mode is
        # invisible to doi_o_verification, so the row must not import (audit D2).
        assert "llm_title_search" not in RESOLVED_LINK_METHODS

    def test_mapped_from_internal_label(self):
        assert run_extract._map_method("llm_title_search_gemini") == "llm_title_search"
        assert run_extract._map_method("llm_title_search_openai") == "llm_title_search"

    def test_candidate_derived_link_is_not_title_search(self):
        assert run_extract._map_method("llm_gemini") == "llm_fulltext"


# ── link_method enum covers everything the pipeline can emit (audit B1) ──────

class TestLinkMethodEnumCoverage:
    """Every link_method the pipeline writes must be in LINK_METHOD_VALUES, and every
    method that identifies an original must be in RESOLVED_LINK_METHODS — csv_to_db
    filters DB imports on that set, so an omission silently drops resolved rows
    (llm_references, 25% of extracted-test.csv, was dropped this way)."""

    def _emitted(self) -> set:
        import inspect
        # _map_method is the single funnel from internal resolution_method labels to
        # the persisted value, so its outputs plus the defaults of the row builders
        # are the complete emitted set.
        methods = set(run_extract._METHOD_MAP.values())
        methods |= {run_extract._map_method(m)
                    for m in ("llm_brand_new_source", "some_unmapped_method",
                              "llm_no_target", "")}
        for fn in (run_extract._merge_multi_row, run_extract._empty_row):
            default = inspect.signature(fn).parameters["link_method"].default
            methods.add(default)
        return methods

    def test_every_emitted_method_is_in_the_enum(self):
        from shared.schema import LINK_METHOD_VALUES
        unlisted = self._emitted() - LINK_METHOD_VALUES
        assert not unlisted, f"link_method values missing from the enum: {unlisted}"

    def test_call_site_literals_are_in_the_enum(self):
        import inspect, re
        from shared.schema import LINK_METHOD_VALUES
        src = inspect.getsource(run_extract)
        literals = set(re.findall(r'link_method\s*=\s*"([^"]+)"', src))
        unlisted = literals - LINK_METHOD_VALUES
        assert not unlisted, f"link_method literals missing from the enum: {unlisted}"

    def test_map_method_passthrough_matches_the_enum(self):
        from shared.schema import LINK_METHOD_VALUES
        for value in LINK_METHOD_VALUES:
            assert run_extract._map_method(value) == value

    def test_reference_screen_resolutions_reach_the_db(self):
        # csv_to_db imports supabase at module level, so read its source instead of
        # importing it: the point is that its import filter is the schema set.
        from pathlib import Path
        from shared.schema import RESOLVED_LINK_METHODS
        assert "llm_references" in RESOLVED_LINK_METHODS
        src = Path(__file__).resolve().parents[1] / "extract" / "csv_to_db.py"
        text = src.read_text(encoding="utf-8")
        assert "from shared.schema import RESOLVED_LINK_METHODS" in text
        assert 'df["link_method"].isin(_RESOLVED_METHODS)' in text

    def test_set_aside_verdicts_are_known_but_unresolved(self):
        from shared.schema import LINK_METHOD_VALUES, RESOLVED_LINK_METHODS
        for value in ("not_a_replication", "screen_disagreement"):
            assert value in LINK_METHOD_VALUES
            assert value not in RESOLVED_LINK_METHODS


class TestOutcomeVocabularyNeverCrosses:
    """A reproduction must only ever carry one of the 9 grid values (or
    cannot_be_determined / not_a_replication), and a replication only the
    replication enum. Every caller of extract_outcome must pass record_type —
    tools/recalibrate_outcomes.py did not, and coded a reproduction as 'success'."""

    def test_every_production_caller_passes_record_type(self):
        import inspect, re
        import extract.run_extract as rx
        import tools.recalibrate_outcomes as rc
        for mod in (rx, rc):
            src = inspect.getsource(mod)
            for m in re.finditer(r"extract_outcome\(", src):
                tail = src[m.end():m.end() + 900]
                depth, body = 1, []
                for ch in tail:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    body.append(ch)
                call = "".join(body)
                assert "record_type" in call, (
                    f"{mod.__name__} calls extract_outcome without record_type; a "
                    f"reproduction would silently be coded in replication vocabulary")

    def test_reproduction_rejects_replication_vocabulary(self):
        from shared.schema import outcome_categories_for
        assert "success" not in outcome_categories_for("reproduction")
        assert "computationally successful, robust" not in outcome_categories_for("replication")


# ── Decision-model attribution and the two-provider requirement ──────────────

class TestClassifyModelAttribution:
    """Routing is the one decision with no attribution otherwise: the match-type
    classifier's model was computed and thrown away."""

    _FILTER_ROW = pd.Series({"doi_r": "10.1/rep", "title_r": "Rep",
                             "filter_status": "replication"})

    def test_merge_row_persists_the_classifier_model(self):
        link = {"resolution_method": "llm_fulltext", "resolved_doi_o": "10.1/orig",
                "resolved_title_o": "Original", "resolved_year_o": 2000,
                "resolved_author_o": "Smith", "resolution_score": 1.0,
                "llm_confidence": "high"}
        with patch("extract.run_extract._build_ref_o", return_value=("ref", "auth", "bib")):
            row = _merge_row(self._FILTER_ROW, link, _MOCK_OUTCOME,
                             "single_original", "high", 1, 1, "gemini-heavy")
        assert row["classify_llm_model"] == "gemini-heavy"

    def test_merge_multi_row_persists_the_classifier_model(self):
        with patch("extract.run_extract._build_ref_o", return_value=("", "", "")):
            row = _merge_multi_row(self._FILTER_ROW,
                                   {"rank": 1, "doi": "10.1/o", "title": "O",
                                    "first_author": "A", "year": 2001,
                                    "confidence": "high"},
                                   _MOCK_OUTCOME, "multiple_original", "high", 2,
                                   classify_model="gemini-heavy")
        assert row["classify_llm_model"] == "gemini-heavy"

    def test_empty_row_persists_the_classifier_model(self):
        row = run_extract._empty_row(self._FILTER_ROW, "single_original", "low",
                                     link_method="target_pending",
                                     classify_model="gemini-heavy")
        assert row["classify_llm_model"] == "gemini-heavy"

    def test_merge_row_persists_the_outcome_model(self):
        link = {"resolution_method": "llm_fulltext", "resolved_doi_o": "10.1/orig",
                "resolved_title_o": "Original", "resolved_year_o": 2000,
                "resolved_author_o": "Smith", "resolution_score": 1.0,
                "llm_confidence": "high", "llm_model": "gemini-link"}
        with patch("extract.run_extract._build_ref_o", return_value=("ref", "auth", "bib")):
            row = _merge_row(self._FILTER_ROW, link, _MOCK_OUTCOME,
                             "single_original", "high", 1, 1, "gemini-heavy")
        # The outcome step fails over independently of the link step, so the two
        # models can differ inside one run — that is the whole point of the column.
        assert row["outcome_llm_model"] == "gemini-outcome"
        assert row["link_llm_model"] == "gemini-link"

    def test_merge_multi_row_persists_the_outcome_model(self):
        with patch("extract.run_extract._build_ref_o", return_value=("", "", "")):
            row = _merge_multi_row(self._FILTER_ROW,
                                   {"rank": 1, "doi": "10.1/o", "title": "O",
                                    "first_author": "A", "year": 2001,
                                    "confidence": "high"},
                                   _MOCK_OUTCOME, "multiple_original", "high", 2,
                                   classify_model="gemini-heavy")
        assert row["outcome_llm_model"] == "gemini-outcome"

    def test_apply_outcome_persists_the_outcome_model(self):
        """The post-gate writer is the one that runs on every coded row — a column
        filled only by _merge_row would be blank on exactly the rows that got coded."""
        row = run_extract._apply_outcome({}, _MOCK_OUTCOME)
        assert row["outcome_llm_model"] == "gemini-outcome"
        assert row["outcome"] == "success"

    def test_keyword_coded_rows_name_the_rule_not_a_model(self):
        from extract.code_outcome import extract_outcome
        out = extract_outcome("10.1/rep", "We failed to replicate the original effect.",
                              title_r="A replication", no_llm=True)
        assert out["outcome"] == "failure"
        assert out["llm_model"] == "keyword"

    def test_classify_llm_model_is_in_the_schema(self):
        assert "classify_llm_model" in EXTRACTED_COLS


class TestMultiRowRecordType:
    """A reproduction is coded in a different outcome vocabulary than a replication,
    so a multi-original reproduction labelled 'replication' is validated against the
    wrong categories."""

    _ORIG = {"rank": 1, "doi": "10.1/o", "title": "O", "first_author": "A",
             "year": 2001, "confidence": "high"}

    def _row(self, filter_status: str) -> dict:
        with patch("extract.run_extract._build_ref_o", return_value=("", "", "")):
            return _merge_multi_row(
                pd.Series({"doi_r": "10.1/rep", "title_r": "Rep",
                           "filter_status": filter_status}),
                self._ORIG, _MOCK_OUTCOME, "multiple_original", "high", 2)

    def test_reproduction_keeps_its_type(self):
        assert self._row("reproduction")["type"] == "reproduction"

    def test_replication_is_unchanged(self):
        assert self._row("replication")["type"] == "replication"

    def test_matches_the_single_original_path(self):
        """_merge_row already honours filter_status; the multi path must not disagree."""
        link = {"resolution_method": "llm_fulltext", "resolved_doi_o": "10.1/orig",
                "resolved_title_o": "Original", "resolved_year_o": 2000,
                "resolved_author_o": "Smith", "resolution_score": 1.0,
                "llm_confidence": "high"}
        with patch("extract.run_extract._build_ref_o", return_value=("r", "a", "b")):
            single = _merge_row(pd.Series({"doi_r": "10.1/rep", "title_r": "Rep",
                                           "filter_status": "reproduction"}),
                                link, _MOCK_OUTCOME, "single_original", "high", 1, 1)
        assert single["type"] == self._row("reproduction")["type"]


class TestRescreenReopensSetAsides:
    """--resume carries every resolved row forward, which freezes the Stage 4.5
    screen's own verdicts under whichever voter pair produced them."""

    @staticmethod
    def _csv(tmp_path, rows: list[dict]) -> Path:
        df = pd.DataFrame(rows)
        for c in EXTRACTED_COLS:
            if c not in df.columns:
                df[c] = ""
        path = tmp_path / "extracted.csv"
        df[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")
        return path

    _ROWS = [
        {"doi_r": "10.1/keep", "filter_status": "replication",
         "link_method": "llm_references", "doi_o": "10.1/o", "outcome": "success"},
        {"doi_r": "10.1/nar", "filter_status": "replication",
         "link_method": "not_a_replication", "outcome": "not_a_replication"},
        {"doi_r": "10.1/dis", "filter_status": "replication",
         "link_method": "screen_disagreement", "outcome": "pending"},
    ]

    def test_without_the_flag_set_asides_are_carried_forward(self, tmp_path):
        resolved, pending = run_extract._load_extracted_rows(self._csv(tmp_path, self._ROWS))
        assert set(resolved) == {"10.1/keep", "10.1/nar", "10.1/dis"}
        assert pending == set()

    def test_rescreen_reopens_only_the_set_asides(self, tmp_path):
        resolved, pending = run_extract._load_extracted_rows(
            self._csv(tmp_path, self._ROWS), rescreen=True)
        assert set(resolved) == {"10.1/keep"}
        assert pending == set()   # reopened rows are re-processed, not carried as pending

    def test_rescreen_reopens_the_whole_multi_original_paper(self, tmp_path):
        rows = [
            {"doi_r": "10.1/multi", "filter_status": "replication", "original_rank": "1",
             "link_method": "llm_references", "doi_o": "10.1/o1", "outcome": "success"},
            {"doi_r": "10.1/multi", "filter_status": "replication", "original_rank": "2",
             "link_method": "screen_disagreement", "outcome": "pending"},
        ]
        resolved, _ = run_extract._load_extracted_rows(self._csv(tmp_path, rows),
                                                      rescreen=True)
        assert resolved == {}

    def test_flag_reaches_run_extract(self):
        import inspect
        assert "rescreen" in inspect.signature(run_extract.run_extract).parameters


class TestResumeReadsTheScreenSetAsides:
    """sanity_check moves the screen's verdicts out of extracted.csv, so a resume
    that reads only extracted.csv re-screens every paper the screen already settled."""

    @staticmethod
    def _write(path: Path, rows: list[dict]) -> None:
        df = pd.DataFrame(rows)
        for c in EXTRACTED_COLS:
            if c not in df.columns:
                df[c] = ""
        df[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")

    def _setup(self, tmp_path) -> Path:
        out = tmp_path / "extracted.csv"
        self._write(out, [{"doi_r": "10.1/keep", "filter_status": "replication",
                           "link_method": "llm_references", "doi_o": "10.1/o",
                           "outcome": "success"}])
        self._write(tmp_path / "not_a_replication.csv",
                    [{"doi_r": "10.1/nar", "filter_status": "replication",
                      "link_method": "not_a_replication", "outcome": "not_a_replication"}])
        self._write(tmp_path / "screen_disagreement.csv",
                    [{"doi_r": "10.1/dis", "filter_status": "replication",
                      "link_method": "screen_disagreement", "outcome": "pending"}])
        return out

    def test_set_aside_papers_count_as_resolved(self, tmp_path):
        resolved, pending = run_extract._load_extracted_rows(self._setup(tmp_path))
        assert set(resolved) == {"10.1/keep", "10.1/nar", "10.1/dis"}
        assert pending == set()

    def test_set_aside_rows_are_not_written_back_to_extracted_csv(self, tmp_path):
        resolved, _ = run_extract._load_extracted_rows(self._setup(tmp_path))
        assert resolved["10.1/nar"] == []
        assert resolved["10.1/dis"] == []
        assert len(resolved["10.1/keep"]) == 1

    def test_rescreen_ignores_the_set_aside_files(self, tmp_path):
        resolved, _ = run_extract._load_extracted_rows(self._setup(tmp_path),
                                                       rescreen=True)
        assert set(resolved) == {"10.1/keep"}

    def test_missing_set_aside_files_are_fine(self, tmp_path):
        out = tmp_path / "extracted.csv"
        self._write(out, [{"doi_r": "10.1/keep", "filter_status": "replication",
                           "link_method": "llm_references", "doi_o": "10.1/o"}])
        resolved, _ = run_extract._load_extracted_rows(out)
        assert set(resolved) == {"10.1/keep"}


class TestScreenProviderPrecheck:
    """The front-door screen needs two providers to have anything to weigh against
    each other, so a run configured with one must fail at startup, not 2,000 rows
    in. Which second key it needs follows SCREEN_VOTER2_MODEL."""

    def test_a_slashless_voter_needs_the_openai_key(self, monkeypatch):
        monkeypatch.setattr(run_extract, "SCREEN_VOTER2_MODEL", "gpt-5.4-mini")
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["k"])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "")
        monkeypatch.setattr(run_extract, "OPENROUTER_API_KEY", "k")
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            run_extract._check_screen_providers(no_llm=False)

    def test_a_slashed_voter_needs_the_openrouter_key(self, monkeypatch):
        monkeypatch.setattr(run_extract, "SCREEN_VOTER2_MODEL", "mistralai/ministral-14b-2512")
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["k"])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "k")
        monkeypatch.setattr(run_extract, "OPENROUTER_API_KEY", "")
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            run_extract._check_screen_providers(no_llm=False)

    def test_missing_gemini_key_raises(self, monkeypatch):
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", [])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "k")
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            run_extract._check_screen_providers(no_llm=False)

    def test_both_keys_present_passes(self, monkeypatch):
        monkeypatch.setattr(run_extract, "SCREEN_VOTER2_MODEL", "gpt-5.4-mini")
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["k"])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "k")
        run_extract._check_screen_providers(no_llm=False)

    def test_no_llm_skips_the_check(self, monkeypatch):
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", [])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "")
        monkeypatch.setattr(run_extract, "OPENROUTER_API_KEY", "")
        run_extract._check_screen_providers(no_llm=True)


# ── Title-search links are provisional, not settled (audit D2) ───────────────

class TestTitleSearchIsProvisional:
    """A title search picks from the whole literature, not a candidate list, and a
    hand-check put its precision near 50%. The row must therefore never present as
    a settled pairing: low confidence, no outcome, and no DB import."""

    def test_link_confidence_is_forced_low(self):
        for method in ("llm_title_search_prepdf", "llm_title_search_gemini",
                       "llm_title_search_openai"):
            link = {"resolution_method": method, "llm_confidence": "high",
                    "resolution_score": 1.0}
            assert run_extract._link_confidence(link) == "low", method

    def test_candidate_derived_links_keep_their_confidence(self):
        link = {"resolution_method": "llm_gemini", "llm_confidence": "high",
                "resolution_score": 1.0}
        assert run_extract._link_confidence(link) == "high"

    def test_no_outcome_is_coded_against_a_title_search_link(self):
        link = {"resolution_method": "llm_title_search_prepdf",
                "resolved_title_o": "Some landmark", "llm_model": "m"}
        out = run_extract._outcome_without_coding("llm_title_search", link)
        assert out is not None, "a provisional link must not be outcome-coded"
        assert out["outcome"] == "cannot_be_determined"
        assert out["outcome_confidence"] == "low"
        assert "provisional" in out["outcome_reasoning"]


# ── The classification screen is Stage 3's front door (audit E1) ─────────────
# 58% of the rows reaching the screen are discarded there. Every call made before
# it — the heavy-model match-type call, the resolution ladder, the PDF, the outcome
# call — is spent on a row that is then thrown away, so the screen runs first and
# the discarded rows must reach NONE of those.

_FILTERED_CSV = (
    "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
    "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
    "10.1000/rep,Rep Paper,Abstract text,2020,Jones,J. Psych,,W2,openalex,"
    "replication,rule_based,direct replication,high\n"
)


def _screen(**over) -> dict:
    return {**_YES_SCREEN, **over}


def _import_mask() -> tuple[set, set]:
    """csv_to_db's (filter_status, link_method) import mask.

    The `supabase` package is not a test dependency, so stub it before importing —
    the same idiom tests/test_csv_to_db.py uses.
    """
    import sys, types
    if "supabase" not in sys.modules:
        stub = types.ModuleType("supabase")
        stub.create_client = lambda url, key: None
        stub.Client = object
        sys.modules["supabase"] = stub
    from extract.csv_to_db import _RESOLVED_METHODS, _RESOLVED_STATUSES
    return _RESOLVED_STATUSES, _RESOLVED_METHODS


class TestFrontDoorScreen:
    def _run(self, screen, tmp_path, monkeypatch, filtered_csv: str = ""):
        """Run Stage 3 over one row with the screen returning `screen`.

        Returns (result_df, match_mock, ladder_mock, outcome_mock) so a test can
        assert that the heavy calls after the front door were never made.
        """
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["test-key"])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "test-key")
        (tmp_path / "filtered.csv").write_text(filtered_csv or _FILTERED_CSV,
                                               encoding="utf-8-sig")
        m_match = MagicMock(return_value=_MOCK_MATCH)
        m_link  = MagicMock(return_value=_MOCK_LINK)
        m_out   = MagicMock(return_value=_MOCK_OUTCOME)
        with patch.object(run_extract, "classify_replication", return_value=screen), \
             patch.object(run_extract, "classify_match_type", m_match), \
             patch.object(run_extract, "run_for_doi", m_link), \
             patch.object(run_extract, "extract_outcome", m_out), \
             patch.object(run_extract, "verify_and_correct",
                          side_effect=lambda doi, *a, **k: {
                              "doi_o": doi, "doi_o_verification": "skipped",
                              "evidence_note": ""}), \
             patch.object(run_extract, "_oa_by_doi", return_value=None), \
             patch.object(run_extract, "DATA_DIR", tmp_path), \
             patch.object(run_extract, "BASE_DIR", tmp_path):
            result = run_extract.run_extract()
        return result, m_match, m_link, m_out

    def test_agreed_none_is_written_without_any_further_call(self, tmp_path, monkeypatch):
        result, m_match, m_link, m_out = self._run(
            _screen(screen_verdict="discard", screen_classification="none",
                    record_type="", categories=["terminology_only"],
                    llm_reasoning="gemini: unrelated",
                    votes=[_vote("gemini", "none"), _vote("openai", "none")]),
            tmp_path, monkeypatch)

        assert list(result["link_method"]) == ["not_a_replication"]
        assert list(result["outcome"]) == ["not_a_replication"]
        assert "gemini=none/confident" in result.iloc[0]["link_evidence"]
        assert result.iloc[0]["screen_categories"] == "terminology_only"
        m_match.assert_not_called()
        m_link.assert_not_called()
        m_out.assert_not_called()

    def test_unconfident_agreed_none_is_still_a_discard(self, tmp_path, monkeypatch):
        """G-softqual discards two "none" votes at any confidence."""
        result, _, m_link, _ = self._run(
            _screen(screen_verdict="discard", screen_classification="none",
                    record_type="",
                    votes=[_vote("gemini", "none", confident=False),
                           _vote("openai", "none", confident=False)]),
            tmp_path, monkeypatch)

        assert list(result["link_method"]) == ["not_a_replication"]
        m_link.assert_not_called()

    @pytest.mark.parametrize("partner", ["unclear", "replication"])
    def test_confident_none_plus_an_unconfident_partner_is_a_discard(
            self, tmp_path, monkeypatch, partner):
        """The softqual clause: an answer the other voter would not stake anything
        on does not outweigh a confident none."""
        result, _, m_link, m_out = self._run(
            _screen(screen_verdict="discard", screen_classification="none",
                    record_type="",
                    votes=[_vote("gemini", "none"),
                           _vote("openai", partner, confident=False)]),
            tmp_path, monkeypatch)

        assert list(result["link_method"]) == ["not_a_replication"]
        m_link.assert_not_called()
        m_out.assert_not_called()

    def test_a_confident_split_proceeds_down_the_ladder(self, tmp_path, monkeypatch):
        """Was screen_disagreement. A false inclusion costs a ladder run; a false
        discard costs the paper, so a real split escalates instead of terminating."""
        result, m_match, m_link, _ = self._run(
            _screen(screen_verdict="proceed", screen_classification="replication",
                    record_type="replication",
                    votes=[_vote("gemini", "replication"), _vote("openai", "none")]),
            tmp_path, monkeypatch)

        assert "screen_disagreement" not in set(result["link_method"])
        assert result.iloc[0]["link_method"] == "same_author_year_title_overlap"
        m_match.assert_called_once()
        m_link.assert_called_once()

    def test_one_vote_is_target_pending_not_a_verdict(self, tmp_path, monkeypatch):
        result, m_match, m_link, m_out = self._run(
            _screen(resolution_method="llm_refscreen_partial", screen_verdict="",
                    record_type="", llm_error="classifier failed: openai"),
            tmp_path, monkeypatch)

        assert list(result["link_method"]) == ["target_pending"]
        m_match.assert_not_called()
        m_link.assert_not_called()
        m_out.assert_not_called()

    def test_no_votes_is_an_api_error(self, tmp_path, monkeypatch):
        result, _, m_link, _ = self._run(
            _screen(resolution_method="llm_refscreen_failed", screen_verdict="",
                    record_type="", llm_error="classifier failed: gemini, openai"),
            tmp_path, monkeypatch)

        assert list(result["link_method"]) == ["api_error"]
        assert list(result["outcome"]) == ["api_error"]
        m_link.assert_not_called()

    def test_the_verdict_is_threaded_into_the_ladder_not_re_voted(self, tmp_path, monkeypatch):
        _, _, m_link, _ = self._run(_YES_SCREEN, tmp_path, monkeypatch)

        assert m_link.call_args[1]["classification"] == _YES_SCREEN

    def test_a_proceed_without_a_qualifying_vote_still_imports(self, tmp_path, monkeypatch):
        """A needs_review row the gate proceeds on without any qualifying vote
        (unclear/unclear) can still resolve an original and get an outcome. If
        filter_status stayed needs_review, csv_to_db's import mask would drop the
        row and the pairing would never reach a human validator."""
        needs_review_csv = _FILTERED_CSV.replace(
            "replication,rule_based,direct replication,high",
            "needs_review,rule_based,phrase without a cite,medium")
        result, _, m_link, m_out = self._run(
            _screen(record_type="", screen_classification="unclear", categories=[],
                    votes=[_vote("gemini", "unclear", confident=False),
                           _vote("openai", "unclear", confident=False)]),
            tmp_path, monkeypatch, filtered_csv=needs_review_csv)

        row = result.iloc[0]
        m_link.assert_called_once()
        assert row["link_method"] == "same_author_year_title_overlap"
        # The paper type defaults to replication, as the old filter_status
        # derivation did for everything non-reproduction…
        assert row["filter_status"] == "replication"
        assert row["type"] == "replication"
        assert m_out.call_args[1]["record_type"] == "replication"
        # …but no call decided it, so provenance still names the rule filter.
        assert row["filter_method"] == "rule_based"
        # The row passes csv_to_db's import mask — the point of all of the above.
        statuses, methods = _import_mask()
        assert row["filter_status"] in statuses
        assert row["link_method"] in methods

    def test_a_proceed_without_a_qualifying_vote_keeps_a_decided_type(
            self, tmp_path, monkeypatch):
        """Stage 2 already said reproduction and no screen call overrode it, so the
        default must not quietly rewrite the row to replication."""
        repro_csv = _FILTERED_CSV.replace(
            "replication,rule_based,direct replication,high",
            "reproduction,rule_based,re-analysis of the original data,high")
        result, _, _, m_out = self._run(
            _screen(record_type="", screen_classification="unclear", categories=[],
                    votes=[_vote("gemini", "unclear", confident=False),
                           _vote("openai", "none", confident=False)]),
            tmp_path, monkeypatch, filtered_csv=repro_csv)

        assert result.iloc[0]["filter_status"] == "reproduction"
        assert result.iloc[0]["type"] == "reproduction"
        assert m_out.call_args[1]["record_type"] == "reproduction"

    def test_the_screen_decides_record_type_and_categories(self, tmp_path, monkeypatch):
        """The screen read the abstract and said what the paper is, so its verdict
        reaches the outcome call, the `type` column and filter_status."""
        result, _, _, m_out = self._run(
            _screen(record_type="reproduction", screen_classification="reproduction"),
            tmp_path, monkeypatch)

        assert m_out.call_args[1]["record_type"] == "reproduction"
        assert result.iloc[0]["type"] == "reproduction"
        assert result.iloc[0]["filter_status"] == "reproduction"
        assert result.iloc[0]["filter_method"] == "screen"
        assert result.iloc[0]["screen_categories"] == "clearly_declared|context_transfer"


class TestMatchTypeLLMGate:
    """The match-type LLM answered single_original for 94% of rows. It can only
    distinguish several targets from distinct author-year citations, so with fewer
    than two the call is gated off — the deterministic rules still run (audit E1)."""

    _ONE_PAIR = dict(_ROW, abstract_r="We replicated Smith (2010) closely.")

    def test_one_author_year_pair_skips_the_llm(self, tmp_path):
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates") as oa, \
             patch("extract.run_extract.call_llm") as llm:
            result = classify_match_type(self._ONE_PAIR)

        assert result["original_match_type"] == "single_original"
        llm.assert_not_called()
        oa.assert_not_called()   # the candidate fetch only exists to feed that prompt

    def test_no_citations_at_all_skips_the_llm(self, tmp_path):
        row = dict(_ROW, abstract_r="A close replication in a new sample.")
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.call_llm") as llm:
            result = classify_match_type(row)

        assert result["original_match_type"] == "single_original"
        llm.assert_not_called()

    def test_two_pairs_still_ask_the_llm(self, tmp_path):
        llm_answer = {"original_match_type": "multiple_original", "confidence": "high"}
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates", return_value=_CAND_MULTI), \
             patch("extract.run_extract.call_llm",
                   return_value=(llm_answer, "gemini-model", "")) as llm:
            result = classify_match_type(_ROW)

        llm.assert_called_once()
        assert result["original_match_type"] == "multiple_original"

    def test_the_rules_still_fire_below_the_gate(self, tmp_path):
        """A Many Labs paper with no citation in its abstract must still route to
        multiple_original — only the LLM call is gated, not the rules."""
        row = dict(_ROW, title_r="Many Labs 2: Investigating Variation",
                   abstract_r="A large-scale project.")
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.call_llm") as llm:
            result = classify_match_type(row)

        assert result["original_match_type"] == "multiple_original"
        llm.assert_not_called()


class TestOutcomeGate:
    """Outcome coding runs only on a resolved link (audit E2). One gate, derived
    from RESOLVED_LINK_METHODS — not a stack of per-method special cases."""

    def test_every_resolved_method_is_coded(self):
        for method in RESOLVED_LINK_METHODS:
            assert run_extract._outcome_without_coding(method, {}) is None, method

    def test_no_unresolved_method_is_coded(self):
        for method in LINK_METHOD_VALUES - RESOLVED_LINK_METHODS:
            assert run_extract._outcome_without_coding(method, {}) is not None, method

    def test_set_aside_and_pending_rows_are_marked_pending(self):
        for method in ("screen_disagreement", "target_pending", "no_original_found"):
            out = run_extract._outcome_without_coding(method, {})
            assert out["outcome"] == "pending", method

    def test_a_not_a_replication_row_keeps_the_screen_verdict(self):
        out = run_extract._outcome_without_coding(
            "not_a_replication", {"llm_reasoning": "gemini: unrelated"})
        assert out["outcome"] == "not_a_replication"
        assert out["outcome_reasoning"] == "gemini: unrelated"

    def test_only_a_real_verdict_names_an_outcome_model(self):
        """outcome_llm_model names the model whose verdict IS the outcome. A pending
        row has no verdict, so stamping the link stage's model on it would read as a
        coding that never happened."""
        for method in ("screen_disagreement", "target_pending", "no_original_found",
                       "llm_title_search", "api_error"):
            out = run_extract._outcome_without_coding(method, {"llm_model": "gemini-link"})
            assert out["llm_model"] == "", method
        settled = run_extract._outcome_without_coding(
            "not_a_replication", {"llm_model": "gemini-light+ministral"})
        assert settled["llm_model"] == "gemini-light+ministral"

    def test_a_guard_demoted_row_is_not_outcome_coded(self, monkeypatch):
        """The guard rejects a self-link AFTER the ladder ran but BEFORE the outcome
        call — so the row must be demoted first and never reach extract_outcome."""
        row = pd.Series({"doi_r": "10.1/rep", "title_r": "T", "abstract_r": "a",
                         "filter_status": "replication"})
        self_link = dict(_MOCK_LINK, resolved_doi_o="10.1/rep")
        with patch.object(run_extract, "run_for_doi", return_value=self_link), \
             patch.object(run_extract, "extract_outcome",
                          side_effect=AssertionError("must not code an outcome")):
            out = run_extract._resolve_and_code(
                "10.1/rep", row, "single_original", "high", "", screen=None,
                no_llm=False, no_pdf=True, resolved_only=False,
                recalibrate_outcomes=False)

        assert out["link_method"] == "target_pending"
        assert out["outcome"] == "pending"

    def test_resolved_only_drops_the_row_before_the_outcome_call(self):
        row = pd.Series({"doi_r": "10.1/rep", "title_r": "T", "abstract_r": "a",
                         "filter_status": "replication"})
        pending = {"resolution_method": "no_fulltext_available", "resolved_doi_o": "",
                   "resolved_title_o": "", "resolved_year_o": None,
                   "resolved_author_o": "", "resolution_score": 0.0}
        with patch.object(run_extract, "run_for_doi", return_value=pending), \
             patch.object(run_extract, "extract_outcome",
                          side_effect=AssertionError("must not code an outcome")):
            out = run_extract._resolve_and_code(
                "10.1/rep", row, "single_original", "high", "", screen=None,
                no_llm=False, no_pdf=True, resolved_only=True,
                recalibrate_outcomes=False)

        assert out is None


# ── Empty parse caches must not poison later runs (audit B4) ────────────────

_EMPTY_PARSE = {m: {"source": m, "abstract": "", "intro": "", "methods": "",
                    "raw_text": "", "references": [], "error": None}
                for m in ("grobid", "pdfminer", "markitdown")}

_FULL_PARSE = {**_EMPTY_PARSE,
               "markitdown": {"source": "markitdown", "abstract": "A real abstract",
                              "intro": "A real intro", "methods": "", "raw_text": "body",
                              "references": [{"title": "r"}], "error": None}}


class TestEmptyParseCache:
    def _cache(self, tmp_path, monkeypatch, results):
        monkeypatch.setattr(run_extract, "PARSE_CACHE_DIR", tmp_path)
        from shared.utils import cache_key
        path = tmp_path / f"parse_{cache_key('10.1/x')}.json"
        path.write_text(json.dumps(results), encoding="utf-8")
        return path

    def test_all_empty_cache_reads_as_a_miss(self, tmp_path, monkeypatch):
        self._cache(tmp_path, monkeypatch, _EMPTY_PARSE)
        assert run_extract._read_parse_cache("10.1/x") is None
        assert run_extract._best_fulltext_from_cache("10.1/x") == ("", "none")

    def test_populated_cache_still_reads(self, tmp_path, monkeypatch):
        self._cache(tmp_path, monkeypatch, _FULL_PARSE)
        assert run_extract._read_parse_cache("10.1/x") is not None
        assert run_extract._best_fulltext_from_cache("10.1/x") == ("body", "tail")

    def test_empty_cache_is_reparsed_not_trusted(self, tmp_path, monkeypatch):
        """The whole bug: the empty file existed, so the re-parse never happened."""
        path = self._cache(tmp_path, monkeypatch, _EMPTY_PARSE)
        monkeypatch.setattr(run_extract, "PDF_CACHE_DIR", tmp_path)
        monkeypatch.setattr(run_extract, "OA_XML_CACHE_DIR", tmp_path)
        with patch.object(run_extract, "_parse_all", return_value=_FULL_PARSE) as pa:
            run_extract._save_parse_cache("10.1/x")
        assert pa.called
        assert json.loads(path.read_text(encoding="utf-8"))["markitdown"]["raw_text"] == "body"

    def test_an_empty_parse_is_never_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_extract, "PARSE_CACHE_DIR", tmp_path)
        monkeypatch.setattr(run_extract, "PDF_CACHE_DIR", tmp_path)
        monkeypatch.setattr(run_extract, "OA_XML_CACHE_DIR", tmp_path)
        with patch.object(run_extract, "_parse_all", return_value=_EMPTY_PARSE):
            run_extract._save_parse_cache("10.1/x")
        assert list(tmp_path.glob("parse_*.json")) == []


class TestParseCacheOnlyAfterTheDocument:
    """Screen exits return with pdf={}. Parsing them wrote the six-empty cache in
    the first place, so the row must not reach the parser at all."""

    def _link(self, **over):
        base = {"pdf_ok": False, "pdf_source": "none"}
        base.update(over)
        return base

    def test_screen_exit_has_no_document(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_extract, "PDF_CACHE_DIR", tmp_path)
        monkeypatch.setattr(run_extract, "OA_XML_CACHE_DIR", tmp_path)
        assert not run_extract._has_document("10.1/x", self._link())

    def test_acquired_pdf_has_a_document(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_extract, "PDF_CACHE_DIR", tmp_path)
        monkeypatch.setattr(run_extract, "OA_XML_CACHE_DIR", tmp_path)
        assert run_extract._has_document(
            "10.1/x", self._link(pdf_ok=True, pdf_source="arxiv"))

    def test_openalex_xml_only_counts_as_a_document(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_extract, "PDF_CACHE_DIR", tmp_path)
        monkeypatch.setattr(run_extract, "OA_XML_CACHE_DIR", tmp_path)
        assert run_extract._has_document(
            "10.1/x", self._link(pdf_source="openalex_xml"))

    def test_pdf_cached_by_an_earlier_run_counts(self, tmp_path, monkeypatch):
        """--recalibrate-outcomes re-reads documents a previous run downloaded."""
        monkeypatch.setattr(run_extract, "PDF_CACHE_DIR", tmp_path)
        monkeypatch.setattr(run_extract, "OA_XML_CACHE_DIR", tmp_path)
        from shared.utils import cache_key
        (tmp_path / f"{cache_key('10.1/x')}.pdf").write_bytes(b"%PDF")
        assert run_extract._has_document("10.1/x", self._link())


class TestMatchTypeFailureIsNotCached:
    """A 429 is not a verdict (audit B8).

    The failure default is single_original, so caching it freezes a Many Labs paper
    whose title does not trip the rule into the single-original pipeline for every
    future run.
    """

    _ROW_MULTI = {
        "doi_r": "10.1/rep", "title_r": "A study of several effects",
        "abstract_r": "We revisit Smith (2010) and Jones (2012) in one paper.",
        "openalex_id_r": "W1", "year_r": "2020",
    }
    _CANDS = [{"doi": "10.9/a", "title": "A", "year": 2010, "first_author": "Smith"},
              {"doi": "10.9/b", "title": "B", "year": 2012, "first_author": "Jones"}]

    def test_failure_writes_no_cache_entry(self, tmp_path):
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates", return_value=self._CANDS), \
             patch("extract.run_extract.call_llm", return_value=(None, "", "quota")):
            result = classify_match_type(self._ROW_MULTI)
        assert result["original_match_type"] == "single_original"
        assert not list(tmp_path.glob("match_type_*.json"))

    def test_a_later_run_can_still_get_a_real_answer(self, tmp_path):
        llm = {"original_match_type": "multiple_original", "confidence": "high"}
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates", return_value=self._CANDS):
            with patch("extract.run_extract.call_llm", return_value=(None, "", "quota")):
                classify_match_type(self._ROW_MULTI)
            with patch("extract.run_extract.call_llm", return_value=(llm, "m", "")):
                result = classify_match_type(self._ROW_MULTI)
        assert result["original_match_type"] == "multiple_original"


class TestOutcomeReadsTheDiscussion:
    """FLoRA's rule: the abstract, and failing that the discussion and conclusion.
    The escalation used to send the first 8,000 characters — i.e. the introduction,
    which routinely reports OTHER studies' replication failures."""

    _PAPER = ("Introduction\n" + ("Earlier work failed to replicate this. " * 100)
              + "\nGeneral Discussion\n"
              + ("Our replication succeeded in every respect. " * 30)
              + "\nReferences\nSmith, J. (2010). A paper.\n")

    def _cache(self, tmp_path, monkeypatch, results):
        import json
        from shared.utils import cache_key
        monkeypatch.setattr(run_extract, "PARSE_CACHE_DIR", tmp_path)
        (tmp_path / f"parse_{cache_key('10.1/x')}.json").write_text(
            json.dumps(results), encoding="utf-8")

    def test_escalation_text_is_the_discussion(self, tmp_path, monkeypatch):
        self._cache(tmp_path, monkeypatch, {
            "markitdown": {"source": "markitdown", "abstract": "a", "intro": "i",
                           "references": [], "raw_text": self._PAPER, "error": None},
        })
        text, provenance = run_extract._best_fulltext_from_cache("10.1/x")
        assert provenance == "discussion"
        assert "Our replication succeeded" in text
        assert "Earlier work failed to replicate" not in text

    def test_a_parser_with_no_raw_text_does_not_hide_one_that_has_it(self, tmp_path,
                                                                     monkeypatch):
        """GROBID scores highly on references but returns no raw_text at all;
        falling back to its abstract+intro discarded a full parse another method
        had already produced."""
        self._cache(tmp_path, monkeypatch, {
            "grobid": {"source": "grobid", "abstract": "a", "intro": "i",
                       "references": [{"title": "t"}] * 40, "raw_text": "",
                       "error": None},
            "markitdown": {"source": "markitdown", "abstract": "a", "intro": "i",
                           "references": [], "raw_text": self._PAPER, "error": None},
        })
        text, provenance = run_extract._best_fulltext_from_cache("10.1/x")
        assert provenance == "discussion"
        assert "Our replication succeeded" in text

    def test_the_model_is_told_which_section_it_is_reading(self, tmp_path, monkeypatch):
        import pandas as pd
        self._cache(tmp_path, monkeypatch, {
            "markitdown": {"source": "markitdown", "abstract": "a", "intro": "i",
                           "references": [], "raw_text": self._PAPER, "error": None},
        })
        captured = {}

        def fake_extract_outcome(doi_r, abstract_r, fulltext="", *a, **kw):
            captured["fulltext"] = fulltext
            return {"outcome": "success", "outcome_phrase": "", "outcome_confidence": "high",
                    "out_quote_source": "fulltext", "outcome_reasoning": ""}

        monkeypatch.setattr(run_extract, "extract_outcome", fake_extract_outcome)
        run_extract._get_outcome(
            "10.1/x",
            pd.Series({"abstract_r": "", "title_r": "T", "filter_status": "replication"}),
            {},
        )
        assert captured["fulltext"].startswith("[SOURCE: discussion / conclusion")
        assert "Our replication succeeded" in captured["fulltext"]
