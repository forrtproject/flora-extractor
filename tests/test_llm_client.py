"""Tests for shared.llm_client retry semantics (#45).

Per the api_error contract in CLAUDE.md, transient LLM failures must retry with
exponential backoff before giving up — a single exception must not immediately
poison a row. call_openai previously had a bare try/except (no retry).
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import shared.llm_client as llm
from shared import rate_limit
from shared.cache import write_cache


def _resp(content: str):
    r = MagicMock()
    r.usage = None
    r.choices = [MagicMock(message=MagicMock(content=content))]
    return r


def test_call_openai_retries_then_succeeds(monkeypatch):
    """Three attempts with 1s/2s backoff. A transient failure that clears on the
    third attempt returns the answer; one that never clears returns (None, error)."""
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
        result, err = llm.call_openai("prompt", model="m")

    assert result == {"outcome": "success"}
    assert calls["n"] == 3
    assert sleeps == [1, 2]  # exponential backoff between the 3 attempts

    down = MagicMock()
    down.chat.completions.create.side_effect = RuntimeError("service down")
    with patch("openai.OpenAI", return_value=down):
        result, err = llm.call_openai("prompt", model="m")

    assert result is None
    assert "service down" in err
    assert down.chat.completions.create.call_count == 3


# ── The front-door screen ────────────────────────────────────────────────────
# Two providers vote on the v3.2 five-field schema and screen_gate() turns the two
# votes into "discard" or "proceed". A missing vote is an API failure, not a verdict.

def _patch_providers(monkeypatch, fn):
    """Serve *fn* from all three provider entry points.

    Which one a call reaches follows the model id, and the model constants move; a
    test about a prompt or a cache key should not have to know who is serving it.
    """
    for _name in ("call_gemini", "call_openai", "call_openrouter"):
        monkeypatch.setattr(llm, _name, fn)


def _v(classification="replication", confident=True, categories=("clearly_declared",)):
    """One raw model response in the v3.2 schema."""
    return {"classification": classification, "confident": confident,
            "categories": list(categories), "evidence_quote": "q", "reasoning": "r"}


def _screen(monkeypatch, tmp_path, gemini_ok: bool, voter2_ok: bool,
            refs=None, target=None, vote=None, calls=None):
    """Run screen_references_with_llm with each voter either answering or failing."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "SCREENING_MODEL_2", "mistralai/ministral-14b-2512")
    vote = vote or _v()

    def gemini(prompt, model=None, **kw):
        if target is not None and "REFERENCE LIST" in prompt:
            return dict(target), None
        return (dict(vote), None) if gemini_ok else (None, "boom")

    def openrouter(prompt, model="", **kw):
        if calls is not None:
            calls.append(model)
        return (dict(vote), None) if voter2_ok else (None, "boom")

    def openai(prompt, model=None, **kw):
        # The target pick routes here whenever LINKING_MODEL is an OpenAI id; a VOTE
        # must not, which is what this guard still protects.
        if "REFERENCE LIST" in prompt:
            return (dict(target) if target is not None
                    else {"targets": [], "unidentified_count": 0, "reasoning": "n"}), None
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
                        lambda p, model=None, **kw: ({"classification": "maybe",
                                                "confident": True,
                                                "categories": ["clearly_declared"]}, None))
    assert llm._classify_once("p", "gemini-3.5-flash-lite", "minimal")["classification"] == "unclear"


@pytest.mark.parametrize("raw,expected", [
    (True, True),        # already a bool
    ("TRUE", True),      # a string, any case
    (None, False),       # absent from the response
])
def test_confident_is_coerced_to_a_bool(monkeypatch, raw, expected):
    monkeypatch.setattr(llm, "call_gemini",
                        lambda p, model=None, **kw: ({"classification": "replication",
                                                "confident": raw, "categories": []}, None))
    vote = llm._classify_once("p", "gemini-3.5-flash-lite", "minimal")
    assert vote["confident"] is expected


def test_categories_keep_enum_values_in_order_and_drop_the_rest(monkeypatch):
    monkeypatch.setattr(llm, "call_gemini", lambda p, model=None, **kw: (
        {"classification": "replication", "confident": True,
         "categories": ["context_transfer", "not_a_category", "clearly_declared"]}, None))
    vote = llm._classify_once("p", "gemini-3.5-flash-lite", "minimal")
    assert vote["categories"] == ["context_transfer", "clearly_declared"]


def test_a_trailing_comma_still_parses():
    """The only malformed-response mode seen in ~2,100 validation calls."""
    assert llm._parse_llm_json('{"classification": "none", "confident": true,}') == {
        "classification": "none", "confident": True}
    assert llm._parse_llm_json('prose {"categories": ["other",],}')["categories"] == ["other"]


# ── Voter-2 routing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("voter_id,provider,other", [
    ("gpt-5.4-mini", "openai", "openrouter"),                # slashless → OpenAI
    ("mistralai/ministral-14b-2512", "openrouter", "openai"),  # slashed → OpenRouter
])
def test_the_voter_id_decides_which_provider_is_called(monkeypatch, voter_id, provider, other):
    """A slash in the voter id means an OpenRouter model; without one it is an
    OpenAI model. The wrong provider must never see the call."""
    monkeypatch.setattr(llm, "SCREENING_MODEL_2", voter_id)
    seen: dict = {}

    def answer(prompt, model=None, **kw):
        seen.update(model=model, **kw)
        return _v(), None

    monkeypatch.setattr(llm, f"call_{provider}", answer)
    monkeypatch.setattr(llm, f"call_{other}", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError(f"a {provider} voter id must not reach {other}")))

    assert [v[0] for v in llm.screen_voters()] == ["gemini", provider]
    assert llm._classify_once("p", voter_id, llm.SCREENING_EFFORT_2)["provider"] == provider
    assert seen["model"] == voter_id
    if provider == "openai":
        assert seen["reasoning_effort"] == "low"


# ── Screen bookkeeping ───────────────────────────────────────────────────────

def test_screen_attributes_each_vote_to_its_model(monkeypatch, tmp_path):
    """A complete screen is two votes, each attributed to the model that cast it,
    and only a complete screen is cached."""
    calls: list[str] = []
    out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True, calls=calls)

    assert out["screen_verdict"] == "proceed"
    assert calls == ["mistralai/ministral-14b-2512"]
    assert [v["provider"] for v in out["votes"]] == ["gemini", "openrouter"]
    assert out["llm_source"] == "gemini+openrouter"
    assert out["llm_model"] == f"{llm.SCREENING_MODEL_1}+mistralai/ministral-14b-2512"
    assert list(tmp_path.glob("classify_*.json"))   # a real verdict is cached


@pytest.mark.parametrize("gemini_ok,method,votes,model", [
    # One vote is not a disagreement — the row waits for a re-run.
    (True,  "llm_refscreen_partial", 1, "gemini_light"),
    # No vote at all is an API failure.
    (False, "llm_refscreen_failed",  0, ""),
])
def test_an_incomplete_screen_is_reported_not_cached(monkeypatch, tmp_path,
                                                     gemini_ok, method, votes, model):
    out = _screen(monkeypatch, tmp_path, gemini_ok=gemini_ok, voter2_ok=False)

    assert out["resolution_method"] == method
    assert out["screen_verdict"] == ""
    assert len(out["votes"]) == votes
    assert "openrouter" in out["llm_error"]
    if not gemini_ok:
        assert "gemini" in out["llm_error"]
    # Only the model that actually answered is named.
    assert out["llm_model"] == (llm.SCREENING_MODEL_1 if model else "")
    assert not list(tmp_path.glob("classify_*.json"))  # uncached: a retry must succeed


@pytest.mark.parametrize("target_picked", [True, False],
                         ids=["target picked", "voters discarded"])
def test_the_row_names_whoever_actually_decided_it(monkeypatch, tmp_path, target_picked):
    """A resolved link was picked by the L5 call, so the row names that model and
    quotes its evidence — not the Q1 voter pair that only said "yes, a replication".
    A discard is the voters' decision, so that row keeps their models, evidence and
    per-model reasoning instead."""
    if target_picked:
        refs = [{"doi": "10.1/orig", "title": "Original", "publication_year": 2015,
                 "first_author": "Smith"}]
        out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True, refs=refs,
                      target={"targets": [{"key": "@smith2015", "match_certain": True,
                                           "target_as_named": "Smith 2015",
                                           "study_numbers": "",
                                           "evidence_quote": "we re-test Smith (2015)"}],
                              "unidentified_count": 0,
                              "reasoning": "abstract names it"})

        assert out["resolution_method"] == "llm_references"
        assert out["resolved_doi_o"] == "10.1/orig"
        assert out["llm_model"] == llm.LINKING_MODEL
        assert out["llm_source"] == llm.provider_for(llm.LINKING_MODEL)
        assert out["llm_evidence"] == "we re-test Smith (2015)"
    else:
        out = _screen(monkeypatch, tmp_path, gemini_ok=True, voter2_ok=True,
                      vote=_v("none", True, ["terminology_only"]))

        assert out["screen_verdict"] == "discard"
        assert out["screen_classification"] == "none"
        assert out["record_type"] == ""
        assert out["llm_model"] == f"{llm.SCREENING_MODEL_1}+mistralai/ministral-14b-2512"
        # Both voters' quotes, attributed: a discard is the pair's decision, so the
        # row carries the pair's evidence rather than the first voter's alone.
        assert out["llm_evidence"] == (f"{llm.SCREENING_MODEL_1}: q || "
                                       "mistralai/ministral-14b-2512: q")
        assert [v["evidence"] for v in out["votes"]] == ["q", "q"]
        assert "gemini: r" in out["llm_reasoning"] and "openrouter: r" in out["llm_reasoning"]


# ── record_type and categories ───────────────────────────────────────────────

def _two_votes(monkeypatch, tmp_path, v1, v2):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "SCREENING_MODEL_2", "mistralai/ministral-14b-2512")
    monkeypatch.setattr(llm, "call_gemini", lambda p, model=None, **kw: (dict(v1), None))
    monkeypatch.setattr(llm, "call_openrouter", lambda p, model="", **kw: (dict(v2), None))
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


def test_categories_are_the_union_of_both_voters_in_enum_order(monkeypatch, tmp_path):
    """The single-vote filter is pinned above; this is the two-vote merge — the
    column holds the union of what both voters saw, still in enum order."""
    out = _two_votes(monkeypatch, tmp_path,
                     _v(categories=["context_transfer", "clearly_declared"]),
                     _v(categories=["clearly_declared", "self_retest"]))
    assert out["categories"] == ["clearly_declared", "self_retest", "context_transfer"]


# ── Classification / target split (audit E1) ─────────────────────────────────
# The Q1 classification is Stage 3's front door and the Q2 target pick stays in the
# ladder, so the two halves must be separately callable and separately cached — and
# a threaded-in verdict must never be re-voted.

def _classify(monkeypatch, tmp_path, calls: list, vote=None,
              model_2: str = "mistralai/ministral-14b-2512"):
    """Voter 2 defaults to an OpenRouter model, which keeps the voter pair DIFFERENT
    from the one the classify equivalence is declared for. Tests of that equivalence
    pass the declared pair's voter 2 instead."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "SCREENING_MODEL_2", model_2)
    vote = vote or _v()

    def _voter(name):
        def call(prompt, model=None, **kw):
            calls.append((name, prompt))
            return dict(vote), None
        return call

    for name in ("gemini", "openrouter", "openai"):
        monkeypatch.setattr(llm, f"call_{name}", _voter(name))
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
    _patch_providers(monkeypatch,
                     lambda prompt, model=None, **kw: (dict(_TARGET_ANSWER), None))
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

    def answered(prompt, model=None, **kw):
        n["calls"] += 1
        return dict(_TARGET_ANSWER), None

    _patch_providers(monkeypatch, answered)
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
    _patch_providers(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(
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
    monkeypatch.setattr(llm, "GEMINI_PAID_KEY_SLOTS", set(paid))
    monkeypatch.setattr(llm, "GEMINI_FLEX_TIMEOUT", 900)
    monkeypatch.setattr(llm, "GEMINI_API_KEYS", list(keys))


_IMGS = [{"mime_type": "image/png", "data": "aGk="}]


@pytest.mark.parametrize("call_site,standard_timeout", [
    (lambda: llm.call_gemini_with_pdf("prompt", b"%PDF-1.4"), 45),
    (lambda: llm.call_gemini_with_images("prompt", _IMGS), 120),
], ids=["pdf", "images"])
@pytest.mark.parametrize("use_flex", [True, False], ids=["enabled", "disabled"])
def test_flex_rides_on_the_largest_payload_calls(monkeypatch, call_site, standard_timeout,
                                                 use_flex):
    """The PDF and image calls carry the biggest payloads, so the 50% flex discount
    has to reach them — and vanish entirely when GEMINI_USE_FLEX is off."""
    _flex_env(monkeypatch, use_flex=use_flex, keys=("k1",))
    posts: list = []

    def post(url, json=None, timeout=None):
        posts.append((dict(json), timeout))
        return _gemini_ok()

    monkeypatch.setattr(llm.requests, "post", post)
    assert call_site() == ({"ok": True}, "")
    if use_flex:
        assert posts[0][0]["service_tier"] == "flex"
        assert posts[0][1] == 900   # flex calls can queue — not the 45s standard timeout
    else:
        assert "service_tier" not in posts[0][0]
        assert posts[0][1] == standard_timeout


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
    assert llm.call_gemini_with_pdf("prompt", b"%PDF-1.4") == ({"ok": True}, "")
    assert len(posts) == 2
    assert "service_tier" not in posts[1][0]
    assert posts[1][1] == 45


# ── OpenAI flex tier ─────────────────────────────────────────────────────────
# The mirror of the Gemini path on the metered provider: half price for queueing,
# with a standard-tier call standing in whenever flex will not serve the request.

def _openai_flex_env(monkeypatch, use_flex=True):
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "OPENAI_USE_FLEX", use_flex)
    monkeypatch.setattr(llm, "OPENAI_FLEX_TIMEOUT", 900)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_openai_flex_is_sent_with_the_long_timeout(monkeypatch):
    _openai_flex_env(monkeypatch)
    calls: list = []

    def create(**kwargs):
        calls.append(kwargs)
        return _resp('{"ok": true}')

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = create
    with patch("openai.OpenAI", return_value=fake_client):
        assert llm.call_openai("prompt", model="m")[0] == {"ok": True}

    assert calls[0]["service_tier"] == "flex"
    assert calls[0]["timeout"] == 900   # flex calls queue — not the client default


def _api_error(status: int, *, code: str = "", param: str = "", message: str = ""):
    """A real OpenAI SDK error with the structured fields the detector reads."""
    import httpx
    import openai

    body = {"message": message, "type": "invalid_request_error",
            "code": code or None, "param": param or None}
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    cls = {400: openai.BadRequestError, 401: openai.AuthenticationError,
           429: openai.RateLimitError}[status]
    return cls(message, response=response, body=body)


def _flex_then_standard(calls: list, flex_exc: Exception):
    """A create() that fails the flex request and serves the standard one."""
    def create(**kwargs):
        calls.append(kwargs)
        if "service_tier" in kwargs:
            raise flex_exc
        return _resp('{"ok": true}')
    return create


def test_openai_flex_capacity_refusal_falls_back_within_the_attempt(monkeypatch):
    # 429 + resource_unavailable: the flex queue has nothing, standard will serve.
    _openai_flex_env(monkeypatch)
    calls: list = []
    exc = _api_error(429, code="resource_unavailable",
                     message="Service tier capacity exceeded for this model")
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = _flex_then_standard(calls, exc)
    with patch("openai.OpenAI", return_value=fake_client):
        assert llm.call_openai("prompt", model="m")[0] == {"ok": True}

    # Two requests, one attempt: a refused tier must not eat a retry from the
    # api_error budget, and the standard call carries no flex timeout.
    assert len(calls) == 2
    assert "service_tier" not in calls[1] and "timeout" not in calls[1]


def test_openai_flex_unsupported_param_falls_back_within_the_attempt(monkeypatch):
    # 400 naming service_tier: flex is not offered for this model/account.
    _openai_flex_env(monkeypatch)
    calls: list = []
    exc = _api_error(400, param="service_tier",
                     message="Invalid value for 'service_tier'")
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = _flex_then_standard(calls, exc)
    with patch("openai.OpenAI", return_value=fake_client):
        assert llm.call_openai("prompt", model="m")[0] == {"ok": True}

    assert len(calls) == 2
    assert "service_tier" not in calls[1]


@pytest.mark.parametrize("exc, label", [
    (_api_error(401, code="invalid_api_key",
                message="Incorrect API key provided (flex project key)"), "401"),
    (_api_error(400, code="model_not_found",
                message="The model does not exist (flex)"), "400-flex-in-message"),
])
def test_openai_non_tier_errors_do_not_trigger_the_standard_fallback(monkeypatch, exc, label):
    # A message merely mentioning flex is not a tier refusal: these must reach the
    # retry loop, so the run sees three flex-tier attempts and no standard call.
    _openai_flex_env(monkeypatch)
    calls: list = []

    def create(**kwargs):
        calls.append(kwargs)
        raise exc

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = create
    with patch("openai.OpenAI", return_value=fake_client):
        result, err = llm.call_openai("prompt", model="m")

    assert result is None and "exception" in err
    assert len(calls) == 3                              # the ordinary retry loop
    assert "service_tier" in calls[0]                   # first attempt was flex
    assert all("service_tier" not in c for c in calls[1:])   # retries stay standard


def test_openai_flex_timeout_is_not_a_refusal(monkeypatch):
    # A flex call can be billed and its response lost near the 900s deadline;
    # resending immediately would double-bill. It takes the retry loop instead.
    import httpx
    import openai
    _openai_flex_env(monkeypatch)
    calls: list = []
    timeout = openai.APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))

    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise timeout
        return _resp('{"ok": true}')

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = create
    with patch("openai.OpenAI", return_value=fake_client):
        assert llm.call_openai("prompt", model="m")[0] == {"ok": True}

    assert len(calls) == 2
    assert "service_tier" in calls[0]        # attempt 1, flex — timed out
    assert "service_tier" not in calls[1]    # attempt 2 of the retry loop, standard


def test_openai_flex_fallback_records_one_call_of_usage(monkeypatch):
    # The refused flex request was never served: exactly one usage record and one
    # budget check may result from the pair.
    _openai_flex_env(monkeypatch)
    recorded: list = []
    checks: list = []
    monkeypatch.setattr(llm, "_record_tokens",
                        lambda *a: recorded.append(a))
    monkeypatch.setattr(llm.token_usage, "check_openai_budget",
                        lambda: checks.append(1))
    monkeypatch.setattr(llm.token_usage, "spent", lambda p: 0)

    calls: list = []
    exc = _api_error(429, code="resource_unavailable", message="no flex capacity")
    served = _resp('{"ok": true}')
    served.usage = MagicMock(prompt_tokens=100, completion_tokens=20, total_tokens=120)

    def create(**kwargs):
        calls.append(kwargs)
        if "service_tier" in kwargs:
            raise exc
        return served

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = create
    with patch("openai.OpenAI", return_value=fake_client):
        assert llm.call_openai("prompt", model="m")[0] == {"ok": True}

    assert len(calls) == 2
    assert recorded == [("openai", "m", 100, 20)]
    assert len(checks) == 1


def test_openai_standard_fallback_failure_enters_the_retry_loop(monkeypatch):
    # Flex refused, standard failed: that is one attempt gone, two retries left,
    # and the retries run at standard tier.
    _openai_flex_env(monkeypatch)
    calls: list = []
    refusal = _api_error(429, code="resource_unavailable", message="no flex capacity")

    def create(**kwargs):
        calls.append(kwargs)
        if "service_tier" in kwargs:
            raise refusal
        raise RuntimeError("transient 503")

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = create
    with patch("openai.OpenAI", return_value=fake_client):
        result, err = llm.call_openai("prompt", model="m")

    assert result is None and "transient 503" in err
    assert len(calls) == 4          # flex + standard, then two standard retries
    assert sum("service_tier" in c for c in calls) == 1


def test_openai_flex_off_by_default(monkeypatch):
    _openai_flex_env(monkeypatch, use_flex=False)
    calls: list = []

    def create(**kwargs):
        calls.append(kwargs)
        return _resp('{"ok": true}')

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = create
    with patch("openai.OpenAI", return_value=fake_client):
        llm.call_openai("prompt", model="m")

    assert all("service_tier" not in c for c in calls)


# ── Reasoning effort ─────────────────────────────────────────────────────────
# A spend lever that changes the answer: it comes from the call site, reaches the
# provider under that provider's own name, and is named by the cache key.

def test_thinking_level_comes_from_the_caller(monkeypatch):
    """The same model id serves three call sites, so call_gemini sends the effort it
    is given and never derives one — an id must not carry another call's thinking."""
    monkeypatch.setattr(llm, "GEMINI_API_KEYS", ["k1"])
    monkeypatch.setattr(llm, "GEMINI_USE_FLEX", False)
    posts: list = []

    def post(url, json=None, timeout=None):
        posts.append(dict(json))
        return _gemini_ok()

    monkeypatch.setattr(llm.requests, "post", post)
    llm.call_gemini("prompt", model="gemini-3-flash-preview", reasoning_effort="minimal")
    llm.call_gemini("prompt", model="gemini-3-flash-preview")

    assert posts[0]["generationConfig"]["thinkingLevel"] == "minimal"
    assert "thinkingLevel" not in posts[1]["generationConfig"]

    # call_model passes the caller's value straight through to the Gemini leg.
    posts.clear()
    llm.call_model("prompt", "gemini-3-flash-preview", reasoning_effort="high")
    assert posts[0]["generationConfig"]["thinkingLevel"] == "high"


def test_cache_model_id_names_the_effort_it_is_given(monkeypatch):
    """The key states what the call sent — nothing is read off the model id."""
    assert llm.cache_model_id("m", "minimal") == "m@effort=minimal"
    assert llm.cache_model_id("m", "") == "m"
    assert llm.cache_model_id("m") == "m"

    # No global is consulted: the id that names LINKING_MODEL keys the same as any
    # other, whatever LINKING_EFFORT happens to be.
    monkeypatch.setattr(llm, "LINKING_EFFORT", "high")
    assert llm.cache_model_id(llm.LINKING_MODEL) == llm.LINKING_MODEL


def test_the_linking_key_format_is_unchanged():
    """The linking cache is the one whose entries were labelled with the effort they
    were actually produced at, so its key string must survive this change byte for
    byte — a new format re-pays for every link on disk."""
    assert llm.cache_model_id(llm.LINKING_MODEL, "medium") == \
        f"{llm.LINKING_MODEL}@effort=medium"


def test_the_screen_key_names_each_voters_own_effort(monkeypatch, tmp_path):
    """The two voters were evaluated at different rungs, so each key component names
    the effort ITS voter was called with — not one value stamped on the pair."""
    calls: list = []
    _classify(monkeypatch, tmp_path, calls)
    llm.classify_replication("10.1/x", "Title", "Abstract")

    voters = "+".join(llm.cache_model_id(m, eff)
                      for _, m, _, eff in llm.screen_voters())
    assert (llm.SCREENING_EFFORT_1, llm.SCREENING_EFFORT_2) == ("minimal", "low")
    assert voters.endswith("@effort=low") and "@effort=minimal+" in voters
    expected = llm.content_key("classify", "10.1/x",
                               llm.prompt_version("build_classify_prompt"),
                               voters,
                               llm.build_classify_prompt("Title", "Abstract"))
    assert [f.stem for f in tmp_path.glob("classify_*.json")] == [expected]


def test_the_gemini_voter_is_sent_minimal_rather_than_left_to_the_default(monkeypatch):
    """Voter 1 ran at the model's own default, which today IS minimal. Sending it
    explicitly is what stops a changed provider default from moving the voter without
    moving the key."""
    monkeypatch.setattr(llm, "GEMINI_API_KEYS", ["k1"])
    monkeypatch.setattr(llm, "GEMINI_USE_FLEX", False)
    posts: list = []
    monkeypatch.setattr(llm.requests, "post",
                        lambda url, json=None, timeout=None: (posts.append(dict(json)),
                                                              _gemini_ok())[1])

    _p, model, _e, effort = llm.screen_voters()[0]
    llm._classify_once("prompt", model, effort)
    assert posts[0]["generationConfig"]["thinkingLevel"] == "minimal"


# ── Declared cache equivalences (issue #171) ─────────────────────────────────
# Keys stay strict by default; a maintainer may register a legacy key that names the
# same computation. The registered one is the classify key's old voter component,
# whose "medium" label was never sent to any provider.

def _classify_key(voter_component: str, prompt: str) -> str:
    return llm.content_key("classify", "10.1/x",
                           llm.prompt_version("build_classify_prompt"),
                           voter_component, prompt)


def test_a_declared_legacy_key_is_migrated_onto_the_current_key(monkeypatch, tmp_path):
    """An entry under the reviewed legacy key answers the call, is re-filed under the
    current key with provenance, and is left where it is for other checkouts.

    Screened by the pair the equivalence is declared for — it is offered to no other."""
    calls: list = []
    _classify(monkeypatch, tmp_path, calls, model_2="gpt-5.4-mini")
    monkeypatch.setattr(llm, "_CLASSIFY_LEGACY_KEY_PARTS", ("legacy-voters",))
    legacy = _classify_key("legacy-voters", llm.build_classify_prompt("Title", "Abstract"))
    llm.write_cache(tmp_path, legacy, dict(_VERDICT_YES))

    out = llm.classify_replication("10.1/x", "Title", "Abstract")

    assert calls == []                       # nothing was re-asked
    assert out["screen_verdict"] == "proceed"
    assert out["cache_migrated"]["from_key"] == legacy
    assert out["cache_migrated"]["prompt_version"] == \
        llm.prompt_version("build_classify_prompt")
    assert "@effort=minimal" in out["cache_migrated"]["voters"]
    # The legacy file is untouched and the answer is now also under the current key.
    assert llm.read_cache(tmp_path, legacy) == _VERDICT_YES
    assert len(list(tmp_path.glob("classify_*.json"))) == 2


def test_the_equivalence_does_not_fire_across_a_prompt_change(monkeypatch, tmp_path):
    """Only the declared component is substituted; every other component is shared
    with the current key, so an entry from a different prompt cannot be claimed."""
    calls: list = []
    _classify(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(llm, "_CLASSIFY_LEGACY_KEY_PARTS", ("legacy-voters",))
    stale = _classify_key("legacy-voters",
                          llm.build_classify_prompt("Other title", "Other abstract"))
    llm.write_cache(tmp_path, stale, dict(_VERDICT_YES))

    llm.classify_replication("10.1/x", "Title", "Abstract")
    assert [c[0] for c in calls] == ["gemini", "openrouter"]   # it voted for itself


def test_the_registered_equivalences_are_the_two_older_labels(monkeypatch, tmp_path):
    """Both declared parts name minimal+low under an older label: the unlabelled pair
    the entries on disk are under, and the "@effort=medium" the key briefly derived
    from a model id and never sent. Each must be a live legacy key, not a comment."""
    assert llm._CLASSIFY_LEGACY_KEY_PARTS == (
        "gemini-3.5-flash-lite+gpt-5.4-mini",
        "gemini-3.5-flash-lite+gpt-5.4-mini@effort=medium")

    calls: list = []
    _classify(monkeypatch, tmp_path, calls, model_2="gpt-5.4-mini")
    prompt = llm.build_classify_prompt("Title", "Abstract")
    for part in llm._CLASSIFY_LEGACY_KEY_PARTS:
        for f in tmp_path.glob("classify_*.json"):
            f.unlink()
        llm.write_cache(tmp_path, _classify_key(part, prompt), dict(_VERDICT_YES))
        out = llm.classify_replication("10.1/x", "Title", "Abstract")
        assert out["cache_migrated"]["from_key"].startswith("classify_")
        assert calls == []


# ── Cache keys (audit E3) ────────────────────────────────────────────────────
# Every LLM cache key must name what the answer depends on. Keying on the DOI
# alone replayed one question's answer for a different question.

_TARGET_ANSWER = {"targets": [{"key": "@smith2015", "match_certain": True,
                               "target_as_named": "Smith 2015", "study_numbers": "",
                               "evidence_quote": "q"}],
                  "unidentified_count": 0, "reasoning": "r"}

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

    def answered(prompt, model=None, **kw):
        n["calls"] += 1
        return dict(_TARGET_ANSWER), None

    _patch_providers(monkeypatch, answered)
    refs_a = [_ref("10.1/orig", "Original")]
    refs_b = [_ref("10.1/other", "A different first reference"), _ref("10.1/orig", "Original")]

    a = llm.screen_references_with_llm("10.1/x", "T", "A", refs_a, classification=dict(_VERDICT_YES))
    b = llm.screen_references_with_llm("10.1/x", "T", "A", refs_b, classification=dict(_VERDICT_YES))

    assert n["calls"] == 2
    assert a["resolved_doi_o"] == "10.1/orig"
    assert b["resolved_doi_o"] == "10.1/other"   # entry 1 of the new list
    assert len(list(tmp_path.glob("reftarget_*.json"))) == 2


def test_reference_pick_carries_the_study_numbers(monkeypatch, tmp_path):
    """The wrapper copies an explicit list of resolved_* fields off the pick, so a
    field missing from that list is silently dropped: llm_references rows wrote an
    empty study_o however precisely the model named the study."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    answer = {**_TARGET_ANSWER,
              "targets": [dict(_TARGET_ANSWER["targets"][0], study_numbers="Study 2")]}
    _patch_providers(monkeypatch, lambda prompt, model=None, **kw: (dict(answer), None))

    out = llm.screen_references_with_llm("10.1/x", "T", "A",
                                         [_ref("10.1/orig", "Original")],
                                         classification=dict(_VERDICT_YES))

    assert out["resolved_doi_o"] == "10.1/orig"
    assert out["resolved_study_o"] == "2"


def test_classify_key_follows_the_voter_models(monkeypatch, tmp_path):
    """PR #97's precedent: the voter pair is part of the verdict, so a swapped voter
    must not read back the previous pair's answer."""
    calls: list = []
    _classify(monkeypatch, tmp_path, calls)
    llm.classify_replication("10.1/x", "Title", "Abstract")
    assert len(calls) == 2
    monkeypatch.setattr(llm, "SCREENING_MODEL_2", "some/other-voter")
    llm.classify_replication("10.1/x", "Title", "Abstract")
    assert len(calls) == 4                                   # re-voted, not replayed


def test_classify_key_follows_the_abstract(monkeypatch, tmp_path):
    calls: list = []
    _classify(monkeypatch, tmp_path, calls)
    llm.classify_replication("10.1/x", "Title", "Abstract")
    llm.classify_replication("10.1/x", "Title", "A backfilled, much longer abstract")
    assert len(calls) == 4


def _identify(monkeypatch, tmp_path, answer, calls):
    """Answer whichever provider LINKING_MODEL routes to.

    All three entry points get the same fake deliberately: these tests are about the
    prompt and the cache key, not about who serves them, and pinning one provider
    made every one of them fail the day LINKING_MODEL moved to another vendor.
    """
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def answered(prompt, model=None, **kw):
        calls.append(prompt)
        return dict(answer), None

    for name in ("call_gemini", "call_openai", "call_openrouter"):
        monkeypatch.setattr(llm, name, answered)


_DECLINE = {"targets": [], "unidentified_count": 0, "reasoning": "none"}

_PICK = {"targets": [{"key": "@smith2015", "match_certain": True,
                      "target_as_named": "Smith (2015)", "study_numbers": "",
                      "evidence_quote": "e"}],
         "unidentified_count": 0, "reasoning": "r"}

_CAND = [{"title": "Original", "year": 2015, "first_author": "Smith", "all_authors": ["Smith"],
          "doi": "10.1/orig", "openalex_id": "W1"}]


def test_identification_declines_are_cached(monkeypatch, tmp_path):
    """A decline is an answer. Caching only successes made every declined full-text
    call repay its API cost on every re-run."""
    calls: list = []
    _identify(monkeypatch, tmp_path, _DECLINE, calls)
    first = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])
    second = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])

    assert first["resolution_method"] == "llm_no_target"
    assert second["resolution_method"] == "llm_no_target"
    assert len(calls) == 1
    assert len(list(tmp_path.glob("llm_*.json"))) == 1


def test_identification_reports_the_target_study_numbers(monkeypatch, tmp_path):
    """The single-original path had no way to reach study_o: the model answered
    study_numbers and only the multi path wrote the column."""
    calls: list = []
    answer = {"targets": [dict(_PICK["targets"][0], study_numbers="Study 1, Exp 2")],
              "unidentified_count": 0, "reasoning": "r"}
    _identify(monkeypatch, tmp_path, answer, calls)
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])
    assert out["resolved_doi_o"] == "10.1/orig"
    assert out["resolved_study_o"] == "1, 2"


def test_identification_api_failure_is_not_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "call_gemini", lambda *a, **k: (None, "boom"))
    monkeypatch.setattr(llm, "call_openai", lambda *a, **k: (None, "boom"))
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "")
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])
    assert out["resolution_method"] == "llm_failed"
    assert not list(tmp_path.glob("llm_*.json"))


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
    rate_limit._next_call_at.clear()
    return c


def test_second_call_waits_only_the_remaining_interval(clock):
    llm._throttle("gemini")
    clock.now += 0.4
    llm._throttle("gemini")
    assert clock.slept == [pytest.approx(0.6)]


def test_a_provider_waits_only_on_its_own_last_call(clock):
    """The first call to a provider, a call to a different provider, and a call after
    the interval has already elapsed all go straight through."""
    llm._throttle("gemini")            # first call to gemini
    llm._throttle("openrouter")        # a different provider's bucket
    clock.now += 5.0
    llm._throttle("gemini")            # interval long since passed
    assert clock.slept == []


# ── The provider follows the model id ────────────────────────────────────────

@pytest.mark.parametrize("model,provider", [
    ("gemini-3.5-flash-lite",            "gemini"),
    ("gpt-5.4-mini",                     "openai"),
    ("mistralai/ministral-14b-2512",     "openrouter"),
])
def test_swapping_a_model_swaps_its_provider(monkeypatch, model, provider):
    """The model constants are named for the question they answer, so changing one
    may move the call to another vendor. Nothing may hardcode 'this call site goes to
    Gemini' — that turns a one-line config change into a 404 from the wrong API."""
    served: list = []
    for name in ("call_gemini", "call_openai", "call_openrouter"):
        monkeypatch.setattr(llm, name, (lambda who: (
            lambda p, model=None, **kw: (served.append(who), ({"ok": True}, ""))[1]))(name))

    result, got, err = llm.call_model("p", model)

    assert (llm.provider_for(model), got, result, err) == (provider, provider, {"ok": True}, "")
    assert served == [f"call_{provider}"]


# ── No fallback (audit C1) ───────────────────────────────────────────────────

def test_an_outage_at_its_provider_ends_the_target_call(monkeypatch, tmp_path):
    """The target pick is LINKING_MODEL's or nobody's. A ladder used to hand it on to
    the next provider and then to a cheap OpenRouter model, so an outage produced a
    link that looked exactly like a resolved one."""
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    serving = f"call_{llm.provider_for(llm.LINKING_MODEL)}"
    others: list = []
    for name in ("call_gemini", "call_openai", "call_openrouter"):
        monkeypatch.setattr(llm, name, (lambda who: (
            lambda p, model=None, **kw: (None, "429") if who == serving
            else (others.append(who), ({"ok": True}, ""))[1]))(name))

    out = _ident()

    assert others == []
    assert (out["resolved"], out["resolution_method"]) == (False, "llm_failed")
    assert out["llm_source"] == "none" and "429" in out["llm_error"]
    assert not list(tmp_path.glob("*.json")), "a provider failure must not be cached"


def _ident(**kw):
    return llm.identify_targets_with_llm("10.1/x", "T", "A", kw.pop("cands", _CAND), [], **kw)


def _reftarget():
    return llm.screen_references_with_llm("10.1/x", "T", "A", [_ref("10.1/orig", "Original")],
                                          classification=dict(_VERDICT_YES))


@pytest.mark.parametrize("axis,first,second", [
    # A different candidate pool is a different question.
    ("the candidate list",
     lambda mp: _ident(),
     lambda mp: _ident(cands=[dict(_CAND[0], title="Another", doi="10.1/other")])),
    # The abstract-stage call and the full-text call are different questions about
    # the same DOI; the old DOI-only key collided them.
    ("the parsed sections",
     lambda mp: _ident(abstract_only=True),
     lambda mp: _ident(intro="The PDF has since been parsed.")),
    # The version is folded in on its own account, not only via the rendered text.
    ("the prompt version",
     lambda mp: _ident(),
     lambda mp: (mp.setattr(llm, "prompt_version", lambda name: "ffffffffffff"),
                 _ident())[1]),
    # The one model that can answer is named by the key, at every rung.
    ("the model",
     lambda mp: _ident(),
     lambda mp: (mp.setattr(llm, "LINKING_MODEL", "gemini-next"), _ident())[1]),
    ("the model, at the reference-target stage",
     lambda mp: _reftarget(),
     lambda mp: (mp.setattr(llm, "LINKING_MODEL", "gemini-next"), _reftarget())[1]),
])
def test_changing_an_axis_of_the_key_re_asks(monkeypatch, tmp_path, axis, first, second):
    """A cache key must name everything the answer depends on: change any one of
    these and the model has to be asked again, not replayed."""
    calls: list = []
    _identify(monkeypatch, tmp_path, _PICK, calls)
    first(monkeypatch)
    second(monkeypatch)
    assert len(calls) == 2, f"the cache key must follow {axis}"


# ── The merged target prompt: acceptance and validation (§8.2) ───────────────
# The model names a @key; the DOI comes from the record that key maps to, in this
# call. Everything it says about the key is checked first — an invented key, a key
# repeated across two "different" targets and a self-reported certainty are each a
# way of writing a wrong original.

def _targets(monkeypatch, tmp_path, answer, calls=None):
    """As _identify: answer on whichever provider LINKING_MODEL routes to."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def answered(prompt, model=None, **kw):
        if calls is not None:
            calls.append(prompt)
        return dict(answer), None

    for name in ("call_gemini", "call_openai", "call_openrouter"):
        monkeypatch.setattr(llm, name, answered)


def _target(**over) -> dict:
    base = {"key": "@smith2015", "match_certain": True, "target_as_named": "Smith (2015)",
            "study_numbers": "", "evidence_quote": "q"}
    base.update(over)
    return base


def test_a_certain_pick_resolves_with_the_mapped_records_doi(monkeypatch, tmp_path):
    _targets(monkeypatch, tmp_path, {"targets": [_target()], "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [], abstract_only=True)

    assert out["resolved"] is True
    assert out["resolved_doi_o"] == "10.1/orig"
    assert out["llm_confidence"] == "high"
    assert out["resolution_method"] == (
        f"llm_cited_candidates_{llm.provider_for(llm.LINKING_MODEL)}")


def test_an_uncertain_pick_does_not_resolve_but_names_its_target(monkeypatch, tmp_path):
    """match_certain is the acceptance gate; a target seen but not matched still has
    to reach Stage 4.6, which searches for it by name."""
    _targets(monkeypatch, tmp_path,
             {"targets": [_target(key=None, match_certain=False,
                                  target_as_named="Ramirez (2014)")],
              "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])

    assert out["resolved"] is False
    assert out["target_as_named"] == "Ramirez (2014)"
    assert out["llm_confidence"] == "low"


def test_an_invented_key_is_demoted_not_obeyed(monkeypatch, tmp_path):
    _targets(monkeypatch, tmp_path, {"targets": [_target(key="@nosuch1999")],
                                     "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])

    assert out["resolved"] is False
    assert out["targets"][0]["key"] is None
    assert out["targets"][0]["match_certain"] is False
    assert "invented key" in out["llm_reasoning"]


def test_a_repeated_key_keeps_only_the_first_entry(monkeypatch, tmp_path):
    _targets(monkeypatch, tmp_path,
             {"targets": [_target(), _target(target_as_named="again")],
              "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])

    assert len(out["targets"]) == 1
    assert out["multi_target"] is False
    assert "duplicate key" in out["llm_reasoning"]


def test_two_targets_are_flagged_and_no_single_link_is_written(monkeypatch, tmp_path):
    cands = _CAND + [{"title": "Another original", "year": 2011, "first_author": "Jones",
                      "all_authors": ["Jones"], "doi": "10.1/second", "openalex_id": "W2"}]
    _targets(monkeypatch, tmp_path,
             {"targets": [_target(), _target(key="@jones2011")], "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", cands, [])

    assert out["multi_target"] is True
    assert out["resolved"] is False
    assert out["resolution_method"] == "llm_multi_target"


def test_a_shortfall_is_recorded_and_a_nonsense_count_becomes_zero(monkeypatch, tmp_path):
    """A model that names 28 studies and matches one has to say so in link_evidence
    rather than let the shortfall vanish — and a count that is not a number is no
    count at all."""
    _targets(monkeypatch, tmp_path,
             {"targets": [_target()], "stated_count": 28, "stated_count_unit": "studies",
              "unidentified_count": 5, "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])

    assert "stated_count=28 studies" in out["llm_evidence"]
    assert "unidentified=5" in out["llm_evidence"]
    assert out["unidentified_count"] == 5

    _targets(monkeypatch, tmp_path,
             {"targets": [], "unidentified_count": "several", "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/y", "T", "A", _CAND, [])

    assert out["unidentified_count"] == 0
    assert "unidentified_count not a number" in out["llm_reasoning"]


def test_the_two_stages_cache_separately(monkeypatch, tmp_path):
    """Same DOI, same rendered prompt, two stages: the reference screen's answer must
    not be replayed as the abstract stage's, or vice versa."""
    calls: list = []
    _targets(monkeypatch, tmp_path, {"targets": [_target()], "reasoning": "r"}, calls)
    llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [], abstract_only=True)
    llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [], cache_prefix="reftarget")
    llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [], abstract_only=False)

    assert len(calls) == 3
    assert len(list(tmp_path.glob("llm_*.json"))) == 2
    assert len(list(tmp_path.glob("reftarget_*.json"))) == 1


def test_the_key_follows_a_records_identity_not_just_its_rendered_line(monkeypatch, tmp_path):
    """The prompt shows a key, authors, a year and a title — never the DOI. But the
    link is built from the record the key maps to, so two lists that render
    identically while mapping the same key to a different DOI are different
    questions, and replaying the first answer writes the stale original."""
    calls: list = []
    _targets(monkeypatch, tmp_path, {"targets": [_target()], "reasoning": "r"}, calls)
    moved = [dict(_CAND[0], doi="10.1/corrected", openalex_id="W99")]

    first  = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])
    second = llm.identify_targets_with_llm("10.1/x", "T", "A", moved, [])

    assert calls[0] == calls[1]                      # the rendered prompt is identical
    assert len(calls) == 2                           # …and yet it was asked again
    assert first["resolved_doi_o"]  == "10.1/orig"
    assert second["resolved_doi_o"] == "10.1/corrected"


# ── The target list as the adapter consumes it (WP1) ─────────────────────────

def test_a_targets_mapped_record_survives_on_the_output(monkeypatch, tmp_path):
    """A @key is only meaningful against the key_map of the call that offered it, and
    run_extract has neither the candidates nor the parsed references to rebuild one."""
    _targets(monkeypatch, tmp_path, {"targets": [_target()], "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])

    assert out["targets"][0]["record"]["doi"] == "10.1/orig"


def test_a_cached_entry_written_without_a_record_is_repaired(monkeypatch, tmp_path):
    """The output shape changed without the cache key changing, so entries written by
    the previous shape come back record-less — and every target in them would be
    dropped as unmatched."""
    calls: list = []
    _targets(monkeypatch, tmp_path, {"targets": [_target()], "reasoning": "r"}, calls)
    llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])

    entry = next(tmp_path.glob("llm_*.json"))
    stored = json.loads(entry.read_text())
    for t in stored["targets"]:
        assert t.pop("record")
    entry.write_text(json.dumps(stored))

    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])
    assert len(calls) == 1                                   # served from cache
    assert out["targets"][0]["record"]["doi"] == "10.1/orig"


def test_target_stage_names_the_rung_that_produced_a_multi_target_answer(monkeypatch, tmp_path):
    """resolution_method on a two-target answer is llm_multi_target, which names no
    rung — but the rows the adapter writes need an honest link_method."""
    cands = _CAND + [{"title": "Another original", "year": 2011, "first_author": "Jones",
                      "all_authors": ["Jones"], "doi": "10.1/second", "openalex_id": "W2"}]
    _targets(monkeypatch, tmp_path,
             {"targets": [_target(), _target(key="@jones2011")], "reasoning": "r"})

    ref = llm.identify_targets_with_llm("10.1/x", "T", "A", cands, [],
                                        cache_prefix="reftarget")
    abstract = llm.identify_targets_with_llm("10.1/x", "T", "A", cands, [],
                                             abstract_only=True)

    assert ref["resolution_method"] == "llm_multi_target"
    assert ref["target_stage"] == "llm_references"
    # The stage still carries the answering provider as a suffix — a ladder leftover,
    # now a pure function of LINKING_MODEL. run_extract._map_method has a row per
    # provider, so the suffix must follow the model rather than be spelled out here.
    assert abstract["target_stage"] == (
        f"llm_cited_candidates_{llm.provider_for(llm.LINKING_MODEL)}")


def test_the_replications_own_study_numbers_are_cleaned(monkeypatch, tmp_path):
    """study_r is the counterpart of study_o: which study of THIS paper re-tests the
    original. Models answer it in prose, exactly as they do study_numbers."""
    _targets(monkeypatch, tmp_path,
             {"targets": [_target(replication_study_numbers="Study 1, Experiment 2")],
              "reasoning": "r"})
    out = llm.identify_targets_with_llm("10.1/x", "T", "A", _CAND, [])

    assert out["resolved_study_r"] == "1, 2"


class TestStudyNumberCleaning:
    def test_prose_forms_reduce_to_a_number(self):
        from shared.llm_client import _clean_study_number
        assert _clean_study_number("Study 2") == "2"
        assert _clean_study_number("Experiment 3a") == "3a"
        assert _clean_study_number(2) == "2"

    def test_absent_or_unparseable_is_empty(self):
        from shared.llm_client import _clean_study_number
        assert _clean_study_number(None) == ""
        assert _clean_study_number("the main study") == ""

    def test_every_named_study_survives_the_answer(self):
        """Splitting on commas alone kept the first number and dropped the rest, so a
        target of two studies was recorded as a target of one."""
        from shared.llm_client import _clean_study_numbers
        assert _clean_study_numbers("Study 1; Study 4") == "1, 4"
        assert _clean_study_numbers("2 and 3")          == "2, 3"
        assert _clean_study_numbers("1 & 2")            == "1, 2"
        assert _clean_study_numbers("Studies 1-3")      == "1, 2, 3"
        assert _clean_study_numbers("Studies 1–3")      == "1, 2, 3"

    def test_repeats_collapse_and_prose_still_reduces(self):
        from shared.llm_client import _clean_study_numbers
        assert _clean_study_numbers("Study 1, Experiment 1") == "1"
        assert _clean_study_numbers("Experiment 3a, 3b")     == "3a, 3b"
        assert _clean_study_numbers("the main study")        == ""
        assert _clean_study_numbers("")                      == ""


# ── Every billed response is recorded, truncated ones above all ───────────────
# A response cut off at the output cap is the MOST expensive shape a reasoning
# model produces — it ran to the ceiling. Gemini returned before _record_tokens on
# both the MAX_TOKENS and the no-candidates branch, and OpenRouter checked the
# length before recording, so the two providers under-reported exactly the calls
# that cost the most while OpenAI reported them.

def _gemini_truncated():
    r = MagicMock()
    r.status_code = 200
    r.text = ""
    r.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "{\"partial\""}]},
                        "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {"promptTokenCount": 900, "totalTokenCount": 5000},
    }
    return r


def _gemini_blocked():
    r = MagicMock()
    r.status_code = 200
    r.text = ""
    r.json.return_value = {
        "candidates": [],
        "promptFeedback": {"blockReason": "SAFETY"},
        "usageMetadata": {"promptTokenCount": 900, "totalTokenCount": 900},
    }
    return r


@pytest.mark.parametrize("response,expected", [
    (_gemini_truncated, ("gemini", "m", 900, 4100)),
    (_gemini_blocked,   ("gemini", "m", 900, 0)),
], ids=["truncated", "blocked"])
def test_gemini_records_tokens_on_every_billed_response(monkeypatch, response, expected):
    _flex_env(monkeypatch, use_flex=False, keys=("k1",))
    recorded: list = []
    monkeypatch.setattr(llm, "_record_tokens", lambda *a: recorded.append(a))
    monkeypatch.setattr(llm.requests, "post",
                        lambda url, json=None, timeout=None: response())
    result, err = llm.call_gemini("prompt", model="m")
    assert result is None and err
    assert recorded == [expected]


def test_openrouter_records_a_truncated_response(monkeypatch):
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "or-test")
    recorded: list = []
    monkeypatch.setattr(llm, "_record_tokens", lambda *a: recorded.append(a))

    cut_off = _resp('{"partial"')
    cut_off.usage = MagicMock(prompt_tokens=900, completion_tokens=4096)
    cut_off.choices[0].finish_reason = "length"
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = cut_off
    with patch("openai.OpenAI", return_value=fake_client):
        result, err = llm.call_openrouter("prompt", model="v/m")

    assert result is None and "truncated" in err
    assert recorded == [("openrouter", "v/m", 900, 4096)]


def test_openrouter_retries_three_times_like_openai(monkeypatch):
    """OpenRouter had one attempt where OpenAI had three, so a single blip at the
    provider serving the pre-screen or a screen slot was the row's answer."""
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "or-test")
    sleeps: list = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient 503")
        return _resp('{"ok": true}')

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = create
    with patch("openai.OpenAI", return_value=fake_client):
        assert llm.call_openrouter("prompt", model="v/m") == ({"ok": True}, "")
    assert calls["n"] == 3
    assert sleeps == [1, 2]

    calls["n"] = -10        # never reaches the success branch
    with patch("openai.OpenAI", return_value=fake_client):
        result, err = llm.call_openrouter("prompt", model="v/m")
    assert result is None and "transient 503" in err


def test_gemini_retries_three_times_like_the_other_providers(monkeypatch):
    """Gemini retried twice at a flat 3s where OpenAI and OpenRouter make three
    attempts at 1s/2s — the same transient outage decided a row differently
    depending on which provider the model id routed to. Key rotation is a separate
    axis: a 429 is not transient on this key, so it moves on without spending the
    retries."""
    _flex_env(monkeypatch, use_flex=False, keys=("k1", "k2"))
    sleeps: list = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    urls: list = []

    def post(url, json=None, timeout=None):
        urls.append(url)
        if "k1" in url:                     # quota gone — next key, no retries here
            r = _gemini_ok()
            r.status_code = 429
            return r
        if len(urls) < 4:                   # two transient failures on the good key
            r = _gemini_ok()
            r.status_code = 503
            return r
        return _gemini_ok()

    monkeypatch.setattr(llm.requests, "post", post)
    assert llm.call_gemini("prompt", model="m") == ({"ok": True}, "")
    assert len(urls) == 4                   # 1 quota refusal + 3 attempts on key 2
    assert sleeps == [1, 2]


def test_every_provider_sends_the_same_output_cap(monkeypatch):
    """The cap decides what a model is ABLE to answer, so the same prompt must not
    get a different ceiling for being served by Gemini rather than OpenAI."""
    _flex_env(monkeypatch, use_flex=False, keys=("k1",))
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "OPENAI_USE_FLEX", False)
    monkeypatch.setattr(llm, "OPENROUTER_API_KEY", "or-test")
    monkeypatch.setattr(llm.token_usage, "check_openai_budget", lambda: None)
    posts: list = []
    monkeypatch.setattr(llm.requests, "post",
                        lambda url, json=None, timeout=None: (posts.append(dict(json)),
                                                              _gemini_ok())[1])

    kwargs: list = []
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = \
        lambda **kw: (kwargs.append(kw), _resp('{"ok": true}'))[1]

    llm.call_gemini("prompt", model="m")
    llm.call_gemini_with_pdf("prompt", b"%PDF-1.4")
    llm.call_gemini_with_images("prompt", _IMGS)
    with patch("openai.OpenAI", return_value=fake_client):
        llm.call_openai("prompt", model="m")
        llm.call_openrouter("prompt", model="v/m")

    cap = llm.JSON_MAX_OUTPUT_TOKENS
    assert [p["generationConfig"]["maxOutputTokens"] for p in posts] == [cap] * 3
    assert kwargs[0]["max_completion_tokens"] == cap
    assert kwargs[1]["max_tokens"] == cap


# ── A block is not an answer about the document ──────────────────────────────
# The document call sites (shared/grobid.py) cache what they are told, so "Gemini
# returned nothing" being an ANSWER meant a safety block was recorded, permanently,
# as "this paper has no reference list".

def _gemini_body(body: dict):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = body
    return r


@pytest.mark.parametrize("body,why", [
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "blockReason=SAFETY"),
    ({}, "blockReason=unknown"),
    ({"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]},
     "finishReason=SAFETY"),
], ids=["prompt-blocked", "no-candidates", "candidate-blocked"])
@pytest.mark.parametrize("call_site", [
    lambda: llm.call_gemini_with_pdf("prompt", b"%PDF-1.4"),
    lambda: llm.call_gemini_with_images("prompt", _IMGS),
], ids=["pdf", "images"])
def test_a_blocked_document_call_reports_an_error_not_an_empty_answer(
        monkeypatch, call_site, body, why):
    _flex_env(monkeypatch, use_flex=False, keys=("k1",))
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm.requests, "post",
                        lambda url, json=None, timeout=None: _gemini_body(body))

    result, err = call_site()
    assert result is None
    assert why in err   # non-empty: grobid raises ReferenceExtractionUnavailable


def test_an_empty_reference_list_is_still_an_answer(monkeypatch):
    """The distinction only works if the model saying "no references here" — a
    parsed JSON list that happens to be empty — still comes back with no error."""
    _flex_env(monkeypatch, use_flex=False, keys=("k1",))
    monkeypatch.setattr(llm.requests, "post", lambda url, json=None, timeout=None:
                        _gemini_body({"candidates": [{"finishReason": "STOP", "content": {
                            "parts": [{"text": '{"references": []}'}]}}]}))
    assert llm.call_gemini_with_pdf("prompt", b"%PDF") == ({"references": []}, "")


# ── The classify cache equivalence names ONE voter pair ──────────────────────

def _legacy_keys(monkeypatch, model_2: str, effort_2: str) -> list[str]:
    monkeypatch.setattr(llm, "SCREENING_MODEL_2", model_2)
    monkeypatch.setattr(llm, "SCREENING_EFFORT_2", effort_2)
    return llm._classify_keys("10.1/r", "", "an abstract")[1]


def test_the_classify_equivalence_is_offered_only_to_the_pair_it_names(monkeypatch):
    """Declared equivalences (issue #171) are a statement about one computation. Held
    unconditionally, a re-screen at a new voter or effort would be served every legacy
    entry from the OLD pair and re-file it under the new pair's key — a rescreen that
    completes instantly, costs nothing and changes no verdict."""
    assert llm._CLASSIFY_EQUIVALENT_VOTER_ID == ("gemini-3.5-flash-lite@effort=minimal"
                                                 "+gpt-5.4-mini@effort=low")

    offered = _legacy_keys(monkeypatch, "gpt-5.4-mini", "low")
    assert len(offered) == len(llm._CLASSIFY_LEGACY_KEY_PARTS)

    assert _legacy_keys(monkeypatch, "gpt-5.4-mini", "medium") == []     # effort moved
    assert _legacy_keys(monkeypatch, "mistralai/ministral-14b-2512", "low") == []  # model moved


def test_a_changed_voter_pair_does_not_read_the_old_pairs_verdict(monkeypatch, tmp_path):
    """The same thing end to end: what the cache actually hands back."""
    monkeypatch.setattr(llm, "LLM_CACHE_DIR", tmp_path)
    legacy = _legacy_keys(monkeypatch, "gpt-5.4-mini", "low")
    write_cache(tmp_path, legacy[0], {"screen_verdict": "discard"})

    assert llm.cached_classification("10.1/r", "", "an abstract")["screen_verdict"] == "discard"

    monkeypatch.setattr(llm, "SCREENING_EFFORT_2", "medium")
    assert llm.cached_classification("10.1/r", "", "an abstract") is None
