import pytest

import shared.llm_client as _llm_client
from shared import token_usage as _token_usage
from validate.app import create_app


@pytest.fixture(autouse=True)
def _token_usage_state_in_tmp(tmp_path_factory, monkeypatch):
    """Keep mocked provider calls out of the real usage record.

    That file is shared by every run on the machine, so a suite that wrote into it
    would both corrupt the usage history and eat the day's real OpenAI ceiling a few
    tokens at a time.
    """
    monkeypatch.setattr(
        _token_usage, "USAGE_STATE_PATH",
        tmp_path_factory.mktemp("token_usage") / "token_usage.json")


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


@pytest.fixture(autouse=True)
def _prescreen_off_unless_asked(monkeypatch):
    """The cheap pre-screen (issue #130) is on in production, and it sits in front of
    every Stage 3 row. A test about the validated screen, the ladder or the budget would
    otherwise reach the pre-screen's providers first and fail for a reason it is not
    about. Tests that exercise the tier set PRESCREEN_ENABLED on the module themselves.
    """
    import extract.run_extract as _run_extract
    monkeypatch.setattr(_run_extract, "PRESCREEN_ENABLED", False, raising=False)
