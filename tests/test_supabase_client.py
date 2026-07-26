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


# ── Pipeline-vs-final confusion matrices (#72) ────────────────────────────────

def test_build_confusion_matrices_basic():
    import shared.supabase_client as sc
    rows = [
        # pipeline outcome vs final outcome
        {"validation_status": "validated", "type": "replication", "final_type": "replication",
         "outcome": "success", "final_outcome": "success"},   # correct
        {"validation_status": "validated", "type": "replication", "final_type": "replication",
         "outcome": "success", "final_outcome": "failure"},   # pipeline wrong: success->failure
        {"validation_status": "validated", "type": "replication", "final_type": "reproduction",
         "outcome": "mixed", "final_outcome": "mixed"},        # type wrong: repl->repro
        {"validation_status": "rejected", "type": "replication", "final_type": "replication",
         "outcome": "success", "final_outcome": "success"},    # excluded (rejected)
        {"validation_status": "validated", "type": "replication", "final_type": "replication",
         "outcome": "failure", "final_outcome": ""},           # not finalised on outcome
    ]
    m = sc.build_confusion_matrices(rows)

    oc = m["outcome"]
    assert oc["n"] == 3          # 3 finalised, non-excluded outcome rows
    assert oc["correct"] == 2    # success/success + mixed/mixed
    assert oc["accuracy"] == round(2 / 3 * 100, 1)
    i, j = oc["labels"].index("success"), oc["labels"].index("failure")
    assert oc["matrix"][i][j] == 1   # one success miscoded as failure

    tp = m["type"]
    assert tp["n"] == 4          # 4 finalised, non-excluded type rows (rejected one excluded)
    ri, ro = tp["labels"].index("replication"), tp["labels"].index("reproduction")
    assert tp["matrix"][ri][ro] == 1   # one replication corrected to reproduction


def test_build_confusion_matrices_label_order_and_extras():
    import shared.supabase_client as sc
    rows = [
        {"validation_status": "validated", "type": "reproduction", "final_type": "reproduction",
         "outcome": "computationally successful, robust",
         "final_outcome": "computationally successful, robust"},
        {"validation_status": "validated", "type": "replication", "final_type": "replication",
         "outcome": "failure", "final_outcome": "failure"},
    ]
    oc = sc.build_confusion_matrices(rows)["outcome"]
    # preferred labels come first in their canonical order; unknown repro-grid label appended
    assert oc["labels"][0] == "failure"
    assert "computationally successful, robust" in oc["labels"]
    assert oc["labels"].index("failure") < oc["labels"].index("computationally successful, robust")


def test_build_confusion_matrices_canonicalises_outcome_vocab():
    """Pipeline 'success' vs validation-final 'successful' must read as agreement,
    not a correction (FLoRA vocab differs from the pipeline's — #72)."""
    import shared.supabase_client as sc
    rows = [
        {"validation_status": "validated", "type": "replication", "final_type": "replication",
         "outcome": "success", "final_outcome": "successful"},           # same, diff vocab
        {"validation_status": "validated", "type": "replication", "final_type": "replication",
         "outcome": "cannot_be_determined", "final_outcome": "uninformative"},  # same
        {"validation_status": "validated", "type": "replication", "final_type": "replication",
         "outcome": "success", "final_outcome": "failed"},               # genuine miscode
    ]
    oc = sc.build_confusion_matrices(rows)["outcome"]
    assert oc["n"] == 3
    assert oc["correct"] == 2, "success/successful and cbd/uninformative are agreement"
    # canonical labels only — no 'successful'/'failed'/'uninformative' leak through
    assert set(oc["labels"]) <= {"success", "failure", "cannot_be_determined"}
    i, j = oc["labels"].index("success"), oc["labels"].index("failure")
    assert oc["matrix"][i][j] == 1


def test_build_confusion_matrices_empty():
    import shared.supabase_client as sc
    m = sc.build_confusion_matrices([])
    assert m["outcome"]["n"] == 0 and m["outcome"]["accuracy"] == 0.0
    assert m["outcome"]["labels"] == [] and m["outcome"]["matrix"] == []
