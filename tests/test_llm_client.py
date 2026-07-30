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
    assert list(tmp_path.glob("classify_*.json"))  # a real verdict is cached


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
    assert not list(tmp_path.glob("classify_*.json"))   # uncached: a retry must succeed


def test_screen_no_votes_is_a_failure(monkeypatch, tmp_path):
    out = _screen(monkeypatch, tmp_path, gemini_ok=False, voter2_ok=False)

    assert out["resolution_method"] == "llm_refscreen_failed"
    assert "gemini" in out["llm_error"] and "openrouter" in out["llm_error"]
    assert out["votes"] == []
    assert not list(tmp_path.glob("classify_*.json"))


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


# ── Classification / target split (audit E1) ─────────────────────────────────
# The Q1 classification is Stage 3's front door and the Q2 target pick stays in the
# ladder, so the two halves must be separately callable and separately cached — and
# a threaded-in verdict must never be re-voted.

def _classify(monkeypatch, tmp_path, calls: list, label: str = "yes"):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    vote = dict(_VOTE, is_replication=label)

    def gemini(prompt, model=None):
        calls.append(("gemini", prompt))
        return dict(vote), None

    def openrouter(prompt, model=""):
        calls.append(("openrouter", prompt))
        return dict(vote), None

    monkeypatch.setattr(llm, "call_gemini", gemini)
    monkeypatch.setattr(llm, "call_openrouter", openrouter)
    return vote


def test_classification_is_callable_without_the_target_pick(monkeypatch, tmp_path):
    calls: list = []
    _classify(monkeypatch, tmp_path, calls)
    out = llm.classify_replication("10.1/x", "Title", "Abstract")

    assert out["is_replication"] == "yes" and out["models_agree"] is True
    assert [c[0] for c in calls] == ["gemini", "openrouter"]   # two votes, nothing else
    assert "resolved" not in out                                # no target fields
    assert list(tmp_path.glob("classify_*.json"))


def test_a_threaded_verdict_is_not_re_voted(monkeypatch, tmp_path):
    """Stage 3 votes at the front door; Stage 4.5 must reuse that verdict."""
    calls: list = []
    _classify(monkeypatch, tmp_path, calls)
    verdict = llm.classify_replication("10.1/x", "Title", "Abstract")
    calls.clear()

    refs = [{"doi": "10.1/orig", "title": "Original", "publication_year": 2015,
             "first_author": "Smith"}]
    monkeypatch.setattr(llm, "call_gemini", lambda prompt, model=None: (
        {"target_number": 1, "confidence": "high", "target_description": "Smith 2015",
         "evidence_quote": "q", "reasoning": "r"}, None))
    out = llm.screen_references_with_llm("10.1/x", "Title", "Abstract", refs,
                                         classification=verdict)

    assert out["resolution_method"] == "llm_references"
    assert out["resolved_doi_o"] == "10.1/orig"
    assert out["models_agree"] is True          # the verdict came through intact
    assert calls == []                          # …without a second classification call


def test_the_target_pick_is_cached_separately(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    verdict = {"resolution_method": "llm_refscreen_declined", "is_replication": "yes",
               "models_agree": True, "classification_confidence": "high", "votes": [],
               "llm_source": "", "llm_model": "", "llm_evidence": "",
               "llm_reasoning": "", "llm_prompt": "", "llm_error": ""}
    refs = [{"doi": "10.1/orig", "title": "Original", "publication_year": 2015,
             "first_author": "Smith"}]
    n = {"calls": 0}

    def gemini(prompt, model=None):
        n["calls"] += 1
        return ({"target_number": 1, "confidence": "high",
                 "target_description": "Smith 2015", "evidence_quote": "q",
                 "reasoning": "r"}, None)

    monkeypatch.setattr(llm, "call_gemini", gemini)
    first = llm.screen_references_with_llm("10.1/x", "T", "A", refs, classification=verdict)
    second = llm.screen_references_with_llm("10.1/x", "T", "A", refs, classification=verdict)

    assert first["resolved_doi_o"] == second["resolved_doi_o"] == "10.1/orig"
    assert n["calls"] == 1
    assert list(tmp_path.glob("reftarget_*.json"))
    assert not list(tmp_path.glob("classify_*.json"))  # the verdict is not re-cached here


def test_no_target_call_when_the_verdict_is_not_yes(monkeypatch, tmp_path):
    calls: list = []
    _classify(monkeypatch, tmp_path, calls, label="no")
    verdict = llm.classify_replication("10.1/x", "Title", "Abstract")
    monkeypatch.setattr(llm, "call_gemini", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no target pick for a paper that is not a replication")))

    out = llm.screen_references_with_llm(
        "10.1/x", "Title", "Abstract",
        [{"doi": "10.1/o", "title": "O", "publication_year": 2015, "first_author": "S"}],
        classification=verdict)

    assert out["is_replication"] == "no"
    assert out["resolved"] is False


# ── Gemini flex tier (audit Q4) ──────────────────────────────────────────────
# Flex is a 50% discount and must be applied to every Gemini call — including the
# PDF and image calls, which carry the largest payloads — but only on paid keys.

def _gemini_ok(text: str = '{"ok": true}'):
    r = MagicMock()
    r.status_code = 200
    r.text = text
    r.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"totalTokenCount": 10},
    }
    return r


def _flex_env(monkeypatch, *, use_flex=True, paid=(1,), keys=("k1", "k2")):
    monkeypatch.setattr(llm, "GEMINI_USE_FLEX", use_flex)
    monkeypatch.setattr(llm, "GEMINI_PAID_KEYS", set(paid))
    monkeypatch.setattr(llm, "GEMINI_FLEX_TIMEOUT", 900)
    monkeypatch.setattr(llm, "GEMINI_API_KEYS", list(keys))


def test_flex_is_sent_on_pdf_calls_when_the_key_is_paid(monkeypatch):
    _flex_env(monkeypatch)
    posts: list = []

    def post(url, json=None, timeout=None):
        posts.append((dict(json), timeout))
        return _gemini_ok()

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.call_gemini_with_pdf("prompt", b"%PDF-1.4") == {"ok": True}
    assert posts[0][0]["service_tier"] == "flex"
    assert posts[0][1] == 900   # flex calls can queue — not the 45s standard timeout


def test_flex_is_sent_on_image_calls_when_the_key_is_paid(monkeypatch):
    _flex_env(monkeypatch)
    posts: list = []

    def post(url, json=None, timeout=None):
        posts.append((dict(json), timeout))
        return _gemini_ok()

    monkeypatch.setattr(llm.requests, "post", post)
    imgs = [{"mime_type": "image/png", "data": "aGk="}]
    assert llm.call_gemini_with_images("prompt", imgs) == {"ok": True}
    assert posts[0][0]["service_tier"] == "flex"
    assert posts[0][1] == 900


def test_flex_is_not_sent_on_an_unpaid_key(monkeypatch):
    # Key 2 is free-tier: it must bill at standard rate and keep the short timeout.
    _flex_env(monkeypatch, paid=(1,))
    posts: list = []

    def post(url, json=None, timeout=None):
        posts.append((dict(json), timeout))
        r = _gemini_ok()
        if "k1" in url:
            r.status_code = 429
        return r

    monkeypatch.setattr(llm.requests, "post", post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.call_gemini_with_pdf("prompt", b"%PDF-1.4") == {"ok": True}
    assert posts[0][0]["service_tier"] == "flex"       # key 1, paid
    assert "service_tier" not in posts[-1][0]          # key 2, free
    assert posts[-1][1] == 45


def test_flex_follows_the_paid_key_not_its_position(monkeypatch):
    # Paid key sits in slot 2 — the old key_idx == 0 heuristic would have missed it.
    _flex_env(monkeypatch, paid=(2,))
    posts: list = []

    def post(url, json=None, timeout=None):
        posts.append((url, dict(json)))
        r = _gemini_ok()
        if "k1" in url:
            r.status_code = 429
        return r

    monkeypatch.setattr(llm.requests, "post", post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    assert llm.call_gemini("prompt") == ({"ok": True}, "")
    assert "service_tier" not in posts[0][1]
    assert posts[-1][1]["service_tier"] == "flex"


def test_flex_rejection_falls_back_to_standard_tier(monkeypatch):
    _flex_env(monkeypatch, keys=("k1",))
    posts: list = []

    def post(url, json=None, timeout=None):
        posts.append((dict(json), timeout))
        if "service_tier" in json:
            r = MagicMock()
            r.status_code = 400
            r.text = '{"error": {"message": "service_tier flex is not supported"}}'
            return r
        return _gemini_ok()

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.call_gemini_with_pdf("prompt", b"%PDF-1.4") == {"ok": True}
    assert len(posts) == 2
    assert "service_tier" not in posts[1][0]
    assert posts[1][1] == 45


def test_flex_is_off_entirely_when_disabled(monkeypatch):
    _flex_env(monkeypatch, use_flex=False, keys=("k1",))
    posts: list = []

    def post(url, json=None, timeout=None):
        posts.append(dict(json))
        return _gemini_ok()

    monkeypatch.setattr(llm.requests, "post", post)
    llm.call_gemini_with_pdf("prompt", b"%PDF-1.4")
    llm.call_gemini_with_images("prompt", [{"mime_type": "image/png", "data": "aGk="}])
    assert all("service_tier" not in p for p in posts)
