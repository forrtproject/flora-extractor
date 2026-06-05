"""
test_analysis_overlap.py — Test suite for Task 2 overlap analysis.
"""

import pandas as pd
import pytest
from pathlib import Path
from analysis.data_loader import load_candidates, load_filtered, load_all_replications


def test_load_candidates_returns_dataframe():
    """Load candidates.csv and verify it has required columns."""
    df = load_candidates()

    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0
    required_cols = {"doi_r", "title_r", "url_r", "authors_r", "year_r", "source"}
    assert required_cols.issubset(
        set(df.columns)
    ), f"Missing: {required_cols - set(df.columns)}"


def test_load_filtered_returns_dataframe():
    """Load filtered.csv and verify it has required columns."""
    df = load_filtered()

    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0
    required_cols = {"doi_r", "title_r", "filter_status", "filter_evidence"}
    assert required_cols.issubset(set(df.columns))


def test_load_all_replications_returns_dataframe():
    """Load all_replications.csv and verify it has required columns."""
    df = load_all_replications()

    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0
    # Check for replication-side columns (not original)
    required_cols = {"doi_r", "study_r", "year_r"}
    assert required_cols.issubset(set(df.columns))


def test_load_candidates_normalizes_doi():
    """Loaded candidates should have normalized DOIs."""
    df = load_candidates()

    # After normalization, DOIs should not have leading/trailing whitespace
    for doi in df["doi_r"].dropna():
        assert isinstance(doi, str)
        # DOI should be lowercase or empty
        assert doi == doi.lower() or doi == ""


# Matching function tests
from analysis.matching import (
    match_by_doi,
    match_by_url,
    match_by_fuzzy_title,
    normalize_url,
)


def test_normalize_url_removes_trailing_slash():
    """URL normalization should strip trailing slashes."""
    assert normalize_url("https://example.com/paper/") == "https://example.com/paper"
    assert normalize_url("https://example.com") == "https://example.com"


def test_match_by_doi_exact():
    """Two rows with matching DOI should match."""
    row1 = {"doi_r": "10.1234/example"}
    row2 = {"doi_o": "10.1234/example"}

    result = match_by_doi(row1, row2)
    assert result == ("doi", 1.0)  # (method, confidence)


def test_match_by_doi_missing():
    """Rows with missing DOI should not match by DOI."""
    row1 = {"doi_r": ""}
    row2 = {"doi_o": "10.1234/example"}

    result = match_by_doi(row1, row2)
    assert result is None


def test_match_by_url_exact():
    """Two rows with matching URL should match."""
    row1 = {"url_r": "https://example.com/paper"}
    row2 = {"url_o": "https://example.com/paper"}

    result = match_by_url(row1, row2)
    assert result == ("url", 1.0)


def test_match_by_fuzzy_title_similar():
    """Similar titles should fuzzy-match above threshold."""
    row1 = {
        "title_r": "The effect of treatment on outcomes in children",
        "year_r": 2020,
        "authors_r": "Smith, J.",
    }
    row2 = {
        "title_o": "The effect of treatment on outcomes in children aged 5-12",
        "year_o": 2020,
        "authors_o": "Smith, J.",
    }

    result = match_by_fuzzy_title(row1, row2)
    assert result is not None
    method, confidence = result
    assert method == "fuzzy_title"
    assert 0.6 <= confidence <= 1.0  # Should be reasonably high


def test_match_by_fuzzy_title_year_mismatch():
    """Different years should not fuzzy-match."""
    row1 = {
        "title_r": "The effect of treatment",
        "year_r": 2020,
        "authors_r": "Smith, J.",
    }
    row2 = {
        "title_o": "The effect of treatment",
        "year_o": 2019,  # Different year
        "authors_o": "Smith, J.",
    }

    result = match_by_fuzzy_title(row1, row2)
    assert result is None  # Year mismatch should disqualify


# Analysis function tests
from analysis.analyses import analyze_recall_gap


def test_analyze_recall_gap_function_exists():
    """Verify analyze_recall_gap function exists and is callable."""
    # Full analysis on real data would timeout - test structure only
    assert callable(analyze_recall_gap)

    # Call with real data (slow but necessary for integration testing)
    # This should be run separately with: TEST_LIVE_ANALYSIS=1 pytest
    import os
    if os.getenv("TEST_LIVE_ANALYSIS"):
        gaps_by_doi, gaps_by_url, gaps_by_fuzzy = analyze_recall_gap()
        assert isinstance(gaps_by_doi, pd.DataFrame)
        assert isinstance(gaps_by_url, pd.DataFrame)
        assert isinstance(gaps_by_fuzzy, pd.DataFrame)
