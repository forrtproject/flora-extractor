"""Wave C: rule diagnostics — what one spec moves, and what already covered it.

The report's `moved` keys read `pile_without -> pile_with`: the difference the
spec MAKES, so a discard rule shows its rows arriving in `discard` from wherever
the rest of the bundle had them.

Run against the synthetic bundle and pool of `tests/engine_bundle.py`. The delta
a rule makes is a property of `diagnose()`, not of any shipped rule, and pinning
it to the shipped bundle meant every deliberate policy change rewrote counts in
tests that were not about policy. The one thing here that IS a fact about the
shipped bundle — that no holdout has been constructed for it — keeps reading
`filter/spec/`.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from filter.engine.diagnostics import diagnose, render_text
from search.snapshot_scan import _POOL_SCHEMA
from tests.engine_bundle import POOL_ROWS, REAL_SPEC_DIR, pool_row, write_bundle

# Two crafted rows for the overlap seam: a phrase in a title that no live rule
# claims but the shadow rule does, and a phrase row a higher-precedence rule
# takes away from syn-replication.
EXTRA_ROWS = [
    pool_row(10, title="Honey bees", abstract="An investigation."),
    pool_row(11, title="A direct replication",
             abstract="A direct replication of the anchoring effect, Smith et al. (2019)."),
]


@pytest.fixture
def pool(tmp_path) -> Path:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    pq.write_table(pa.Table.from_pylist(POOL_ROWS + EXTRA_ROWS, schema=_POOL_SCHEMA),
                   pool_dir / "2024.parquet")
    return pool_dir


@pytest.fixture
def spec_dir(tmp_path) -> Path:
    return write_bundle(tmp_path / "spec")


def test_removing_a_rule_hands_its_rows_back_to_the_next_rule(pool, spec_dir):
    """syn-dataset outranks syn-replication on row 1: without it that row falls
    through to the cheap tier, which is the difference the rule makes."""
    report = diagnose(pool, spec_dir, "syn-dataset")
    assert report["moved"] == {"screen_cheap->discard": 1}
    moved = report["sample"][0]
    assert (moved["from_pile"], moved["to_pile"]) == ("screen_cheap", "discard")
    assert moved["evidence"] == "type=dataset"


def test_removing_a_rule_nothing_else_covers_hands_its_rows_to_pending(pool, spec_dir):
    """A rule with no lower-precedence competitor for its rows leaves them
    unclaimed, not re-routed."""
    assert diagnose(pool, spec_dir, "syn-concept")["moved"] == {"pending->screen_cheap": 1}


def test_overlap_separates_an_exclusive_hit_from_a_covered_one(pool, spec_dir):
    overlap = diagnose(pool, spec_dir, "syn-replication")["overlap"]
    # Rows 4 and 7 are syn-replication's alone; rows 1, 2 and 11 are matched by
    # it and won by a higher-precedence rule.
    assert overlap["matched"] == 5
    assert overlap["exclusive"] == 2
    assert overlap["covered"] == 3
    assert overlap["by_spec"]["syn-cite"] == {"both": 2, "covered_by": 2}
    assert overlap["by_spec"]["syn-dataset"] == {"both": 1, "covered_by": 1}


def test_the_moved_sample_is_stable_under_its_seed(pool, spec_dir):
    ids = [row["work_id"] for row in diagnose(pool, spec_dir, "syn-replication",
                                              seed=5, sample_n=3)["sample"]]
    assert ids == [row["work_id"] for row in diagnose(pool, spec_dir, "syn-replication",
                                                      seed=5, sample_n=3)["sample"]]


def test_an_absent_holdout_is_reported_rather_than_assumed(pool):
    assert not (REAL_SPEC_DIR / "holdout.json").exists(), "update this test: #146-2 landed"
    assert diagnose(pool, REAL_SPEC_DIR, "not-a-report-type")["holdout"] == "not_constructed"


def test_a_discard_rule_reports_its_measurement_and_a_route_rule_does_not(pool, spec_dir):
    discard = diagnose(pool, spec_dir, "syn-dataset")["measurement"]
    assert discard["required"] and discard["measured"]
    assert diagnose(pool, spec_dir, "syn-concept")["measurement"]["required"] is False


def test_a_shadow_spec_moves_nothing_while_still_reporting_its_matches(pool, spec_dir):
    report = diagnose(pool, spec_dir, "syn-shadow")
    assert report["shadow"] is True
    assert report["moved"] == {}
    assert report["overlap"]["matched"] > 0
    assert report["overlap"]["matched"] == report["overlap"]["covered"]


def test_the_rendered_block_names_the_spec_its_moves_and_its_holdout(pool, spec_dir):
    text = render_text(diagnose(pool, spec_dir, "syn-dataset"))
    assert "spec syn-dataset" in text
    assert "screen_cheap->discard" in text
    assert "holdout: not_constructed" in text


def test_diagnosing_an_unknown_spec_is_an_error(pool, spec_dir):
    with pytest.raises(ValueError, match="no spec"):
        diagnose(pool, spec_dir, "no-such-rule")
