"""Tests for the persisted token record and the OpenAI daily cap (shared/token_usage.py).

Two behaviours in one file because they share one file on disk: every provider's
usage is recorded per day, provider and model with input and output kept apart, and
OpenAI — the metered provider — additionally cannot be called once the day's total
reaches OPENAI_DAILY_TOKEN_BUDGET. The date is injected rather than read off the
clock, so the day boundary is actually asserted.
"""
from unittest.mock import MagicMock, patch

import pytest

import shared.llm_client as llm
from shared import token_usage as tu


@pytest.fixture(autouse=True)
def _state_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tu, "USAGE_STATE_PATH", tmp_path / "token_usage.json")


def _call_openai(monkeypatch, prompt_tokens=100, completion_tokens=20,
                 model="gpt-5.4-mini"):
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    response = MagicMock()
    response.usage = MagicMock(prompt_tokens=prompt_tokens,
                               completion_tokens=completion_tokens,
                               total_tokens=prompt_tokens + completion_tokens)
    response.choices = [MagicMock(finish_reason="stop",
                                  message=MagicMock(content='{"ok": true}'))]
    client = MagicMock()
    client.chat.completions.create.return_value = response
    with patch("openai.OpenAI", return_value=client):
        return llm.call_openai("prompt", model=model)


def _call_gemini(monkeypatch, prompt_tokens=1000, total_tokens=1500,
                 model="gemini-3.5-flash-lite"):
    monkeypatch.setattr(llm, "GEMINI_API_KEYS", ["k"])
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": prompt_tokens,
                          "totalTokenCount": total_tokens},
    }
    with patch.object(llm, "_gemini_post", return_value=response):
        return llm.call_gemini("prompt", model=model)


# ── Recording: every provider, independent of the cap ────────────────────────

def test_usage_is_recorded_per_model_with_input_and_output_apart(monkeypatch):
    _call_openai(monkeypatch, prompt_tokens=900, completion_tokens=100)

    assert tu.usage() == {"openai": {"gpt-5.4-mini": {"in": 900, "out": 100}}}


def test_gemini_thinking_tokens_count_as_output(monkeypatch):
    """usageMetadata reports thinking tokens only inside totalTokenCount, and they
    are billed as output — taking candidatesTokenCount would undercount them."""
    _call_gemini(monkeypatch, prompt_tokens=1000, total_tokens=1500)

    assert tu.usage()["gemini"] == {"gemini-3.5-flash-lite": {"in": 1000, "out": 500}}


def test_repeated_calls_accumulate_per_model_and_day():
    tu.record("openai", "gpt-5.4-mini", 10, 5, day="2026-08-01")
    tu.record("openai", "gpt-5.4-mini", 1, 2, day="2026-08-01")
    tu.record("gemini", "flash-lite",   7, 3, day="2026-08-01")
    tu.record("openai", "gpt-5.4-mini", 100, 100, day="2026-08-02")

    assert tu.usage("2026-08-01") == {
        "openai": {"gpt-5.4-mini": {"in": 11, "out": 7}},
        "gemini": {"flash-lite":   {"in": 7,  "out": 3}},
    }
    assert tu.usage("2026-08-02") == {"openai": {"gpt-5.4-mini": {"in": 100, "out": 100}}}


def test_a_provider_that_reports_nothing_is_not_guessed_at():
    tu.record("openrouter", "some/model", 0, 0, day="2026-08-01")

    assert tu.usage("2026-08-01") == {}


# ── The OpenAI daily cap ─────────────────────────────────────────────────────

def test_a_call_below_the_cap_goes_through_and_charges_prompt_plus_completion(monkeypatch):
    monkeypatch.setattr(tu, "OPENAI_DAILY_TOKEN_BUDGET", 8_000_000)
    tu.record("openai", "gpt-5.4-mini", 7_000_000, 999_999)

    assert _call_openai(monkeypatch, prompt_tokens=900, completion_tokens=100)[0] == {"ok": True}
    assert tu.spent("openai") == 7_999_999 + 1000


def test_an_openai_call_past_the_cap_raises_before_it_spends(monkeypatch):
    monkeypatch.setattr(tu, "OPENAI_DAILY_TOKEN_BUDGET", 8_000_000)
    tu.record("openai", "gpt-5.4-mini", 8_000_000, 0)
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    client = MagicMock()

    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(tu.TokenBudgetExhausted, match="OPENAI_DAILY_TOKEN_BUDGET"):
            llm.call_openai("prompt")

    client.chat.completions.create.assert_not_called()


def test_a_zero_cap_lifts_it(monkeypatch):
    """The one documented override — nothing else disables the ceiling."""
    monkeypatch.setattr(tu, "OPENAI_DAILY_TOKEN_BUDGET", 0)
    tu.record("openai", "gpt-5.4-mini", 50_000_000, 0)

    assert _call_openai(monkeypatch)[0] == {"ok": True}


def test_gemini_is_recorded_but_never_charged_to_the_cap(monkeypatch):
    """Only OpenAI is metered here, so Gemini spend is neither counted against the
    cap nor blocked by an OpenAI budget that is already gone."""
    monkeypatch.setattr(tu, "OPENAI_DAILY_TOKEN_BUDGET", 8_000_000)
    tu.record("openai", "gpt-5.4-mini", 8_000_000, 0)

    result, _ = _call_gemini(monkeypatch, prompt_tokens=1000, total_tokens=1200)

    assert result == {"ok": True}
    assert tu.spent("gemini") == 1200
    assert tu.spent("openai") == 8_000_000
