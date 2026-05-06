"""Tests for --no-llm, --match-type-only, --outcome-only CLI flags."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from extract.code_outcome import extract_outcome
from extract.run_extract import classify_match_type


class TestNoLlmExtractOutcome:
    def test_no_llm_skips_llm_and_returns_uninformative_when_no_keyword(self):
        """With no_llm=True and no keyword match, returns uninformative without calling LLM."""
        result = extract_outcome(
            "10.1234/test",
            abstract_r="We conducted a study across multiple labs.",
            fulltext="",
            title_r="Multi-site study",
            no_llm=True,
        )
        assert result["outcome"] == "uninformative"
        assert result["out_quote_source"] == ""

    def test_no_llm_still_returns_keyword_hit(self):
        """With no_llm=True, keyword matches still work."""
        result = extract_outcome(
            "10.1234/test2",
            abstract_r="We failed to replicate the original finding.",
            fulltext="",
            title_r="Test",
            no_llm=True,
        )
        assert result["outcome"] == "failure"

    def test_no_llm_does_not_call_llm_client(self):
        with patch("extract.code_outcome.call_llm") as mock_llm:
            extract_outcome(
                "10.1234/test3",
                abstract_r="Results were ambiguous and unclear.",
                fulltext="",
                title_r="Study",
                no_llm=True,
            )
        mock_llm.assert_not_called()


class TestNoLlmClassifyMatchType:
    def test_no_llm_returns_rule_result_when_rule_fires(self):
        row = {
            "doi_r": "10.1234/ml",
            "title_r": "Many Labs 5: Investigating the Reproducibility of Influential Results",
            "abstract_r": "",
            "openalex_id_r": "",
            "year_r": "2020",
        }
        result = classify_match_type(row, no_llm=True)
        assert result["original_match_type"] == "multiple_original"

    def test_no_llm_returns_single_original_default_when_no_rule(self):
        row = {
            "doi_r": "10.1234/single",
            "title_r": "Replication of ego depletion",
            "abstract_r": "We replicated the original ego depletion effect.",
            "openalex_id_r": "",
            "year_r": "2019",
        }
        with patch("extract.run_extract._llm_classify_match_type") as mock_llm:
            result = classify_match_type(row, no_llm=True)
        mock_llm.assert_not_called()
        assert result["original_match_type"] == "single_original"
