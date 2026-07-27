"""Tests for shared.llm_client retry semantics (#45).

Per the api_error contract in CLAUDE.md, transient LLM failures must retry with
exponential backoff before giving up — a single exception must not immediately
poison a row. call_openai previously had a bare try/except (no retry).
"""
from unittest.mock import MagicMock, patch

import shared.llm_client as llm


def _resp(content: str):
    r = MagicMock()
    r.usage = None
    r.choices = [MagicMock(message=MagicMock(content=content))]
    return r


def test_call_openai_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_openai_disabled", False)
    sleeps: list = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient 503")
        return _resp('{"outcome": "success"}')

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = create
    with patch("openai.OpenAI", return_value=fake_client):
        result, err = llm.call_openai("prompt")

    assert result == {"outcome": "success"}
    assert calls["n"] == 3
    assert sleeps == [1, 2]  # exponential backoff between the 3 attempts


def test_call_openai_returns_none_after_three_failures(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_openai_disabled", False)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = RuntimeError("service down")
    with patch("openai.OpenAI", return_value=fake_client):
        result, err = llm.call_openai("prompt")

    assert result is None
    assert "service down" in err
    assert fake_client.chat.completions.create.call_count == 3


def _screen(monkeypatch, tmp_path, gemini_ok: bool, openai_ok: bool):
    """Run screen_references_with_llm with each classifier either answering or failing."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    vote = {"is_replication": "yes", "classification_confidence": "high",
            "evidence_quote": "q", "reasoning": "r"}
    monkeypatch.setattr(llm, "call_gemini",
                        lambda p, model=None: ((dict(vote), None) if gemini_ok else (None, "boom")))
    monkeypatch.setattr(llm, "call_openai",
                        lambda p, model=None: ((dict(vote), None) if openai_ok else (None, "boom")))
    return llm.screen_references_with_llm("10.1/x", "Title", "Abstract", [])


def test_screen_one_classifier_failure_is_not_a_disagreement(monkeypatch, tmp_path):
    """A missing vote is an API failure, not a verdict — it must not look like disagreement."""
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, openai_ok=False)

    assert out["resolution_method"] == "llm_refscreen_failed"
    assert "openai" in out["llm_error"]
    assert len(out["votes"]) < 2          # link_original keys the disagreement branch on this
    assert not list(tmp_path.glob("refscreen_*.json"))  # not cached: a retry must be able to succeed


def test_screen_two_votes_is_cached(monkeypatch, tmp_path):
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, openai_ok=True)

    assert out["models_agree"] is True
    assert len(out["votes"]) == 2
    assert list(tmp_path.glob("refscreen_*.json"))
