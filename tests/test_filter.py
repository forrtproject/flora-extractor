"""
Smoke tests for the rule-based filter, against classify_row() — the function
Stage 2 actually calls per row.
"""

import pandas as pd

from filter.phrase_detection import (
    find_replication_phrase_span,
    is_non_scholarly_context,
    is_reproduction_only,
)
from filter.rule_filter import classify_row
from shared.schema import FILTERED_COLS


def test_phrase_detection_positive():
    text = "We replicated the original study by Smith (2010)."
    assert find_replication_phrase_span(text)[0] == "we replicated"


def test_phrase_detection_excludes_dna():
    text = "DNA replication in eukaryotes via the replication fork machinery."
    assert is_non_scholarly_context(text)
    assert find_replication_phrase_span(text) is None


def test_phrase_detection_excludes_code():
    text = "Replication of the dataset using a public repository pipeline."
    assert is_non_scholarly_context(text)
    assert find_replication_phrase_span(text) is None


def test_qualifier_phrases_match_plurals():
    """``\\b`` after "replication" fails on the "s" of "replications", so every
    singular-only qualifier pattern silently missed the plural for years."""
    for text in ("Two conceptual replications of Smith (2010).",
                 "We report direct replications of the original effect.",
                 "Three independent replications of Jones (2015)."):
        assert find_replication_phrase_span(text) is not None, text


def test_biological_replication_of_word_order_excluded():
    """BIOLOGICAL only caught "<organism> replication"; virology abstracts using
    the "replication of <organism>" order passed the filter."""
    for text in ("The replication of enteroviruses features low fidelity.",
                 "Restriction of Replication of Oncolytic Herpes Simplex Virus."):
        assert is_non_scholarly_context(text), text


def test_data_availability_boilerplate_excluded():
    text = "Data and code to reproduce the results in this paper are on OSF."
    assert is_non_scholarly_context(text)


def test_reproduction_only():
    text = "We report a computational reproduction of Brown's (2018) original analysis."
    assert find_replication_phrase_span(text) is not None
    assert is_reproduction_only(text)


def test_replication_with_other_phrases_not_flagged_reproduction_only():
    text = "A direct replication of Smith (2010); reproducibility of the result was not the main aim."
    # Both replication and reproduction phrases fire → NOT reproduction-only
    assert find_replication_phrase_span(text) is not None
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
    out = classify_row(_row(
        "A direct replication of the original effect",
        "We attempted a direct replication of Smith (2010). The results held.",
    ))
    assert out["filter_status"] == "replication"
    assert out["filter_confidence"] == "high"


def test_rule_filter_reproduction_with_cite():
    out = classify_row(_row(
        "Reproducibility study",
        "We ran a computational reproduction of Brown (2018) and found no support.",
    ))
    assert out["filter_status"] == "reproduction"


def test_rule_filter_date_phrase_not_treated_as_cite():
    """A replication phrase plus only a date range (no real author-year cite)
    must fall to needs_review, not auto-accept via a single_bare false match."""
    out = classify_row(_row(
        "A replication study",
        "We attempted to replicate the original effect. "
        "Data were collected between January 2020 and March 2020.",
    ))
    assert out["filter_status"] == "needs_review"
    assert "cite:" not in out["filter_evidence"]


def test_rule_filter_real_cite_still_accepts():
    """A genuine author-year citation still promotes to a high-confidence accept."""
    out = classify_row(_row(
        "A replication study",
        "We attempted a direct replication of Smith (2010) and the effect held.",
    ))
    assert out["filter_status"] == "replication"
    assert out["filter_confidence"] == "high"
    assert "cite:" in out["filter_evidence"]


def test_rule_filter_phrase_no_cite_needs_review():
    out = classify_row(_row(
        "We replicate prior findings",
        "We replicate prior findings in a different population without naming a target study.",
    ))
    assert out["filter_status"] == "needs_review"
    assert out["filter_confidence"] == "medium"


def test_rule_filter_dna_excluded():
    out = classify_row(_row(
        "DNA replication mechanisms",
        "We study DNA replication forks in cells.",
    ))
    assert out["filter_status"] == "false_positive"
    assert "exclusion:" in out["filter_evidence"]


def test_rule_filter_excludes_figshare_data_doi():
    """#17: figshare DOIs are data records, not articles — reject even if the text
    reads like a replication with a citation."""
    row = _row("A direct replication of Smith (2019)",
               "We report a direct replication of Smith (2019).")
    row["doi_r"] = "10.6084/m9.figshare.4213113.v1"
    out = classify_row(row)
    assert out["filter_status"] == "false_positive"
    assert out["filter_evidence"] == "exclusion:figshare_data_record"


def test_rule_filter_excludes_peer_review_object_doi():
    """#17: a /reviews/ DOI segment marks a peer-review object, never the study."""
    row = _row("A direct replication of Smith (2019)",
               "We report a direct replication of Smith (2019).")
    row["doi_r"] = "10.7287/peerj.10325v0.1/reviews/2"
    out = classify_row(row)
    assert out["filter_status"] == "false_positive"
    assert out["filter_evidence"] == "exclusion:peer_review_object"


def test_rule_filter_normal_doi_unaffected_by_doi_exclusion():
    """A real article DOI with a genuine replication+cite still passes."""
    row = _row("A direct replication of Smith (2019)",
               "We report a direct replication of Smith (2019).")
    row["doi_r"] = "10.1037/xge0000123"
    assert classify_row(row)["filter_status"] == "replication"


def test_rule_filter_exclusion_with_phrase_and_cite_readmitted():
    """#44: an exclusion pattern that misfires on an in-scope computational
    reproduction (phrase + author-year cite both present) is readmitted to
    needs_review for the LLM, not hard-rejected."""
    out = classify_row(_row(
        "Reproducing an analysis",
        "We replicated the code of Smith (2019) exactly and re-ran their analysis.",
    ))
    assert out["filter_status"] == "needs_review"
    assert "phrase+cite present" in out["filter_evidence"]


def test_rule_filter_exclusion_without_cite_still_rejected():
    """An exclusion with no rescuing author-year cite stays a hard false_positive."""
    out = classify_row(_row(
        "Software replication",
        "We replicated the code using a public pipeline, no prior study named.",
    ))
    assert out["filter_status"] == "false_positive"
    assert out["filter_evidence"].startswith("exclusion:")


def test_rule_filter_no_phrase_false_positive():
    out = classify_row(_row(
        "On consumer choice in supermarkets",
        "A field experiment on heuristic decision-making with no replication terminology.",
    ))
    assert out["filter_status"] == "false_positive"


def test_rule_filter_phrase_and_cite_different_sentences_needs_review():
    """Reconstructs the confirmed false-positive pattern (Atwood/Oryx and Crake case):
    a replication-flavored phrase and an unrelated author-year citation appear in
    different sentences, with no topical connection between them."""
    out = classify_row(_row(
        "Merging facts with fiction: replication of COVID-19 in dystopian fiction",
        "This article discusses cross-species transplantation themes in dystopian "
        "fiction. (Glover, 2009) The article by Jayne Glover discusses ecological "
        "philosophy in the same novel.",
        # Note: deliberately avoids the word "viral" next to "replication" here —
        # that would trip the existing BIOLOGICAL exclusion pattern in
        # exclusion-patterns.yaml (viral/virus/dna/... + replication) and return
        # false_positive before the proximity gate is even reached, which is not
        # what this test is checking.
    ))
    assert out["filter_status"] == "needs_review"
    assert out["filter_confidence"] == "medium"
    assert "no same-sentence cite" in out["filter_evidence"]


def test_rule_filter_picks_same_sentence_citation_over_earlier_one():
    """When multiple citations exist, the same-sentence one must be used as sample_cite,
    not simply the first citation found in the whole text."""
    out = classify_row(_row(
        "A study of engineering education",
        "Jones (1999) discussed unrelated background context. "
        "We attempted a direct replication of Smith (2010) in a new sample.",
    ))
    assert out["filter_status"] == "replication"
    assert "smith" in out["filter_evidence"].lower()


def test_rule_filter_row_plus_verdict_fits_the_filtered_schema():
    row = _row("t", "a")
    out = {**row, **classify_row(row)}
    for col in ("filter_status", "filter_method", "filter_evidence", "filter_confidence"):
        assert col in out
    assert list(pd.DataFrame([out]).reindex(columns=FILTERED_COLS).columns) == FILTERED_COLS
