"""Tests for the hard daily LLM token budget (shared/token_budget.py).

The budget is a spend ceiling, not a warning: once the day's total is reached the
next call must be refused rather than billed. It has to survive a restart (the total
is on disk, not in the process) and it has to start over at midnight, so the date is
injected here rather than read off the clock.
"""
import pytest

from shared import token_budget as tb


@pytest.fixture(autouse=True)
def _state_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "BUDGET_STATE_PATH", tmp_path / "token_budget.json")


def test_the_total_accumulates_across_calls_within_a_day():
    tb.record(1000, day="2026-08-01")
    tb.record(2500, day="2026-08-01")
    assert tb.spent_today("2026-08-01") == 3500


def test_a_new_date_starts_from_zero():
    tb.record(9_000_000, day="2026-08-01")
    assert tb.spent_today("2026-08-02") == 0
    tb.record(10, day="2026-08-02")
    assert tb.spent_today("2026-08-02") == 10


def test_check_passes_below_the_budget(monkeypatch):
    monkeypatch.setattr(tb, "DAILY_TOKEN_BUDGET", 8_000_000)
    tb.record(7_999_999, day="2026-08-01")
    tb.check("2026-08-01")


def test_check_raises_once_the_budget_is_spent(monkeypatch):
    monkeypatch.setattr(tb, "DAILY_TOKEN_BUDGET", 8_000_000)
    tb.record(8_000_000, day="2026-08-01")
    with pytest.raises(tb.TokenBudgetExhausted, match="DAILY_TOKEN_BUDGET"):
        tb.check("2026-08-01")


def test_a_zero_budget_lifts_the_cap(monkeypatch):
    """The one documented override — everything else keeps the ceiling."""
    monkeypatch.setattr(tb, "DAILY_TOKEN_BUDGET", 0)
    tb.record(50_000_000, day="2026-08-01")
    tb.check("2026-08-01")


def test_a_provider_call_charges_the_day(monkeypatch, tmp_path):
    """call_openai's usage field must reach the persisted total, not just the
    in-process stage counter."""
    from unittest.mock import MagicMock, patch
    import shared.llm_client as llm

    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(tb, "DAILY_TOKEN_BUDGET", 8_000_000)
    response = MagicMock()
    response.usage = MagicMock(total_tokens=1234)
    response.choices = [MagicMock(finish_reason="stop",
                                  message=MagicMock(content='{"ok": true}'))]
    client = MagicMock()
    client.chat.completions.create.return_value = response
    with patch("openai.OpenAI", return_value=client):
        llm.call_openai("prompt")

    assert tb.spent_today() == 1234
