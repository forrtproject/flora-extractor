import os
import socket
from pathlib import Path

import pytest

import shared.llm_client as _llm_client
from shared import token_usage as _token_usage
from validate.app import create_app

_LIVE_DIR = Path(__file__).resolve().parent / "live"

_BLOCKED_MSG = ("Network access blocked in tests — put live tests in "
                "tests/live/ behind TEST_LIVE_API=1")


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Fail any test that opens a real socket instead of mocking its API call.

    A mock that stops matching the code it stands in for silently starts calling
    the real API: the suite still passes, but it now costs money, needs keys and
    depends on a third party being up. Blocking connect() turns that into a loud
    failure at the moment the call escapes. Tests under tests/live/ are meant to
    reach the network, and TEST_LIVE_API=1 is how the suite asks for them.
    """
    if os.getenv("TEST_LIVE_API"):
        return
    if _LIVE_DIR in Path(str(request.node.path)).resolve().parents:
        return

    def _blocked(self, address, *args, **kwargs):
        raise RuntimeError(f"{_BLOCKED_MSG} (attempted: {address!r})")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


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
