"""
test_rule_analysis.py — Tests for rule analysis and audit functions.

Tests Phase 1 of Task 4: audit extraction performance and identify improvements.
"""

import pandas as pd
import pytest
from analysis.rule_analysis import (
    audit_extracted_csv,
    analyze_link_method_distribution,
    find_missing_doi_rows,
    analyze_confidence_distribution,
)


def test_audit_extracted_csv_returns_dict():
    """Audit function should return dict with stats."""
    audit = audit_extracted_csv()

    assert isinstance(audit, dict)
    assert "total_rows" in audit
    assert "by_link_method" in audit
    assert "by_link_confidence" in audit
    assert "missing_doi_count" in audit


def test_link_method_distribution():
    """Analyze link method distribution."""
    dist = analyze_link_method_distribution()

    assert isinstance(dist, pd.DataFrame)
    # Should have link_method and count columns
    if len(dist) > 0:
        assert "link_method" in dist.columns
        assert "count" in dist.columns


def test_find_missing_doi_rows():
    """Find rows with missing DOI."""
    missing = find_missing_doi_rows()

    assert isinstance(missing, pd.DataFrame)
    if len(missing) > 0:
        # Should have these columns
        assert "doi_r" in missing.columns
        assert "title_r" in missing.columns


def test_analyze_confidence_distribution():
    """Analyze link confidence distribution."""
    conf = analyze_confidence_distribution()

    assert isinstance(conf, pd.DataFrame)
    if len(conf) > 0:
        assert "link_confidence" in conf.columns
        assert "count" in conf.columns
