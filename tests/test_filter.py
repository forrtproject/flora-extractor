"""
Smoke tests for the rule-based filter.

LLM filter is exercised separately under ``tests/live/`` (not yet implemented);
no live API call is made by the default test run.
"""

import pandas as pd

from filter.phrase_detection import (
    find_replication_phrase,
    find_replication_phrase_span,
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


def test_find_replication_phrase_span_returns_offsets():
    # Avoids the literal substring "replication of" (checked first in
    # REPLICATION_PHRASES) so the match is deterministically "direct replication".
    text = "Intro sentence. We attempted a direct replication in a new sample of Smith's work (2010)."
    result = find_replication_phrase_span(text)
    assert result is not None
    phrase, start, end = result
    assert phrase == "direct replication"
    assert text[start:end] == "direct replication"


def test_find_replication_phrase_span_none_when_no_phrase():
    assert find_replication_phrase_span("A field experiment on consumer choice.") is None


def test_find_replication_phrase_span_none_for_dna():
    assert find_replication_phrase_span("DNA replication in eukaryotic cells.") is None


def test_find_replication_phrase_still_works_via_wrapper():
    """Existing find_replication_phrase() must keep its current signature/behavior."""
    text = "We replicated the original study by Smith (2010)."
    assert find_replication_phrase(text) == "we replicated"


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


def test_rule_filter_phrase_and_cite_different_sentences_needs_review():
    """Reconstructs the confirmed false-positive pattern (Atwood/Oryx and Crake case):
    a replication-flavored phrase and an unrelated author-year citation appear in
    different sentences, with no topical connection between them."""
    df = pd.DataFrame([_row(
        "Merging facts with fiction: replication of COVID-19 in dystopian fiction",
        "This article discusses cross-species transplantation themes in dystopian "
        "fiction. (Glover, 2009) The article by Jayne Glover discusses ecological "
        "philosophy in the same novel.",
        # Note: deliberately avoids the word "viral" next to "replication" here —
        # that would trip the existing BIOLOGICAL exclusion pattern in
        # exclusion-patterns.yaml (viral/virus/dna/... + replication) and return
        # false_positive before the proximity gate is even reached, which is not
        # what this test is checking.
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "needs_review"
    assert out.loc[0, "filter_confidence"] == "medium"
    assert "no same-sentence cite" in out.loc[0, "filter_evidence"]


def test_rule_filter_same_sentence_still_high_confidence():
    """Regression check: phrase and citation in the same sentence keep working as before."""
    df = pd.DataFrame([_row(
        "A direct replication of the original effect",
        "We attempted a direct replication of Smith (2010). The results held.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "replication"
    assert out.loc[0, "filter_confidence"] == "high"


def test_rule_filter_picks_same_sentence_citation_over_earlier_one():
    """When multiple citations exist, the same-sentence one must be used as sample_cite,
    not simply the first citation found in the whole text."""
    df = pd.DataFrame([_row(
        "A study of engineering education",
        "Jones (1999) discussed unrelated background context. "
        "We attempted a direct replication of Smith (2010) in a new sample.",
    )])
    out = apply_rule_filter(df)
    assert out.loc[0, "filter_status"] == "replication"
    assert "smith" in out.loc[0, "filter_evidence"].lower()


def test_rule_filter_emits_filter_columns():
    df = pd.DataFrame([_row("t", "a"), _row("t2", "a2")])
    out = apply_rule_filter(df)
    for col in ("filter_status", "filter_method", "filter_evidence", "filter_confidence"):
        assert col in out.columns

    df2 = pd.DataFrame(out)
    df2 = df2.reindex(columns=FILTERED_COLS)
    assert list(df2.columns) == FILTERED_COLS
