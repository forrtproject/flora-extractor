"""
Smoke tests for the rule-based filter.

LLM filter is exercised separately under ``tests/live/`` (not yet implemented);
no live API call is made by the default test run.
"""

import pandas as pd

from filter.phrase_detection import (
    find_replication_phrase,
    has_replication_phrase,
    is_non_scholarly_context,
    is_reproduction_only,
)
from filter.rule_filter import apply_rule_filter
from shared.schema import FILTERED_COLS


def test_phrase_detection_positive():
    text = "We replicated the original study by Smith (2010)."
    assert has_replication_phrase(text)
    assert find_replication_phrase(text) == "we replicated"


def test_phrase_detection_excludes_dna():
    text = "DNA replication in eukaryotes via the replication fork machinery."
    assert is_non_scholarly_context(text)
    assert not has_replication_phrase(text)


def test_phrase_detection_excludes_code():
    text = "Replication of the dataset using a public repository pipeline."
    assert is_non_scholarly_context(text)
    assert not has_replication_phrase(text)


def test_reproduction_only():
    text = "We tested the reproducibility of Brown's (2018) original effect."
    assert has_replication_phrase(text)
    assert is_reproduction_only(text)


def test_replication_with_other_phrases_not_flagged_reproduction_only():
    text = "A direct replication of Smith (2010); reproducibility of the result was not the main aim."
    # Both replication and reproduction phrases fire → NOT reproduction-only
    assert has_replication_phrase(text)
    assert not is_reproduction_only(text)


def _row(title: str, abstract: str, year: int = 2020) -> dict:
    return {
        "doi_r": "10.1/test",
        "title_r": title,
        "abstract_r": abstract,
        "year_r": year,
        "authors_r": "X",
        "journal_r": "J",
        "url_r": "",
        "openalex_id_r": "",
        "source": "openalex",
    }


def test_rule_filter_replication_with_cite():
    df = pd.DataFrame([_row(
        "A direct replication of the original effect",
        "We attempted a direct replication of Smith (2010). The results held.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "replication"
    assert out.loc[0, "filter_confidence"] == "high"


def test_rule_filter_reproduction_with_cite():
    df = pd.DataFrame([_row(
        "Reproducibility study",
        "We tested the reproducibility of Brown (2018) and found no support.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "reproduction"


def test_rule_filter_date_phrase_not_treated_as_cite():
    """A replication phrase plus only a date range (no real author-year cite)
    must fall to needs_review, not auto-accept via a single_bare false match."""
    df = pd.DataFrame([_row(
        "A replication study",
        "We attempted to replicate the original effect. "
        "Data were collected between January 2020 and March 2020.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "needs_review"
    assert "cite:" not in out.loc[0, "filter_evidence"]


def test_rule_filter_real_cite_still_accepts():
    """A genuine author-year citation still promotes to a high-confidence accept."""
    df = pd.DataFrame([_row(
        "A replication study",
        "We attempted a direct replication of Smith (2010) and the effect held.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "replication"
    assert out.loc[0, "filter_confidence"] == "high"
    assert "cite:" in out.loc[0, "filter_evidence"]


def test_rule_filter_phrase_no_cite_needs_review():
    df = pd.DataFrame([_row(
        "We replicate prior findings",
        "We replicate prior findings in a different population without naming a target study.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "needs_review"
    assert out.loc[0, "filter_confidence"] == "medium"


def test_rule_filter_dna_excluded():
    df = pd.DataFrame([_row(
        "DNA replication mechanisms",
        "We study DNA replication forks in cells.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "false_positive"
    assert "exclusion:" in out.loc[0, "filter_evidence"]


def test_rule_filter_no_phrase_false_positive():
    df = pd.DataFrame([_row(
        "On consumer choice in supermarkets",
        "A field experiment on heuristic decision-making with no replication terminology.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "false_positive"


def test_rule_filter_emits_filter_columns():
    df = pd.DataFrame([_row("t", "a"), _row("t2", "a2")])
    out = apply_rule_filter(df)
    for col in ("filter_status", "filter_method", "filter_evidence", "filter_confidence"):
        assert col in out.columns

    df2 = pd.DataFrame(out)
    df2 = df2.reindex(columns=FILTERED_COLS)
    assert list(df2.columns) == FILTERED_COLS
