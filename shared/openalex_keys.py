"""
openalex_keys.py — the OpenAlex credential, and what to do when it runs out.

OpenAlex bills per request against a per-key daily budget that resets at midnight
UTC, so a long run can drain one key mid-flight; ``OPENALEX_API_KEYS`` holds the
rest. Rotation only works if every caller shares one current-key pointer, and the
pipeline had three separate HTTP layers — Stage 3's client, Stage 1's paginator,
and the abstract backfill's session — of which only the first could rotate. The
other two sent the first key and died on a refusal with keys still unspent.

This module owns that pointer. It deliberately holds no request logic: the three
layers have genuinely different retry and checkpoint semantics (Stage 1 must save
its cursor and stop the phrase; Stage 3 must raise), and only the credential and
the refusal test are common.
"""

import threading

import requests

from shared.config import OPENALEX_API_KEYS, RESEARCHER_EMAIL, log

_BASE_HEADERS: dict[str, str] = {
    "User-Agent": f"FLoRA-Extractor/1.0 (mailto:{RESEARCHER_EMAIL})"
}

# Index into OPENALEX_API_KEYS of the key currently in use. Advanced only by
# rotate_key(), on a budget refusal.
_key_idx = 0
_key_lock = threading.Lock()


def headers() -> dict[str, str]:
    """Request headers carrying the key currently in rotation.

    OpenAlex only recognises the key as a Bearer token; a bare key in the
    Authorization header is silently ignored and the request is served from the
    anonymous pool (1000/day, shared per-IP) instead of the keyed pool. That
    mis-auth is why Stage 3 hit "Insufficient budget" 429s while Stage 1 (which
    already sent Bearer) did not. Verified against the live API 2026-07-23.
    """
    if _key_idx < len(OPENALEX_API_KEYS):
        return {**_BASE_HEADERS, "Authorization": f"Bearer {OPENALEX_API_KEYS[_key_idx]}"}
    return dict(_BASE_HEADERS)


def current_index() -> int:
    """Which key headers() is handing out right now.

    A concurrent caller reads it before its request so it can say, on a refusal,
    which key was refused — see rotate_key.
    """
    return _key_idx


def rotate_key(from_idx: "int | None" = None) -> bool:
    """Advance past the key the refusal was about. False when none is left.

    *from_idx* is the key the caller's request actually carried (from
    current_index()). Callers are concurrent — Stage 3 runs its rows in a thread
    pool — and a drained key refuses every request that was in flight when it ran
    out, so N threads report one exhaustion. A refusal on a key the pointer has
    already moved past is that same exhaustion, and rotating again for it would
    burn a fresh key with a whole day's budget on it. Omitting *from_idx* keeps the
    old unconditional behaviour, which is right for a single-threaded caller.
    """
    global _key_idx
    with _key_lock:
        if from_idx is not None and from_idx < _key_idx:
            return True     # someone else already rotated off that key
        if _key_idx + 1 >= len(OPENALEX_API_KEYS):
            return False
        _key_idx += 1
        log.warning("OpenAlex key %d/%d out of budget — rotating to key %d",
                    _key_idx, len(OPENALEX_API_KEYS), _key_idx + 1)
        return True


def is_budget_refusal(resp: "requests.Response") -> bool:
    """True when a 429 means 'you cannot afford this', not 'you are too fast'."""
    try:
        body = resp.json()
    except ValueError:
        return False
    if str(body.get("message", "")).lower().startswith("insufficient budget"):
        return True
    return body.get("dailyRemainingUsd") == 0 and body.get("prepaidRemainingUsd") == 0


def quota_message(resp: "requests.Response") -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return (
        f"OpenAlex quota exhausted: {body.get('message', 'insufficient budget')} "
        f"(daily remaining ${body.get('dailyRemainingUsd', '?')}, "
        f"prepaid remaining ${body.get('prepaidRemainingUsd', '?')}). "
        "Stopping: continuing without candidates would silently degrade every link. "
        "Add funds at https://openalex.org/pricing or wait for the midnight-UTC reset."
    )
