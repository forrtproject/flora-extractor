"""The one-off repair of works left holding a single expensive-tier vote.

Two seams: which works it selects (fewer than two live verdict rows, optionally
under one claim), and that a dry run spends nothing.
"""

import pytest

from analysis import repair_single_vote as repair
from filter.engine.tiers import TIER_EXPENSIVE, Work

CLAIM = "claim-broken"


class FakeClient:
    """Only the two reads the selection uses; anything else is a test failure."""

    def __init__(self, rows):
        self.rows = rows

    def verdicts(self, tier, claim_ids=None):
        rows = [r for r in self.rows if r["tier"] == tier]
        if claim_ids is None:
            return rows
        return [r for r in rows if r["claim_id"] in set(claim_ids)]


ROWS = [
    {"id": "v1", "claim_id": CLAIM, "work_id": 1, "tier": TIER_EXPENSIVE,
     "model": "gpt-5.4-mini", "verdict": "none"},
    {"id": "v2", "claim_id": CLAIM, "work_id": 2, "tier": TIER_EXPENSIVE,
     "model": "gpt-5.4-mini", "verdict": "replication"},
    {"id": "v3", "claim_id": "claim-ok", "work_id": 3, "tier": TIER_EXPENSIVE,
     "model": "gpt-5.4-mini", "verdict": "none"},
    {"id": "v4", "claim_id": "claim-ok", "work_id": 3, "tier": TIER_EXPENSIVE,
     "model": "gemini", "verdict": "none"},
]


def test_only_works_short_of_two_votes_are_selected():
    """A complete two-vote screen is decided; a single vote is an API failure the
    tier checkpointed anyway, and it is the only thing this repair touches."""
    short = repair.short_voted(FakeClient(ROWS), TIER_EXPENSIVE)
    assert set(short) == {1, 2}

    # Named claim: the works this batch broke, not every short-voted work ever.
    only_broken = repair.short_voted(FakeClient(ROWS), TIER_EXPENSIVE, claim_id=CLAIM)
    assert set(only_broken) == {1, 2}
    assert repair.short_voted(FakeClient(ROWS), TIER_EXPENSIVE,
                              claim_id="claim-ok") == {}


def test_a_dry_run_claims_nothing_and_spends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(repair, "open_store", lambda *a, **k: None)
    monkeypatch.setattr(repair, "load_aliases", lambda path: {})
    monkeypatch.setattr(repair, "ClaimsClient", lambda: FakeClient(ROWS))
    monkeypatch.setattr(repair, "pile_works", lambda *a, **k: [
        Work(1, "10.1/a", "A title", "An abstract.", TIER_EXPENSIVE),
        Work(2, "10.1/b", "B title", "Another abstract.", TIER_EXPENSIVE)])

    def forbidden(*a, **k):
        raise AssertionError("a dry run must not spend")

    monkeypatch.setattr(repair, "run_tier", forbidden)
    monkeypatch.setattr(repair, "_supersede_orphans", forbidden)

    assert repair.main(["--release", "rel-a"]) == 0
    out = capsys.readouterr().out
    assert "2 work(s) with fewer than two verdict rows" in out
    assert "Dry run" in out

    monkeypatch.setattr(repair, "run_tier", forbidden)
    with pytest.raises(AssertionError):
        repair.main(["--release", "rel-a", "--run"])
