"""
token_usage.py — what every LLM call cost, and the one provider that is capped.

Two jobs, one file on disk (cache/token_usage.json):

  * **Recording.** Every provider call that reports usage is written down per day,
    per provider, per model, with input and output tokens kept apart — they are
    priced differently, so a single total cannot answer what a run cost. A provider
    that reports no usage contributes nothing rather than an estimate.
  * **The OpenAI daily cap.** OpenAI is the metered spend here, so it is the only
    provider with a ceiling. call_openai checks the day's OpenAI total against
    OPENAI_DAILY_TOKEN_BUDGET before it spends, and raises TokenBudgetExhausted
    instead of calling. Gemini and OpenRouter are recorded but never capped.

Shape:  {"2026-08-01": {"openai": {"gpt-5.4-mini": {"in": 1200, "out": 340}}}}

Provider is a level of its own because the cap has to sum one provider's spend and a
model id does not name its provider — gpt-5.4-mini reached through OpenRouter is not
OpenAI spend.

token_counter (in-process, per pipeline stage) answers a different question and is
not persisted; this is the record that survives the process.
"""
from __future__ import annotations

import json
import os
from datetime import date

from .config import CACHE_DIR, OPENAI_DAILY_TOKEN_BUDGET, log
from .utils import csv_lock

USAGE_STATE_PATH = CACHE_DIR / "token_usage.json"


class TokenBudgetExhausted(RuntimeError):
    """The day's OpenAI token budget is spent, so no further OpenAI call may be made.

    Raised before the call rather than after it, and never caught as a per-row
    api_error: a row that failed for lack of budget has not been examined, and
    writing it as examined-and-undecidable would hide the whole remaining run.
    """


def _today() -> str:
    return date.today().isoformat()


def _read_all() -> dict:
    if not USAGE_STATE_PATH.exists():
        return {}
    try:
        state = json.loads(USAGE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_all(state: dict) -> None:
    """Write through a temp file and os.replace, so a crash mid-write cannot truncate
    the record of everything spent before it."""
    USAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_STATE_PATH.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, USAGE_STATE_PATH)


def record(provider: str, model: str,
           input_tokens: int, output_tokens: int, day: str = "") -> None:
    """Add one call's reported usage to the day's record."""
    if input_tokens <= 0 and output_tokens <= 0:
        return                    # the provider reported nothing; do not invent it
    day = day or _today()
    USAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The read-modify-write must be atomic across processes, or two concurrent runs
    # each replay the other's starting state and spend disappears from the record.
    with csv_lock(USAGE_STATE_PATH):
        state  = _read_all()
        bucket = state.setdefault(day, {}).setdefault(provider, {}).setdefault(
            model or "unknown", {"in": 0, "out": 0})
        bucket["in"]  += max(input_tokens, 0)
        bucket["out"] += max(output_tokens, 0)
        _write_all(state)


def usage(day: str = "") -> dict:
    """The day's record: {provider: {model: {"in": n, "out": n}}}."""
    return _read_all().get(day or _today(), {})


def spent(provider: str, day: str = "") -> int:
    """Input + output tokens recorded for one provider on `day`."""
    return sum(m.get("in", 0) + m.get("out", 0)
               for m in usage(day).get(provider, {}).values())


def check_openai_budget(day: str = "") -> None:
    """Raise TokenBudgetExhausted if the day's OpenAI budget is already spent."""
    if OPENAI_DAILY_TOKEN_BUDGET <= 0:
        return
    day   = day or _today()
    total = spent("openai", day)
    if total < OPENAI_DAILY_TOKEN_BUDGET:
        return
    log.error("Daily OpenAI token budget exhausted: %s of %s tokens spent on %s",
              f"{total:,}", f"{OPENAI_DAILY_TOKEN_BUDGET:,}", day)
    raise TokenBudgetExhausted(
        f"Daily OpenAI token budget exhausted: {total:,} of "
        f"{OPENAI_DAILY_TOKEN_BUDGET:,} tokens spent on {day}. Set "
        f"OPENAI_DAILY_TOKEN_BUDGET in .env to raise it (0 disables the cap), or "
        f"continue tomorrow.")
