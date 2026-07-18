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
    def test_no_llm_skips_llm_and_returns_cannot_be_determined_when_no_keyword(self):
        """With no_llm=True and no keyword match, returns cannot_be_determined without calling LLM."""
        result = extract_outcome(
            "10.1234/test",
            abstract_r="We conducted a study across multiple labs.",
            fulltext="",
            title_r="Multi-site study",
            no_llm=True,
        )
        assert result["outcome"] == "cannot_be_determined"
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


# ── --extracted-test flag tests ───────────────────────────────────────────────

import pandas as pd
from pathlib import Path
from shared.schema import EXTRACTED_COLS, FILTERED_COLS


def _make_filtered_csv(tmp_path: Path, dois: list) -> Path:
    rows = []
    for i, doi in enumerate(dois):
        row = {col: "" for col in FILTERED_COLS}
        row["doi_r"]         = doi
        row["title_r"]       = f"Title {i}"
        row["filter_status"] = "replication"
        row["year_r"]        = "2020"
        rows.append(row)
    p = tmp_path / "filtered.csv"
    pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
    return p


def _make_extracted_csv(tmp_path: Path, doi: str, link_method: str) -> Path:
    row = {col: "" for col in EXTRACTED_COLS}
    row["doi_r"]         = doi
    row["link_method"]   = link_method
    row["filter_status"] = "replication"
    row["pair_id"]       = "abc"
    p = tmp_path / "extracted.csv"
    pd.DataFrame([row]).to_csv(p, index=False, encoding="utf-8-sig")
    return p


_EMPTY_LINK = {
    "resolution_method": "none", "resolved_doi_o": "",
    "resolved_title_o": "", "resolved_author_o": "",
    "resolved_year_o": "", "llm_confidence": "low",
    "resolution_score": 0, "llm_evidence": "", "llm_model": "",
}
_EMPTY_OUTCOME = {
    "outcome": "uninformative", "outcome_phrase": "",
    "outcome_confidence": "low", "out_quote_source": "",
    "outcome_reasoning": "",
}


class TestExtractedTestFlag:
    def _run_test_extract(self, tmp_path, filtered_dois, main_doi, main_link_method):
        """Helper: sets up CSVs and runs run_extract in test mode with all API calls mocked."""
        import extract.run_extract as rex
        (tmp_path / "llm").mkdir(exist_ok=True)
        _make_filtered_csv(tmp_path, filtered_dois)
        _make_extracted_csv(tmp_path, main_doi, main_link_method)
        test_out = tmp_path / "extracted-test.csv"
        prod_csv = tmp_path / "extracted.csv"
        with patch.object(rex, "DATA_DIR", tmp_path), \
             patch.object(rex, "LLM_CACHE_DIR", tmp_path / "llm"), \
             patch("extract.run_extract.classify_match_type",
                   return_value={"original_match_type": "single_original",
                                 "original_match_confidence": "low"}), \
             patch("extract.run_extract.run_for_doi", return_value=_EMPTY_LINK), \
             patch("extract.run_extract._get_outcome", return_value=_EMPTY_OUTCOME), \
             patch("extract.run_extract._save_parse_cache"):
            rex.run_extract(no_llm=True, no_pdf=True, out_path=test_out)
        return test_out

    def test_skips_resolved_doi_from_main_csv(self, tmp_path):
        """DOI already resolved in extracted.csv must not appear in extracted-test.csv."""
        doi_resolved = "10.1111/resolved"
        doi_new      = "10.2222/new"
        test_path = self._run_test_extract(
            tmp_path, [doi_resolved, doi_new], doi_resolved, "author_year_match"
        )
        assert test_path.exists(), "extracted-test.csv was not created"
        df = pd.read_csv(test_path, dtype=str, encoding="utf-8-sig").fillna("")
        dois_in_test = set(df["doi_r"].tolist())
        assert doi_resolved not in dois_in_test, "resolved DOI should be skipped in test mode"
        assert doi_new in dois_in_test, "new DOI should be processed in test mode"

    def test_processes_target_pending_doi_from_main_csv(self, tmp_path):
        """DOI with target_pending in extracted.csv must be re-processed in test mode."""
        doi_pending = "10.3333/pending"
        test_path = self._run_test_extract(
            tmp_path, [doi_pending], doi_pending, "target_pending"
        )
        assert test_path.exists()
        df = pd.read_csv(test_path, dtype=str, encoding="utf-8-sig").fillna("")
        assert doi_pending in df["doi_r"].tolist(), "target_pending DOI should be re-processed"
