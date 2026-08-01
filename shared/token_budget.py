"""
token_budget.py — the hard daily ceiling on LLM spend.

token_counter reports what a run spent; this module decides whether the next call
may happen at all. The total lives in a small JSON file under cache/, keyed by the
local calendar date, so it survives a restart, is shared by concurrent runs, and
starts again at zero after midnight. Every provider's usage field feeds the same
total: the budget is on tokens bought, not on any one API.

When the day's total reaches DAILY_TOKEN_BUDGET the next call raises
TokenBudgetExhausted, which callers treat as a clean stop rather than a row error.
Set DAILY_TOKEN_BUDGET=0 to lift the cap.
"""
from __future__ import annotations

import json
from datetime import date

from .config import CACHE_DIR, DAILY_TOKEN_BUDGET, log

BUDGET_STATE_PATH = CACHE_DIR / "token_budget.json"


class TokenBudgetExhausted(RuntimeError):
    """The day's LLM token budget is spent, so no further call may be made.

    Raised before the call rather than after it, and never caught as a per-row
    api_error: a row that failed for lack of budget has not been examined, and
    writing it as examined-and-undecidable would hide the whole remaining run.
    """


def _today() -> str:
    return date.today().isoformat()


def _read(day: str) -> int:
    """Tokens already recorded for `day`, or 0 if the file is for another day."""
    if not BUDGET_STATE_PATH.exists():
        return 0
    try:
        state = json.loads(BUDGET_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(state.get("tokens", 0)) if state.get("date") == day else 0


def spent_today(day: str = "") -> int:
    return _read(day or _today())


def record(n_tokens: int, day: str = "") -> int:
    """Add a completed call's tokens to the day's total and return the new total."""
    day = day or _today()
    if n_tokens <= 0:
        return _read(day)
    total = _read(day) + n_tokens
    BUDGET_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_STATE_PATH.write_text(json.dumps({"date": day, "tokens": total}),
                                 encoding="utf-8")
    return total


def check(day: str = "") -> None:
    """Raise TokenBudgetExhausted if the day's budget is already spent."""
    if DAILY_TOKEN_BUDGET <= 0:
        return
    day   = day or _today()
    spent = _read(day)
    if spent < DAILY_TOKEN_BUDGET:
        return
    log.error("Daily LLM token budget exhausted: %s of %s tokens spent on %s",
              f"{spent:,}", f"{DAILY_TOKEN_BUDGET:,}", day)
    raise TokenBudgetExhausted(
        f"Daily LLM token budget exhausted: {spent:,} of {DAILY_TOKEN_BUDGET:,} "
        f"tokens spent on {day}. Set DAILY_TOKEN_BUDGET in .env to raise it "
        f"(0 disables the cap), or continue tomorrow.")
