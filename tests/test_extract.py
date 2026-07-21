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

from shared.schema import EXTRACTED_COLS, OUTCOME_CATEGORIES, make_pair_id
from shared.cache import read_dual_cache, write_dual_cache
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
        ("significant but smaller effect than the original study reported", "mixed"),
        ("No evidence was found for precognition in any experiment", "failure"),
        ("adapted the procedure in a different cultural population", "descriptive"),
    ])
    def test_keyword_scan(self, text, expected):
        hit = _keyword_scan(text, "abstract")
        assert hit is not None, f"No match for: {text!r}"
        assert hit["outcome"] == expected, (
            f"Expected {expected}, got {hit['outcome']} for: {text!r}"
        )

    def test_no_match_returns_none(self):
        hit = _keyword_scan("we attempted this study across multiple sites", "abstract")
        assert hit is None

    def test_failure_beats_success_keyword(self):
        hit = _keyword_scan("we failed to replicate the originally replicated finding", "abstract")
        assert hit["outcome"] == "failure"

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
    def test_abstract_keyword_hit_skips_llm(self):
        """Keyword match in abstract should not call the LLM."""
        with patch("extract.code_outcome.call_llm") as mock_llm:
            result = extract_outcome(
                "10.1234/test",
                abstract_r="we found no evidence of the original effect",
                title_r="A Replication Study",
            )
        mock_llm.assert_not_called()
        assert result["outcome"] == "failure"
        assert result["out_quote_source"] == "abstract"

    def test_uninformative_triggers_llm(self):
        """No keyword match should fall through to LLM."""
        mock_llm_result = {"outcome": "mixed", "outcome_phrase": "partial support",
                           "outcome_confidence": "medium", "out_quote_source": "abstract"}
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
            {"resolution_method": "llm_abstract", "llm_confidence": "high"}
        ) == "high"

    def test_llm_failure_returns_cannot_be_determined(self, tmp_path):
        """LLM failure should return cannot_be_determined, not crash."""
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(None, "", "quota | error")):
            result = extract_outcome("10.1234/fail", abstract_r="ambiguous text")
        assert result["outcome"] == "cannot_be_determined"
        assert result["outcome_confidence"] == "low"

    def test_llm_result_cached(self, tmp_path):
        """LLM result should be written to cache and reused."""
        mock_result = {"outcome": "success", "outcome_phrase": "replicated",
                       "outcome_confidence": "high", "out_quote_source": "abstract"}
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
                       "outcome_confidence": "low", "out_quote_source": ""}
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
                          "outcome_confidence": "high", "out_quote_source": "abstract",
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
        result = extract_outcome(
            "10.1234/kw", abstract_r="we failed to replicate the original finding"
        )
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

    def test_uninformative_dropped(self):
        assert "uninformative" not in OUTCOME_CATEGORIES

    def test_cannot_be_determined_present(self):
        assert "cannot_be_determined" in OUTCOME_CATEGORIES

    def test_categories_are_exact(self):
        # not_a_replication is a genuine classifier output (is_genuine_attempt=false),
        # so it belongs in the category enum alongside #61's unified five.
        assert OUTCOME_CATEGORIES == {
            "success", "failure", "mixed", "descriptive", "cannot_be_determined",
            "not_a_replication",
        }

    def test_legacy_uninformative_still_valid_for_stored_rows(self):
        from shared.schema import OUTCOME_VALUES
        assert "uninformative" not in OUTCOME_CATEGORIES
        assert "uninformative" in OUTCOME_VALUES


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


class TestOutcomePromptContent:
    def _prompt(self, tmp_path, **kw):
        ret = {"outcome": "success", "outcome_phrase": "x", "outcome_confidence": "high",
               "out_quote_source": "abstract", "outcome_reasoning": ""}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(ret, "m", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            extract_outcome("10.1234/pr", abstract_r="ambiguous abstract", title_r="T", **kw)
        return mock_llm.call_args_list[0][0][0]

    def test_example_one_relabelled_descriptive(self, tmp_path):
        prompt = self._prompt(tmp_path)
        assert "1. DESCRIPTIVE" in prompt
        assert "UNINFORMATIVE" not in prompt

    def test_no_default_to_cannot_be_determined_line(self, tmp_path):
        prompt = self._prompt(tmp_path)
        assert "rather than 'uninformative'" not in prompt

    def test_abstract_prompt_quote_source_excludes_fulltext(self, tmp_path):
        prompt = self._prompt(tmp_path)
        assert '"out_quote_source": "<abstract|title>"' in prompt

    def test_abstract_truncated_at_3000(self, tmp_path):
        long_abstract = ("A" * 2999) + "MARKER_INSIDE" + ("B" * 3000) + "MARKER_OUTSIDE"
        ret = {"outcome": "success", "outcome_phrase": "x", "outcome_confidence": "high",
               "out_quote_source": "abstract", "outcome_reasoning": ""}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(ret, "m", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            extract_outcome("10.1234/trunc", abstract_r=long_abstract, title_r="T")
        prompt = mock_llm.call_args_list[0][0][0]
        assert "MARKER_OUTSIDE" not in prompt
        assert "MARKER_INSIDE" not in prompt  # sits just past the 3000-char cap
        assert "…" in prompt


class TestDualCache:
    def test_write_creates_both_keys(self, tmp_path):
        write_dual_cache(tmp_path, "legacy1", "content1", {"outcome": "success"})
        assert (tmp_path / "legacy1.json").exists()
        assert (tmp_path / "content1.json").exists()

    def test_accumulate_prefers_legacy(self, tmp_path):
        write_cache_json(tmp_path, "legacy2", {"outcome": "OLD"})
        write_cache_json(tmp_path, "content2", {"outcome": "NEW"})
        got = read_dual_cache(tmp_path, "legacy2", "content2", mode="accumulate")
        assert got["outcome"] == "OLD"

    def test_accumulate_falls_back_to_content(self, tmp_path):
        write_cache_json(tmp_path, "content3", {"outcome": "NEW"})
        got = read_dual_cache(tmp_path, "legacy3", "content3", mode="accumulate")
        assert got["outcome"] == "NEW"

    def test_latest_ignores_legacy(self, tmp_path):
        write_cache_json(tmp_path, "legacy4", {"outcome": "OLD"})
        # No content entry → latest mode returns None even though legacy exists.
        assert read_dual_cache(tmp_path, "legacy4", "content4", mode="latest") is None
        write_cache_json(tmp_path, "content4", {"outcome": "NEW"})
        got = read_dual_cache(tmp_path, "legacy4", "content4", mode="latest")
        assert got["outcome"] == "NEW"

    def test_llm_outcome_dual_writes(self, tmp_path):
        ret = {"outcome": "success", "outcome_phrase": "x", "outcome_confidence": "high",
               "out_quote_source": "abstract", "outcome_reasoning": ""}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(ret, "m", "")), \
             patch("extract.code_outcome.time.sleep"):
            extract_outcome("10.1234/dual", abstract_r="ambiguous abstract", title_r="T")
        files = list(tmp_path.glob("outcome_*.json"))
        assert len(files) == 2  # legacy DOI key + content key


def write_cache_json(cache_dir, key, data):
    import json as _json
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(_json.dumps(data), encoding="utf-8")


# ── classify_match_type unit tests (Issue 8) ─────────────────────────────────

_ROW = {
    "doi_r": "10.1000/test",
    "title_r": "A Replication Study",
    "abstract_r": "We replicated Smith (2010) and found consistent results.",
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
             patch("extract.run_extract.call_llm", return_value=(llm_result, "gemini-model", "")), \
             patch("extract.run_extract.time.sleep"):
            return classify_match_type(row)

    def test_returns_single_original(self, tmp_path):
        llm = {"original_match_type": "single_original",
               "original_match_confidence": "high", "reasoning": "one clear target"}
        result = self._classify(tmp_path, _CAND_SINGLE, llm)
        assert result["original_match_type"] == "single_original"
        assert result["original_match_confidence"] == "high"

    def test_returns_multiple_match(self, tmp_path):
        llm = {"original_match_type": "multiple_match",
               "original_match_confidence": "high", "reasoning": "same author/year"}
        result = self._classify(tmp_path, _CAND_MULTI, llm)
        assert result["original_match_type"] == "multiple_match"

    def test_returns_multiple_original(self, tmp_path):
        row = dict(_ROW, abstract_r="We replicated Smith (2010) and Jones (2012).")
        llm = {"original_match_type": "multiple_original",
               "original_match_confidence": "medium", "reasoning": "two independent targets"}
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
        """Second call with same doi_r must use cache — OpenAlex + LLM not called again."""
        llm = {"original_match_type": "single_original",
               "original_match_confidence": "high", "reasoning": "cached"}
        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates",
                   return_value=_CAND_SINGLE) as mock_oa, \
             patch("extract.run_extract.call_llm", return_value=(llm, "gemini-model", "")) as mock_llm, \
             patch("extract.run_extract.time.sleep"):
            classify_match_type(_ROW)  # first call — populates cache
            classify_match_type(_ROW)  # second call — should use cache
        assert mock_oa.call_count == 1
        assert mock_llm.call_count == 1

    def test_invalid_llm_match_type_normalised(self, tmp_path):
        """LLM returning an unknown match_type value should become single_original."""
        llm = {"original_match_type": "unknown_value", "original_match_confidence": "high"}
        result = self._classify(tmp_path, _CAND_SINGLE, llm)
        assert result["original_match_type"] == "single_original"

    def test_prompt_includes_pattern_count_and_candidates(self, tmp_path):
        """The LLM prompt must include distinct pattern count and candidate list."""
        captured_prompt = []
        def fake_llm(prompt, gemini_model=""):
            captured_prompt.append(prompt)
            return ({"original_match_type": "single_original",
                     "original_match_confidence": "high"}, "gemini-model", "")

        with patch("extract.run_extract.LLM_CACHE_DIR", tmp_path), \
             patch("extract.run_extract.find_all_candidates", return_value=_CAND_SINGLE), \
             patch("extract.run_extract.call_llm", side_effect=fake_llm), \
             patch("extract.run_extract.time.sleep"):
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
    "outcome_reasoning": "",
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


class TestRunExtract:
    def _run(self, filtered_csv: str, mock_multi=None, mock_match=None):
        """Helper: write a temp CSV, run extract with mocked APIs, return result DataFrame."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                        delete=False, encoding="utf-8-sig") as f:
            f.write(filtered_csv)
            tmp = Path(f.name)

        with patch("extract.run_extract.classify_match_type",
                   return_value=mock_match or _MOCK_MATCH), \
             patch("extract.run_extract.run_for_doi", return_value=_MOCK_LINK), \
             patch("extract.run_extract.run_multi_original_for_doi",
                   return_value=mock_multi or {
                       "is_false_positive": False, "n_originals": 0,
                       "originals": [], "originals_json": "[]"}), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME), \
             patch("extract.run_extract.DATA_DIR", tmp.parent), \
             patch("extract.run_extract.BASE_DIR", tmp.parent):
            filtered_path = tmp.parent / "filtered.csv"
            if not filtered_path.exists():
                filtered_path.write_text(tmp.read_text(encoding="utf-8-sig"),
                                         encoding="utf-8-sig")
            from extract.run_extract import run_extract
            result = run_extract()

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

        with patch("extract.run_extract.classify_match_type", mock_classify), \
             patch("extract.run_extract.run_for_doi", return_value=_MOCK_LINK), \
             patch("extract.run_extract.run_multi_original_for_doi",
                   return_value={"is_false_positive": False, "n_originals": 0,
                                 "originals": [], "originals_json": "[]"}), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME), \
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

    def test_type_column_set_from_filter_status(self):
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/rep,Rep Paper,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "replication,rule_based,direct replication,high\n"
            "10.1000/repro,Repro Paper,Abstract,2020,Jones,J. Psych,,W2,openalex,"
            "reproduction,rule_based,reproduction study,high\n"
        )
        result = self._run(csv)
        types = dict(zip(result["doi_r"], result["type"]))
        assert types["10.1000/rep"] == "replication"
        assert types["10.1000/repro"] == "reproduction"

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

        with patch("extract.run_extract.classify_match_type", mock_classify), \
             patch("extract.run_extract.run_for_doi", return_value=_MOCK_LINK), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME), \
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

        with patch("extract.run_extract.classify_match_type", return_value=_MOCK_MATCH), \
             patch("extract.run_extract.run_for_doi", side_effect=Exception("API timeout")), \
             patch("extract.run_extract.run_multi_original_for_doi",
                   return_value={"is_false_positive": False, "n_originals": 0,
                                 "originals": [], "originals_json": "[]"}), \
             patch("extract.run_extract.extract_outcome", return_value=_MOCK_OUTCOME), \
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
        with patch("extract.run_extract.classify_match_type", return_value=_MOCK_MATCH), \
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

    def test_link_method_label_is_passed_through(self):
        r = self._merge({"rank": 1, "doi": "10.1/x", "title": "X",
                         "first_author": "A", "year": 2001, "confidence": "high"},
                        link_method="llm_abstract")
        assert r["link_method"] == "llm_abstract"


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
