"""
test_apa_resolver.py — Tests for APA reference resolver.

Tests Task 6 implementation: CrossRef API + CSV fallback for missing DOI resolution.
"""

import pandas as pd
import pytest
from analysis.apa_resolver import (
    load_missing_dois,
    format_apa_reference,
    load_fallback_csv,
)


def test_load_missing_dois_returns_dataframe():
    """Should load replications without DOI from filtered.csv + extracted.csv."""
    df = load_missing_dois()

    assert isinstance(df, pd.DataFrame)
    # If data exists, should have replication columns
    if len(df) > 0:
        required_cols = {"title_r", "authors_r", "year_r"}
        assert required_cols.issubset(set(df.columns))


def test_format_apa_reference_basic():
    """Format basic APA reference from CrossRef metadata."""
    metadata = {
        "authors": [
            {"family": "Smith", "given": "B."},
            {"family": "Jones", "given": "A."},
        ],
        "year": 2023,
        "title": "A replication study on X",
        "journal": "Journal of Psychology",
        "volume": 45,
        "issue": 3,
        "pages": "234-245",
    }

    apa = format_apa_reference(metadata)

    assert "Smith" in apa
    assert "2023" in apa
    assert "A replication study on X" in apa
    assert "Journal of Psychology" in apa


def test_format_apa_reference_single_author():
    """Format APA reference with single author."""
    metadata = {
        "authors": [{"family": "Brown", "given": "C."}],
        "year": 2022,
        "title": "Reproducing X findings",
        "journal": "Nature",
    }

    apa = format_apa_reference(metadata)

    assert "Brown" in apa
    assert "2022" in apa


def test_format_apa_reference_no_journal():
    """Format APA reference without journal."""
    metadata = {
        "authors": [{"family": "Green", "given": "D."}],
        "year": 2021,
        "title": "A study on Y",
    }

    apa = format_apa_reference(metadata)

    assert "Green" in apa
    assert "2021" in apa
    assert "A study on Y" in apa


def test_load_fallback_csv_returns_dataframe():
    """Load fallback CSV should return DataFrame (even if empty)."""
    df = load_fallback_csv()

    assert isinstance(df, pd.DataFrame)
    # Should have expected columns if not empty
    if len(df) > 0:
        expected_cols = {"title_r", "authors_r", "year_r"}
        assert expected_cols.issubset(set(df.columns))
