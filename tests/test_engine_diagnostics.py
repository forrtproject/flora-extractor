"""Wave C: rule diagnostics — what one spec moves, and what already covered it.

The report's `moved` keys read `pile_without -> pile_with`: the difference the
spec MAKES, so a discard rule shows its rows arriving in `discard` from wherever
the rest of the bundle had them.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from filter.engine.diagnostics import diagnose, render_text
from search.snapshot_scan import _POOL_SCHEMA
from tests.test_engine_store import POOL_ROWS, _row

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"

# Two crafted rows for the overlap seam: a title stem nothing else claims, and a
# title stem a higher-precedence phrase rule takes the row away from.
EXTRA_ROWS = [
    _row(10, title="Replikationsstudie über Bienen", abstract="Eine Untersuchung."),
    _row(11, title="A direct replication",
         abstract="We report a direct replication of the anchoring effect."),
]


@pytest.fixture
def pool(tmp_path) -> Path:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    pq.write_table(pa.Table.from_pylist(POOL_ROWS + EXTRA_ROWS, schema=_POOL_SCHEMA),
                   pool_dir / "2024.parquet")
    return pool_dir


def test_removing_the_dataset_rule_hands_its_row_back_to_the_next_rule(pool):
    report = diagnose(pool, SPEC_DIR, "dataset-type")
    assert report["moved"] == {"screen_cheap->discard": 1}
    moved = report["sample"][0]
    assert (moved["from_pile"], moved["to_pile"]) == ("screen_cheap", "discard")
    assert moved["evidence"] == "type=dataset"


def test_overlap_separates_an_exclusive_hit_from_a_covered_one(pool):
    overlap = diagnose(pool, SPEC_DIR, "title-stem")["overlap"]
    # The German stem row is title-stem's alone; the English phrase rows are
    # matched by title-stem and won by a higher-precedence rule.
    assert overlap["exclusive"] == 1
    assert overlap["covered"] == overlap["matched"] - 1
    assert overlap["by_spec"]["phrase-replication"]["covered_by"] >= 1
    assert overlap["by_spec"]["dataset-type"] == {"both": 1, "covered_by": 1}


def test_the_moved_sample_is_stable_under_its_seed(pool):
    ids = [row["work_id"] for row in diagnose(pool, SPEC_DIR, "title-stem",
                                              seed=5, sample_n=3)["sample"]]
    assert ids == [row["work_id"] for row in diagnose(pool, SPEC_DIR, "title-stem",
                                                      seed=5, sample_n=3)["sample"]]


def test_an_absent_holdout_is_reported_rather_than_assumed(pool):
    assert not (SPEC_DIR / "holdout.json").exists(), "update this test: #146-2 landed"
    assert diagnose(pool, SPEC_DIR, "dataset-type")["holdout"] == "not_constructed"


def test_a_discard_rule_reports_its_measurement_and_a_route_rule_does_not(pool):
    discard = diagnose(pool, SPEC_DIR, "dataset-type")["measurement"]
    assert discard["required"] and discard["measured"]
    assert diagnose(pool, SPEC_DIR, "title-stem")["measurement"]["required"] is False


def test_a_shadow_spec_moves_nothing_while_still_reporting_its_matches(pool):
    report = diagnose(pool, SPEC_DIR, "reproduce-verb-arms")
    assert report["shadow"] is True
    assert report["moved"] == {}
    assert report["overlap"]["matched"] > 0
    assert report["overlap"]["matched"] == report["overlap"]["covered"]


def test_the_rendered_block_names_the_spec_its_moves_and_its_holdout(pool):
    text = render_text(diagnose(pool, SPEC_DIR, "dataset-type"))
    assert "spec dataset-type" in text
    assert "screen_cheap->discard" in text
    assert "holdout: not_constructed" in text


def test_diagnosing_an_unknown_spec_is_an_error(pool):
    with pytest.raises(ValueError, match="no spec"):
        diagnose(pool, SPEC_DIR, "no-such-rule")
