"""Tests for shared/supabase_client.py — all Supabase calls are mocked."""
import os
import time
from unittest.mock import patch, MagicMock

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "")


def test_not_configured_returns_error():
    """When SUPABASE_URL is empty, all functions return error dict."""
    import shared.supabase_client as sc
    original = sc.SUPABASE_URL
    sc.SUPABASE_URL = ""
    try:
        assert sc.get_validation_stats() == {"error": "supabase_not_configured"}
        assert sc.get_correction_frequency() == {"error": "supabase_not_configured"}
        assert sc.get_validated_outcomes() == {"error": "supabase_not_configured"}
        assert sc.get_drilldown_page(1, "all", "all") == {"error": "supabase_not_configured"}
    finally:
        sc.SUPABASE_URL = original


def test_validation_stats_shape(monkeypatch):
    """get_validation_stats returns expected keys with mocked HTTP."""
    import shared.supabase_client as sc
    monkeypatch.setattr(sc, "SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(sc, "SUPABASE_SERVICE_KEY", "fake-key")
    sc._CACHE.clear()

    mock_unvalidated = [
        {"validation_status": "unvalidated"},
        {"validation_status": "unvalidated"},
        {"validation_status": "validated"},
        {"validation_status": "need_review"},
        {"validation_status": "validation_inprogress"},
    ]
    mock_queue = [
        {"validator_id": "alice", "is_validated": True},
        {"validator_id": "bob",   "is_validated": True},
        {"validator_id": "alice", "is_validated": False},
    ]

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.raise_for_status = MagicMock()
        if "validation_queue" in url:
            m.json.return_value = mock_queue
        else:
            m.json.return_value = mock_unvalidated
        return m

    with patch("shared.supabase_client.requests.get", side_effect=fake_get):
        result = sc.get_validation_stats()

    assert result["total"] == 5
    assert result["unvalidated"] == 2
    assert result["validated"] == 1
    assert result["need_review"] == 1
    assert result["in_progress"] == 1
    assert result["total_judgements"] == 2
    assert result["active_validators"] == 2
    # completion_rate = filled slots / total slots (a progress metric, not agreement)
    assert "completion_rate" in result
    assert result["completion_rate"] == round(2 / 3, 4)
    assert "agreement_rate" not in result


def test_cache_is_used(monkeypatch):
    """Second call within TTL does not make HTTP request."""
    import shared.supabase_client as sc
    monkeypatch.setattr(sc, "SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(sc, "SUPABASE_SERVICE_KEY", "fake-key")
    sc._CACHE.clear()

    sentinel = {"total": 99, "unvalidated": 99, "validated": 0, "need_review": 0,
                "in_progress": 0, "total_judgements": 0, "active_validators": 0,
                "completion_rate": 0.0, "tiebreakers": 0}
    sc._CACHE["validation_stats"] = {"ts": time.time(), "data": sentinel}

    with patch("shared.supabase_client.requests.get") as mock_get:
        result = sc.get_validation_stats()
        mock_get.assert_not_called()

    assert result["total"] == 99


def test_correction_frequency_shape(monkeypatch):
    """
    Counts use validation_queue schema: record_id + type_check/original_check/outcome_check.
    Two rows from the same record_id count as 1 incorrect, not 2.
    """
    import shared.supabase_client as sc
    monkeypatch.setattr(sc, "SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(sc, "SUPABASE_SERVICE_KEY", "fake-key")
    sc._CACHE.clear()

    mock_rows = [
        # record A, validator_1 — type and outcome incorrect
        {"record_id": "aaa", "type_check": "incorrect", "original_check": "correct",
         "outcome_check": "incorrect"},
        # record A, validator_2 — outcome also incorrect (same record; still counts once)
        {"record_id": "aaa", "type_check": "correct", "original_check": "incorrect",
         "outcome_check": "incorrect"},
        # record B — all correct
        {"record_id": "bbb", "type_check": "correct", "original_check": "correct",
         "outcome_check": "correct"},
    ]

    def fake_get(url, **kwargs):
        m = MagicMock()
        m.raise_for_status = MagicMock()
        m.json.return_value = mock_rows
        return m

    with patch("shared.supabase_client.requests.get", side_effect=fake_get):
        result = sc.get_correction_frequency()

    assert "type_incorrect" in result
    assert "original_incorrect" in result
    assert "outcome_incorrect" in result
    # record A had type incorrect → 1; record B did not → still 1
    assert result["type_incorrect"] == 1
    # record A had original incorrect (via validator_2) → 1
    assert result["original_incorrect"] == 1
    # record A had outcome incorrect → 1
    assert result["outcome_incorrect"] == 1
