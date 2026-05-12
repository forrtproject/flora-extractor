"""
Tests for shared/openalex_client.py — candidate-matching logic.

All OpenAlex HTTP calls are mocked; no network access required.
Run: python -m pytest tests/test_openalex_client.py -v
"""
from unittest.mock import patch

import pytest

from shared.openalex_client import (
    author_matches,
    extract_author_year_patterns,
    find_all_candidates,
)


# ── extract_author_year_patterns ──────────────────────────────────────────────

class TestExtractAuthorYearPatterns:
    def test_single_paren(self):
        patterns = extract_author_year_patterns("Smith (2020) found that ego depletion exists.")
        assert any(p["surname"] == "smith" and p["year"] == 2020 for p in patterns)

    def test_etal_paren(self):
        patterns = extract_author_year_patterns("Baumeister et al. (1998) showed self-control.")
        assert any(p["surname"] == "baumeister" and p["year"] == 1998 for p in patterns)

    def test_max_year_filters_future(self):
        patterns = extract_author_year_patterns("Smith (2025) tested this", max_year=2022)
        assert not any(p["year"] == 2025 for p in patterns)

    def test_years_before_1900_excluded(self):
        patterns = extract_author_year_patterns("Darwin 1859 described evolution")
        assert not any(p["year"] < 1900 for p in patterns)

    def test_multi_author_pattern(self):
        patterns = extract_author_year_patterns("Jones and Smith (2015) replicated the effect.")
        assert len(patterns) >= 1

    def test_empty_text_returns_empty(self):
        assert extract_author_year_patterns("") == []

    def test_no_citation_returns_empty(self):
        patterns = extract_author_year_patterns("We conducted a study with 100 participants.")
        assert patterns == []


# ── author_matches ────────────────────────────────────────────────────────────

class TestAuthorMatches:
    def test_exact_match(self):
        assert author_matches("smith", ["Smith"]) is True

    def test_case_insensitive(self):
        assert author_matches("SMITH", ["smith"]) is True

    def test_prefix_match_cited_shorter(self):
        # "baum" is a prefix of "baumeister" (≥ min_prefix=3)
        assert author_matches("baum", ["Baumeister"]) is True

    def test_prefix_match_ref_shorter(self):
        # "johnson" starts with "john" (reversed prefix direction)
        assert author_matches("johnson", ["John"]) is True

    def test_no_match(self):
        assert author_matches("jones", ["Smith", "Baumeister"]) is False

    def test_empty_cited_surname(self):
        assert author_matches("", ["Smith"]) is False

    def test_short_prefix_below_min(self):
        # "sm" is shorter than min_prefix=3 → no prefix match
        assert author_matches("sm", ["Smith"]) is False

    def test_near_prefix_one_char_diff(self):
        # "smitt" vs "smith" — differ only at last char → near-prefix match
        assert author_matches("smitt", ["Smith"]) is True


# ── find_all_candidates ───────────────────────────────────────────────────────

_REFS = [
    {
        "id": "https://openalex.org/W111",
        "doi": "https://doi.org/10.1000/smith2010",
        "title": "Smith Study on Cognitive Bias",
        "publication_year": 2010,
        "authorships": [{"author": {"display_name": "John Smith"}}],
    },
    {
        "id": "https://openalex.org/W222",
        "doi": "https://doi.org/10.1000/jones2015",
        "title": "Jones Study on Memory",
        "publication_year": 2015,
        "authorships": [{"author": {"display_name": "Alice Jones"}}],
    },
]


class TestFindAllCandidates:
    def _run(self, tmp_path, abstract, doi_r="10.9999/rep", year_r=2020, oa_id="W999"):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=_REFS):
            return find_all_candidates(doi_r, oa_id, "", abstract, year_r, "")

    def test_correct_candidate_found(self, tmp_path):
        cands = self._run(tmp_path, "We replicated Smith (2010) and found consistent results.")
        dois = [c["doi"] for c in cands]
        assert "10.1000/smith2010" in dois

    def test_year_tolerance_plus_one(self, tmp_path):
        """Pattern citing year 2009 should match a reference with year 2010 (±1 window)."""
        refs = [{
            "id": "https://openalex.org/W333",
            "doi": "https://doi.org/10.1000/smith2010",
            "title": "Smith Study",
            "publication_year": 2010,
            "authorships": [{"author": {"display_name": "John Smith"}}],
        }]
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata", return_value=refs):
            cands = find_all_candidates(
                "10.9999/rep", "W999", "",
                "We replicated Smith (2009) in our study.", 2020, "",
            )
        assert "10.1000/smith2010" in [c["doi"] for c in cands]

    def test_year_tolerance_minus_one(self, tmp_path):
        """Pattern citing year 2011 should match a reference with year 2010 (±1 window)."""
        refs = [{
            "id": "https://openalex.org/W333",
            "doi": "https://doi.org/10.1000/smith2010",
            "title": "Smith Study",
            "publication_year": 2010,
            "authorships": [{"author": {"display_name": "John Smith"}}],
        }]
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata", return_value=refs):
            cands = find_all_candidates(
                "10.9999/rep", "W999", "",
                "We replicated Smith (2011) in our study.", 2020, "",
            )
        assert "10.1000/smith2010" in [c["doi"] for c in cands]

    def test_empty_openalex_id_returns_empty(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path):
            cands = find_all_candidates("10.9999/rep", "", "", "Smith (2010)", 2020, "")
        assert cands == []

    def test_no_citation_pattern_returns_empty(self, tmp_path):
        """When the abstract has no author-year patterns, no candidates are returned."""
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=_REFS):
            cands = find_all_candidates(
                "10.9999/rep", "W999", "",
                "We conducted a study with no cited prior work.", 2020, "",
            )
        assert cands == []

    def test_self_doi_excluded(self, tmp_path):
        """The replication paper's own DOI must not appear in the candidate list."""
        refs_with_self = _REFS + [{
            "id": "https://openalex.org/W444",
            "doi": "https://doi.org/10.9999/rep",
            "title": "This Paper Itself",
            "publication_year": 2010,
            "authorships": [{"author": {"display_name": "John Smith"}}],
        }]
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=refs_with_self):
            cands = find_all_candidates(
                "10.9999/rep", "W999", "",
                "We replicated Smith (2010).", 2020, "",
            )
        assert "10.9999/rep" not in [c["doi"] for c in cands]

    def test_results_cached_second_call_skips_api(self, tmp_path):
        """Second call with the same doi_r must return from cache without calling the API."""
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=_REFS) as mock_fetch:
            find_all_candidates("10.9999/rep", "W999", "", "Smith (2010) studied this.", 2020, "")
            find_all_candidates("10.9999/rep", "W999", "", "Smith (2010) studied this.", 2020, "")
        assert mock_fetch.call_count == 1

    def test_candidate_fields_present(self, tmp_path):
        """Every candidate dict must have the required fields."""
        cands = self._run(tmp_path, "We replicated Smith (2010).")
        required = {"openalex_id", "doi", "title", "year", "first_author",
                    "match_year_exact", "cited_pattern"}
        for c in cands:
            missing = required - set(c.keys())
            assert not missing, f"Candidate missing fields: {missing}"
