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

from shared.schema import EXTRACTED_COLS
from extract.code_outcome import extract_outcome, _keyword_scan
from extract.run_extract import (
    classify_match_type,
    _llm_classify_match_type,
    _map_method,
    _score_to_confidence,
)


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

    def test_llm_failure_returns_uninformative(self):
        """LLM failure should return uninformative, not crash."""
        with patch("extract.code_outcome.call_llm", return_value=(None, "", "quota | error")):
            result = extract_outcome("10.1234/fail", abstract_r="ambiguous text")
        assert result["outcome"] == "uninformative"
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

    def test_invalid_llm_outcome_normalised(self):
        """LLM returning unexpected outcome value should become uninformative."""
        mock_result = {"outcome": "uncertain", "outcome_phrase": "",
                       "outcome_confidence": "low", "out_quote_source": ""}
        with patch("extract.code_outcome.call_llm", return_value=(mock_result, "gemini-model", "")), \
             patch("extract.code_outcome.time.sleep"):
            result = extract_outcome("10.1234/bad", abstract_r="ambiguous text")
        assert result["outcome"] == "uninformative"


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

    def test_false_positives_pass_through_unchanged(self):
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

        # Both rows in output (false_positive passes through)
        assert len(result) == 2
        doi_set = set(result["doi_r"])
        assert "10.1000/fp" in doi_set
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


# ── Schema integration test ───────────────────────────────────────────────────

def test_sample_extracted_schema():
    """sample_extracted.csv must contain all EXTRACTED_COLS."""
    df = pd.read_csv("misc/sample_extracted.csv", dtype=str,
                     on_bad_lines="skip").fillna("")
    missing = [c for c in EXTRACTED_COLS if c not in df.columns]
    assert not missing, f"Missing columns in sample_extracted.csv: {missing}"
