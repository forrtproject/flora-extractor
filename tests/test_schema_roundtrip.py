"""Per-stage schema round-trip over the checked-in sample CSVs.

CLAUDE.md prescribes a validate_csv_columns() check on every stage's output, but
only Stage 2 had one. The samples in misc/ are what a contributor reads to learn a
stage's output shape and what the stage tests load as fixtures, so a column renamed
in schema.py without updating them turns the documented contract into a lie that
nothing catches — data/ is gitignored, so the samples are the only committed
artefacts a test can hold the schema against.

The same argument applies to the VALUES a column may hold. schema.py declares a
value set per categorical column, and until this file read them they declared
nothing: nothing imported them, so a category added to the pipeline and not to the
set, or a set listing a value the pipeline stopped writing, cost nothing. Holding
the samples against them is what makes them a contract rather than a comment.
"""
from pathlib import Path

import pandas as pd
import pytest

from shared.schema import (
    CANDIDATES_COLS, DOI_VERIFICATION_VALUES, ENGINE_EXPORTED_COLS, EXTRACTED_COLS,
    FILTER_CONFIDENCE_VALUES, FILTER_STATUS_VALUES, FILTERED_COLS,
    ORIGINAL_MATCH_TYPE_VALUES, SOURCE_VALUES, TYPE_VALUES, validate_csv_columns,
)

_MISC = Path(__file__).resolve().parents[1] / "misc"

_SAMPLES = [
    pytest.param("candidates", "sample_candidates.csv", CANDIDATES_COLS,
                 id="candidates"),
    pytest.param("filtered", "sample_filtered.csv", FILTERED_COLS, id="filtered"),
    pytest.param("extracted", "sample_extracted.csv", EXTRACTED_COLS,
                 id="extracted"),
]


def _read(filename: str) -> pd.DataFrame:
    return pd.read_csv(_MISC / filename, dtype=str, encoding="utf-8-sig").fillna("")


@pytest.mark.parametrize("stage,filename,cols", _SAMPLES)
def test_sample_csv_carries_every_column_of_its_stage(stage, filename, cols):
    df = _read(filename)

    missing = validate_csv_columns(list(df.columns), stage)
    assert not missing, f"{filename} is missing {stage} columns: {missing}"
    assert list(df.columns)[:len(cols)] == cols, (
        f"{filename} column order has drifted from schema.{stage.upper()}_COLS")


def test_the_filtered_sample_is_the_full_engine_handoff_contract():
    """The file Stage 3 reads is ENGINE_EXPORTED_COLS, not FILTERED_COLS.

    A sample stopping at FILTERED_COLS would be an input Stage 3 refuses at startup
    (`_require_screen_verdicts`), which is the opposite of what a fixture is for.
    """
    assert list(_read("sample_filtered.csv").columns) == ENGINE_EXPORTED_COLS


def test_each_stage_extends_the_previous_one():
    """The stages are additive: Stage 2 appends to Stage 1's columns and Stage 3 to
    Stage 2's. A column inserted mid-list instead of appended silently reorders every
    downstream CSV."""
    assert FILTERED_COLS[:len(CANDIDATES_COLS)] == CANDIDATES_COLS
    assert set(FILTERED_COLS).issubset(set(EXTRACTED_COLS))


# (column, the set schema.py declares for it). Blanks are always allowed: every one
# of these columns is legitimately empty on some row — an unscreened row has no
# `type`, an unverified row no `doi_o_verification`.
_VALUE_SETS = [
    ("sample_candidates.csv", "source", SOURCE_VALUES),
    ("sample_filtered.csv", "source", SOURCE_VALUES),
    ("sample_filtered.csv", "filter_status", FILTER_STATUS_VALUES),
    ("sample_filtered.csv", "filter_confidence", FILTER_CONFIDENCE_VALUES),
    ("sample_extracted.csv", "source", SOURCE_VALUES),
    ("sample_extracted.csv", "filter_status", FILTER_STATUS_VALUES),
    ("sample_extracted.csv", "filter_confidence", FILTER_CONFIDENCE_VALUES),
    ("sample_extracted.csv", "original_match_type", ORIGINAL_MATCH_TYPE_VALUES),
    ("sample_extracted.csv", "doi_o_verification", DOI_VERIFICATION_VALUES),
    ("sample_extracted.csv", "type", TYPE_VALUES),
]


@pytest.mark.parametrize("filename,column,allowed", _VALUE_SETS,
                         ids=[f"{f.split('_')[1][:-4]}-{c}" for f, c, _ in _VALUE_SETS])
def test_sample_categorical_values_are_in_their_declared_set(filename, column, allowed):
    values = {v.strip() for v in _read(filename)[column] if v.strip()}
    assert values <= allowed, (
        f"misc/{filename} column {column!r} holds {sorted(values - allowed)}, which "
        f"schema.py's value set does not declare")
