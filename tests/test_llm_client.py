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


# ── Stage 4.5 reference screen ───────────────────────────────────────────────
# The screen votes two providers on "is this a replication?". A missing vote is an
# API failure, not a verdict, and must never reach the pipeline as a disagreement.
# (Extends the two regression tests from PR #84, which this work supersedes.)

_VOTE = {"is_replication": "yes", "classification_confidence": "high",
         "confidence": "high", "evidence_quote": "q", "reasoning": "r"}


def _screen(monkeypatch, tmp_path, gemini_ok: bool, voter2_ok: bool,
            refs=None, target=None, vote_label: str = "yes", calls=None):
    """Run screen_references_with_llm with each classifier either answering or failing."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    vote = dict(_VOTE, is_replication=vote_label)

    def gemini(prompt, model=None):
        if target is not None and "REFERENCES" in prompt:
            return dict(target), None
        return (dict(vote), None) if gemini_ok else (None, "boom")

    def openrouter(prompt, model=""):
        if calls is not None:
            calls.append(model)
        return (dict(vote), None) if voter2_ok else (None, "boom")

    def openai(prompt, model=None):
        raise AssertionError("the screen must not call OpenAI for its second vote")

    monkeypatch.setattr(llm, "call_gemini", gemini)
    monkeypatch.setattr(llm, "call_openrouter", openrouter)
    monkeypatch.setattr(llm, "call_openai", openai)
    return llm.screen_references_with_llm("10.1/x", "Title", "Abstract", refs or [])


def test_screen_both_votes_is_a_complete_screen(monkeypatch, tmp_path):
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True)

    assert out["models_agree"] is True
    assert len(out["votes"]) == 2
    assert out["resolution_method"] != "llm_refscreen_partial"
    assert list(tmp_path.glob("refscreen_*.json"))  # a real verdict is cached


def test_screen_second_vote_runs_the_configured_openrouter_model(monkeypatch, tmp_path):
    """Voter 2 is Ministral on OpenRouter, not an OpenAI model — the helper asserts
    OpenAI is never called, and the vote must be attributed to the configured model."""
    calls: list[str] = []
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True, calls=calls)

    assert calls == [llm.SCREEN_VOTER2_MODEL]
    assert llm.SCREEN_VOTER2_MODEL == "mistralai/ministral-14b-2512"
    assert llm.SCREEN_PROVIDERS == ("gemini", "openrouter")
    assert [v["provider"] for v in out["votes"]] == ["gemini", "openrouter"]
    assert out["llm_source"] == "gemini+openrouter"
    assert out["llm_model"] == f"{llm.GEMINI_LIGHT_MODEL}+{llm.SCREEN_VOTER2_MODEL}"


def test_screen_one_vote_is_partial_not_a_disagreement(monkeypatch, tmp_path):
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=False)

    assert out["resolution_method"] == "llm_refscreen_partial"
    assert "openrouter" in out["llm_error"]
    assert len(out["votes"]) == 1
    assert out["llm_model"] == llm.GEMINI_LIGHT_MODEL   # the model that did answer
    assert not list(tmp_path.glob("refscreen_*.json"))  # uncached: a retry must succeed


def test_screen_no_votes_is_a_failure(monkeypatch, tmp_path):
    out = _screen(monkeypatch, tmp_path, gemini_ok=False, voter2_ok=False)

    assert out["resolution_method"] == "llm_refscreen_failed"
    assert "gemini" in out["llm_error"] and "openrouter" in out["llm_error"]
    assert out["votes"] == []
    assert not list(tmp_path.glob("refscreen_*.json"))


def test_screen_attributes_a_resolved_link_to_the_target_picker(monkeypatch, tmp_path):
    """The reference was picked by the L5 call, so the row must name that model and
    quote its evidence — not the Q1 classifier pair that only said "yes, a replication"."""
    refs = [{"doi": "10.1/orig", "title": "Original", "publication_year": 2015,
             "first_author": "Smith"}]
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True, refs=refs,
                  target={"target_number": 1, "confidence": "high",
                          "target_description": "Smith 2015",
                          "evidence_quote": "we re-test Smith (2015)",
                          "reasoning": "abstract names it"})

    assert out["resolution_method"] == "llm_references"
    assert out["resolved_doi_o"] == "10.1/orig"
    assert out["llm_model"] == llm.GEMINI_HEAVY_MODEL
    assert out["llm_source"] == "gemini"
    assert out["llm_evidence"] == "we re-test Smith (2015)"


def test_screen_keeps_classifier_attribution_when_no_target_is_picked(monkeypatch, tmp_path):
    """A 'no' verdict is the classifiers' decision, so the discard path keeps their
    models, evidence and per-model reasoning — the row is set aside for review."""
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True, vote_label="no")

    assert out["is_replication"] == "no"
    assert out["llm_model"] == f"{llm.GEMINI_LIGHT_MODEL}+{llm.SCREEN_VOTER2_MODEL}"
    assert out["llm_evidence"] == "q"
    assert "gemini: r" in out["llm_reasoning"] and "openrouter: r" in out["llm_reasoning"]
