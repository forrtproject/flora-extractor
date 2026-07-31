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


# ── Multi-original DOI resolution (never trust LLM-emitted DOIs) ──────────────

class TestMultiDoiResolution:
    """identify_all_originals_with_llm must resolve each original's DOI from
    title+author+year via resolve_doi_by_metadata, ignoring any DOI the LLM
    emitted — mirroring the single-original path."""

    def _run_identify(self, tmp_path, llm_raw, candidates=None, resolve_hit=None):
        from shared import llm_client
        with patch.object(llm_client, "LLM_CACHE_DIR", tmp_path), \
             patch.object(llm_client, "call_gemini", return_value=(llm_raw, "")), \
             patch.object(llm_client, "time"), \
             patch("shared.doi_verify.resolve_doi_by_metadata",
                   return_value=resolve_hit) as mock_resolve:
            out = llm_client.identify_all_originals_with_llm(
                "10.9999/rep", "Multi Replication", "abstract",
                candidates or [], {},
            )
        return out, mock_resolve

    def test_hallucinated_doi_ignored_and_replaced(self, tmp_path):
        """LLM emits a DOI; output must use the metadata-resolved DOI instead."""
        llm_raw = {
            "is_false_positive": False, "reasoning": "multi",
            "originals": [{
                "rank": 1, "candidate_number": None,
                "title": "Real Original Study", "first_author_surname": "Doe",
                "year": 2011, "doi": "10.9999/HALLUCINATED",
                "evidence": "e", "confidence": "high", "outcome": "success",
            }],
        }
        out, mock_resolve = self._run_identify(
            tmp_path, llm_raw,
            resolve_hit={"doi": "10.1234/real", "title": "Real Original Study",
                         "year": 2011, "openalex_id": "", "source": "crossref"},
        )
        assert out["originals"][0]["doi"] == "10.1234/real"
        # Resolution used the LLM title/author/year and excluded the replication DOI.
        args, kwargs = mock_resolve.call_args
        assert args[0] == "Real Original Study"
        assert args[1] == "Doe"
        assert kwargs.get("exclude_doi") == "10.9999/rep"

    def test_hallucinated_doi_dropped_when_unresolvable(self, tmp_path):
        """If metadata search finds nothing, the hallucinated DOI is not kept."""
        llm_raw = {
            "is_false_positive": False, "reasoning": "multi",
            "originals": [{
                "rank": 1, "candidate_number": None,
                "title": "Some Study", "first_author_surname": "Roe",
                "year": 2005, "doi": "10.9999/FAKE",
                "evidence": "e", "confidence": "medium", "outcome": "failure",
            }],
        }
        out, _ = self._run_identify(tmp_path, llm_raw, resolve_hit=None)
        assert out["originals"][0]["doi"] == ""

    def test_candidate_doi_used_without_metadata_search(self, tmp_path):
        """When a candidate is selected, its verified OpenAlex DOI is used and
        resolve_doi_by_metadata is not called."""
        candidates = [{
            "doi": "10.5555/cand", "title": "Candidate Title", "year": 2009,
            "first_author": "Cand", "openalex_id": "W1", "all_authors": ["Cand"],
        }]
        llm_raw = {
            "is_false_positive": False, "reasoning": "multi",
            "originals": [{
                "rank": 1, "candidate_number": 1, "title": "",
                "doi": "10.9999/HALLUCINATED", "evidence": "e",
                "confidence": "high", "outcome": "success",
            }],
        }
        out, mock_resolve = self._run_identify(tmp_path, llm_raw, candidates=candidates)
        assert out["originals"][0]["doi"] == "10.5555/cand"
        mock_resolve.assert_not_called()


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
        from extract.run_extract import _collapse_same_paper_originals
        out = _collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1"),
            self._orig(2, "10.1000/a", "2"),
            self._orig(3, "10.1000/b", ""),
        ])
        assert len(out) == 2
        assert out[0]["study_number"] == "1, 2"
        assert [o["rank"] for o in out] == [1, 2]
        assert out[1]["doi"] == "10.1000/b"

    def test_distinct_papers_are_not_collapsed(self):
        from extract.run_extract import _collapse_same_paper_originals
        out = _collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1"),
            self._orig(2, "10.1000/b", "1"),
        ])
        assert len(out) == 2
        assert [o["study_number"] for o in out] == ["1", "1"]

    def test_conflicting_outcomes_aggregate_to_mixed(self):
        from extract.run_extract import _collapse_same_paper_originals
        out = _collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1", outcome="success"),
            self._orig(2, "10.1000/a", "2", outcome="failure"),
        ])
        assert out[0]["outcome"] == "mixed"

    def test_silent_study_does_not_outvote_a_verdict(self):
        from extract.run_extract import _collapse_same_paper_originals
        out = _collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1", outcome="failure"),
            self._orig(2, "10.1000/a", "2", outcome="cannot_be_determined"),
        ])
        assert out[0]["outcome"] == "failure"

    def test_partial_study_numbers_are_dropped_not_guessed(self):
        """Claiming "1" when a second study went unnumbered would assert the
        replication targeted a study it never named."""
        from extract.run_extract import _collapse_same_paper_originals
        out = _collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1"),
            self._orig(2, "10.1000/a", ""),
        ])
        assert out[0]["study_number"] == ""

    def test_collapsed_row_takes_the_weakest_confidence(self):
        from extract.run_extract import _collapse_same_paper_originals
        out = _collapse_same_paper_originals([
            self._orig(1, "10.1000/a", "1", conf="high"),
            self._orig(2, "10.1000/a", "2", conf="low"),
        ])
        assert out[0]["confidence"] == "low"

    def test_doi_less_entries_group_by_title(self):
        from extract.run_extract import _collapse_same_paper_originals
        out = _collapse_same_paper_originals([
            self._orig(1, "", "1", title="The  Same Paper"),
            self._orig(2, "", "2", title="the same paper"),
            self._orig(3, "", "1", title="A Different Paper"),
        ])
        assert len(out) == 2
        assert out[0]["study_number"] == "1, 2"

    def test_study_o_reaches_the_row(self):
        import pandas as pd
        from extract.run_extract import _merge_multi_row
        row = _merge_multi_row(
            pd.Series({"doi_r": "10.9/r", "title_r": "R", "filter_status": "replication"}),
            self._orig(1, "10.1000/a", "1, 2"),
            {"outcome": "success"}, "multiple_original", "high", 1,
        )
        assert row["study_o"] == "1, 2"


class TestStudyNumberCleaning:
    def test_prose_forms_reduce_to_a_number(self):
        from shared.llm_client import _clean_study_number
        assert _clean_study_number("Study 2") == "2"
        assert _clean_study_number("Experiment 3a") == "3a"
        assert _clean_study_number(2) == "2"

    def test_absent_or_unparseable_is_empty(self):
        from shared.llm_client import _clean_study_number
        assert _clean_study_number(None) == ""
        assert _clean_study_number("the main study") == ""
