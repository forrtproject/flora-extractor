"""
Tests for extract/multi_original.py — multi-original pipeline.

All external API calls (OpenAlex, PDF, GROBID, LLM) are mocked.
Run: python -m pytest tests/test_multi_original.py -v
"""
import json
from unittest.mock import patch

import pandas as pd
import pytest

from extract.multi_original import run_multi_original_for_doi


_PDF_NONE = {
    "pdf_url": "", "pdf_source": "none", "pdf_path": "", "pdf_ok": False, "html_text": "",
}
_GROBID_EMPTY = {
    "grobid_status": "not_attempted", "n_refs_parsed": 0, "sections": {},
}

_ORIG_A = {
    "rank": 1, "title": "Study A on Cognitive Bias", "doi": "10.1000/a",
    "first_author": "Smith", "year": 2010,
    "evidence": "Smith et al. (2010)", "confidence": "high",
}
_ORIG_B = {
    "rank": 2, "title": "Study B on Memory Effects", "doi": "10.1000/b",
    "first_author": "Jones", "year": 2012,
    "evidence": "Jones (2012)", "confidence": "medium",
}
_ORIG_C = {
    "rank": 3, "title": "Study C on Social Priming", "doi": "10.1000/c",
    "first_author": "Kim", "year": 2015,
    "evidence": "Kim et al. (2015)", "confidence": "low",
}


def _llm_result(originals, is_fp=False):
    return {
        "is_false_positive": is_fp,
        "n_originals": len(originals),
        "originals": originals,
        "llm_source": "gemini",
        "llm_reasoning": "test reasoning",
    }


def _rep_df(doi_r="10.9999/rep", title="Multi-target Replication",
             abstract="We replicated Smith (2010) and Jones (2012)."):
    return pd.DataFrame([{
        "doi_r": doi_r, "study_r": title, "abstract_r": abstract,
        "year_r": "2020", "url_r": "", "openalex_id_r": "W999",
        "author_year_pattern_r": "",
    }])


def _run(doi_r, llm_result, rep_df=None):
    """Helper: run multi_original_for_doi with all external calls mocked."""
    df = rep_df if rep_df is not None else _rep_df(doi_r)
    with patch("extract.multi_original.find_all_candidates", return_value=[]), \
         patch("extract.multi_original.acquire_pdf", return_value=_PDF_NONE), \
         patch("extract.multi_original.run_grobid", return_value=_GROBID_EMPTY), \
         patch("extract.multi_original.identify_all_originals_with_llm",
               return_value=llm_result):
        return run_multi_original_for_doi(doi_r, df)


# ── Two-original case ─────────────────────────────────────────────────────────

class TestTwoOriginals:
    def test_n_originals_is_two(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A, _ORIG_B]))
        assert result["n_originals"] == 2

    def test_originals_json_has_two_items(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A, _ORIG_B]))
        originals = json.loads(result["originals_json"])
        assert len(originals) == 2

    def test_originals_json_contains_correct_dois(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A, _ORIG_B]))
        originals = json.loads(result["originals_json"])
        dois = [o["doi"] for o in originals]
        assert "10.1000/a" in dois
        assert "10.1000/b" in dois

    def test_ranks_are_sequential(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A, _ORIG_B]))
        originals = json.loads(result["originals_json"])
        ranks = sorted(o["rank"] for o in originals)
        assert ranks == [1, 2]

    def test_is_false_positive_false(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A, _ORIG_B]))
        assert result["is_false_positive"] is False


# ── Three-original case ───────────────────────────────────────────────────────

class TestThreeOriginals:
    def test_n_originals_is_three(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A, _ORIG_B, _ORIG_C]))
        assert result["n_originals"] == 3

    def test_originals_json_has_three_items(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A, _ORIG_B, _ORIG_C]))
        originals = json.loads(result["originals_json"])
        assert len(originals) == 3

    def test_ranks_are_one_two_three(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A, _ORIG_B, _ORIG_C]))
        originals = json.loads(result["originals_json"])
        assert sorted(o["rank"] for o in originals) == [1, 2, 3]


# ── False-positive / partial failure case ────────────────────────────────────

class TestFalsePositiveFlag:
    def test_is_false_positive_set_when_llm_flags(self):
        """When LLM finds only 1 original (false positive), flag must be True."""
        result = _run("10.9999/rep", _llm_result([_ORIG_A], is_fp=True))
        assert result["is_false_positive"] is True

    def test_partial_originals_still_present_in_json(self):
        """Even a false-positive result should have the partial originals in json
        so the caller (run_extract.py) can re-route rather than lose data."""
        result = _run("10.9999/rep", _llm_result([_ORIG_A], is_fp=True))
        originals = json.loads(result["originals_json"])
        assert len(originals) == 1
        assert originals[0]["doi"] == "10.1000/a"

    def test_empty_originals_list(self):
        """LLM returning zero originals — n_originals=0, originals_json='[]'."""
        result = _run("10.9999/rep", _llm_result([], is_fp=True))
        assert result["n_originals"] == 0
        assert json.loads(result["originals_json"]) == []


# ── DOI cleaning + metadata ───────────────────────────────────────────────────

class TestMetadata:
    def test_doi_cleaned_on_input(self):
        """Prefixed DOI must be stripped in the returned dict."""
        result = _run("https://doi.org/10.9999/rep", _llm_result([_ORIG_A]))
        assert result["doi_r"] == "10.9999/rep"

    def test_all_candidates_json_present(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A]))
        assert "all_candidates_json" in result

    def test_pdf_source_in_result(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A]))
        assert result["pdf_source"] == "none"

    def test_grobid_status_in_result(self):
        result = _run("10.9999/rep", _llm_result([_ORIG_A]))
        assert result["grobid_status"] == "not_attempted"
