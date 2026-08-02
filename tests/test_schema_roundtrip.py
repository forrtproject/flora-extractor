"""
Per-stage schema round-trip over the checked-in sample CSVs.

CLAUDE.md prescribes a validate_csv_columns() check on every stage's output, but
only Stage 2 had one. The samples in misc/ are what a contributor reads to learn a
stage's output shape and what the stage tests load as fixtures, so a column renamed
in schema.py without updating them turns the documented contract into a lie that
nothing catches — data/ is gitignored, so the samples are the only committed
artefacts a test can hold the schema against.
"""
from pathlib import Path

import pandas as pd
import pytest

from shared.schema import (
    CANDIDATES_COLS, EXTRACTED_COLS, FILTERED_COLS, validate_csv_columns,
)

_MISC = Path(__file__).resolve().parents[1] / "misc"

# The Stage 1 and Stage 2 samples predate the `ref_r` column and are xfailed rather
# than dropped, so the drift stays visible in the suite output instead of being
# silently untested. sample_candidates.csv is additionally unparseable — its rows
# carry 11-12 fields against a 9-field header, so its abstracts' embedded commas
# were never quoted. Both are fixture bugs in misc/, not schema bugs; regenerating
# them is a separate change. strict=True means the markers fail the day they are.
_STALE = pytest.mark.xfail(strict=True, reason="misc/ sample predates ref_r "
                                               "(sample_candidates.csv is also "
                                               "malformed) — regenerate the samples")

_SAMPLES = [
    pytest.param("candidates", "sample_candidates.csv", CANDIDATES_COLS,
                 marks=_STALE, id="candidates"),
    pytest.param("filtered", "sample_filtered.csv", FILTERED_COLS,
                 marks=_STALE, id="filtered"),
    pytest.param("extracted", "sample_extracted.csv", EXTRACTED_COLS,
                 id="extracted"),
]


@pytest.mark.parametrize("stage,filename,cols", _SAMPLES)
def test_sample_csv_carries_every_column_of_its_stage(stage, filename, cols):
    df = pd.read_csv(_MISC / filename, dtype=str, encoding="utf-8-sig", nrows=5)

    missing = validate_csv_columns(list(df.columns), stage)
    assert not missing, f"{filename} is missing {stage} columns: {missing}"
    assert list(df.columns)[:len(cols)] == cols, (
        f"{filename} column order has drifted from schema.{stage.upper()}_COLS")


def test_each_stage_extends_the_previous_one():
    """The stages are additive: Stage 2 appends to Stage 1's columns and Stage 3 to
    Stage 2's. A column inserted mid-list instead of appended silently reorders every
    downstream CSV."""
    assert FILTERED_COLS[:len(CANDIDATES_COLS)] == CANDIDATES_COLS
    assert set(FILTERED_COLS).issubset(set(EXTRACTED_COLS))
