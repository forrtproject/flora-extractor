"""
token_usage.py — persisted LLM usage and daily token/dollar-equivalent ceilings.

Two jobs, one file on disk (cache/token_usage.json):

  * **Recording.** Every provider call that reports usage is written down per day,
    per provider, per model, with input and output tokens kept apart — they are
    priced differently, so a single total cannot answer what a run cost. A provider
    that reports no usage contributes an unpriced marker, never invented tokens.
  * **The legacy OpenAI token cap.** call_openai checks the day's OpenAI total against
    OPENAI_DAILY_TOKEN_BUDGET before it spends, and raises TokenBudgetExhausted
    instead of calling. An optional LLM_DAILY_USD_BUDGET additionally caps all
    three providers using conservative direct-provider rates and billed OpenRouter
    dollars, including the usage already recorded before it was enabled.

Shape:  {"2026-08-01": {"openai": {"gpt-5.4-mini":
         {"in": 1200, "out": 340, "cached_in": 900, "cache_write_in": 0}}}}
`unpriced_calls` marks missing usage for every provider and missing billed cost
for OpenRouter. An enabled dollar cap refuses these records rather than silently
omitting them, even when other calls did report tokens or dollars.
The cache fields, and `usd` (the billed cost, which only OpenRouter reports), are
omitted when the provider did not report a positive value;
older records only have in/out. Cached and written tokens are subsets of in, not
additional tokens. Use them for cost accounting, never for the daily token cap.

Provider is a level of its own because the cap has to sum one provider's spend and a
model id does not name its provider — gpt-5.4-mini reached through OpenRouter is not
OpenAI spend.

token_counter (in-process, per pipeline stage) answers a different question and is
not persisted; this is the record that survives the process.
"""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone

from .config import CACHE_DIR, LLM_DAILY_USD_BUDGET, OPENAI_DAILY_TOKEN_BUDGET, log
from .utils import csv_lock

USAGE_STATE_PATH = CACHE_DIR / "token_usage.json"

# The record is written by every LLM call, and both the filter engine's tier runners
# and Stage 3's row loop make those calls from a thread pool. The file lock below
# keeps two PROCESSES from replaying each other's starting state; it says nothing
# about two threads of one process, which is what this guards.
_record_lock = threading.Lock()

# USD per million tokens: input, cached input, cache write, output. Budget
# accounting deliberately assumes standard tier, and Luna's LONG-context rate:
# old aggregate records cannot recover individual requests' tier/context length.
# Thus Flex, short prompts and free Gemini calls never undercut the ceiling.
# Verified 2026-10-13 against developers.openai.com/api/docs/pricing and
# ai.google.dev/gemini-api/docs/pricing. OpenRouter is billed, never reconstructed.
_BUDGET_PRICES = {
    ("openai", "gpt-6-luna"): (0.20, 0.02, 0.25, 0.75),
    ("gemini", "gemini-3.1-flash-lite"): (0.25, 0.025, 0.25, 1.50),
}


class TokenBudgetExhausted(RuntimeError):
    """A daily token/dollar budget is spent or its cost cannot be established.

    Raised before the call rather than after it, and never caught as a per-row
    api_error: a row that failed for lack of budget has not been examined, and
    writing it as examined-and-undecidable would hide the whole remaining run.
    """


def _today() -> str:
    # The UTC date, because the day this budget models is OpenAI's: the free daily
    # token allocation resets at midnight UTC, and a local-date key opened the
    # budget two hours before the allocation it stands for (observed 2026-08-09).
    return datetime.now(timezone.utc).date().isoformat()


def _read_all(*, strict: bool = False) -> dict:
    if not USAGE_STATE_PATH.exists():
        return {}
    try:
        state = json.loads(USAGE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if strict:
            raise TokenBudgetExhausted("Cannot enforce LLM_DAILY_USD_BUDGET: unreadable usage record") from exc
        return {}
    if strict and not isinstance(state, dict):
        raise TokenBudgetExhausted("Cannot enforce LLM_DAILY_USD_BUDGET: invalid usage record")
    return state if isinstance(state, dict) else {}


def _write_all(state: dict) -> None:
    """Write through a temp file and os.replace, so a crash mid-write cannot truncate
    the record of everything spent before it."""
    USAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_STATE_PATH.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, USAGE_STATE_PATH)


def record(provider: str, model: str,
           input_tokens: int, output_tokens: int, day: str = "",
           *, cached_input_tokens: int = 0,
           cache_write_input_tokens: int = 0, usd: float = 0.0,
           usage_missing: bool = False) -> None:
    """Add one call's reported usage to the day's record. *usd* is what the
    provider says it billed, where it says so (OpenRouter); it is summed as
    `usd` and left out when unreported, never estimated. Missing usage is persisted
    as an unpriced marker for all providers; OpenRouter missing billing is also
    marked, independently of whether token counts were supplied."""
    if input_tokens <= 0 and output_tokens <= 0 and usd <= 0 and not usage_missing and provider != "openrouter":
        return                    # the provider reported nothing; do not invent it
    day = day or _today()
    USAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The read-modify-write must be atomic across processes, or two concurrent runs
    # each replay the other's starting state and spend disappears from the record.
    with _record_lock, csv_lock(USAGE_STATE_PATH):
        state  = _read_all()
        models = state.setdefault(day, {}).setdefault(provider, {})
        model = model or "unknown"
        if model not in models:
            models[model] = {"in": 0, "out": 0}
            if provider == "openrouter":
                models[model]["billing_complete"] = True
        bucket = models[model]
        # Never certify old aggregate history by adding one new billed call.
        if provider == "openrouter" and "billing_complete" not in bucket:
            bucket["billing_complete"] = False
        bucket["in"]  += max(input_tokens, 0)
        bucket["out"] += max(output_tokens, 0)
        if cached_input_tokens > 0:
            bucket["cached_in"] = bucket.get("cached_in", 0) + cached_input_tokens
        if cache_write_input_tokens > 0:
            bucket["cache_write_in"] = (bucket.get("cache_write_in", 0)
                                        + cache_write_input_tokens)
        if usd > 0:
            bucket["usd"] = round(bucket.get("usd", 0.0) + usd, 9)
        if usage_missing or (provider == "openrouter" and usd <= 0):
            bucket["unpriced_calls"] = bucket.get("unpriced_calls", 0) + 1
        _write_all(state)


def usage(day: str = "") -> dict:
    """The day's record: {provider: {model: {"in": n, "out": n}}}."""
    return _read_all().get(day or _today(), {})


def all_usage() -> dict:
    """The whole record: {day: {provider: {model: {"in": n, "out": n}}}}.

    Days with no usage are simply absent — nothing zero-fills them.
    """
    return _read_all()


def spent(provider: str, day: str = "") -> int:
    """Input + output tokens recorded for one provider on `day`."""
    return sum(m.get("in", 0) + m.get("out", 0)
               for m in usage(day).get(provider, {}).values())


def check_openai_budget(day: str = "") -> None:
    """Raise TokenBudgetExhausted if the day's OpenAI budget is already spent."""
    check_usd_budget(day)
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


def spent_usd(day: str = "") -> float:
    """Billed OpenRouter dollars plus conservative direct-provider equivalents.

    Missing prices/billing refuse an enabled budget instead of silently treating
    an unpriced model as free. Cache counters are subsets, never extra input.
    """
    total = 0.0
    records = _read_all(strict=True).get(day or _today(), {})
    if not isinstance(records, dict):
        raise TokenBudgetExhausted("Cannot enforce LLM_DAILY_USD_BUDGET: invalid usage record")
    for provider, models in records.items():
        if not isinstance(models, dict):
            raise TokenBudgetExhausted("Cannot enforce LLM_DAILY_USD_BUDGET: invalid usage record")
        for model, tokens in models.items():
            if not isinstance(tokens, dict) or any(
                not isinstance(tokens.get(key, 0), (int, float))
                or isinstance(tokens.get(key, 0), bool)
                or not math.isfinite(tokens.get(key, 0)) or tokens.get(key, 0) < 0
                for key in ("in", "out", "cached_in", "cache_write_in", "usd", "unpriced_calls")
            ):
                raise TokenBudgetExhausted("Cannot enforce LLM_DAILY_USD_BUDGET: invalid usage record")
            if tokens.get("unpriced_calls"):
                raise TokenBudgetExhausted(
                    f"Cannot enforce LLM_DAILY_USD_BUDGET: no billed cost or usage for {model}")
            if provider == "openrouter":
                if "usd" not in tokens or tokens.get("billing_complete") is not True:
                    raise TokenBudgetExhausted(
                        f"Cannot enforce LLM_DAILY_USD_BUDGET: no billed cost or unknown billing completeness for {model}")
                total += tokens["usd"]
                continue
            rates = _BUDGET_PRICES.get((provider, model))
            if rates is None:
                raise TokenBudgetExhausted(
                    f"Cannot enforce LLM_DAILY_USD_BUDGET: no price for {provider}/{model}")
            n_in = max(tokens.get("in", 0), 0)
            cached = min(max(tokens.get("cached_in", 0), 0), n_in)
            written = min(max(tokens.get("cache_write_in", 0), 0), n_in - cached)
            total += ((n_in - cached - written) * rates[0]
                      + cached * rates[1] + written * rates[2]
                      + max(tokens.get("out", 0), 0) * rates[3]) / 1_000_000
    return total


def check_usd_budget(day: str = "") -> None:
    """Stop buying calls at the dollar ceiling; in-flight calls may finish.

    Like the token ceiling, this is a pre-call check, not a reservation. The
    maximum overshoot is the calls already in flight across the worker pools.
    """
    if LLM_DAILY_USD_BUDGET <= 0:
        return
    day = day or _today()
    total = spent_usd(day)
    if total >= LLM_DAILY_USD_BUDGET:
        raise TokenBudgetExhausted(
            f"Daily LLM dollar budget exhausted: ${total:.4f} of "
            f"${LLM_DAILY_USD_BUDGET:.2f} on {day} "
            "(billed OpenRouter + conservative direct-provider estimates). "
            "Set LLM_DAILY_USD_BUDGET in .env to raise it, or continue tomorrow.")
