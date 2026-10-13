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
    monkeypatch.setattr(tu, "LLM_DAILY_USD_BUDGET", 0)


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


def test_cache_counters_are_recorded_as_subsets_of_input():
    tu.record("openai", "gpt-5.6-luna", 5000, 100, day="2026-08-01",
              cached_input_tokens=3000, cache_write_input_tokens=1000)
    tu.record("openai", "gpt-5.6-luna", 5000, 100, day="2026-08-01",
              cached_input_tokens=4000)

    assert tu.usage("2026-08-01")["openai"]["gpt-5.6-luna"] == {
        "in": 10000, "out": 200, "cached_in": 7000, "cache_write_in": 1000}
    assert tu.spent("openai", "2026-08-01") == 10200


def test_openai_cache_counters_flow_from_response_to_usage_record(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    response = MagicMock()
    response.usage = MagicMock(
        prompt_tokens=5000, completion_tokens=100, total_tokens=5100,
        prompt_tokens_details=MagicMock(cached_tokens=3000,
                                        cache_write_tokens=1000))
    response.choices = [MagicMock(finish_reason="stop",
                                  message=MagicMock(content='{"ok": true}'))]
    client = MagicMock()
    client.chat.completions.create.return_value = response

    with patch("openai.OpenAI", return_value=client):
        assert llm.call_openai("prompt", model="gpt-5.6-luna")[0] == {"ok": True}

    assert tu.usage()["openai"]["gpt-5.6-luna"] == {
        "in": 5000, "out": 100, "cached_in": 3000,
        "cache_write_in": 1000}


def test_concurrent_calls_do_not_lose_each_other_s_tokens():
    """Every LLM call records, and Stage 3 and the filter engine's tiers both make
    those calls from a thread pool. The record is a read-modify-write of one file, so
    without a lock the threads replay each other's starting state and spend vanishes."""
    from concurrent.futures import ThreadPoolExecutor

    def hammer() -> None:
        for _ in range(25):
            tu.record("openai", "gpt-5.4-mini", 10, 1, day="2026-08-01")

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(hammer) for _ in range(8)]:
            future.result()

    assert tu.usage("2026-08-01") == {
        "openai": {"gpt-5.4-mini": {"in": 8 * 25 * 10, "out": 8 * 25}}}


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
            llm.call_openai("prompt", model="m")

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


def test_a_reported_cost_is_summed_and_an_unreported_one_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(tu, "USAGE_STATE_PATH", tmp_path / "u.json")
    tu.record("openrouter", "a/b", 100, 10, day="2026-09-25", usd=0.0001)
    tu.record("openrouter", "a/b", 100, 10, day="2026-09-25", usd=0.0002)
    tu.record("openai", "m", 100, 10, day="2026-09-25")
    day = tu.usage("2026-09-25")
    assert day["openrouter"]["a/b"]["usd"] == pytest.approx(0.0003)
    assert "usd" not in day["openai"]["m"]


def test_usd_budget_sums_billed_cost_and_disjoint_cache_tokens():
    tu.record("openai", "gpt-6-luna", 1_000_000, 1_000_000,
              day="2026-10-13", cached_input_tokens=400_000,
              cache_write_input_tokens=200_000)
    tu.record("gemini", "gemini-3.1-flash-lite", 1_000_000, 100_000,
              day="2026-10-13", cached_input_tokens=200_000)
    tu.record("openrouter", "deepseek/deepseek-v4-flash", 9_000_000, 9_000_000,
              day="2026-10-13", usd=0.30)
    # OpenAI .888 + Gemini .355 + OpenRouter .30; no token-derived OR bill.
    assert tu.spent_usd("2026-10-13") == pytest.approx(1.543)


def test_dollar_cap_resets_by_utc_day_and_does_not_depend_on_token_cap(monkeypatch):
    monkeypatch.setattr(tu, "LLM_DAILY_USD_BUDGET", 5)
    monkeypatch.setattr(tu, "OPENAI_DAILY_TOKEN_BUDGET", 0)
    tu.record("openrouter", "a/b", 1, 1, day="2026-10-13", usd=5)
    with pytest.raises(tu.TokenBudgetExhausted, match="LLM_DAILY_USD_BUDGET"):
        tu.check_openai_budget("2026-10-13")
    tu.check_openai_budget("2026-10-14")


@pytest.mark.parametrize("provider,model", [("openai", "unknown"), ("openrouter", "a/b")])
def test_unpriced_usage_refuses_instead_of_counting_as_free(monkeypatch, provider, model):
    monkeypatch.setattr(tu, "LLM_DAILY_USD_BUDGET", 5)
    tu.record(provider, model, 100, 100)
    with pytest.raises(tu.TokenBudgetExhausted, match="Cannot enforce"):
        tu.check_usd_budget()


def test_missing_billing_after_a_billed_openrouter_call_refuses(monkeypatch):
    monkeypatch.setattr(tu, "LLM_DAILY_USD_BUDGET", 5)
    tu.record("openrouter", "a/b", 100, 10, usd=0.1)
    tu.record("openrouter", "a/b", 100, 10)
    with pytest.raises(tu.TokenBudgetExhausted, match="no billed cost"):
        tu.check_usd_budget()


@pytest.mark.parametrize("contents", ['broken json', '[]'])
def test_invalid_usage_record_does_not_reset_dollar_spend(monkeypatch, contents):
    monkeypatch.setattr(tu, "LLM_DAILY_USD_BUDGET", 5)
    tu.USAGE_STATE_PATH.write_text(contents)
    with pytest.raises(tu.TokenBudgetExhausted, match="usage record"):
        tu.check_usd_budget()


@pytest.mark.parametrize("provider", ["openai", "gemini", "openrouter", "pdf", "images"])
def test_dollar_cap_blocks_each_provider_before_network(monkeypatch, provider):
    monkeypatch.setattr(tu, "LLM_DAILY_USD_BUDGET", 5)
    tu.record("openrouter", "a/b", 100, 100, usd=5)
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "GEMINI_API_KEYS", ["k"])
    with patch("openai.OpenAI") as client, patch.object(llm.requests, "post") as post:
        with pytest.raises(tu.TokenBudgetExhausted):
            if provider == "openai":
                llm.call_openai("prompt", "gpt-6-luna")
            elif provider == "gemini":
                llm.call_gemini("prompt", "gemini-3.1-flash-lite")
            elif provider == "openrouter":
                llm.call_openrouter("prompt", "deepseek/deepseek-v4-flash")
            elif provider == "pdf":
                llm.call_gemini_with_pdf("prompt", b"pdf")
            else:
                llm.call_gemini_with_images("prompt", [])
        client.assert_not_called()
        post.assert_not_called()
