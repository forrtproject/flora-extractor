"""Tests for shared.llm_client retry semantics (#45).

Per the api_error contract in CLAUDE.md, transient LLM failures must retry with
exponential backoff before giving up — a single exception must not immediately
poison a row. call_openai previously had a bare try/except (no retry).
"""
from unittest.mock import MagicMock, patch

import pytest

import shared.llm_client as llm


def _resp(content: str):
    r = MagicMock()
    r.usage = None
    r.choices = [MagicMock(message=MagicMock(content=content))]
    return r


def test_call_openai_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
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
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = RuntimeError("service down")
    with patch("openai.OpenAI", return_value=fake_client):
        result, err = llm.call_openai("prompt")

    assert result is None
    assert "service down" in err
    assert fake_client.chat.completions.create.call_count == 3


# ── The front-door screen ────────────────────────────────────────────────────
# Two providers vote on the v3.2 five-field schema and screen_gate() turns the two
# votes into "discard" or "proceed". A missing vote is an API failure, not a verdict.

def _v(classification="replication", confident=True, categories=("clearly_declared",)):
    """One raw model response in the v3.2 schema."""
    return {"classification": classification, "confident": confident,
            "categories": list(categories), "evidence_quote": "q", "reasoning": "r"}


def _screen(monkeypatch, tmp_path, gemini_ok: bool, voter2_ok: bool,
            refs=None, target=None, vote=None, calls=None):
    """Run screen_references_with_llm with each voter either answering or failing."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "SCREEN_VOTER2_MODEL", "mistralai/ministral-14b-2512")
    vote = vote or _v()

    def gemini(prompt, model=None):
        if target is not None and "REFERENCES" in prompt:
            return dict(target), None
        return (dict(vote), None) if gemini_ok else (None, "boom")

    def openrouter(prompt, model=""):
        if calls is not None:
            calls.append(model)
        return (dict(vote), None) if voter2_ok else (None, "boom")

    def openai(prompt, model=None, reasoning_effort=""):
        raise AssertionError("an OpenRouter voter id must not reach call_openai")

    monkeypatch.setattr(llm, "call_gemini", gemini)
    monkeypatch.setattr(llm, "call_openrouter", openrouter)
    monkeypatch.setattr(llm, "call_openai", openai)
    return llm.screen_references_with_llm("10.1/x", "Title", "Abstract", refs or [])


# ── screen_gate: G-softqual, mirroring analysis/screening_eval/gate_sweep_v32.py ──

@pytest.mark.parametrize("votes,expected", [
    # Both "none", at any confidence → discard.
    ([_v("none", True),  _v("none", True)],  "discard"),
    ([_v("none", False), _v("none", False)], "discard"),
    ([_v("none", True),  _v("none", False)], "discard"),
    # One confident "none" + an unconfident partner → discard (the softqual clause).
    ([_v("none", True),  _v("unclear", False)],     "discard"),
    ([_v("none", True),  _v("replication", False)], "discard"),
    ([_v("unclear", False), _v("none", True)],      "discard"),
    # A confident split is a real disagreement — it proceeds.
    ([_v("none", True),  _v("replication", True)], "proceed"),
    ([_v("none", True),  _v("unclear", True)],     "proceed"),
    # No confident "none" at all → proceed.
    ([_v("none", False), _v("replication", False)], "proceed"),
    ([_v("replication", True), _v("reproduction", True)], "proceed"),
    ([_v("unclear", False), _v("unclear", False)],  "proceed"),
])
def test_screen_gate(votes, expected):
    parsed = [dict(v, provider="p") for v in votes]
    assert llm.screen_gate(parsed) == expected


def test_screen_gate_needs_two_votes():
    assert llm.screen_gate([dict(_v("none"), provider="gemini")]) is None
    assert llm.screen_gate([]) is None


# ── Vote parsing ─────────────────────────────────────────────────────────────

def test_a_classification_outside_the_enum_becomes_unclear(monkeypatch):
    monkeypatch.setattr(llm, "call_gemini",
                        lambda p, model=None: ({"classification": "maybe",
                                                "confident": True,
                                                "categories": ["clearly_declared"]}, None))
    assert llm._classify_once("p", "gemini", "flash-lite")["classification"] == "unclear"


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False), ("true", True), ("false", False),
    ("TRUE", True), (None, False), ("", False),
])
def test_confident_is_coerced_to_a_bool(monkeypatch, raw, expected):
    monkeypatch.setattr(llm, "call_gemini",
                        lambda p, model=None: ({"classification": "replication",
                                                "confident": raw, "categories": []}, None))
    vote = llm._classify_once("p", "gemini", "flash-lite")
    assert vote["confident"] is expected


def test_categories_keep_enum_values_in_order_and_drop_the_rest(monkeypatch):
    monkeypatch.setattr(llm, "call_gemini", lambda p, model=None: (
        {"classification": "replication", "confident": True,
         "categories": ["context_transfer", "not_a_category", "clearly_declared"]}, None))
    vote = llm._classify_once("p", "gemini", "flash-lite")
    assert vote["categories"] == ["context_transfer", "clearly_declared"]


def test_a_trailing_comma_still_parses():
    """The only malformed-response mode seen in ~2,100 validation calls."""
    assert llm._parse_llm_json('{"classification": "none", "confident": true,}') == {
        "classification": "none", "confident": True}
    assert llm._parse_llm_json('prose {"categories": ["other",],}')["categories"] == ["other"]


# ── Voter-2 routing ──────────────────────────────────────────────────────────

def test_a_slashless_voter_id_calls_openai_not_openrouter(monkeypatch):
    monkeypatch.setattr(llm, "SCREEN_VOTER2_MODEL", "gpt-5.4-mini")
    seen: dict = {}

    def openai(prompt, model=None, reasoning_effort=""):
        seen.update(model=model, reasoning_effort=reasoning_effort)
        return _v(), None

    monkeypatch.setattr(llm, "call_openai", openai)
    monkeypatch.setattr(llm, "call_openrouter", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("an OpenAI voter id must not reach OpenRouter")))

    assert [v[0] for v in llm.screen_voters()] == ["gemini", "openai"]
    vote = llm._classify_once("p", "openai", "gpt-5.4-mini")
    assert vote["provider"] == "openai"
    assert seen == {"model": "gpt-5.4-mini", "reasoning_effort": "low"}


def test_a_slashed_voter_id_calls_openrouter(monkeypatch):
    monkeypatch.setattr(llm, "SCREEN_VOTER2_MODEL", "mistralai/ministral-14b-2512")
    seen: dict = {}
    monkeypatch.setattr(llm, "call_openrouter",
                        lambda p, model="": (seen.update(model=model), (_v(), None))[1])
    monkeypatch.setattr(llm, "call_openai", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("an OpenRouter voter id must not reach OpenAI")))

    assert [v[0] for v in llm.screen_voters()] == ["gemini", "openrouter"]
    assert llm._classify_once("p", "openrouter", "mistralai/ministral-14b-2512")["provider"] == "openrouter"
    assert seen["model"] == "mistralai/ministral-14b-2512"


# ── Screen bookkeeping ───────────────────────────────────────────────────────

def test_screen_both_votes_is_a_complete_screen(monkeypatch, tmp_path):
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True)

    assert out["screen_verdict"] == "proceed"
    assert len(out["votes"]) == 2
    assert out["resolution_method"] != "llm_refscreen_partial"
    assert list(tmp_path.glob("classify_*.json"))  # a real verdict is cached


def test_screen_attributes_each_vote_to_its_model(monkeypatch, tmp_path):
    calls: list[str] = []
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True, calls=calls)

    assert calls == ["mistralai/ministral-14b-2512"]
    assert [v["provider"] for v in out["votes"]] == ["gemini", "openrouter"]
    assert out["llm_source"] == "gemini+openrouter"
    assert out["llm_model"] == f"{llm.GEMINI_LIGHT_MODEL}+mistralai/ministral-14b-2512"


def test_screen_one_vote_is_partial_not_a_verdict(monkeypatch, tmp_path):
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=False)

    assert out["resolution_method"] == "llm_refscreen_partial"
    assert out["screen_verdict"] == ""
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
    quote its evidence — not the Q1 voter pair that only said "yes, a replication"."""
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


def test_screen_keeps_voter_attribution_when_no_target_is_picked(monkeypatch, tmp_path):
    """A discard is the voters' decision, so the row keeps their models, evidence
    and per-model reasoning — it is set aside for review."""
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True,
                  vote=_v("none", True, ["terminology_only"]))

    assert out["screen_verdict"] == "discard"
    assert out["screen_classification"] == "none"
    assert out["record_type"] == ""
    assert out["llm_model"] == f"{llm.GEMINI_LIGHT_MODEL}+mistralai/ministral-14b-2512"
    assert out["llm_evidence"] == "q"
    assert "gemini: r" in out["llm_reasoning"] and "openrouter: r" in out["llm_reasoning"]


# ── record_type and categories ───────────────────────────────────────────────

def _two_votes(monkeypatch, tmp_path, v1, v2):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "SCREEN_VOTER2_MODEL", "mistralai/ministral-14b-2512")
    monkeypatch.setattr(llm, "call_gemini", lambda p, model=None: (dict(v1), None))
    monkeypatch.setattr(llm, "call_openrouter", lambda p, model="": (dict(v2), None))
    return llm.classify_replication("10.1/x", "Title", "Abstract")


@pytest.mark.parametrize("v1,v2,expected", [
    (_v("replication"),  _v("replication"),  "replication"),
    (_v("reproduction"), _v("reproduction"), "reproduction"),
    # A split or a "both" falls back to voter 1's qualifying answer…
    (_v("replication"),  _v("reproduction"), "replication"),
    (_v("reproduction"), _v("replication"),  "reproduction"),
    (_v("both"),         _v("reproduction"), "replication"),
    # …and to voter 2's when voter 1 gave no qualifying answer.
    (_v("none", True),   _v("reproduction", True), "reproduction"),
    (_v("unclear", False), _v("replication", True), "replication"),
    # Neither qualifying → no record_type at all.
    (_v("none"),         _v("unclear"),      ""),
])
def test_record_type_from_the_votes(monkeypatch, tmp_path, v1, v2, expected):
    assert _two_votes(monkeypatch, tmp_path, v1, v2)["record_type"] == expected


def test_categories_are_the_union_in_enum_order(monkeypatch, tmp_path):
    out = _two_votes(monkeypatch, tmp_path,
                     _v(categories=["context_transfer", "clearly_declared"]),
                     _v(categories=["clearly_declared", "self_retest"]))
    assert out["categories"] == ["clearly_declared", "self_retest", "context_transfer"]


# ── Classification / target split (audit E1) ─────────────────────────────────
# The Q1 classification is Stage 3's front door and the Q2 target pick stays in the
# ladder, so the two halves must be separately callable and separately cached — and
# a threaded-in verdict must never be re-voted.

def _classify(monkeypatch, tmp_path, calls: list, vote=None):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "SCREEN_VOTER2_MODEL", "mistralai/ministral-14b-2512")
    vote = vote or _v()

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

    assert out["screen_verdict"] == "proceed" and out["record_type"] == "replication"
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
    assert out["screen_verdict"] == "proceed"   # the verdict came through intact
    assert calls == []                          # …without a second classification call


def test_the_target_pick_is_cached_separately(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    verdict = {"resolution_method": "llm_refscreen_declined", "screen_verdict": "proceed",
               "screen_classification": "replication", "record_type": "replication",
               "categories": [], "votes": [],
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


def test_no_target_call_when_no_voter_gave_a_qualifying_answer(monkeypatch, tmp_path):
    calls: list = []
    _classify(monkeypatch, tmp_path, calls, vote=_v("none", True))
    verdict = llm.classify_replication("10.1/x", "Title", "Abstract")
    monkeypatch.setattr(llm, "call_gemini", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no target pick for a paper that is not a replication")))

    out = llm.screen_references_with_llm(
        "10.1/x", "Title", "Abstract",
        [{"doi": "10.1/o", "title": "O", "publication_year": 2015, "first_author": "S"}],
        classification=verdict)

    assert out["screen_verdict"] == "discard"
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


# ── Cache keys (audit E3) ────────────────────────────────────────────────────
# Every LLM cache key must name what the answer depends on. Keying on the DOI
# alone replayed one question's answer for a different question.

_TARGET_ANSWER = {"target_number": 1, "confidence": "high",
                  "target_description": "Smith 2015", "evidence_quote": "q",
                  "reasoning": "r"}

_VERDICT_YES = {"resolution_method": "llm_refscreen_declined",
                "screen_verdict": "proceed", "screen_classification": "replication",
                "record_type": "replication", "categories": [], "votes": [],
                "llm_source": "", "llm_model": "", "llm_evidence": "",
                "llm_reasoning": "", "llm_prompt": "", "llm_error": ""}


def _ref(doi: str, title: str):
    return {"doi": doi, "title": title, "publication_year": 2015, "first_author": "Smith"}


def test_target_key_follows_the_reference_list(monkeypatch, tmp_path):
    """A changed reference list renumbers the choices, so the old pick must not be
    replayed against it."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    n = {"calls": 0}

    def gemini(prompt, model=None):
        n["calls"] += 1
        return dict(_TARGET_ANSWER), None

    monkeypatch.setattr(llm, "call_gemini", gemini)
    refs_a = [_ref("10.1/orig", "Original")]
    refs_b = [_ref("10.1/other", "A different first reference"), _ref("10.1/orig", "Original")]

    a = llm.screen_references_with_llm("10.1/x", "T", "A", refs_a, classification=dict(_VERDICT_YES))
    b = llm.screen_references_with_llm("10.1/x", "T", "A", refs_b, classification=dict(_VERDICT_YES))

    assert n["calls"] == 2
    assert a["resolved_doi_o"] == "10.1/orig"
    assert b["resolved_doi_o"] == "10.1/other"   # entry 1 of the new list
    assert len(list(tmp_path.glob("reftarget_*.json"))) == 2


def test_classify_key_follows_the_voter_models(monkeypatch, tmp_path):
    """PR #97's precedent: the voter pair is part of the verdict, so a swapped voter
    must not read back the previous pair's answer."""
    calls: list = []
    _classify(monkeypatch, tmp_path, calls)
    llm.classify_replication("10.1/x", "Title", "Abstract")
    assert len(calls) == 2
    monkeypatch.setattr(llm, "SCREEN_VOTER2_MODEL", "some/other-voter")
    llm.classify_replication("10.1/x", "Title", "Abstract")
    assert len(calls) == 4                                   # re-voted, not replayed


def test_classify_key_follows_the_abstract(monkeypatch, tmp_path):
    calls: list = []
    _classify(monkeypatch, tmp_path, calls)
    llm.classify_replication("10.1/x", "Title", "Abstract")
    llm.classify_replication("10.1/x", "Title", "A backfilled, much longer abstract")
    assert len(calls) == 4


def _identify(monkeypatch, tmp_path, answer, calls):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def gemini(prompt, model=None):
        calls.append(prompt)
        return dict(answer), None

    monkeypatch.setattr(llm, "call_gemini", gemini)
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "")


_DECLINE = {"selected_candidate_number": None, "selected_title": "", "selected_year": None,
            "selected_first_author": "", "confidence": "low", "evidence": "", "reasoning": "none"}

_PICK = {"selected_candidate_number": 1, "selected_title": "Original", "selected_year": 2015,
         "selected_first_author": "Smith", "confidence": "high", "evidence": "e",
         "reasoning": "r"}

_CAND = [{"title": "Original", "year": 2015, "first_author": "Smith", "all_authors": ["Smith"],
          "doi": "10.1/orig", "openalex_id": "W1"}]


def test_identification_declines_are_cached(monkeypatch, tmp_path):
    """A decline is an answer. Caching only successes made every declined full-text
    call repay its API cost on every re-run."""
    calls: list = []
    _identify(monkeypatch, tmp_path, _DECLINE, calls)
    first = llm.identify_original_with_llm("10.1/x", "T", "A", "", _CAND, {})
    second = llm.identify_original_with_llm("10.1/x", "T", "A", "", _CAND, {})

    assert first["resolution_method"] == "llm_no_target"
    assert second["resolution_method"] == "llm_no_target"
    assert len(calls) == 1
    assert len(list(tmp_path.glob("llm_*.json"))) == 1


def test_identification_key_follows_the_candidates(monkeypatch, tmp_path):
    calls: list = []
    _identify(monkeypatch, tmp_path, _PICK, calls)
    llm.identify_original_with_llm("10.1/x", "T", "A", "", _CAND, {})
    other = [dict(_CAND[0], title="A different candidate", doi="10.1/other")]
    llm.identify_original_with_llm("10.1/x", "T", "A", "", other, {})
    assert len(calls) == 2


def test_identification_key_follows_the_parsed_sections(monkeypatch, tmp_path):
    """The abstract-stage call and the full-text call are different questions about
    the same DOI; the old DOI-only key collided them."""
    calls: list = []
    _identify(monkeypatch, tmp_path, _PICK, calls)
    llm.identify_original_with_llm("10.1/x", "T", "A", "", _CAND, {}, abstract_only=True)
    llm.identify_original_with_llm("10.1/x", "T", "A", "", _CAND,
                                   {"intro": "The PDF has since been parsed.",
                                    "references": []})
    assert len(calls) == 2


def test_identification_api_failure_is_not_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "call_gemini", lambda *a, **k: (None, "boom"))
    monkeypatch.setattr(llm, "call_openai", lambda *a, **k: (None, "boom"))
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "")
    out = llm.identify_original_with_llm("10.1/x", "T", "A", "", _CAND, {})
    assert out["resolution_method"] == "llm_failed"
    assert not list(tmp_path.glob("llm_*.json"))


def test_identification_key_follows_the_prompt_version(monkeypatch, tmp_path):
    """The version is folded in on its own account, not only via the rendered text."""
    calls: list = []
    _identify(monkeypatch, tmp_path, _PICK, calls)
    llm.identify_original_with_llm("10.1/x", "T", "A", "", _CAND, {})
    monkeypatch.setattr(llm, "prompt_version", lambda name: "ffffffffffff")
    llm.identify_original_with_llm("10.1/x", "T", "A", "", _CAND, {})
    assert len(calls) == 2


# ── Per-provider rate limiting (audit E5) ────────────────────────────────────
# One global interval charged every provider for every other provider's calls;
# the screen's two votes go to different providers and still waited a full second
# between them. Each provider now waits only on its own last call.

class _Clock:
    def __init__(self):
        self.now = 100.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(llm.time, "monotonic", c.monotonic)
    monkeypatch.setattr(llm.time, "sleep", c.sleep)
    monkeypatch.setattr(llm, "_PROVIDER_RATE_SEC",
                        {"gemini": 1.0, "openai": 0.5, "openrouter": 0.5})
    llm._last_call_at.clear()
    return c


def test_first_call_to_a_provider_does_not_wait(clock):
    llm._throttle("gemini")
    assert clock.slept == []


def test_second_call_waits_only_the_remaining_interval(clock):
    llm._throttle("gemini")
    clock.now += 0.4
    llm._throttle("gemini")
    assert clock.slept == [pytest.approx(0.6)]


def test_a_different_provider_does_not_wait_on_this_one(clock):
    llm._throttle("gemini")
    llm._throttle("openrouter")
    assert clock.slept == []


def test_no_wait_once_the_interval_has_already_passed(clock):
    llm._throttle("openai")
    clock.now += 5.0
    llm._throttle("openai")
    assert clock.slept == []


# ── Provider ladder (audit C1) ───────────────────────────────────────────────

def test_ladder_names_the_provider_that_answered(monkeypatch):
    monkeypatch.setattr(llm, "call_gemini", lambda p, model=None: (None, "429"))
    monkeypatch.setattr(llm, "call_openai",
                        lambda p, model=None: ({"ok": True}, ""))
    result, provider, model, err = llm.call_llm_ladder("p", gemini_model="g",
                                                       openai_model="o")
    assert (result, provider, model, err) == ({"ok": True}, "openai", "o", "")


def test_ladder_falls_through_to_openrouter(monkeypatch):
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setattr(llm, "call_gemini", lambda p, model=None: (None, "429"))
    monkeypatch.setattr(llm, "call_openai", lambda p, model=None: (None, "500"))
    monkeypatch.setattr(llm, "call_openrouter", lambda p, model="": ({"ok": True}, ""))
    result, provider, _model, _err = llm.call_llm_ladder("p")
    assert result == {"ok": True}
    assert provider == "openrouter"


def test_ladder_reports_every_provider_error_when_all_fail(monkeypatch):
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(llm, "call_gemini", lambda p, model=None: (None, "429"))
    monkeypatch.setattr(llm, "call_openai", lambda p, model=None: (None, "500"))
    result, provider, model, err = llm.call_llm_ladder("p")
    assert (result, provider, model) == (None, "none", "")
    assert "429" in err and "500" in err


def test_ladder_can_stop_before_openrouter(monkeypatch):
    """The reference-target pick must not fall through to the cheap last resort:
    a wrong original is worse than an unresolved one."""
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setattr(llm, "call_gemini", lambda p, model=None: (None, "429"))
    monkeypatch.setattr(llm, "call_openai", lambda p, model=None: (None, "500"))
    monkeypatch.setattr(llm, "call_openrouter", lambda p, model="": ({"ok": True}, ""))
    result, provider, _model, _err = llm.call_llm_ladder("p", openrouter=False)
    assert (result, provider) == (None, "none")


# ── Cache keys name every model the ladder can reach ─────────────────────────

def test_ladder_fingerprint_lists_every_reachable_model():
    fp = llm.ladder_fingerprint("gem-1", "oai-1")
    assert fp.split("|") == ["gem-1", "oai-1", llm.OPENROUTER_HEAVY_MODEL]
    assert llm.ladder_fingerprint("gem-1", "oai-1", openrouter=False) == "gem-1|oai-1"


def test_ladder_fingerprint_moves_with_the_fallback_models(monkeypatch):
    before = llm.ladder_fingerprint("gem-1")
    monkeypatch.setattr(llm, "OPENAI_MODEL", "oai-next")
    assert llm.ladder_fingerprint("gem-1") != before


def test_identification_key_follows_the_openai_fallback(monkeypatch, tmp_path):
    """Gemini going down means OpenAI answers — so the key has to name it too."""
    calls: list = []
    _identify(monkeypatch, tmp_path, {"resolved": False, "reasoning": "no"}, calls)
    llm.identify_original_with_llm("10.1/x", "T", "A", "p", [], {})
    monkeypatch.setattr(llm, "OPENAI_MODEL", "oai-next")
    llm.identify_original_with_llm("10.1/x", "T", "A", "p", [], {})
    assert len(calls) == 2


def test_target_key_follows_the_openai_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    verdict = {"resolution_method": "llm_refscreen_declined",
               "screen_verdict": "proceed", "screen_classification": "replication",
               "record_type": "replication", "categories": [], "votes": [],
               "llm_source": "", "llm_model": "", "llm_evidence": "",
               "llm_reasoning": "", "llm_prompt": "", "llm_error": ""}
    refs = [_ref("10.1/orig", "Original")]
    n = {"calls": 0}

    def gemini(prompt, model=None):
        n["calls"] += 1
        return ({"target_number": 1, "confidence": "high",
                 "target_description": "Smith 2015", "evidence_quote": "q",
                 "reasoning": "r"}, None)

    monkeypatch.setattr(llm, "call_gemini", gemini)
    llm.screen_references_with_llm("10.1/x", "T", "A", refs, classification=verdict)
    monkeypatch.setattr(llm, "OPENAI_MODEL", "oai-next")
    llm.screen_references_with_llm("10.1/x", "T", "A", refs, classification=verdict)
    assert n["calls"] == 2
