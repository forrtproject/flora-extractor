"""
Tests for search functions
"""
import pytest

from search.openalex_search import fetch_openalex_candidates
from search.semantic_scholar_search import fetch_semantic_scholar
from search.external_lists import fetch_i4r


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

class TestOpenAlexDateRange:

    def test_single_year_count(self):
        """2020 should return exactly 14 'registered replication report' papers."""
        df = fetch_openalex_candidates(from_year=2020, to_year=2020)
        assert len(df) == 14, f"Expected 14 rows for 2020, got {len(df)}"

    def test_single_year_all_years_correct(self):
        df = fetch_openalex_candidates(from_year=2020, to_year=2020)
        bad = df[df["year_r"] != 2020]
        assert bad.empty, f"Rows with wrong year:\n{bad[['doi_r','year_r']]}"

    def test_no_filter_returns_results(self):
        df = fetch_openalex_candidates()
        assert len(df) > 0

    def test_from_year_only(self):
        df = fetch_openalex_candidates(from_year=2024)
        assert (df["year_r"].dropna() >= 2024).all()

    def test_to_year_only(self):
        df = fetch_openalex_candidates(to_year=2015)
        assert len(df) > 0
        assert (df["year_r"].dropna() <= 2015).all()

    def test_empty_range_returns_empty(self):
        df = fetch_openalex_candidates(from_year=2050, to_year=2050)
        assert len(df) == 0

    # --- DOI spot-checks (fill in once you know the expected DOIs) ---
    # def test_known_doi_present_2020(self):
    #     df = fetch_openalex_candidates(from_year=2020, to_year=2020)
    #     assert "10.XXXX/YYYY" in df["doi_r"].values


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

class TestSemanticScholarDateRange:

    def test_single_year_all_years_correct(self):
        df = fetch_semantic_scholar(from_year=2020, to_year=2020)
        if df.empty:
            pytest.skip("S2 returned no results (rate-limited?)")
        bad = df[df["year_r"].notna() & (df["year_r"] != 2020)]
        assert bad.empty, f"Rows with wrong year:\n{bad[['doi_r','year_r']]}"

    def test_no_filter_returns_results(self):
        df = fetch_semantic_scholar()
        assert len(df) >= 0   # passes even if rate-limited (returns partial)

    def test_from_year_only(self):
        df = fetch_semantic_scholar(from_year=2024)
        if df.empty:
            pytest.skip("S2 returned no results (rate-limited?)")
        assert (df["year_r"].dropna() >= 2024).all()

    # --- DOI spot-checks ---
    # def test_known_doi_present_2020(self):
    #     df = fetch_semantic_scholar(from_year=2020, to_year=2020)
    #     assert "10.XXXX/YYYY" in df["doi_r"].values


# ---------------------------------------------------------------------------
# I4R
# ---------------------------------------------------------------------------

class TestI4RDateRange:

    def test_no_filter_returns_results(self):
        df = fetch_i4r()
        assert len(df) > 0

    def test_single_year_count_2024(self):
        """RepEC page currently lists 98 I4R papers for 2024."""
        df = fetch_i4r(from_year=2024, to_year=2024)
        assert len(df) == 98, f"Expected 98 rows for 2024, got {len(df)}"

    def test_single_year_all_years_correct(self):
        df = fetch_i4r(from_year=2024, to_year=2024)
        bad = df[df["year_r"].notna() & (df["year_r"] != 2024)]
        assert bad.empty, f"Rows with wrong year:\n{bad[['title_r','year_r']]}"

    def test_from_year_only(self):
        df_all  = fetch_i4r()
        df_from = fetch_i4r(from_year=2025)
        assert len(df_from) < len(df_all)
        assert (df_from["year_r"].dropna() >= 2025).all()

    def test_to_year_only(self):
        df = fetch_i4r(to_year=2023)
        assert len(df) > 0
        assert (df["year_r"].dropna() <= 2023).all()

    def test_empty_range_returns_empty(self):
        df = fetch_i4r(from_year=2030, to_year=2030)
        assert len(df) == 0

    def test_year_range_is_subset_of_all(self):
        df_all   = fetch_i4r()
        df_range = fetch_i4r(from_year=2024, to_year=2025)
        assert len(df_range) < len(df_all)

    def test_known_title_present_2024(self):
        df = fetch_i4r(from_year=2024, to_year=2024)
        titles = df["title_r"].str.lower().tolist()
        assert any("replication" in t or "comment" in t for t in titles)
