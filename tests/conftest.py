import os
import socket
import time
from pathlib import Path

import pytest

import shared.llm_client as _llm_client
from shared import abstract_store as _abstract_store
from shared import rate_limit as _rate_limit
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
def _no_retry_backoff(request, monkeypatch):
    """Serve the 1s/2s/4s retry backoff instantly.

    Blocking the socket makes every call the code was supposed to have mocked fail
    for real, and each one then sleeps its way through the retry ladder: seven
    seconds per escaped call, which is where nearly all of the suite's wall time
    went. The retry COUNT is what tests assert on; the waiting between attempts
    only exists to be polite to a live API, so it buys nothing here.
    """
    if os.getenv("TEST_LIVE_API"):
        return
    if _LIVE_DIR in Path(str(request.node.path)).resolve().parents:
        return
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


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
def _abstract_store_in_tmp(tmp_path_factory, monkeypatch):
    """Point the abstract store at a throwaway database.

    It is one shared SQLite file on this machine, and it is now also the
    checkpoint: a test that wrote into the real one would mark identifiers as
    already-answered and quietly cost a later run the abstracts it skipped.
    """
    monkeypatch.setattr(
        _abstract_store, "ABSTRACT_DB_PATH",
        tmp_path_factory.mktemp("abstracts") / "abstracts.sqlite")
    _abstract_store.close()
    yield
    _abstract_store.close()


@pytest.fixture(autouse=True)
def _no_provider_throttle(monkeypatch):
    """Zero the per-provider LLM rate limits and forget the reserved call slots.

    Without this every mocked provider call in the suite waits out a real
    wall-clock interval, and a test that records time.sleep() calls sees the
    throttle's waits mixed in with the ones it is asserting on.
    """
    monkeypatch.setattr(
        _llm_client, "_PROVIDER_RATE_SEC",
        {p: 0.0 for p in _llm_client._PROVIDER_RATE_SEC})
    _rate_limit._next_call_at.clear()


# ── The screen verdict a Stage 3 input row carries ───────────────────────────
# The front-door screen runs in Stage 2, so every filtered.csv Stage 3 accepts has
# SCREEN_COLS on it (shared/schema.py) and Stage 3 reads its verdict from there. A
# fixture that omits them is not a Stage 3 input at all — the run refuses it — so
# these two helpers write the verdict onto a test CSV the way `filter.engine
# handoff` does.

# Two voters, both saying "replication" confidently: the ordinary proceed.
SCREEN_PROCEED = {
    "screen_verdict": "proceed", "record_type": "replication",
    "categories": ["clearly_declared", "context_transfer"],
    "votes": [{"provider": "gemini", "classification": "replication",
               "confident": True},
              {"provider": "openai", "classification": "replication",
               "confident": True}],
    "llm_evidence": "", "llm_reasoning": "",
}


def screen_cells(screen: "dict | None" = None) -> dict:
    """`SCREEN_COLS` as CSV cells, from a `classify_replication()`-shaped dict.

    `None` writes them all blank — a row that reached Stage 3 with no verdict,
    which an `--as-routed` handoff can produce.
    """
    from shared.schema import SCREEN_COLS

    if screen is None:
        return {col: "" for col in SCREEN_COLS}
    votes = screen.get("votes") or []
    return {
        "screen_verdict": screen.get("screen_verdict", ""),
        "screen_record_type": screen.get("record_type", ""),
        "screen_categories": "|".join(screen.get("categories") or []),
        # The handoff writes the MODEL here; the tests' voters are named for their
        # provider, and provider_for() maps those names back to themselves.
        "screen_votes": "|".join(
            f"{v.get('model') or v['provider']}={v['classification']}/"
            f"{'confident' if v['confident'] else 'unconfident'}" for v in votes),
        "screen_evidence": screen.get("llm_evidence", ""),
        "screen_reasoning": screen.get("llm_reasoning", ""),
    }


def with_screen(csv_text: str, screen: "dict | None" = SCREEN_PROCEED) -> str:
    """The same filtered.csv text, with Stage 2's screen verdict on every row."""
    import csv as _csv
    import io

    from shared.schema import SCREEN_COLS

    reader = _csv.DictReader(io.StringIO(csv_text))
    cells = screen_cells(screen)
    out = io.StringIO()
    writer = _csv.DictWriter(out, fieldnames=list(reader.fieldnames or []) + SCREEN_COLS,
                             lineterminator="\n")
    writer.writeheader()
    for row in reader:
        writer.writerow({**row, **cells})
    return out.getvalue()


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
