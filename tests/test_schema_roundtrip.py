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
    CANDIDATES_COLS, EXTRACTED_COLS, FILTERED_COLS, make_pair_id,
    outcome_categories_for, validate_csv_columns,
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


def test_the_outcome_vocabulary_is_selected_by_type():
    """Reproductions are coded on a different grid from replications; a row's type
    must select it, or every reproduction verdict is coerced away."""
    repro = outcome_categories_for("reproduction")
    repl = outcome_categories_for("replication")
    assert "computationally reproducible, robust" in repro
    assert "computationally reproducible, robust" not in repl
    assert "success" in repl and "success" not in repro
    assert "cannot_be_determined" in repro and "cannot_be_determined" in repl


# ── pair_id: frozen for rows the validation DB already holds ─────────────────

def test_doi_pair_hashes_are_frozen():
    """pair_id is the identity key the validation DB already holds, and csv_to_db
    skips pair_ids it has seen. If the hash of a row with a doi_o ever changes,
    every imported record re-imports as a duplicate — so these literals are the
    pre-fallback md5("doi_r|doi_o") values and must never move."""
    assert (make_pair_id("10.1/rep", "10.2/orig")
            == "cdb1325243087bf3f8292ff737cf69cc")
    assert (make_pair_id("10.25669/9kzj-tc3j", "10.1037/0022-3514.51.6.1173")
            == "22e94c46165158b30a740f3e66114c82")
    assert make_pair_id("", "") == "b99834bc19bbad24580b3adfa04fb947"


def test_identifierless_row_keeps_its_legacy_hash():
    """extracted.csv holds 129 single-original rows with a blank doi_o AND a blank
    oa_work_id_o, already keyed on md5("doi_r|") in the validation DB. Nothing may
    re-key them, which is why the single-original writer never passes title_o —
    this literal is the real pair_id of one of those rows."""
    assert (make_pair_id("10.34917/4332616", "", "")
            == "422738f9134f6255828b6088979c7ae3")


@pytest.mark.parametrize("args,equivalent", [
    # doi_o decides; the work id and the title beside it are ignored
    (("10.1/rep", "10.2/orig", "W123", "A Title"), ("10.1/rep", "10.2/orig")),
    # without a doi_o the work id decides, and the title is ignored
    (("10.1/rep", "", "W1", "Some Title"), ("10.1/rep", "", "W1", "Another Title")),
    # the work id's URL form and the title's whitespace are normalised first
    (("10.1/rep", "", "https://openalex.org/W1"), ("10.1/rep", "", "w1")),
    (("10.1/rep", "", "", "  Gender   Advertisements "),
     ("10.1/rep", "", "", "Gender Advertisements")),
    # a non-work OpenAlex id is no identifier at all — the title is used instead
    (("10.1/rep", "", "A5023888391", "T"), ("10.1/rep", "", "", "T")),
])
def test_the_identifier_precedence(args, equivalent):
    assert make_pair_id(*args) == make_pair_id(*equivalent)
