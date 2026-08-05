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
    RESOLVED_LINK_METHODS,
    make_pair_id,
)
from shared.cache import content_key, read_cache
import extract.code_outcome as code_outcome
import extract.link_original as link_original
import extract.run_extract as run_extract
import shared.llm_client as llm_client
from extract.code_outcome import extract_outcome, _keyword_scan, _expand_to_sentences
from extract.run_extract import (
    _map_method,
    _merge_row,
    _merge_multi_row,
    _score_to_confidence,
)
from shared.token_usage import TokenBudgetExhausted


# ── Sentence expansion unit tests ────────────────────────────────────────────

class TestExpandToSentences:
    def test_context_window(self):
        """n_context sentences either side of the match, clamped at both ends."""
        text = "First sentence. We replicated the effect. Third sentence."
        start = text.index("We replicated")
        assert "We replicated the effect" in _expand_to_sentences(
            text, start, start + 5, n_context=0)
        window = _expand_to_sentences(text, start, start + 5, n_context=1)
        assert "First sentence" in window
        assert "We replicated the effect" in window
        assert "Third sentence" in window
        # A match in the first sentence clamps rather than running off the front.
        head = "Failed to replicate. Second sentence. Third sentence."
        clamped = _expand_to_sentences(head, 0, 18, n_context=1)
        assert "Failed to replicate" in clamped
        assert "Second sentence" in clamped
        # A lone sentence and an empty string are both non-errors.
        assert "We replicated the effect" in _expand_to_sentences(
            "We replicated the effect.", 3, 15, n_context=1)
        assert _expand_to_sentences("", 0, 0) == ""

    def test_et_al_not_split(self):
        text = "Smith et al. found an effect. The replication failed."
        start = text.index("The replication")
        result = _expand_to_sentences(text, start, start + 20, n_context=1)
        assert "Smith et al" in result
        assert "replication failed" in result


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
        # A failure phrase outranks a success keyword in the same clause.
        ("we failed to replicate the originally replicated finding", "failure"),
        # Declines: effect size alone must not decide the outcome. `mixed` requires
        # the authors' own evidence to be partly supporting and partly not; a
        # supported-but-smaller effect is a success. Neither is decidable here, so
        # the row goes to the LLM rather than being coded on magnitude alone.
        ("significant but smaller effect than the original study reported", None),
        ("the replication produced a reduced effect magnitude", None),
        ("we attempted this study across multiple sites", None),
    ])
    def test_keyword_scan(self, text, expected):
        hit = _keyword_scan(text, "abstract")
        if expected is None:
            assert hit is None, f"Expected no hit for: {text!r}"
            return
        assert hit is not None, f"No match for: {text!r}"
        assert hit["outcome"] == expected, (
            f"Expected {expected}, got {hit['outcome']} for: {text!r}"
        )

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
        is reported, so an explicit success claim in the same abstract wins. Alone,
        the same phrase still codes failure — but only at medium confidence.
        """
        hit = _keyword_scan(text, "abstract")
        assert hit is not None
        assert hit["outcome"] == "success"
        alone = _keyword_scan("We found no evidence of ego depletion.", "abstract")
        assert alone["outcome"] == "failure"
        assert alone["outcome_confidence"] == "medium"

    def test_outcome_phrase_spans_the_surrounding_sentences(self):
        text = ("Prior work found a large effect. We failed to replicate this effect "
                "in our sample. Our power was 0.95.")
        hit = _keyword_scan(text, "abstract")
        assert hit is not None
        assert len(hit["outcome_phrase"]) > len("failed to replicate")
        assert ("Prior work" in hit["outcome_phrase"]
                or "power was" in hit["outcome_phrase"])
        # The scanned source is reported back, not inferred.
        assert hit["out_quote_source"] == "abstract"
        assert _keyword_scan("successfully replicated", "fulltext")["out_quote_source"] == "fulltext"


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
        """#70: a 'failed to replicate' abstract whose full text shows the paper checks
        no named original becomes not_a_replication instead of a coded failure. The veto
        is the FULL-TEXT pass's record_type_check — the abstract pass never carries it."""
        abstract_cbd = {"outcome": "cannot_be_determined", "outcome_phrase": "",
                        "confident": False, "out_quote_source": ""}
        fulltext_neither = {"outcome": "failure", "outcome_phrase": "background prose",
                            "record_type_check": "neither", "confident": True,
                            "out_quote_source": "fulltext"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(abstract_cbd, "m", ""), (fulltext_neither, "m", "")]), \
             patch("extract.code_outcome.time.sleep"):
            result = extract_outcome(
                "10.1234/veto",
                abstract_r="prior work failed to replicate the effect, we do something else",
                fulltext="This paper is about something else entirely.",
                title_r="Not actually a replication",
            )
        assert result["outcome"] == "not_a_replication"

    def test_keyword_hit_skips_llm_in_no_llm_mode(self):
        """The keyword fast-path, whole: with the LLM off a replication abstract is
        coded from the keyword scan alone — no call, no reasoning, and the row names
        the rule rather than a model that never answered."""
        with patch("extract.code_outcome.call_llm") as mock_llm:
            result = extract_outcome(
                "10.1234/test",
                abstract_r="we found no evidence of the original effect",
                title_r="A Replication Study",
                record_type="replication",
                no_llm=True,
            )
        mock_llm.assert_not_called()
        assert result["outcome"] == "failure"
        assert result["out_quote_source"] == "abstract"
        assert result.get("outcome_reasoning", "") == ""
        assert result["llm_model"] == "keyword"

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

    def test_outcome_reasoning_empty_on_llm_failure(self):
        with patch("extract.code_outcome.call_llm", return_value=(None, "", "")):
            result = extract_outcome("10.1234/fail2", abstract_r="ambiguous")
        assert result.get("outcome_reasoning", "") == ""

    def test_abstract_pass_never_reads_a_record_type_check(self, tmp_path):
        """The abstract pass sees exactly the evidence two validated screen voters
        already saw, so it is not asked to re-decide what the paper is — and a stray
        answer must not end the row."""
        llm_return = {
            "outcome": "failure",
            "outcome_phrase": "We did not find support for the original effect.",
            "record_type_check": "neither",
            "confident": True,
            "out_quote_source": "abstract",
            "outcome_reasoning": "Authors explicitly state the effect did not replicate.",
        }
        result, mock_llm = self._run_llm(tmp_path, llm_return=llm_return)
        assert "record_type_check" not in mock_llm.call_args[0][0]
        assert result["outcome"] == "failure"


# ── Outcome-coding unification tests ─────────────────────────────────────────

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


class TestFulltextEscalation:
    _ABS_CBD = {"outcome": "cannot_be_determined", "outcome_phrase": "",
                "outcome_confidence": "low", "out_quote_source": "abstract",
                "outcome_reasoning": "abstract too thin"}
    _FT_FAIL = {"outcome": "failure", "outcome_phrase": "The effect did not replicate.",
                "outcome_confidence": "high", "out_quote_source": "fulltext",
                "outcome_reasoning": "results section is explicit"}

    # No abstract → escalate even though the abstract call did not return cbd.
    _ABS_THIN = {"outcome": "success", "outcome_phrase": "", "outcome_confidence": "low",
                 "out_quote_source": "title", "outcome_reasoning": ""}

    @pytest.mark.parametrize("abstract,first", [
        ("ambiguous abstract", _ABS_CBD),   # the abstract could not settle it
        ("",                   _ABS_THIN),  # there was no abstract to settle it with
    ])
    def test_escalation_fires(self, tmp_path, abstract, first):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(first, "m", ""), (self._FT_FAIL, "m", "")]) as mock_llm:
            result = extract_outcome(
                "10.1234/esc", abstract_r=abstract,
                fulltext="RESULTS: the effect did not replicate.", title_r="A Study",
            )
        assert mock_llm.call_count == 2
        # Second (escalation) prompt must contain the parsed fulltext.
        assert "did not replicate" in mock_llm.call_args_list[1][0][0]
        assert result["outcome"] == "failure"
        assert result["out_quote_source"] == "fulltext"

    @pytest.mark.parametrize("flag,fulltext", [
        (False, "RESULTS: the effect did not replicate."),   # escalation switched off
        (True,  ""),                                         # nothing to escalate to
    ])
    def test_no_escalation(self, tmp_path, flag, fulltext):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", flag), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(self._ABS_CBD, "m", ""), (self._FT_FAIL, "m", "")]) as mock_llm:
            result = extract_outcome(
                "10.1234/noesc", abstract_r="ambiguous abstract",
                fulltext=fulltext, title_r="A Study",
            )
        assert mock_llm.call_count == 1
        assert result["outcome"] == "cannot_be_determined"

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


class TestOutcomePromptOffersEveryCategory:
    """A category the schema defines but the prompt's answer options omit is a
    category the model can never return — the parser then coerces it away silently,
    and the coding vocabulary quietly shrinks. The two must agree by construction."""

    @staticmethod
    def _schema_block(prompt: str) -> str:
        """The response-schema section: the enumerated answer fields, nothing else."""
        start = prompt.index("Return exactly")
        return prompt[start:prompt.index("Use these field names", start)]

    def test_replication_prompt_offers_every_replication_category(self):
        from shared.prompts import build_outcome_prompt
        from shared.schema import outcome_categories_for
        block = self._schema_block(build_outcome_prompt("t", "a"))
        for category in outcome_categories_for("replication"):
            # not_a_replication is the full-text pass's is_genuine_attempt veto, not
            # an option the model picks from the outcome enum.
            if category == "not_a_replication":
                continue
            assert f'"{category}"' in block, category

    def test_reproduction_prompt_offers_every_axis_value(self):
        from shared.prompts import build_repro_outcome_prompt
        from shared.schema import (COMPUTATION_OUTCOME_VALUES,
                                   ROBUSTNESS_OUTCOME_VALUES)
        block = self._schema_block(build_repro_outcome_prompt("t", "a"))
        for value in COMPUTATION_OUTCOME_VALUES | ROBUSTNESS_OUTCOME_VALUES:
            assert f'"{value}"' in block, value


class TestOutcomePromptPlaceholderInjection:
    """Paper text that happens to contain a template placeholder must render literally.
    Chained .replace() calls rescanned their own output, so a title containing
    "{abstract_r}" was replaced by the abstract."""

    def test_placeholder_text_in_every_input_survives_verbatim(self):
        from shared.prompts import build_outcome_prompt, build_repro_outcome_prompt
        marks = {"title": "T {abstract_r} {fulltext_block}",
                 "abstract": "A {fulltext_block} {title_r}",
                 "authors": "{title_r} et al",
                 "year": "{field_count}",
                 "orig_title": "O {evidence_line} {record_type_check_field}",
                 "text": "F {abstract_r} {original_block}"}
        for build in (build_outcome_prompt, build_repro_outcome_prompt):
            prompt = build(marks["title"], marks["abstract"], marks["authors"],
                           marks["year"], marks["orig_title"], text_snip=marks["text"],
                           multi_original=True)
            for value in marks.values():
                assert value in prompt, (build.__name__, value)


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

    def test_the_multi_original_flag_misses(self, tmp_path):
        """The flag adds a sentence to the prompt telling the model the paper's other
        originals are coded on their own rows. Leaving it out of the key let a
        per-target row read back a single-original row's answer for the same pair."""
        self._run(tmp_path, abstract_r="a", title_r="T")
        mock = self._run(tmp_path, abstract_r="a", title_r="T", multi_original=True)
        assert mock.call_count == 1
        assert len(list(tmp_path.glob("outcome_*.json"))) == 2

    def test_the_flag_is_appended_only_when_set(self, tmp_path):
        """A key component that is always present moves EVERY key. The outcome cache
        is the most expensive one the pipeline holds — its entries may carry a
        full-text escalation — so a single-original key must be byte-identical to the
        one written before the flag existed."""
        from shared.cache import content_key
        from shared.llm_client import ladder_fingerprint
        from shared.config import GEMINI_HEAVY_MODEL
        from shared.prompts import prompt_version

        parts = (ladder_fingerprint(GEMINI_HEAVY_MODEL),
                 prompt_version("build_outcome_prompt"), "replication",
                 "T", "a", "", "", "", "")
        pre_pr = content_key("outcome", "10.1234/key", *parts)
        with_flag = content_key("outcome", "10.1234/key", *parts, "multi_original")

        self._run(tmp_path, abstract_r="a", title_r="T")
        assert [f.stem for f in tmp_path.glob("outcome_*.json")] == [pre_pr]
        assert with_flag != pre_pr

    def test_changed_fulltext_misses(self, tmp_path):
        """The escalation reads the fulltext, so a re-parsed PDF must not replay the
        verdict reached without it."""
        self._run(tmp_path, abstract_r="a", title_r="T", fulltext="old text")
        mock = self._run(tmp_path, abstract_r="a", title_r="T", fulltext="new text")
        assert mock.call_count == 1

    def test_prompt_version_in_key(self, tmp_path, monkeypatch):
        from shared import prompts
        self._run(tmp_path, abstract_r="a", title_r="T")
        monkeypatch.setattr(prompts, "_OUTCOME_TEMPLATE",
                            prompts._OUTCOME_TEMPLATE + " EDIT")
        prompts.prompt_version.cache_clear()
        try:
            mock = self._run(tmp_path, abstract_r="a", title_r="T")
        finally:
            prompts.prompt_version.cache_clear()
        assert mock.call_count == 1


def write_cache_json(cache_dir, key, data):
    import json as _json
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(_json.dumps(data), encoding="utf-8")


# ── run_extract orchestration tests ──────────────────────────────────────────

_MOCK_LINK = {
    "resolved": True,
    "resolution_method": "same_author_year_title_overlap",
    "resolution_score": 0.95,
    "resolved_doi_o": "10.1037/h0054651",
    "resolved_title_o": "The Original Study",
    "resolved_year_o": 1935,
    "resolved_author_o": "Smith",
    "llm_evidence": "Smith (1935)",
    "grobid_intro": "",
}
_MOCK_OUTCOME = {
    "outcome": "success", "outcome_phrase": "replicated",
    "outcome_confidence": "high", "out_quote_source": "abstract",
    "outcome_reasoning": "", "llm_model": "gemini-outcome",
}


def _mock_target(key: str, doi: str, title: str, author: str, year: int,
                 **over) -> dict:
    """One entry of identify_targets_with_llm's validated target list.

    The record is the mapped @key: the DOI comes from it, never from the model."""
    target = {"key": key, "match_certain": True, "target_as_named": title,
              "study_numbers": "", "replication_study_numbers": "",
              "evidence_quote": f"{author} et al. ({year})",
              "record": {"doi": doi, "title": title, "first_author": author,
                         "year": year, "openalex_id": ""}}
    target.update(over)
    return target


# A ladder that named two originals and accepted neither as THE link — what the
# per-target adapter exists for.
_MOCK_MULTI_LINK = dict(
    _MOCK_LINK, resolved=False, resolution_method="llm_multi_target",
    multi_target=True, n_targets=2, target_stage="llm_gemini",
    unidentified_count=0, resolved_doi_o="", resolved_title_o="",
    targets=[_mock_target("@jones2000", "10.1000/a", "Study A", "Jones", 2000),
             _mock_target("@kim2001",   "10.1000/b", "Study B", "Kim",   2001)],
)

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

    def _run(self, filtered_csv: str, link=None, screen=None, **run_kwargs):
        """Helper: write a temp CSV, run extract with mocked APIs, return result DataFrame."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                        delete=False, encoding="utf-8-sig") as f:
            f.write(filtered_csv)
            tmp = Path(f.name)

        with patch("extract.run_extract.classify_replication",
                   return_value=screen or _YES_SCREEN), \
             patch("extract.run_extract.run_for_doi", return_value=link or _MOCK_LINK), \
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
        """False positives must appear in output without running the ladder."""
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/fp,False Pos,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "false_positive,rule_based,not a replication,high\n"
            "10.1000/rep,Real Rep,Abstract,2020,Jones,J. Psych,,W2,openalex,"
            "replication,rule_based,direct replication,high\n"
        )
        mock_ladder = MagicMock(return_value=_MOCK_LINK)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                         delete=False, encoding="utf-8-sig") as f:
            f.write(csv)
            tmp = Path(f.name)

        with patch("extract.run_extract.classify_replication", return_value=_YES_SCREEN), \
             patch("extract.run_extract.run_for_doi", mock_ladder), \
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
        # the ladder ran only for the replication row, not the false positive
        assert mock_ladder.call_count == 1
        assert "fp" not in mock_ladder.call_args[0][0]

    def test_two_targets_expand_to_two_rows(self):
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/multi,Multi-target,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "replication,rule_based,direct replication,high\n"
        )
        result = self._run(csv, link=_MOCK_MULTI_LINK)
        assert len(result) == 2
        assert list(result["doi_o"]) == ["10.1000/a", "10.1000/b"]
        assert list(result["original_rank"].astype(int)) == [1, 2]
        assert list(result["n_originals"].astype(int)) == [2, 2]
        assert set(result["original_match_type"]) == {"multiple_original"}

    _TYPE_CSV = (
        "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
        "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
        "10.1000/rep,Rep Paper,Abstract,2020,Smith,J. Psych,,W1,openalex,"
        "replication,rule_based,direct replication,high\n"
        "10.1000/repro,Repro Paper,Abstract,2020,Jones,J. Psych,,W2,openalex,"
        "reproduction,rule_based,reproduction study,high\n"
    )

    def test_type_column_falls_back_to_filter_status_without_an_llm(self):
        """--no-llm runs no screen, so Stage 2's filter_status is all there is."""
        result = self._run(self._TYPE_CSV, no_llm=True)
        types = dict(zip(result["doi_r"], result["type"]))
        assert types["10.1000/rep"] == "replication"
        assert types["10.1000/repro"] == "reproduction"
        assert set(result["screen_categories"]) == {""}

    def test_rows_are_streamed_in_chunks_abstract_bearing_ones_first(self, monkeypatch):
        """filtered.csv is read in chunks, so the abstract-first ordering and the
        --limit count must hold across chunk boundaries, not just within one."""
        monkeypatch.setattr(run_extract, "_CHUNK_ROWS", 2)
        header = ("doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
                  "openalex_id_r,source,filter_status,filter_method,filter_evidence,"
                  "filter_confidence\n")
        rows = "".join(
            f"10.1000/r{i},Paper {i},{'Abstract' if i % 2 else ''},2020,Smith,"
            f"J. Psych,,W{i},openalex,replication,rule_based,direct replication,high\n"
            for i in range(6))
        result = self._run(header + rows, limit=4)
        # Four rows processed, and the three with an abstract came first.
        assert list(result["doi_r"]) == ["10.1000/r1", "10.1000/r3", "10.1000/r5",
                                         "10.1000/r0"]

    def test_the_ladder_is_not_run_for_false_positives(self):
        """Routing test: false_positive must bypass the resolution ladder entirely."""
        csv = (
            "doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
            "openalex_id_r,source,filter_status,filter_method,filter_evidence,filter_confidence\n"
            "10.1000/fp,FP,Abstract,2020,Smith,J. Psych,,W1,openalex,"
            "false_positive,rule_based,meta-discussion,high\n"
        )
        mock_ladder = MagicMock(return_value=_MOCK_LINK)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                         delete=False, encoding="utf-8-sig") as f:
            f.write(csv)
            tmp = Path(f.name)

        with patch("extract.run_extract.classify_replication", return_value=_YES_SCREEN), \
             patch("extract.run_extract.run_for_doi", mock_ladder), \
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

        mock_ladder.assert_not_called()


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
             patch("extract.run_extract.run_for_doi", side_effect=Exception("API timeout")), \
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
             patch("extract.run_extract.run_for_doi", return_value=_MOCK_LINK), \
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

    def test_merge_row_carries_the_target_study_numbers(self):
        """The resolved link names which study inside the original was targeted;
        before this the column was written only by the multi-original path."""
        link = {"resolution_method": "llm_fulltext", "resolved_doi_o": "10.1/orig",
                "resolved_title_o": "Original", "resolved_year_o": 2000,
                "resolved_author_o": "Smith", "resolved_study_o": "1, 2",
                "resolution_score": 1.0, "llm_confidence": "high"}
        filter_row = pd.Series({"doi_r": "10.1/rep", "title_r": "Rep",
                                "filter_status": "replication"})
        with patch("extract.run_extract._build_ref_o", return_value=("ref", "auth")):
            row = _merge_row(filter_row, link, _MOCK_OUTCOME,
                             "single_original", "high", 1, 1)
        assert row["study_o"] == "1, 2"


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
                "outcome_confidence": "high", "out_quote_source": "abstract"}

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

    def test_two_doi_less_originals_with_openalex_ids_are_distinct(self):
        r1 = self._merge({"rank": 1, "doi": "", "title": "A Book",
                          "openalex_id": "W1", "confidence": "high"})
        r2 = self._merge({"rank": 2, "doi": "", "title": "A Book",
                          "openalex_id": "W2", "confidence": "high"})
        assert r1["pair_id"] != r2["pair_id"]


# ── The gate's study-count bound ──────────────────────────────────────────────

class TestStudyCountBound:
    """3 ≤ N < 1900 — a captured year is not a study count. The count no longer
    routes a row anywhere; it decides whether a deterministic ladder stage may END
    the row (see link_original.may_stop_at_a_rule)."""

    def test_year_in_title_is_not_a_count(self):
        assert not link_original._study_count_stated("Replication of 2019 findings", "")

    def test_year_in_abstract_is_not_a_count(self):
        assert not link_original._study_count_stated(
            "A paper", "We report replications of 2019 studies conducted earlier.")

    def test_a_count_in_the_title_is_stated(self):
        assert link_original._study_count_stated("Replication of 12 studies", "")

    def test_a_count_in_the_abstract_is_stated(self):
        assert link_original._study_count_stated(
            "A paper", "We replicated 28 classic studies across many labs.")

    def test_a_count_below_the_minimum_is_not_stated(self):
        assert not link_original._study_count_stated("Replication of 2 studies", "")

    def test_a_project_name_alone_is_not_a_count(self):
        """Many labs replicating ONE original is one target — the old rule read the
        project name as N originals and expanded single-target papers into N rows."""
        assert not link_original._study_count_stated(
            "Many Labs 2: replicating effects", "")


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

    def test_an_osf_record_named_only_by_url_is_still_skipped(self, tmp_path):
        """FLoRA identifies an OSF record by URL five times more often than by DOI
        — over flora.csv, 366 rows by URL against 51 by DOI. The Reproducibility
        Project rows are the case that found it: all 92 carry the aggregate Science
        paper in `doi_r` and the individual replication's OSF page in `url_r`, so a
        DOI-keyed skip list re-extracts every one of them."""
        flora = self._flora(tmp_path, [
            {"doi_r": "10.1126/science.aac4716", "doi_r_alt": "",
             "url_r": "https://osf.io/su6bm"},
            {"doi_r": "", "doi_r_alt": "", "url_r": "http://osf.io/XSE7Q/"},
            {"doi_r": "", "doi_r_alt": "", "url_r": "https://example.com/paper"},
        ])
        got = run_extract._load_flora_skip_dois(None, flora)
        assert "10.17605/osf.io/su6bm" in got
        assert "10.17605/osf.io/xse7q" in got          # case and trailing slash
        assert got == {"10.1126/science.aac4716", "10.17605/osf.io/su6bm",
                       "10.17605/osf.io/xse7q"}

    def test_the_entry_sheet_contributes_osf_urls_only_when_validated(self, tmp_path):
        sheet = self._sheet(tmp_path, [
            {"doi_r": "10.1/chosen", "validation_status": "validated - chosen",
             "url_r": "https://osf.io/aaaaa"},
            {"doi_r": "10.1/blank", "validation_status": "",
             "url_r": "https://osf.io/bbbbb"},
        ])
        assert run_extract._load_flora_skip_dois(sheet, None) == {
            "10.1/chosen", "10.17605/osf.io/aaaaa"}


# ── Reproduction outcome coding (3x3 computation/robustness grid) ────────────

class TestReproductionOutcome:
    """Reproductions use a different vocabulary from replications; the row's
    type must select it, or every reproduction verdict is coerced away."""

    def test_vocabulary_selected_by_type(self):
        from shared.schema import outcome_categories_for
        repro = outcome_categories_for("reproduction")
        repl = outcome_categories_for("replication")
        assert "computationally reproducible, robust" in repro
        assert "computationally reproducible, robust" not in repl
        assert "success" in repl and "success" not in repro
        assert "cannot_be_determined" in repro and "cannot_be_determined" in repl

    def test_axes_are_stored_and_the_outcome_is_derived(self, tmp_path):
        mock = {"outcome_computation": "computational issues",
                "outcome_computational_quote": "The coefficient differed.",
                "out_quote_computational_source": "abstract",
                "outcome_robustness": "robustness challenges",
                "outcome_robustness_quote": "Two specifications reversed the sign.",
                "out_quote_robust_source": "abstract",
                "confident": True, "outcome_reasoning": "r"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")), \
             patch("extract.code_outcome.time.sleep"):
            res = extract_outcome("10.1/repro", abstract_r="we re-ran their code",
                                  record_type="reproduction")
        assert res["outcome"] == "computational issues, robustness challenges"
        assert res["outcome_computation"] == "computational issues"
        assert res["outcome_robustness"] == "robustness challenges"
        assert res["out_quote_robust_source"] == "abstract"
        assert res["outcome_confidence"] == "high"

    def test_unsettled_axis_derives_cannot_be_determined(self, tmp_path):
        """Half a verdict must not read as a whole one — but the settled axis is
        still stored."""
        mock = {"outcome_computation": "computationally reproducible",
                "outcome_computational_quote": "Every number came out again.",
                "out_quote_computational_source": "abstract",
                "outcome_robustness": "cannot_be_determined",
                "confident": False, "outcome_reasoning": "r"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", False), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")), \
             patch("extract.code_outcome.time.sleep"):
            res = extract_outcome("10.1/half", abstract_r="a re-analysis",
                                  record_type="reproduction")
        assert res["outcome"] == "cannot_be_determined"
        assert res["outcome_computation"] == "computationally reproducible"
        assert res["outcome_confidence"] == "low"

    def test_replication_value_rejected_for_reproduction(self, tmp_path):
        """If the LLM answers with the replication vocabulary for a reproduction,
        it must NOT be accepted silently."""
        mock = {"outcome": "success", "outcome_phrase": "q", "confident": True,
                "out_quote_source": "abstract", "outcome_reasoning": "r"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", False), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")), \
             patch("extract.code_outcome.time.sleep"):
            res = extract_outcome("10.1/repro2", abstract_r="re-analysis",
                                  record_type="reproduction")
        assert res["outcome"] == "cannot_be_determined"

    def test_reproduction_skips_replication_keyword_scan(self, tmp_path):
        """'failed to replicate' in a reproduction abstract must not shortcut to
        the replication enum — it must reach the reproduction LLM prompt."""
        mock = {"outcome_computation": "computational issues",
                "outcome_computational_quote": "q",
                "out_quote_computational_source": "abstract",
                "outcome_robustness": "not checked",
                "outcome_robustness_quote": "", "out_quote_robust_source": "",
                "confident": True, "outcome_reasoning": "r"}
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.call_llm", return_value=(mock, "m", "")) as mock_llm, \
             patch("extract.code_outcome.time.sleep"):
            res = extract_outcome("10.1/repro3",
                                  abstract_r="We failed to replicate the reported numbers.",
                                  record_type="reproduction")
        assert mock_llm.called, "reproduction must not be short-circuited by keyword scan"
        assert res["outcome"] == "computational issues, not checked"
        assert "reproduction study" in mock_llm.call_args[0][0]
        assert "AXIS 1 — outcome_computation" in mock_llm.call_args[0][0]


# ── Per-axis escalation, record_type_check and response repair ──────────────

class TestReproductionEscalation:
    """A reproduction has two axes, and either one unresolved is a reason to read the
    full text — but the escalation re-codes BOTH, so no row ever carries one axis from
    the abstract beside the other from the full text."""

    _ABS_HALF = {"outcome_computation": "computationally reproducible",
                 "outcome_computational_quote": "All numbers matched.",
                 "out_quote_computational_source": "abstract",
                 "outcome_robustness": "cannot_be_determined",
                 "outcome_robustness_quote": "", "out_quote_robust_source": "",
                 "confident": False, "outcome_reasoning": "robustness unclear"}
    _FT_BOTH = {"outcome_computation": "technical failure",
                "outcome_computational_quote": "The deposited archive would not run.",
                "out_quote_computational_source": "fulltext",
                "outcome_robustness": "robustness challenges",
                "outcome_robustness_quote": "Two specifications reversed the sign.",
                "out_quote_robust_source": "fulltext",
                "confident": True, "outcome_reasoning": "both settled from the methods"}

    def _run(self, tmp_path, responses, **kw):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(r, "m", "") for r in responses]) as mock_llm:
            result = extract_outcome("10.1/axis", abstract_r="a re-analysis",
                                     fulltext="METHODS: we re-ran the archive.",
                                     title_r="A Reproduction",
                                     record_type="reproduction", **kw)
        return result, mock_llm

    def test_one_unsettled_axis_escalates(self, tmp_path):
        result, mock_llm = self._run(tmp_path, [self._ABS_HALF, self._FT_BOTH])
        assert mock_llm.call_count == 2

    def test_escalation_replaces_both_axes(self, tmp_path):
        result, _ = self._run(tmp_path, [self._ABS_HALF, self._FT_BOTH])
        assert result["outcome_computation"] == "technical failure"
        assert result["outcome_robustness"] == "robustness challenges"
        assert result["outcome"] == "technical failure, robustness challenges"

    def test_both_axes_settled_does_not_escalate(self, tmp_path):
        settled = dict(self._ABS_HALF, outcome_robustness="robust",
                       outcome_robustness_quote="It held across specifications.",
                       out_quote_robust_source="abstract")
        result, mock_llm = self._run(tmp_path, [settled])
        assert mock_llm.call_count == 1
        assert result["outcome"] == "computationally reproducible, robust"


class TestRecordTypeCheckRecode:
    """The full-text pass is the first call in the pipeline to see the methods, and
    the screen that set `type` could not. A row coded in the wrong vocabulary is
    re-coded once — one hop, never a loop."""

    _ABS_CBD = {"outcome": "cannot_be_determined", "outcome_phrase": "",
                "confident": False, "out_quote_source": ""}
    _FT_SAYS_REPRO = {"outcome": "failure", "outcome_phrase": "It did not hold.",
                      "record_type_check": "reproduction", "confident": True,
                      "out_quote_source": "fulltext", "outcome_reasoning": "r"}
    _REPRO_VERDICT = {"outcome_computation": "computational issues",
                      "outcome_computational_quote": "The coefficient differed.",
                      "out_quote_computational_source": "abstract",
                      "outcome_robustness": "not checked",
                      "outcome_robustness_quote": "", "out_quote_robust_source": "",
                      "confident": True, "outcome_reasoning": "r"}

    def _run(self, tmp_path, responses, record_type="replication"):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", True), \
             patch("extract.code_outcome.time.sleep"), \
             patch("extract.code_outcome.call_llm",
                   side_effect=[(r, "m", "") for r in responses]) as mock_llm:
            result = extract_outcome("10.1/type", abstract_r="ambiguous",
                                     fulltext="METHODS: we re-analysed their data.",
                                     title_r="A Study", record_type=record_type)
        return result, mock_llm

    def test_other_vocabulary_recodes_once_and_reports_the_type(self, tmp_path):
        result, mock_llm = self._run(
            tmp_path, [self._ABS_CBD, self._FT_SAYS_REPRO, self._REPRO_VERDICT])
        # abstract, full text, then one reproduction-vocabulary call.
        assert mock_llm.call_count == 3
        assert result["outcome"] == "computational issues, not checked"
        assert result["record_type"] == "reproduction"
        assert "reproduction study" in mock_llm.call_args_list[2][0][0]

    def test_the_recode_is_not_itself_re_checked(self, tmp_path):
        """The reproduction call's full-text pass saying "replication" must not send
        the row back — one hop, no loop."""
        repro_abs_cbd = {"outcome_computation": "cannot_be_determined",
                         "outcome_robustness": "cannot_be_determined",
                         "confident": False, "outcome_reasoning": "r"}
        repro_ft = dict(self._REPRO_VERDICT, record_type_check="replication")
        result, mock_llm = self._run(
            tmp_path, [self._ABS_CBD, self._FT_SAYS_REPRO, repro_abs_cbd, repro_ft])
        assert mock_llm.call_count == 4
        assert result["record_type"] == "reproduction"

    @pytest.mark.parametrize("check", ["replication", "unclear"])
    def test_an_answer_that_is_not_the_other_vocabulary_changes_nothing(self, tmp_path,
                                                                        check):
        """Only the other vocabulary triggers a recode: agreement and "unclear" both
        leave the row coded as it was, with no third call."""
        ft = dict(self._FT_SAYS_REPRO, record_type_check=check)
        result, mock_llm = self._run(tmp_path, [self._ABS_CBD, ft])
        assert mock_llm.call_count == 2
        assert result["outcome"] == "failure"
        assert "record_type" not in result

    def test_a_failed_recode_is_not_cached(self, tmp_path):
        """The recode's own call failing yields api_error, which the outer call must not
        checkpoint under the replication key — a re-run has to try again."""
        first, _ = self._run(
            tmp_path, [self._ABS_CBD, self._FT_SAYS_REPRO, None, None, None])
        assert first["outcome"] == "api_error"
        second, mock_llm = self._run(
            tmp_path, [self._ABS_CBD, self._FT_SAYS_REPRO, self._REPRO_VERDICT])
        assert mock_llm.call_count == 3
        assert second["outcome"] == "computational issues, not checked"

    def test_a_recoded_type_reaches_the_row(self):
        row = run_extract._apply_outcome(
            {"type": "replication"},
            {"outcome": "computational issues, robust", "record_type": "reproduction"})
        assert row["type"] == "reproduction"


class TestOutcomeResponseRepair:
    """The prompts no longer spend model attention on JSON discipline, so the parser
    repairs what a parser should repair. (The trailing-comma repair lives in
    llm_client._parse_llm_json and is covered there.)"""

    def _run(self, tmp_path, response):
        with patch("extract.code_outcome.LLM_CACHE_DIR", tmp_path), \
             patch("extract.code_outcome.OUTCOME_FULLTEXT_ESCALATION", False), \
             patch("extract.code_outcome.call_llm", return_value=(response, "m", "")), \
             patch("extract.code_outcome.time.sleep"):
            return extract_outcome("10.1/repair", abstract_r="an abstract",
                                   title_r="T")

    def test_string_true_is_read_as_confident(self, tmp_path):
        for value in ("true", "True"):
            result = self._run(tmp_path, {"outcome": "success", "outcome_phrase": "q",
                                          "out_quote_source": "abstract",
                                          "confident": value,
                                          "outcome_reasoning": f"r{value}"})
            assert result["outcome_confidence"] == "high", value

    def test_string_false_is_not_read_as_confident(self, tmp_path):
        result = self._run(tmp_path, {"outcome": "success", "outcome_phrase": "q",
                                      "out_quote_source": "abstract",
                                      "confident": "false", "outcome_reasoning": "r"})
        assert result["outcome_confidence"] == "low"

    def test_null_becomes_empty_not_the_string_none(self, tmp_path):
        result = self._run(tmp_path, {"outcome": "success", "outcome_phrase": None,
                                      "out_quote_source": None, "confident": True,
                                      "outcome_reasoning": None})
        assert result["outcome_phrase"] == ""
        assert result["out_quote_source"] == ""
        assert result["outcome_reasoning"] == ""


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

    @pytest.mark.parametrize("title_o", [
        "A Study of Things",
        "  a study of THINGS.  ",   # case and punctuation must not hide a self-link
    ])
    def test_self_link_by_title_rejected(self, title_o):
        out = run_extract._guard_original_link(self._row(doi_o="", title_o=title_o))
        assert out["link_method"] == "target_pending"

    def test_demotion_clears_a_merged_outcome(self):
        """The multi-original path merges the outcome before the guard runs, so a
        rejected row would otherwise carry a coded outcome on an unresolved link."""
        out = run_extract._guard_original_link(self._row(
            doi_o="10.1/repl", outcome="success", outcome_phrase="we replicated it",
            outcome_confidence="high", out_quote_source="abstract"))
        assert out["link_method"] == "target_pending"
        assert out["outcome"] == "pending"
        assert out["outcome_phrase"] == ""
        assert out["outcome_confidence"] == "low"
        assert out["out_quote_source"] == ""

    def test_demotion_clears_the_rejected_original(self):
        """study_o and title_o described the original the guard just threw out; left
        behind, they name a study inside a paper the row no longer links to."""
        out = run_extract._guard_original_link(
            self._row(doi_o="10.1/repl", study_o="1, 2"))
        assert out["link_method"] == "target_pending"
        assert out["study_o"] == ""
        assert out["title_o"] == ""

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

    @pytest.mark.parametrize("openalex_hit", [
        None,                              # nothing came back at all
        {"doi": "", "openalex_id": ""},    # a hit with no identifier on it
    ])
    def test_genuinely_empty_doi_with_real_title_is_kept(self, openalex_hit):
        """No DOI anywhere, but a substantive distinct title -> keep the row and
        mark it explicitly rather than dropping a valid original. With no identifier
        to key on, pair_id must stay exactly as it was."""
        with patch("extract.run_extract._search_crossref_by_title", return_value=None), \
             patch("extract.run_extract._search_openalex_by_title",
                   return_value=openalex_hit):
            out = run_extract._guard_original_link(self._row(doi_o=""))
        assert out["link_method"] == "llm_fulltext"
        assert out["doi_o"] == ""
        assert out["doi_o_verification"] == "no_doi"
        assert out.get("oa_work_id_o", "") == ""
        assert out["pair_id"] == "p"

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


class TestReferenceStringTargets:
    """REGRESSION (doi_r 10.1016/j.physbeh.2021.113324, PR #122 acceptance run): the
    per-target adapter wrote two rows whose title_o was the raw citation line a
    numeric reference list had been parsed into — "[2] L.J.T. Balter, et al.,
    Low-grade inflammation decrea…" and "[3] M. Moieni, M.R" — the second with an
    empty doi_o and link_confidence "high"."""

    _CITATION = ("[2] L.J.T. Balter, et al., Low-grade inflammation decreases emotion "
                 "recognition - Evidence from the vaccination model of inflammation, "
                 "Brain Behav. Immun. 73 (2018) 216-221.")
    _FRAGMENT = "[3] M. Moieni, M.R"

    def _entry(self, title: str, doi: str = "", hit=None) -> dict:
        target = {"match_certain": True, "target_as_named": "T", "study_numbers": "",
                  "replication_study_numbers": "", "evidence_quote": "q",
                  "record": {"doi": doi, "title": title, "first_author": "Balter",
                             "year": 2018}}
        with patch("shared.doi_verify.resolve_doi_by_metadata", return_value=hit):
            return run_extract._target_entry(target, "10.1/repl")

    def test_a_citation_string_is_cleaned_down_to_its_title(self):
        entry = self._entry(self._CITATION, doi="10.2/orig")
        assert entry["title"].startswith("Low-grade inflammation decreases emotion "
                                         "recognition")
        assert "[2]" not in entry["title"] and "Balter" not in entry["title"]
        assert entry["confidence"] == "high", "a cleaned title with a DOI is checkable"

    def test_no_doi_or_a_fragment_title_is_never_high_confidence(self):
        assert self._entry(self._CITATION)["confidence"] == "low", "no DOI"
        fragment = self._entry(self._FRAGMENT)
        assert fragment["confidence"] == "low"
        assert fragment["doi"] == ""

    def test_settlement_does_not_advertise_a_low_link_as_a_high_match(self):
        """original_match_confidence is settled after the guard and --resolved-only
        from the link METHOD; a resolved method on a record nobody can check is still
        not a high-confidence match."""
        link = {"targets": [{"match_certain": True, "target_as_named": "T",
                             "study_numbers": "", "replication_study_numbers": "",
                             "evidence_quote": "q",
                             "record": {"doi": "10.2/orig", "title": self._FRAGMENT,
                                        "first_author": "Moieni", "year": 2015}}],
                "target_stage": "llm_fulltext", "unidentified_count": 0,
                "llm_model": "m", "pdf_source": "", "parse_method": "", "pdf_ok": False}
        row = pd.Series({"doi_r": "10.1/repl", "title_r": "R",
                         "filter_status": "replication"})
        with patch("extract.run_extract._build_ref_o", return_value=("", "", "")), \
             patch.object(run_extract, "_has_document", return_value=False), \
             patch.object(run_extract, "_get_outcome", return_value={}), \
             patch.object(run_extract, "_verify_row", side_effect=lambda r: r):
            rows = run_extract._per_target_rows(row, "10.1/repl", link, None,
                                                no_llm=True, no_pdf=True,
                                                resolved_only=False,
                                                recalibrate_outcomes=False)
        assert len(rows) == 1
        assert rows[0]["link_confidence"] == "low"
        assert rows[0]["original_match_confidence"] == "low"

    def test_a_fragment_title_with_no_doi_is_pending(self):
        """The guard's usable-title rule has to catch citation fragments: "[3] M.
        Moieni, M.R" is long enough to clear the length threshold on its own."""
        row = {"doi_r": "10.1/repl", "title_r": "A Study of Things", "doi_o": "",
               "title_o": self._FRAGMENT, "link_method": "llm_fulltext",
               "link_confidence": "high", "pair_id": "p",
               "doi_o_verification": "verified"}
        with patch("extract.run_extract._search_crossref_by_title") as crossref, \
             patch("extract.run_extract._search_openalex_by_title") as openalex:
            out = run_extract._guard_original_link(row)
        assert out["link_method"] == "target_pending"
        assert out["link_confidence"] == "low"
        assert out["title_o"] == ""
        assert not crossref.called and not openalex.called, \
            "a fragment must not be sent to a title search either"


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


# ── Title-search provenance is visible in link_method (fix 2) ────────────────

class TestTitleSearchProvenance:
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

    def test_map_method_passthrough_matches_the_enum(self):
        from shared.schema import LINK_METHOD_VALUES
        for value in LINK_METHOD_VALUES:
            assert run_extract._map_method(value) == value

    def test_the_reference_screen_resolves(self):
        """csv_to_db filters DB imports on RESOLVED_LINK_METHODS, so an omission
        silently drops resolved rows — llm_references, 25% of extracted-test.csv,
        was dropped exactly this way."""
        assert "llm_references" in RESOLVED_LINK_METHODS


# ── Decision-model attribution and the two-provider requirement ──────────────

class TestClassifyModelAttribution:
    """Routing is the one decision with no attribution otherwise: the match-type
    classifier's model was computed and thrown away."""

    _FILTER_ROW = pd.Series({"doi_r": "10.1/rep", "title_r": "Rep",
                             "filter_status": "replication"})

    _LINK = {"resolution_method": "llm_fulltext", "resolved_doi_o": "10.1/orig",
             "resolved_title_o": "Original", "resolved_year_o": 2000,
             "resolved_author_o": "Smith", "resolution_score": 1.0,
             "llm_confidence": "high", "llm_model": "gemini-link"}
    _ORIG = {"rank": 1, "doi": "10.1/o", "title": "O", "first_author": "A",
             "year": 2001, "confidence": "high"}

    def _merge_row(self):
        with patch("extract.run_extract._build_ref_o", return_value=("ref", "auth", "bib")):
            return _merge_row(self._FILTER_ROW, self._LINK, _MOCK_OUTCOME,
                              "single_original", "high", 1, 1, "gemini-heavy")

    def _merge_multi_row(self):
        with patch("extract.run_extract._build_ref_o", return_value=("", "", "")):
            return _merge_multi_row(self._FILTER_ROW, self._ORIG, _MOCK_OUTCOME,
                                    "multiple_original", "high", 2,
                                    classify_model="gemini-heavy")

    def _empty_row(self):
        return run_extract._empty_row(self._FILTER_ROW, "single_original", "low",
                                      link_method="target_pending",
                                      classify_model="gemini-heavy")

    @pytest.mark.parametrize("builder", ["_merge_row", "_merge_multi_row", "_empty_row"])
    def test_the_classifier_model_is_persisted(self, builder):
        assert getattr(self, builder)()["classify_llm_model"] == "gemini-heavy"

    @pytest.mark.parametrize("builder", ["_merge_row", "_merge_multi_row"])
    def test_the_outcome_model_is_persisted(self, builder):
        assert getattr(self, builder)()["outcome_llm_model"] == "gemini-outcome"

    def test_apply_outcome_persists_the_outcome_model(self):
        """The post-gate writer is the one that runs on every coded row — a column
        filled only by _merge_row would be blank on exactly the rows that got coded."""
        row = run_extract._apply_outcome({}, _MOCK_OUTCOME)
        assert row["outcome_llm_model"] == "gemini-outcome"
        assert row["outcome"] == "success"

    def test_the_link_model_is_the_link_stages_own(self):
        """The outcome step fails over independently of the link step, so the two
        models can differ inside one run — that is the whole point of the column."""
        row = self._merge_row()
        assert row["link_llm_model"] == "gemini-link"
        assert row["outcome_llm_model"] == "gemini-outcome"


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

    @pytest.mark.parametrize("filter_status", ["replication", "reproduction"])
    def test_both_paths_carry_stage_2s_type(self, filter_status):
        """_merge_row already honours filter_status; the multi path must not disagree."""
        link = {"resolution_method": "llm_fulltext", "resolved_doi_o": "10.1/orig",
                "resolved_title_o": "Original", "resolved_year_o": 2000,
                "resolved_author_o": "Smith", "resolution_score": 1.0,
                "llm_confidence": "high"}
        with patch("extract.run_extract._build_ref_o", return_value=("r", "a", "b")):
            single = _merge_row(pd.Series({"doi_r": "10.1/rep", "title_r": "Rep",
                                           "filter_status": filter_status}),
                                link, _MOCK_OUTCOME, "single_original", "high", 1, 1)
        assert self._row(filter_status)["type"] == filter_status
        assert single["type"] == filter_status


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

    def test_set_aside_keys_union_both_files_and_key_doi_less_rows(self, tmp_path):
        """The set of keys itself: both files are read, and a row with no DOI keys on
        the next identifier in the chain rather than collapsing to the empty key."""
        self._setup(tmp_path)
        self._write(tmp_path / "not_a_replication.csv",
                    [{"doi_r": "10.1/nar", "link_method": "not_a_replication"},
                     {"doi_r": "", "openalex_id_r": "W7", "title_r": "No DOI",
                      "link_method": "not_a_replication"}])
        assert run_extract._screen_set_aside_keys(tmp_path) == {
            "10.1/nar", "oa:W7", "10.1/dis"}


class TestScreenProviderPrecheck:
    """The front-door screen needs two providers to have anything to weigh against
    each other, so a run configured with one must fail at startup, not 2,000 rows
    in. Which second key it needs follows SCREEN_VOTER2_MODEL."""

    @pytest.mark.parametrize("voter2,missing", [
        ("gpt-5.4-mini",                  "OPENAI_API_KEY"),
        ("mistralai/ministral-14b-2512",  "OPENROUTER_API_KEY"),
    ])
    def test_the_second_voters_own_key_is_required(self, monkeypatch, voter2, missing):
        monkeypatch.setattr(llm_client, "SCREEN_VOTER2_MODEL", voter2)
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["k"])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "k")
        monkeypatch.setattr(run_extract, "OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(run_extract, missing, "")
        with pytest.raises(RuntimeError, match=missing):
            run_extract._check_screen_providers(no_llm=False)

    def test_missing_gemini_key_raises(self, monkeypatch):
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", [])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "k")
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            run_extract._check_screen_providers(no_llm=False)

    def test_a_configured_run_passes_and_no_llm_skips_the_check(self, monkeypatch):
        monkeypatch.setattr(llm_client, "SCREEN_VOTER2_MODEL", "gpt-5.4-mini")
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["k"])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "k")
        run_extract._check_screen_providers(no_llm=False)
        # --no-llm makes no screen call at all, so no key is needed.
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

        Returns (result_df, ladder_mock, outcome_mock) so a test can assert that the
        heavy calls after the front door were never made.
        """
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["test-key"])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "test-key")
        (tmp_path / "filtered.csv").write_text(filtered_csv or _FILTERED_CSV,
                                               encoding="utf-8-sig")
        m_link  = MagicMock(return_value=_MOCK_LINK)
        m_out   = MagicMock(return_value=_MOCK_OUTCOME)
        with patch.object(run_extract, "classify_replication", return_value=screen), \
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
        return result, m_link, m_out

    @pytest.mark.parametrize("confident", [True, False])
    def test_agreed_none_is_written_without_any_further_call(self, tmp_path, monkeypatch,
                                                             confident):
        """G-softqual discards two "none" votes at any confidence."""
        result, m_link, m_out = self._run(
            _screen(screen_verdict="discard", screen_classification="none",
                    record_type="", categories=["terminology_only"],
                    llm_reasoning="gemini: unrelated",
                    votes=[_vote("gemini", "none", confident=confident),
                           _vote("openai", "none", confident=confident)]),
            tmp_path, monkeypatch)

        assert list(result["link_method"]) == ["not_a_replication"]
        assert list(result["outcome"]) == ["not_a_replication"]
        label = "confident" if confident else "unconfident"
        assert f"gemini=none/{label}" in result.iloc[0]["link_evidence"]
        assert result.iloc[0]["screen_categories"] == "terminology_only"
        m_link.assert_not_called()
        m_out.assert_not_called()

    @pytest.mark.parametrize("partner", ["unclear", "replication"])
    def test_confident_none_plus_an_unconfident_partner_is_a_discard(
            self, tmp_path, monkeypatch, partner):
        """The softqual clause: an answer the other voter would not stake anything
        on does not outweigh a confident none."""
        result, m_link, m_out = self._run(
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
        result, m_link, _ = self._run(
            _screen(screen_verdict="proceed", screen_classification="replication",
                    record_type="replication",
                    votes=[_vote("gemini", "replication"), _vote("openai", "none")]),
            tmp_path, monkeypatch)

        assert "screen_disagreement" not in set(result["link_method"])
        assert result.iloc[0]["link_method"] == "same_author_year_title_overlap"
        m_link.assert_called_once()

    def test_one_vote_is_target_pending_not_a_verdict(self, tmp_path, monkeypatch):
        result, m_link, m_out = self._run(
            _screen(resolution_method="llm_refscreen_partial", screen_verdict="",
                    record_type="", llm_error="classifier failed: openai"),
            tmp_path, monkeypatch)

        assert list(result["link_method"]) == ["target_pending"]
        m_link.assert_not_called()
        m_out.assert_not_called()

    def test_no_votes_is_an_api_error(self, tmp_path, monkeypatch):
        result, m_link, _ = self._run(
            _screen(resolution_method="llm_refscreen_failed", screen_verdict="",
                    record_type="", llm_error="classifier failed: gemini, openai"),
            tmp_path, monkeypatch)

        assert list(result["link_method"]) == ["api_error"]
        assert list(result["outcome"]) == ["api_error"]
        m_link.assert_not_called()

    def test_the_verdict_is_threaded_into_the_ladder_not_re_voted(self, tmp_path, monkeypatch):
        _, m_link, _ = self._run(_YES_SCREEN, tmp_path, monkeypatch)

        assert m_link.call_args[1]["classification"] == _YES_SCREEN

    def test_a_proceed_without_a_qualifying_vote_invents_no_type(self, tmp_path, monkeypatch):
        """A needs_review row the gate proceeds on without any qualifying vote
        (unclear/unclear) still resolves an original and is still outcome-coded, but
        nothing has said what kind of paper it is. Writing "replication" there would
        be a guess presented as a reading, so the type stays empty, the row stays
        needs_review, and csv_to_db leaves it for the check page."""
        needs_review_csv = _FILTERED_CSV.replace(
            "replication,rule_based,direct replication,high",
            "needs_review,rule_based,phrase without a cite,medium")
        result, m_link, m_out = self._run(
            _screen(record_type="", screen_classification="unclear", categories=[],
                    votes=[_vote("gemini", "unclear", confident=False),
                           _vote("openai", "unclear", confident=False)]),
            tmp_path, monkeypatch, filtered_csv=needs_review_csv)

        row = result.iloc[0]
        m_link.assert_called_once()
        assert row["link_method"] == "same_author_year_title_overlap"
        assert row["type"] == ""
        assert row["filter_status"] == "needs_review"
        assert row["filter_method"] == "rule_based"
        # Coded on the replication vocabulary, the more general of the two grids.
        assert m_out.call_args[1]["record_type"] == ""
        # It waits for a human rather than importing — that is the point.
        statuses, _ = _import_mask()
        assert row["filter_status"] not in statuses

    def test_a_proceed_without_a_qualifying_vote_keeps_stage_2s_type(
            self, tmp_path, monkeypatch):
        """Stage 2 already said reproduction and no screen call overrode it. That is
        a decided type, not an invented one, so it stands — and with it the
        computation/robustness vocabulary the outcome call has to use."""
        repro_csv = _FILTERED_CSV.replace(
            "replication,rule_based,direct replication,high",
            "reproduction,rule_based,re-analysis of the original data,high")
        result, _, m_out = self._run(
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
        result, _, m_out = self._run(
            _screen(record_type="reproduction", screen_classification="reproduction"),
            tmp_path, monkeypatch)

        assert m_out.call_args[1]["record_type"] == "reproduction"
        assert result.iloc[0]["type"] == "reproduction"
        assert result.iloc[0]["filter_status"] == "reproduction"
        assert result.iloc[0]["filter_method"] == "screen"
        assert result.iloc[0]["screen_categories"] == "clearly_declared|context_transfer"


class TestDailyTokenBudgetStops:
    """The daily budget is a spend ceiling, not a row error. When it runs out the
    run stops the way Ctrl-C does: the rows already written stay on disk, and the
    row that could not be screened is not written as an examined api_error."""

    _TWO_ROWS = _FILTERED_CSV + (
        "10.1000/rep2,Second Paper,Abstract text,2021,Smith,J. Psych,,W3,openalex,"
        "replication,rule_based,direct replication,high\n")

    def test_the_run_stops_and_keeps_the_rows_written_before_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_extract, "GEMINI_API_KEYS", ["test-key"])
        monkeypatch.setattr(run_extract, "OPENAI_API_KEY", "test-key")
        (tmp_path / "filtered.csv").write_text(self._TWO_ROWS, encoding="utf-8-sig")
        screen = MagicMock(side_effect=[_YES_SCREEN,
                                        TokenBudgetExhausted("budget spent")])

        with patch.object(run_extract, "classify_replication", screen), \
             patch.object(run_extract, "run_for_doi", return_value=_MOCK_LINK), \
             patch.object(run_extract, "extract_outcome", return_value=_MOCK_OUTCOME), \
             patch.object(run_extract, "verify_and_correct",
                          side_effect=lambda doi, *a, **k: {
                              "doi_o": doi, "doi_o_verification": "skipped",
                              "evidence_note": ""}), \
             patch.object(run_extract, "_oa_by_doi", return_value=None), \
             patch.object(run_extract, "DATA_DIR", tmp_path), \
             patch.object(run_extract, "BASE_DIR", tmp_path):
            with pytest.raises(TokenBudgetExhausted):
                run_extract.run_extract()

        written = pd.read_csv(tmp_path / "extracted.csv", dtype=str, encoding="utf-8-sig")
        assert list(written["doi_r"]) == ["10.1000/rep"]
        assert "api_error" not in set(written["link_method"])


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
            rows = run_extract._resolve_and_code(
                "10.1/rep", row, screen=None,
                no_llm=False, no_pdf=True, resolved_only=False,
                recalibrate_outcomes=False)

        assert len(rows) == 1
        assert rows[0]["link_method"] == "target_pending"
        assert rows[0]["outcome"] == "pending"

    def test_resolved_only_drops_the_row_before_the_outcome_call(self):
        row = pd.Series({"doi_r": "10.1/rep", "title_r": "T", "abstract_r": "a",
                         "filter_status": "replication"})
        pending = {"resolution_method": "no_fulltext_available", "resolved_doi_o": "",
                   "resolved_title_o": "", "resolved_year_o": None,
                   "resolved_author_o": "", "resolution_score": 0.0}
        with patch.object(run_extract, "run_for_doi", return_value=pending), \
             patch.object(run_extract, "extract_outcome",
                          side_effect=AssertionError("must not code an outcome")):
            rows = run_extract._resolve_and_code(
                "10.1/rep", row, screen=None,
                no_llm=False, no_pdf=True, resolved_only=True,
                recalibrate_outcomes=False)

        assert rows == []


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

    def test_pdf_cached_by_an_earlier_run_counts(self, tmp_path, monkeypatch):
        """--recalibrate-outcomes re-reads documents a previous run downloaded."""
        monkeypatch.setattr(run_extract, "PDF_CACHE_DIR", tmp_path)
        monkeypatch.setattr(run_extract, "OA_XML_CACHE_DIR", tmp_path)
        from shared.utils import cache_key
        (tmp_path / f"{cache_key('10.1/x')}.pdf").write_bytes(b"%PDF")
        assert run_extract._has_document("10.1/x", self._link())


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


# ── FLoRA's coding level: one row per pair of REFERENCES ─────────────────────

class TestSamePaperStudiesCollapse:
    """FLoRA's coding level: one row per pair of REFERENCES.

    Several studies replicated from the same original paper are one entry with their
    numbers in study_o; several original papers are several entries. Before this, every
    targeted study became its own row — and rows sharing a doi_o also shared a pair_id,
    which is the key every other system joins on.
    """

    @staticmethod
    def _orig(rank, doi, study_number="", outcome="success", title="T", conf="high"):
        return {"rank": rank, "doi": doi, "title": title, "first_author": "Smith",
                "year": 2010, "study_number": study_number, "outcome": outcome,
                "evidence": f"ev{rank}", "outcome_evidence": f"oev{rank}",
                "confidence": conf}

    def test_same_doi_collapses_and_joins_study_numbers(self):
        out = run_extract._collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1"),
            self._orig(2, "10.1000/a", "2"),
            self._orig(3, "10.1000/b", ""),
        ])
        assert len(out) == 2
        assert out[0]["study_number"] == "1, 2"
        assert [o["rank"] for o in out] == [1, 2]
        assert out[1]["doi"] == "10.1000/b"

    def test_distinct_papers_are_not_collapsed(self):
        out = run_extract._collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1"),
            self._orig(2, "10.1000/b", "1"),
        ])
        assert len(out) == 2
        assert [o["study_number"] for o in out] == ["1", "1"]

    def test_conflicting_outcomes_aggregate_to_mixed(self):
        out = run_extract._collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1", outcome="success"),
            self._orig(2, "10.1000/a", "2", outcome="failure"),
        ])
        assert out[0]["outcome"] == "mixed"

    def test_silent_study_does_not_outvote_a_verdict(self):
        out = run_extract._collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1", outcome="failure"),
            self._orig(2, "10.1000/a", "2", outcome="cannot_be_determined"),
        ])
        assert out[0]["outcome"] == "failure"

    def test_partial_study_numbers_are_dropped_not_guessed(self):
        """Claiming "1" when a second study went unnumbered would assert the
        replication targeted a study it never named."""
        out = run_extract._collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1"),
            self._orig(2, "10.1000/a", ""),
        ])
        assert out[0]["study_number"] == ""

    def test_collapsed_row_takes_the_weakest_confidence(self):
        out = run_extract._collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1", conf="high"),
            self._orig(2, "10.1000/a", "2", conf="low"),
        ])
        assert out[0]["confidence"] == "low"

    def test_doi_less_entries_group_by_title(self):
        out = run_extract._collapse_same_paper_originals([
            self._orig(1, "", "1", title="The  Same Paper"),
            self._orig(2, "", "2", title="the same paper"),
            self._orig(3, "", "1", title="A Different Paper"),
        ])
        assert len(out) == 2
        assert out[0]["study_number"] == "1, 2"

    def test_doi_less_entries_with_the_same_title_but_different_years_stay_apart(self):
        """Generic titles repeat across the literature — the title alone is not an
        identity, so a DOI-less entry is keyed on year and first author too."""
        a = self._orig(1, "", "1", title="Experiment 1")
        b = self._orig(2, "", "1", title="Experiment 1")
        b["year"] = 1998
        out = run_extract._collapse_same_paper_originals([a, b])
        assert len(out) == 2
        assert [o["rank"] for o in out] == [1, 2]

    def test_doi_less_entries_with_the_same_title_but_different_authors_stay_apart(self):
        a = self._orig(1, "", "1", title="Experiment 1")
        b = self._orig(2, "", "1", title="Experiment 1")
        b["first_author"] = "Jones"
        out = run_extract._collapse_same_paper_originals([a, b])
        assert len(out) == 2

    def test_study_o_reaches_the_row(self):
        row = _merge_multi_row(
            pd.Series({"doi_r": "10.9/r", "title_r": "R", "filter_status": "replication"}),
            self._orig(1, "10.1000/a", "1, 2"),
            {"outcome": "success"}, "multiple_original", "high", 1,
        )
        assert row["study_o"] == "1, 2"

    def test_study_r_is_a_union_over_the_merged_studies(self):
        """study_o says which studies of the ORIGINAL are targeted and needs every
        member to have named one; study_r says which of THIS paper's studies re-test
        it, and a member that named none does not unsay the ones that did."""
        a = self._orig(1, "10.1000/a", "1"); a["study_r"] = "1"
        b = self._orig(2, "10.1000/a", "2"); b["study_r"] = "2"
        out = run_extract._collapse_same_paper_originals([a, b])
        assert out[0]["study_r"] == "1, 2"

    def test_study_r_reaches_the_row(self):
        row = _merge_multi_row(
            pd.Series({"doi_r": "10.9/r", "title_r": "R", "filter_status": "replication"}),
            dict(self._orig(1, "10.1000/a", "1, 2"), study_r="3"),
            {"outcome": "success"}, "multiple_original", "high", 1,
        )
        assert row["study_o"] == "1, 2"
        assert row["study_r"] == "3"

    def test_a_legacy_study_r_title_never_survives_into_the_column(self):
        """The seeded columns used study_r for a TITLE. _base_row blanks it, so only a
        producer that sets a real number can fill it."""
        row = _merge_multi_row(
            pd.Series({"doi_r": "10.9/r", "title_r": "R", "study_r": "A Paper Title",
                       "filter_status": "replication"}),
            self._orig(1, "10.1000/a", "1"),
            {"outcome": "success"}, "single_original", "high", 1,
        )
        assert row["study_r"] == ""


# ── The per-target adapter (WP1) ─────────────────────────────────────────────
# identify_targets_with_llm answers "which originals does this paper re-test?" over
# a keyed namespace. When it names targets without accepting one of them as THE
# link, _resolve_and_code writes one row per confirmed target rather than one link
# for the first and silence for the rest.

class TestPerTargetAdapter:
    _ROW = pd.Series({"doi_r": "10.1/rep", "title_r": "T", "abstract_r": "a",
                      "filter_status": "replication"})

    _SCREEN = {"screen_verdict": "proceed", "screen_classification": "reproduction",
               "record_type": "reproduction",
               "categories": ["clearly_declared", "self_retest"],
               "resolution_method": "llm_refscreen_declined", "votes": []}

    @staticmethod
    def _link(targets, **over):
        base = dict(_MOCK_LINK, resolved=False, resolution_method="llm_multi_target",
                    resolved_doi_o="", resolved_title_o="", multi_target=len(targets) > 1,
                    n_targets=len(targets), target_stage="llm_gemini",
                    unidentified_count=0, targets=targets, llm_model="gemini-heavy")
        base.update(over)
        return base

    def _run(self, targets, outcome=None, screen=None, resolved_only=False, **over):
        link = self._link(targets, **over)
        with patch.object(run_extract, "run_for_doi", return_value=link), \
             patch.object(run_extract, "_has_document", return_value=False), \
             patch.object(run_extract, "extract_outcome",
                          return_value=outcome if outcome is not None
                          else _MOCK_OUTCOME) as coder:
            rows = run_extract._resolve_and_code(
                "10.1/rep", self._ROW, screen=screen, no_llm=False, no_pdf=True,
                resolved_only=resolved_only, recalibrate_outcomes=False)
        return rows, coder

    _TWO = [_mock_target("@smith2009", "10.1/a", "First original", "Smith", 2009),
            _mock_target("@jones2011", "10.1/b", "Second original", "Jones", 2011)]

    def test_the_reroute_consumes_the_targets(self):
        """The brief's named seam: the target list the ladder already produced IS the
        answer, and each of its entries becomes a row."""
        rows, coder = self._run(self._TWO)

        assert [r["doi_o"] for r in rows] == ["10.1/a", "10.1/b"]
        assert [r["title_o"] for r in rows] == ["First original", "Second original"]
        assert all(r["original_match_type"] == "multiple_original" for r in rows)
        assert all(r["n_originals"] == 2 for r in rows)
        assert [r["original_rank"] for r in rows] == [1, 2]
        assert not hasattr(run_extract, "run_multi_original_for_doi")

        assert coder.call_count == 2
        assert [c.kwargs["original_title"] for c in coder.call_args_list] == [
            "First original", "Second original"]
        assert all(c.kwargs["multi_original"] is True for c in coder.call_args_list)

    def test_the_rows_carry_a_legal_outcome_block(self):
        """The retired path wrote out_quote_source="llm_multi" and a three-valued
        confidence, neither of which is in the schema's vocabulary."""
        rows, _ = self._run(self._TWO)

        for r in rows:
            assert r["out_quote_source"] in {"abstract", "title", "fulltext"}
            assert r["outcome_confidence"] in {"high", "low"}
            assert r["link_confidence"] in {"high", "low"}
            assert r["link_method"] == "llm_fulltext"

    def test_a_reproduction_target_carries_the_axis_fields(self):
        repro_outcome = {
            "outcome": "computationally reproducible, robustness challenges",
            "outcome_phrase": "", "outcome_confidence": "high", "out_quote_source": "",
            "outcome_reasoning": "", "llm_model": "gemini-outcome",
            "outcome_computation": "computationally reproducible",
            "outcome_computational_quote": "the code reproduced every table",
            "out_quote_computational_source": "fulltext",
            "outcome_robustness": "robustness challenges",
            "outcome_robustness_quote": "the effect did not survive the alternative spec",
            "out_quote_robust_source": "fulltext",
        }
        rows, _ = self._run(self._TWO, outcome=repro_outcome, screen=self._SCREEN)

        for r in rows:
            assert r["type"] == "reproduction"
            assert r["outcome_computation"] == "computationally reproducible"
            assert r["outcome_robustness"] == "robustness challenges"
            assert r["out_quote_computational_source"] == "fulltext"
            assert r["out_quote_robust_source"] == "fulltext"

    def test_the_guard_runs_before_the_outcome(self):
        """A self-link is demoted to target_pending, and the pipeline's most
        expensive call is never spent on a row that is about to be dropped."""
        targets = [_mock_target("@self", "10.1/rep", "The paper itself", "Self", 2020)]
        with patch.object(run_extract, "run_for_doi",
                          return_value=self._link(targets)), \
             patch.object(run_extract, "_has_document", return_value=False), \
             patch.object(run_extract, "extract_outcome",
                          side_effect=AssertionError("coded a demoted row")) as coder:
            rows = run_extract._resolve_and_code(
                "10.1/rep", self._ROW, screen=None, no_llm=True, no_pdf=True,
                resolved_only=False, recalibrate_outcomes=False)

        coder.assert_not_called()
        assert rows[0]["link_method"] == "target_pending"
        assert rows[0]["doi_o"] == ""
        assert rows[0]["outcome"] == "pending"

    def test_two_targets_on_one_original_collapse_to_one_row(self):
        """FLoRA's coding level is one row per pair of REFERENCES: two studies of the
        same original paper are one row with their numbers joined — and one outcome
        call, not two."""
        targets = [_mock_target("@smith2009", "10.1/a", "First original", "Smith", 2009,
                                study_numbers="Study 1"),
                   _mock_target("@smith2009b", "10.1/a", "First original", "Smith", 2009,
                                study_numbers="Experiment 2")]
        rows, coder = self._run(targets)

        assert len(rows) == 1
        assert rows[0]["study_o"] == "1, 2"
        assert rows[0]["n_originals"] == 1
        assert rows[0]["original_match_type"] == "single_original"
        assert coder.call_count == 1
        assert coder.call_args.kwargs["multi_original"] is False

    def test_study_r_reaches_the_row_per_target(self):
        targets = [_mock_target("@smith2009", "10.1/a", "First", "Smith", 2009,
                                replication_study_numbers="Study 1"),
                   _mock_target("@jones2011", "10.1/b", "Second", "Jones", 2011,
                                replication_study_numbers="Experiment 2, 3")]
        rows, _ = self._run(targets)
        assert [r["study_r"] for r in rows] == ["1", "2, 3"]

    def test_resolved_only_drops_a_demoted_row_and_renumbers_the_rest(self):
        """audit_extracted requires ranks 1..n with n_originals == the group size, so
        the renumbering has to happen after the drops, not before."""
        targets = [_mock_target("@self", "10.1/rep", "The paper itself", "Self", 2020),
                   _mock_target("@jones2011", "10.1/b", "Second original", "Jones", 2011)]
        rows, _ = self._run(targets, resolved_only=True)

        assert len(rows) == 1
        assert rows[0]["doi_o"] == "10.1/b"
        assert rows[0]["original_rank"] == 1
        assert rows[0]["n_originals"] == 1

    def test_a_demoted_target_row_stays_pending_beside_a_kept_sibling(self):
        """The adapter codes the outcome AFTER the guard, so a demoted target has no
        verdict to keep: it must still be written unresolved and pending, next to a
        sibling that was coded — the shape sanity_check routes to target_pending.csv
        and csv_to_db skips, rather than a row with a verdict on no original."""
        targets = [_mock_target("@self", "10.1/rep", "The paper itself", "Self", 2020),
                   _mock_target("@jones2011", "10.1/b", "Second original", "Jones", 2011)]
        rows, coder = self._run(targets)

        assert coder.call_count == 1  # only the surviving target is coded
        demoted, kept = rows
        assert demoted["link_method"] == "target_pending"
        assert demoted["doi_o"] == "" and demoted["title_o"] == ""
        assert demoted["outcome"] == "pending"
        assert demoted["outcome_phrase"] == "" and demoted["out_quote_source"] == ""
        assert demoted["original_match_confidence"] == "low"
        assert kept["doi_o"] == "10.1/b" and kept["outcome"] == _MOCK_OUTCOME["outcome"]
        assert [r["original_rank"] for r in rows] == [1, 2]
        assert all(r["n_originals"] == 2 for r in rows)

    def test_an_unmatched_target_is_reported_not_written(self):
        """A target with no keyed record names a study we cannot identify: there is no
        published record to write a row about, and the shortfall belongs on the rows
        that were written."""
        targets = self._TWO + [{"key": None, "match_certain": False,
                                "target_as_named": "Ramirez (2014)",
                                "study_numbers": "", "replication_study_numbers": "",
                                "evidence_quote": "q", "record": None}]
        rows, _ = self._run(targets, unidentified_count=1)

        assert len(rows) == 2
        assert all("identified 2 of 4 targets" in r["link_evidence"] for r in rows)
        assert all("Ramirez (2014)" in r["link_evidence"] for r in rows)

    def test_no_matchable_target_writes_one_pending_row(self):
        targets = [{"key": None, "match_certain": False, "target_as_named": "A",
                    "study_numbers": "", "replication_study_numbers": "",
                    "evidence_quote": "q", "record": None},
                   {"key": None, "match_certain": False, "target_as_named": "B",
                    "study_numbers": "", "replication_study_numbers": "",
                    "evidence_quote": "q", "record": None}]
        rows, _ = self._run(targets)

        assert len(rows) == 1
        assert rows[0]["link_method"] == "target_pending"
        assert "2 originals" in rows[0]["link_evidence"]

    def test_an_unresolved_rerouted_row_keeps_what_the_screen_decided(self):
        """The screen ran and classified the paper; the ladder finding no original
        does not undo that, and a pending row stripped of its categories and type
        reads as a paper nobody looked at."""
        targets = [{"key": None, "match_certain": False, "target_as_named": "A",
                    "study_numbers": "", "replication_study_numbers": "",
                    "evidence_quote": "q", "record": None},
                   {"key": None, "match_certain": False, "target_as_named": "B",
                    "study_numbers": "", "replication_study_numbers": "",
                    "evidence_quote": "q", "record": None}]
        rows, _ = self._run(targets, screen=self._SCREEN)

        assert rows[0]["screen_categories"] == "clearly_declared|self_retest"
        assert rows[0]["type"] == "reproduction"

    def test_a_resolved_single_link_does_not_reach_the_adapter(self):
        """A ladder that accepted one target already wrote the link; rerouting it
        would rebuild the same row through a second code path."""
        link = dict(_MOCK_LINK, targets=[self._TWO[0]], n_targets=1,
                    resolved_study_r="2")
        with patch.object(run_extract, "run_for_doi", return_value=link), \
             patch.object(run_extract, "_has_document", return_value=False), \
             patch.object(run_extract, "extract_outcome", return_value=_MOCK_OUTCOME), \
             patch.object(run_extract, "verify_and_correct",
                          side_effect=lambda doi, *a, **k: {
                              "doi_o": doi, "doi_o_verification": "skipped",
                              "evidence_note": ""}):
            rows = run_extract._resolve_and_code(
                "10.1/rep", self._ROW, screen=None, no_llm=False, no_pdf=True,
                resolved_only=False, recalibrate_outcomes=False)

        assert len(rows) == 1
        assert rows[0]["link_method"] == "same_author_year_title_overlap"
        assert rows[0]["doi_o"] == "10.1037/h0054651"
        assert rows[0]["study_r"] == "2"          # from the link, not from a target


class TestAdapterRowsSettleAfterTheDrops:
    """Everything that describes the GROUP — the match type, the count, the
    confidence — is only knowable once the guard and --resolved-only have run."""

    _ROW = pd.Series({"doi_r": "10.1/rep", "title_r": "T", "abstract_r": "a",
                      "filter_status": "replication"})

    def _run(self, targets, resolved_only=False, **over):
        link = dict(_MOCK_LINK, resolved=False, resolution_method="llm_multi_target",
                    resolved_doi_o="", resolved_title_o="", multi_target=True,
                    n_targets=len(targets), target_stage="llm_gemini",
                    unidentified_count=0, targets=targets, llm_model="gemini-heavy",
                    **over)
        with patch.object(run_extract, "run_for_doi", return_value=link), \
             patch.object(run_extract, "_has_document", return_value=False), \
             patch.object(run_extract, "extract_outcome", return_value=_MOCK_OUTCOME):
            return run_extract._resolve_and_code(
                "10.1/rep", self._ROW, screen=None, no_llm=False, no_pdf=True,
                resolved_only=resolved_only, recalibrate_outcomes=False)

    def test_a_paper_whose_second_target_is_rejected_is_not_multiple_original(self):
        """The match type is the row count, and the row count is only final after the
        guard: a surviving single row that still says multiple_original overcounts
        every multi-original figure on the dashboard."""
        rows = self._run([
            _mock_target("@self", "10.1/rep", "The paper itself", "Self", 2020),
            _mock_target("@jones2011", "10.1/b", "Second original", "Jones", 2011)])

        assert len(rows) == 2
        survivors = [r for r in rows if r["link_method"] != "target_pending"]
        assert len(survivors) == 1
        # Both rows agree on the group, and the demoted one is not a high-confidence
        # match to an original it no longer has.
        assert {r["original_match_type"] for r in rows} == {"multiple_original"}
        demoted = [r for r in rows if r["link_method"] == "target_pending"][0]
        assert demoted["original_match_confidence"] == "low"
        assert survivors[0]["original_match_confidence"] == "high"

    def test_resolved_only_leaves_a_single_original_paper(self):
        rows = self._run([
            _mock_target("@self", "10.1/rep", "The paper itself", "Self", 2020),
            _mock_target("@jones2011", "10.1/b", "Second original", "Jones", 2011)],
            resolved_only=True)

        assert len(rows) == 1
        assert rows[0]["original_match_type"] == "single_original"
        assert rows[0]["n_originals"] == 1
        assert rows[0]["original_match_confidence"] == "high"

    def test_a_provisional_link_is_never_written_at_high_confidence(self):
        """A DOI the pipeline had to search for is ~50% precise. The single path caps
        it at low through _link_confidence; the adapter must not undo that."""
        target = _mock_target("@smith2009", "", "An unindexed original", "Smith", 2009)
        with patch.object(run_extract, "run_for_doi", return_value=dict(
                _MOCK_LINK, resolved=False, resolution_method="llm_multi_target",
                resolved_doi_o="", resolved_title_o="", multi_target=True, n_targets=1,
                target_stage="llm_gemini", unidentified_count=0, targets=[target])), \
             patch.object(run_extract, "_has_document", return_value=False), \
             patch("shared.doi_verify.resolve_doi_by_metadata",
                   return_value={"doi": "10.9/found"}), \
             patch.object(run_extract, "extract_outcome", return_value=_MOCK_OUTCOME):
            rows = run_extract._resolve_and_code(
                "10.1/rep", self._ROW, screen=None, no_llm=False, no_pdf=True,
                resolved_only=False, recalibrate_outcomes=False)

        assert rows[0]["link_method"] == "llm_title_search"
        assert rows[0]["link_confidence"] == "low"
        assert rows[0]["original_match_confidence"] == "low"   # not a resolved method

    def test_two_targets_that_verify_to_one_doi_become_one_row(self):
        """The collapse groups what the MODEL said; the guard then recovers a DOI for
        a target that had none, so two entries can arrive at one doi_o afterwards —
        and two rows sharing a doi_o share a pair_id."""
        targets = [_mock_target("@a", "", "An unindexed original", "Smith", 2009,
                                study_numbers="1", replication_study_numbers="1"),
                   _mock_target("@b", "", "An unindexed original (reprint)", "Smith",
                                2009, study_numbers="2",
                                replication_study_numbers="2")]
        outcomes = iter([dict(_MOCK_OUTCOME, outcome="success"),
                         dict(_MOCK_OUTCOME, outcome="failure")])
        with patch.object(run_extract, "run_for_doi", return_value=dict(
                _MOCK_LINK, resolved=False, resolution_method="llm_multi_target",
                resolved_doi_o="", resolved_title_o="", multi_target=True, n_targets=2,
                target_stage="llm_gemini", unidentified_count=0, targets=targets)), \
             patch.object(run_extract, "_has_document", return_value=False), \
             patch("shared.doi_verify.resolve_doi_by_metadata", return_value=None), \
             patch.object(run_extract, "_search_crossref_by_title",
                          return_value={"doi": "10.9/same", "openalex_id": ""}), \
             patch.object(run_extract, "extract_outcome",
                          side_effect=lambda *a, **k: next(outcomes)):
            rows = run_extract._resolve_and_code(
                "10.1/rep", self._ROW, screen=None, no_llm=False, no_pdf=True,
                resolved_only=False, recalibrate_outcomes=False)

        assert len(rows) == 1
        assert rows[0]["doi_o"] == "10.9/same"
        assert rows[0]["study_o"] == "1, 2"
        assert rows[0]["study_r"] == "1, 2"
        # Two verdicts about one original are reconciled by FLoRA's rule, not by
        # taking whichever row happened to be first.
        assert rows[0]["outcome"] == "mixed"
        assert rows[0]["n_originals"] == 1


def test_a_rejected_row_states_nothing_about_the_original_it_lost():
    """Every one of these fields describes the original the guard just rejected. A row
    that keeps them reads as a link to a paper the pipeline explicitly refused."""
    row = {"doi_r": "10.1/rep", "title_r": "A Study of Things",
           "doi_o": "10.1/rep", "title_o": "A Study of Things", "study_o": "1",
           "study_r": "2", "year_o": "2009", "authors_o": "Smith, J.",
           "ref_o": "Smith, J. (2009). A Study of Things.",
           "bibtex_ref_o": "@article{Smith_2009,}", "oa_work_id_o": "W123",
           "link_method": "llm_fulltext", "link_confidence": "high"}
    out = run_extract._guard_original_link(row)

    assert out["link_method"] == "target_pending"
    for column in ("doi_o", "title_o", "study_o", "study_r", "year_o", "authors_o",
                   "ref_o", "bibtex_ref_o", "oa_work_id_o"):
        assert out[column] == "", column


def test_study_r_is_not_written_onto_an_unresolved_link():
    """The ladder can carry a target list past an exit that resolved nothing. Those
    study numbers belong to the targets' own rows, not to a row with no original."""
    unresolved = {"resolution_method": "needs_fulltext", "resolved": False,
                  "resolved_doi_o": "", "resolved_title_o": "", "resolved_year_o": None,
                  "resolved_author_o": "", "resolved_study_o": "", "resolved_study_r": "2"}
    with patch("extract.run_extract._build_ref_o", return_value=("", "", "")):
        row = _merge_row(pd.Series({"doi_r": "10.1/rep", "title_r": "T",
                                    "filter_status": "replication"}),
                         unresolved, {}, "single_original", "low", 1, 1)
    assert row["study_r"] == ""


class TestFulltextProvenanceReachesTheRow:
    """A row coded llm_fulltext asserts a model read the paper; pdf_source and
    parse_method are what say WHICH document, on both writer paths. "none" is the
    waterfall's word for a failed attempt and is blank on the row."""

    _ROW = pd.Series({"doi_r": "10.1/rep", "title_r": "T",
                      "filter_status": "replication"})

    def test_the_single_link_path_records_the_tier_and_the_parser(self):
        link = {"resolution_method": "llm_fulltext", "resolved": True,
                "resolved_doi_o": "10.1/orig", "resolved_title_o": "O",
                "resolved_year_o": 2009, "resolved_author_o": "Smith",
                "resolved_study_o": "", "resolved_study_r": "",
                "pdf_source": "unpaywall_pdf", "parse_method": "pdfminer"}
        with patch("extract.run_extract._build_ref_o", return_value=("", "", "")):
            row = _merge_row(self._ROW, link, {}, "single_original", "high", 1, 1)
        assert (row["pdf_source"], row["parse_method"]) == ("unpaywall_pdf", "pdfminer")

    def test_a_row_that_acquired_nothing_carries_no_provenance(self):
        link = {"resolution_method": "no_fulltext_available", "resolved": False,
                "resolved_doi_o": "", "resolved_title_o": "", "resolved_year_o": None,
                "resolved_author_o": "", "resolved_study_o": "",
                "pdf_source": "none", "parse_method": ""}
        with patch("extract.run_extract._build_ref_o", return_value=("", "", "")):
            row = _merge_row(self._ROW, link, {}, "single_original", "low", 1, 1)
        assert (row["pdf_source"], row["parse_method"]) == ("", "")

    def test_every_per_target_row_carries_the_papers_provenance(self):
        """The whole group was read from one document, so one provenance covers it."""
        link = {"targets": [
                    {"match_certain": True, "target_as_named": "O1",
                     "study_numbers": "", "replication_study_numbers": "",
                     "evidence_quote": "q",
                     "record": {"doi": "10.1/o1", "title": "O1",
                                "first_author": "Smith", "year": 2009}},
                    {"match_certain": True, "target_as_named": "O2",
                     "study_numbers": "", "replication_study_numbers": "",
                     "evidence_quote": "q",
                     "record": {"doi": "10.1/o2", "title": "O2",
                                "first_author": "Jones", "year": 2011}}],
                "target_stage": "llm_fulltext", "unidentified_count": 0,
                "llm_model": "m", "pdf_source": "openalex_xml",
                "parse_method": "openalex_xml", "pdf_ok": False}
        with patch("extract.run_extract._build_ref_o", return_value=("", "", "")), \
             patch.object(run_extract, "_has_document", return_value=False), \
             patch.object(run_extract, "_get_outcome", return_value={}), \
             patch.object(run_extract, "_verify_row", side_effect=lambda r: r):
            rows = run_extract._per_target_rows(self._ROW, "10.1/rep", link, None,
                                                no_llm=True, no_pdf=True,
                                                resolved_only=False,
                                                recalibrate_outcomes=False)
        assert len(rows) == 2
        assert all((r["pdf_source"], r["parse_method"])
                   == ("openalex_xml", "openalex_xml") for r in rows)


def test_no_live_reference_to_the_deleted_builders():
    """The prompts and the writer values the pre-redesign multi pipeline owned. Both
    values stay legal on the read side; nothing may write them again."""
    import shared.prompts as prompts
    assert not hasattr(prompts, "build_multi_original_prompt")
    assert not hasattr(prompts, "build_match_type_prompt")
    assert not hasattr(prompts, "OUTCOME_ENUM")
    assert not hasattr(llm_client, "identify_all_originals_with_llm")

    for path in sorted(Path("extract").glob("*.py")):
        assert '"llm_multi"' not in path.read_text(encoding="utf-8"), path

    # medium reached the CSV through the multi writer's confidence pass-through.
    with patch("extract.run_extract._build_ref_o", return_value=("", "", "")):
        row = _merge_multi_row(
            pd.Series({"doi_r": "10.1/r", "title_r": "R", "filter_status": "replication"}),
            {"rank": 1, "doi": "10.1/o", "title": "O", "confidence": "medium"},
            {}, "single_original", "high", 1)
    assert row["link_confidence"] == "low"
