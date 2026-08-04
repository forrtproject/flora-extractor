"""`analysis/rule_report.py` — the applied-rules overview.

Four seams, over the synthetic bundle and pool of `tests/engine_bundle.py`: the
won-vs-matched distinction, the shadow contract (evaluated and counted, never
credited with a win), the difference between "not screened yet" and "screened and
nothing proceeded", and the refusal to open a locked store. Everything the report
computes about the SHIPPED rules is policy, and policy is asserted in
`tests/test_engine_spec.py`, not here.
"""

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis import rule_report
from filter.engine.spec import load_specs
from filter.engine.store import build_routing, open_store
from filter.engine.workids import load_aliases
from search.snapshot_scan import _POOL_SCHEMA
from tests.engine_bundle import POOL_ROWS, write_bundle

RELEASE = "test-release"


@pytest.fixture
def bundle(tmp_path) -> dict:
    """A routed store, its spec dir and its pool — the report's three inputs."""
    spec_dir = write_bundle(tmp_path / "spec")
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    pq.write_table(pa.Table.from_pylist(POOL_ROWS, schema=_POOL_SCHEMA),
                   pool_dir / "2024.parquet")
    store_path = tmp_path / "engine.duckdb"
    con = open_store(store_path)
    build_routing(con, pool_dir, load_specs(spec_dir), RELEASE,
                  aliases=load_aliases(spec_dir / "aliases.json"))
    con.close()
    return {"spec_dir": spec_dir, "pool": pool_dir, "store": store_path,
            "missing": tmp_path / "absent.csv"}


def report(bundle, monkeypatch, decisions=None) -> dict:
    monkeypatch.setattr(rule_report, "_claims_client",
                        lambda: ((object(), "") if decisions is not None
                                 else (None, "SUPABASE_URL unset")))
    if decisions is not None:
        monkeypatch.setattr(rule_report, "screen_decisions",
                            lambda client, release: decisions)
    return rule_report.build(
        spec_dir=bundle["spec_dir"], pool_dir=bundle["pool"],
        store=bundle["store"], release=RELEASE, flora_path=bundle["missing"],
        negatives_path=bundle["missing"],
        aliases_path=bundle["spec_dir"] / "aliases.json", workers=2)


def rule(built: dict, spec_id: str) -> dict:
    return next(r for r in built["rules"] if r["id"] == spec_id)


def test_won_is_not_matched(bundle, monkeypatch):
    """`syn-replication` matches four works and wins two of them.

    Works 1 and 2 match its regex but are claimed by higher-precedence rules, so a
    report that counted matches as wins would credit it with twice its effect.
    Work 7 is one of its wins and was downgraded to `pending/no_text` — still its
    win, and reported as such.
    """
    built = report(bundle, monkeypatch)
    row = rule(built, "syn-replication")
    assert row["matched"] == 4
    assert row["won"] == 2
    assert row["won_no_text"] == 1
    assert row["would_win"] is None          # a live rule has no counterfactual


def test_shadow_is_counted_but_never_wins(bundle, monkeypatch):
    """A draft rule is read out of `evaluations`, and wins nothing.

    `syn-shadow` matches works 1 and 6. Work 1 is already claimed by a
    higher-precedence live discard, so it would win only work 6.
    """
    built = report(bundle, monkeypatch)
    row = rule(built, "syn-shadow")
    assert row["shadow"] is True
    assert row["matched"] == 2
    assert row["won"] == 0
    assert row["would_win"] == 1

    con = duckdb.connect(str(bundle["store"]), read_only=True)
    winners = {r[0] for r in con.execute(
        "SELECT DISTINCT rule_id FROM routing WHERE release_id = ?",
        [RELEASE]).fetchall()}
    con.close()
    assert "syn-shadow" not in winners


def test_unscreened_and_zero_proceed_read_differently(bundle, monkeypatch):
    """A rule nobody screened must not look like a rule that screened badly."""
    unscreened = report(bundle, monkeypatch)
    assert rule(unscreened, "syn-cite")["screen"] is None
    assert rule_report._precision_cell(None) == "no verdicts"

    # Work 2 is `syn-cite`'s win; one recorded verdict, and it discarded.
    screened = report(bundle, monkeypatch,
                      decisions={"screen_expensive": {2: "discard"}})
    cite = rule(screened, "syn-cite")["screen"]
    assert (cite["screened"], cite["proceed"], cite["discard"]) == (1, 0, 1)
    assert cite["precision"] == 0.0
    cell = rule_report._precision_cell(cite)
    assert cell.startswith("0% (n=1)") and "no verdicts" not in cell

    other = rule(screened, "syn-replication")["screen"]
    assert other["screened"] == 0
    assert rule_report._precision_cell(other) == "not screened"


def test_locked_store_refuses_rather_than_hangs(bundle, tmp_path):
    """A `route` holding the write lock is a message, not a stack trace."""
    with pytest.raises(rule_report.StoreUnavailable, match="no routing store"):
        rule_report.open_readonly(tmp_path / "nowhere.duckdb")

    writer = open_store(bundle["store"])          # holds the store read-write
    try:
        with pytest.raises(rule_report.StoreUnavailable, match="filter.engine route"):
            rule_report.open_readonly(bundle["store"])
    finally:
        writer.close()
