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


# ── the prospective-registration guard ───────────────────────────────────────

_REGISTRATION_ROW = {"type": "replication", "doi_r": "10.17605/osf.io/jdh29",
                     "pdf_source": "osf_registration"}
_CODED = {"outcome": "successful", "outcome_confidence": "high",
          "outcome_phrase": "the replication succeeded",
          "out_quote_source": "fulltext", "llm_model": "gemini-outcome"}


def _guarded(row: dict, template: str = "", name: str = "") -> dict:
    """`_apply_outcome` over a coded outcome, with the guard told what the cache holds.

    Both halves read this machine's cache directory, so every test says what its own
    half found there rather than depending on what happens to be on disk.
    """
    with patch.object(run_extract, "osf_registration_template",
                      return_value=template), \
            patch.object(run_extract, "osf_document_name", return_value=name):
        return _apply_outcome(dict(row), dict(_CODED))


def test_a_prospective_registration_form_settles_no_outcome():
    """A pre-data-collection form states a plan; its planned success criteria read as
    results (issue #196). The row is refused rather than coded from one."""
    row = _guarded(_REGISTRATION_ROW,
                   template="Replication Recipe (Brandt et al., 2013): Pre-Registration")
    assert row["outcome"] == "cannot_be_determined"
    assert row["outcome_phrase"] == ""
    assert row["outcome_llm_model"] == ""
    assert "before data collection" in row["outcome_reasoning"]


def test_a_post_completion_registration_is_coded_normally():
    """The Post-Completion form reports a finished study — it is what this tier reads
    an OSF registration FOR, and Open-Ended deposits stay codeable beside it."""
    for template in ("Replication Recipe (Brandt et al., 2013): Post-Completion",
                     "Open-Ended Registration"):
        row = _guarded(_REGISTRATION_ROW, template=template)
        assert row["outcome"] == "successful", template
        assert row["outcome_llm_model"] == "gemini-outcome", template


def test_a_plan_named_osf_file_settles_no_outcome():
    """The second door: a project that deposited only a plan hands the ladder a
    document whose background passages read as results (issue #196, zya9n)."""
    row = _guarded({**_REGISTRATION_ROW, "pdf_source": "osf_files"},
                   name="Extension-Analytic plan for mere ownership_20180628-G.docx")
    assert row["outcome"] == "cannot_be_determined"
    assert row["outcome_phrase"] == ""
    assert "Analytic plan" in row["outcome_reasoning"]


def test_a_manuscript_in_the_same_project_is_coded_normally():
    """A file the project deposited AFTER the study reports results, and the
    registration's own template says nothing about it."""
    row = _guarded({**_REGISTRATION_ROW, "pdf_source": "osf_files"},
                   template="OSF Preregistration", name="manuscript_final.pdf")
    assert row["outcome"] == "successful"


def test_a_rejected_registration_is_not_downgraded_to_a_withheld_outcome():
    """A plan is not a replication whose result is unknown — nothing was re-tested, so
    the record is rejected (issue #196). `record_type_check: "neither"` is what says so,
    and the prospective-registration guard must leave that verdict standing: overwriting
    it with cannot_be_determined would turn a rejection back into a pending study."""
    rejected = normalise_outcome_block(
        {"outcome": "successful", "outcome_phrase": "successful if p < .05",
         "record_type_check": "neither"},
        record_type="replication", has_text=True)
    assert rejected["outcome"] == "not_a_replication"
    assert rejected["outcome_phrase"] == ""

    row = _guarded({**_REGISTRATION_ROW, "pdf_source": "osf_files"},
                   name="Preregistration.pdf")
    row = _apply_outcome(dict(row), dict(rejected))
    assert row["outcome"] == "not_a_replication"


def test_an_unnamed_osf_file_is_coded_normally():
    """The guard refuses on what a name asserts. A file whose name was never recorded
    asserts nothing, so it settles nothing — the row keeps its coded outcome."""
    row = _guarded({**_REGISTRATION_ROW, "pdf_source": "osf_files"}, name="")
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
