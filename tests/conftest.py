import pytest

import shared.llm_client as _llm_client
from shared import token_budget as _token_budget
from validate.app import create_app


@pytest.fixture(autouse=True)
def _token_budget_state_in_tmp(tmp_path_factory, monkeypatch):
    """Keep mocked provider calls out of the real daily token budget.

    The budget file is shared by every run on the machine, so a suite that spent
    into it would eat the day's real ceiling a few tokens at a time.
    """
    monkeypatch.setattr(
        _token_budget, "BUDGET_STATE_PATH",
        tmp_path_factory.mktemp("token_budget") / "token_budget.json")


@pytest.fixture(autouse=True)
def _no_provider_throttle(monkeypatch):
    """Zero the per-provider LLM rate limits and forget the last-call timestamps.

    Without this every mocked provider call in the suite waits out a real
    wall-clock interval, and a test that records time.sleep() calls sees the
    throttle's waits mixed in with the ones it is asserting on.
    """
    monkeypatch.setattr(
        _llm_client, "_PROVIDER_RATE_SEC",
        {p: 0.0 for p in _llm_client._PROVIDER_RATE_SEC})
    _llm_client._last_call_at.clear()


@pytest.fixture()
def app():
    test_app = create_app({"TESTING": True, "SECRET_KEY": "test"})
    return test_app


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["reviewer_id"] = "tester"
        yield c
