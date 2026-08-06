"""The OpenAlex credential pointer: which key goes out, and what a refusal moves.

Rotation is the only thing standing between a drained key and a run that stops with
budget still unspent, so the seams tested here are: the pointer picks a key, a
refusal advances it, an exhausted list SURFACES (returns False) instead of quietly
handing out the last key again, and a stale refusal from a concurrent caller does
not burn a fresh key.
"""
from unittest.mock import Mock

import pytest

from shared import openalex_keys as oak


@pytest.fixture()
def three_keys(monkeypatch):
    """Three keys and a pointer at the first, restored after the test.

    The pointer is module state shared by every caller in the process, so a test
    that left it advanced would hand the next test someone else's key.
    """
    monkeypatch.setattr(oak, "OPENALEX_API_KEYS", ["k1", "k2", "k3"])
    monkeypatch.setattr(oak, "_key_idx", 0)


def test_headers_carry_the_current_key_as_a_bearer_token(three_keys):
    # A bare key is ignored by OpenAlex and the request falls to the anonymous
    # pool, so the "Bearer " prefix is load-bearing, not cosmetic.
    assert oak.headers()["Authorization"] == "Bearer k1"
    assert oak.current_index() == 0


def test_no_keys_configured_sends_no_authorization_header(monkeypatch):
    monkeypatch.setattr(oak, "OPENALEX_API_KEYS", [])
    monkeypatch.setattr(oak, "_key_idx", 0)
    assert "Authorization" not in oak.headers()


def test_rotation_advances_the_key_headers_hands_out(three_keys):
    assert oak.rotate_key() is True
    assert oak.current_index() == 1
    assert oak.headers()["Authorization"] == "Bearer k2"


def test_the_last_key_refuses_rather_than_reusing_itself(three_keys):
    assert oak.rotate_key() is True   # -> k2
    assert oak.rotate_key() is True   # -> k3
    assert oak.rotate_key() is False  # nothing left: the caller must stop
    assert oak.current_index() == 2
    assert oak.headers()["Authorization"] == "Bearer k3"


def test_a_refusal_about_an_already_rotated_key_does_not_burn_the_next_one(three_keys):
    """N in-flight threads report one exhaustion; only the first may rotate."""
    oak.rotate_key(from_idx=0)          # thread A rotates off k1
    assert oak.current_index() == 1
    assert oak.rotate_key(from_idx=0) is True   # thread B's stale refusal
    assert oak.current_index() == 1             # still k2 — k3 keeps its budget


def _resp(payload, raises: bool = False) -> Mock:
    resp = Mock()
    resp.json.side_effect = ValueError("not json") if raises else None
    resp.json.return_value = payload
    return resp


@pytest.mark.parametrize("payload, expected", [
    ({"message": "Insufficient budget for this request"}, True),
    ({"dailyRemainingUsd": 0, "prepaidRemainingUsd": 0}, True),
    ({"dailyRemainingUsd": 0, "prepaidRemainingUsd": 1.5}, False),
    ({"message": "Too many requests"}, False),
])
def test_budget_refusal_is_told_apart_from_going_too_fast(payload, expected):
    assert oak.is_budget_refusal(_resp(payload)) is expected


def test_an_unparseable_body_is_not_read_as_a_budget_refusal():
    # Rotating on a 429 whose body we could not read would spend a key on what may
    # have been an ordinary rate limit.
    assert oak.is_budget_refusal(_resp(None, raises=True)) is False


def test_quota_message_survives_an_unparseable_body():
    msg = oak.quota_message(_resp(None, raises=True))
    assert "OpenAlex quota exhausted" in msg
