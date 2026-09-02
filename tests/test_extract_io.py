"""
Stage 3 row contracts: the outcome writer's reproduction axes and the reference
builders.

These are seams every extracted row passes through but that the behavioural tests
reach only indirectly. All external calls are mocked.

The CSV writer, the resume/skip decision and the chunked input filters used to be
tested here too. They are gone with the CSV runner: `data/extracted.csv` is written
by `extract/export.py` (tests/test_extract_export.py) and the checkpoint is a verdict
row in the state authority (tests/test_extract_tier.py).
"""
import pandas as pd
import pytest
from unittest.mock import patch

import extract.run_extract as run_extract
from extract.run_extract import _apply_outcome, build_bibtex
from shared.schema import normalise_outcome_block


# ── _apply_outcome on the reproduction grid ──────────────────────────────────

def test_apply_outcome_writes_the_grid_verdict_and_both_axes():
    """The derived join and the two axes it was derived from travel together — a row
    carrying the joined outcome without its axes cannot be checked against the paper."""
    outcome = {
        "outcome": "computational issues, robustness challenges",
        "outcome_confidence": "high", "outcome_phrase": "", "out_quote_source": "",
        "outcome_reasoning": "both axes settled", "llm_model": "gemini-outcome",
        "outcome_computation": "computational issues",
        "outcome_computational_quote": "The coefficient differed.",
        "out_quote_computational_source": "abstract",
        "outcome_robustness": "robustness challenges",
        "outcome_robustness_quote": "Two specifications reversed the sign.",
        "out_quote_robust_source": "abstract",
    }
    row = _apply_outcome({"type": "reproduction"}, outcome)
    assert row["outcome"] == "computational issues, robustness challenges"
    for col in run_extract._OUTCOME_AXIS_COLS:
        assert row[col] == outcome[col], col
    assert row["outcome_llm_model"] == "gemini-outcome"


def test_apply_outcome_blanks_the_axes_on_a_replication():
    """A replication has no axes, and a stale value from a previous coding would read
    as a reproduction verdict on a replication row."""
    row = _apply_outcome(
        {"type": "replication", "outcome_computation": "computational issues"},
        {"outcome": "successful", "outcome_confidence": "high"})
    assert row["outcome"] == "successful"
    for col in run_extract._OUTCOME_AXIS_COLS:
        assert row[col] == "", col


# ── ref_o / bibtex_ref_o: the human-readable citation of the original ────────

class TestReferenceBuilders:
    _META = {"authors": ["Smith, J.", "Jones, A."], "year": 2010,
             "title": "The Original Study", "journal": "J. Psych",
             "volume": "51", "issue": "6", "first_page": "1173",
             "last_page": "1182", "doi": "10.1037/orig"}

    def test_build_bibtex_for_a_journal_article(self):
        entry = build_bibtex(self._META["authors"], "2010", self._META["title"],
                             journal="J. Psych", volume="51", issue="6",
                             first_page="1173", last_page="1182", doi="10.1037/orig")
        assert entry.startswith("@article{Smith_2010, ")
        assert entry.endswith("}")
        assert "title={The Original Study}" in entry
        assert "author={Smith, J. and Jones, A.}" in entry
        assert "journal={J. Psych}" in entry
        assert "volume={51}" in entry and "number={6}" in entry
        assert "pages={1173--1182}" in entry
        assert "doi={10.1037/orig}" in entry
        assert "url={https://doi.org/10.1037/orig}" in entry

    def test_build_bibtex_without_a_journal_is_misc(self):
        entry = build_bibtex([], "", "A Working Paper")
        assert entry.startswith("@misc{Unknown, ")
        assert "title={A Working Paper}" in entry
        assert "journal=" not in entry and "year=" not in entry

    def test_build_ref_o_from_doi_metadata(self):
        with patch.object(run_extract, "_oa_full_meta", return_value=self._META):
            ref, authors, bibtex = run_extract._build_ref_o("10.1037/orig")
        assert ref == ("Smith, J., & Jones, A. (2010). The Original Study. "
                       "J. Psych, 51(6), 1173–1182. https://doi.org/10.1037/orig")
        assert authors == "Smith, J.; Jones, A."
        assert bibtex.startswith("@article{Smith_2010, ")

    def test_build_ref_o_falls_back_to_surname_and_year(self):
        """No metadata anywhere: the row still needs something a reviewer can read,
        and an empty bibtex rather than a fabricated one."""
        with patch.object(run_extract, "_oa_full_meta", return_value=None), \
             patch.object(run_extract, "_search_crossref_by_title", return_value=None), \
             patch.object(run_extract, "_search_openalex_by_title", return_value=None):
            ref, authors, bibtex = run_extract._build_ref_o(
                "", fallback_author="John Smith", fallback_year="1935",
                title_o="An Unindexed Book")
        assert ref == "Smith · 1935"
        assert authors == "Smith"
        assert bibtex == ""


# ── normalise_outcome_block: the reproduction axes ───────────────────────────

def test_a_capitalised_axis_value_is_the_value_it_was_offered():
    """Both axis vocabularies are lower case, and a model that capitalises its own
    sentence still means the value it picked. Coercing "Computationally reproducible"
    to cannot_be_determined throws away a settled verdict the run paid for; case is
    settled here rather than by spending prompt tokens on it."""
    block = normalise_outcome_block(
        {"outcome_computation": " Computationally Reproducible ",
         "outcome_robustness": "ROBUST", "confident": True},
        record_type="reproduction", has_text=True)
    assert block["outcome_computation"] == "computationally reproducible"
    assert block["outcome_robustness"] == "robust"
    assert block["outcome"] == "computationally reproducible, robust"


def test_an_axis_value_outside_the_vocabulary_is_still_unsettled():
    """Lower-casing widens what is recognised, not what is accepted: a value the
    pipeline cannot act on is recorded as unsettled."""
    block = normalise_outcome_block(
        {"outcome_computation": "Mostly Fine", "outcome_robustness": "robust",
         "confident": True},
        record_type="reproduction", has_text=True)
    assert block["outcome_computation"] == "cannot_be_determined"


# ── normalise_outcome_block: the prospective-registration veto ────────────────
# The population this was written for is OSF records admitted on their TITLE, with no
# abstract and no full text, so the veto must fire with has_text=False. That is the
# whole point of reading study_status ungated — see the comment beside it in
# shared/schema.py.

def test_a_prospective_study_status_vetoes_the_outcome_without_any_text():
    block = normalise_outcome_block(
        {"study_status": "prospective", "outcome": "successful", "confident": True},
        record_type="replication", has_text=False)
    assert block["outcome"] == "prospective_registration"
    # A plan that speculates about its expected result still has no result.
    assert block["outcome_phrase"] == ""
    assert block["out_quote_source"] == ""
    assert block["study_status"] == "prospective"


def test_not_a_replication_outranks_prospective():
    """"Does not test this original" is the stronger statement: a record that is both
    a plan and not about the named original belongs with the false positives."""
    block = normalise_outcome_block(
        {"study_status": "prospective", "record_type_check": "neither"},
        record_type="replication", has_text=True)
    assert block["outcome"] == "not_a_replication"


def test_a_completed_study_status_changes_nothing():
    """The veto must be inert on every row that is not a plan — this runs over the
    whole corpus, and a status the model did not answer must never move an outcome."""
    for status in ("completed", "", "unclear", None):
        block = normalise_outcome_block(
            {"study_status": status, "outcome": "successful", "confident": True},
            record_type="replication", has_text=True)
        assert block["outcome"] == "successful", status
