"""
Live Stage 1 search tests — real OpenAlex / Semantic Scholar / RePEc calls.

Guarded by TEST_LIVE_API so a plain `pytest` run never touches the network:

    TEST_LIVE_API=1 python -m pytest tests/live/test_search_live.py

Counts on these sources drift as records are added, so the assertions are lower
bounds and range relations rather than exact totals.
"""
import os

import pytest

from search import openalex_search as oa
from search.external_lists import fetch_i4r
from search.semantic_scholar_search import fetch_semantic_scholar_candidates

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run live API tests",
)


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

class TestOpenAlexDateRange:

    def test_single_year_count(self):
        """2020 had 14 'registered replication report' papers when this was written;
        the index only grows, so assert the floor rather than the exact count."""
        df = oa.fetch_phrase(
            phrase="registered replication report",
            from_year=2020,
            to_year=2020
        )
        assert len(df) >= 14, f"Expected at least 14 rows for 2020, got {len(df)}"

    def test_single_year_all_years_correct(self):
        df = oa.fetch_openalex_candidates(from_year=2020, to_year=2020)
        bad = df[df["year_r"] != 2020]
        assert bad.empty, f"Rows with wrong year:\n{bad[['doi_r','year_r']]}"

    def test_no_filter_returns_results(self):
        df = oa.fetch_openalex_candidates()
        assert len(df) > 0

    def test_from_year_only(self):
        df = oa.fetch_openalex_candidates(from_year=2024)
        assert (df["year_r"].dropna() >= 2024).all()

    def test_to_year_only(self):
        df = oa.fetch_openalex_candidates(to_year=2015)
        assert len(df) > 0
        assert (df["year_r"].dropna() <= 2015).all()

    def test_empty_range_returns_empty(self):
        df = oa.fetch_openalex_candidates(from_year=2050, to_year=2050)
        assert len(df) == 0


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

class TestSemanticScholarDateRange:

    def test_single_year_all_years_correct(self):
        df = fetch_semantic_scholar_candidates(from_year=2020, to_year=2020)
        if df.empty:
            pytest.skip("S2 returned no results (rate-limited?)")
        bad = df[df["year_r"].notna() & (df["year_r"] != 2020)]
        assert bad.empty, f"Rows with wrong year:\n{bad[['doi_r','year_r']]}"

    def test_from_year_only(self):
        df = fetch_semantic_scholar_candidates(from_year=2024)
        if df.empty:
            pytest.skip("S2 returned no results (rate-limited?)")
        assert (df["year_r"].dropna() >= 2024).all()


# ---------------------------------------------------------------------------
# I4R
# ---------------------------------------------------------------------------

class TestI4RDateRange:

    def test_no_filter_returns_results(self):
        df = fetch_i4r()
        assert len(df) > 0

    def test_single_year_count_2024(self):
        """The RePEc page listed 98 I4R papers for 2024 when this was written; it
        only gains entries, so assert the floor."""
        df = fetch_i4r(from_year=2024, to_year=2024)
        assert len(df) >= 98, f"Expected at least 98 rows for 2024, got {len(df)}"

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
