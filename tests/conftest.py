import pytest

import shared.llm_client as _llm_client
from validate.app import create_app


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
