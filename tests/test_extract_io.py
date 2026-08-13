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


# ── the prospective-registration guard ───────────────────────────────────────

_REGISTRATION_ROW = {"type": "replication", "doi_r": "10.17605/osf.io/jdh29",
                     "pdf_source": "osf_registration"}
_CODED = {"outcome": "successful", "outcome_confidence": "high",
          "outcome_phrase": "the replication succeeded",
          "out_quote_source": "fulltext", "llm_model": "gemini-outcome"}


def test_a_prospective_registration_form_settles_no_outcome():
    """A pre-data-collection form states a plan; its planned success criteria read as
    results (issue #196). The row is refused rather than coded from one."""
    with patch.object(run_extract, "osf_registration_template",
                      return_value="Replication Recipe (Brandt et al., 2013): "
                                   "Pre-Registration"):
        row = _apply_outcome(dict(_REGISTRATION_ROW), dict(_CODED))
    assert row["outcome"] == "cannot_be_determined"
    assert row["outcome_phrase"] == ""
    assert row["outcome_llm_model"] == ""
    assert "before data collection" in row["outcome_reasoning"]


def test_a_post_completion_registration_is_coded_normally():
    """The Post-Completion form reports a finished study — it is what this tier reads
    an OSF registration FOR, and Open-Ended deposits stay codeable beside it."""
    for template in ("Replication Recipe (Brandt et al., 2013): Post-Completion",
                     "Open-Ended Registration"):
        with patch.object(run_extract, "osf_registration_template",
                          return_value=template):
            row = _apply_outcome(dict(_REGISTRATION_ROW), dict(_CODED))
        assert row["outcome"] == "successful", template
        assert row["outcome_llm_model"] == "gemini-outcome", template


def test_the_guard_does_not_touch_a_row_coded_from_a_document():
    """It fires on the registration form alone: a row whose outcome came from the
    manuscript in the same project's storage is coded from results."""
    with patch.object(run_extract, "osf_registration_template",
                      return_value="OSF Preregistration"):
        row = _apply_outcome({**_REGISTRATION_ROW, "pdf_source": "osf_files"},
                             dict(_CODED))
    assert row["outcome"] == "successful"


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
