"""
llm_client.py — the pipeline's LLM calls: provider ladder, target identification,
and the two-model replication screen.

Provider ladder: Gemini → OpenAI → OpenRouter, in call_llm_ladder(). Each provider
is rate-limited against its own last-call timestamp.

Public API:
    identify_targets_with_llm(doi_r, study_r, abstract_r, candidates,
                              references, …) → dict
"""
import base64
import json
import re
import time
from pathlib import Path
from typing import Optional

import requests

from .config import (
    GEMINI_API_KEYS, GEMINI_MODEL, GEMINI_LIGHT_MODEL, GEMINI_HEAVY_MODEL,
    GEMINI_USE_FLEX, GEMINI_FLEX_TIMEOUT, GEMINI_PAID_KEYS, GEMINI_RATE_SEC,
    GEMINI_THINKING_LEVEL,
    LLM_CACHE_DIR,
    OPENAI_API_KEY, OPENAI_MODEL, OPENAI_RATE_SEC,
    OPENAI_USE_FLEX, OPENAI_FLEX_TIMEOUT,
    OPENROUTER_API_KEY, OPENROUTER_HEAVY_MODEL, OPENROUTER_RATE_SEC,
    SCREEN_VOTER2_MODEL,
    log,
)
from . import token_counter, token_usage
from .cache import content_key, read_cache, write_cache
from .prompts import (
    JSON_SYSTEM_MESSAGE,
    build_classify_prompt, build_target_prompt,
    prompt_version,
)
from .target_keys import assign_target_keys
from .utils import clean_doi

# Output cap for the JSON-returning chat calls. It was 1024, which on a reasoning
# model (gpt-5-mini) also has to cover hidden reasoning tokens — while the outcome
# prompts ask for a quote of up to ~1200 characters, ~300 tokens of visible output
# before any other field. A truncated response is not valid JSON, so it was
# indistinguishable from a parse failure and got retried rather than reported;
# both call sites now log it explicitly.
JSON_MAX_OUTPUT_TOKENS = 4096

# Sampling temperature is deliberately not set on any provider. gpt-5-mini accepts
# only the default ("Unsupported value: 'temperature' does not support 0.0 with this
# model"), so pinning 0.0 on Gemini and OpenRouter — as this module used to — meant a
# row coded by the OpenAI leg was not comparable to one coded by Gemini. Note the
# consequence: Gemini and OpenRouter now sample at their defaults rather than
# greedily, so repeat runs are less deterministic than before.

# ── Token accounting ──────────────────────────────────────────────────────────
# Every provider call reports what it cost in its usage field. That number goes to
# two places: token_counter, which attributes it to a pipeline stage for the
# end-of-run summary, and token_usage, which persists it per day, provider and model
# with input and output kept apart. Only call_openai also checks a ceiling — see
# OPENAI_DAILY_TOKEN_BUDGET; the other providers are recorded, not capped.


def _record_tokens(provider: str, model: str, n_in: int, n_out: int) -> None:
    """Charge a completed call to the run's stage total and the day's usage record."""
    token_counter.record(provider, n_in + n_out)
    token_usage.record(provider, model, n_in, n_out)


def _gemini_usage(body: dict) -> tuple[int, int]:
    """(input, output) tokens from a Gemini response body.

    Output is the total minus the prompt rather than candidatesTokenCount: thinking
    tokens are billed as output and are only in the total. A response without
    usageMetadata yields (0, 0), which records nothing.
    """
    u     = body.get("usageMetadata") or {}
    n_in  = int(u.get("promptTokenCount", 0) or 0)
    total = int(u.get("totalTokenCount", 0) or 0)
    n_out = (total - n_in) if total else int(u.get("candidatesTokenCount", 0) or 0)
    return n_in, max(n_out, 0)


# ── Per-provider rate limiting ────────────────────────────────────────────────
# Each provider is throttled against its OWN last-call timestamp, immediately
# before the request goes out. Charging the wait to the caller after a successful
# call — as every call site used to — made the delay unconditional even when the
# next call went to a different provider, or was served from cache, or never came.

_PROVIDER_RATE_SEC = {
    "gemini":     GEMINI_RATE_SEC,
    "openai":     OPENAI_RATE_SEC,
    "openrouter": OPENROUTER_RATE_SEC,
}
_last_call_at: dict[str, float] = {}


def _throttle(provider: str) -> None:
    """Sleep only as long as this provider's minimum interval still has to run."""
    interval = _PROVIDER_RATE_SEC.get(provider, 1.0)
    last     = _last_call_at.get(provider)
    if last is not None:
        remaining = interval - (time.monotonic() - last)
        if remaining > 0:
            time.sleep(remaining)
    _last_call_at[provider] = time.monotonic()


# ── JSON parsing (handles markdown-fenced output) ─────────────────────────────

_WS_RE = re.compile(r"\s+")


def _parse_llm_json(text: str) -> Optional[dict]:
    """
    Parse a JSON dict from an LLM response.
    Strips markdown fences (```json ... ```) before parsing.
    Falls back to extracting the first {...} block.
    """
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$",          "", text).strip()
    # A trailing comma before } or ] was the only malformed-response mode seen in
    # ~2,100 screening-validation calls (6 cases), and json.loads rejects it.
    text = re.sub(r",\s*([}\]])", r"\1", text)

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            result = json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None


# ── Gemini flex inference ─────────────────────────────────────────────────────
# Flex costs 50% less (identical to Batch pricing) but is only offered on paid
# keys, so the decision follows which key is in use — GEMINI_PAID_KEYS — rather
# than the key's position in the rotation.

def _gemini_use_flex(key_idx: int) -> bool:
    return GEMINI_USE_FLEX and (key_idx + 1) in GEMINI_PAID_KEYS


def _gemini_post(url: str, payload: dict, key_idx: int, base_timeout: int):
    """
    POST to Gemini, adding service_tier=flex when the key in use is paid.

    Flex requests can queue for up to 15 minutes, so they get GEMINI_FLEX_TIMEOUT
    instead of the call site's standard timeout. If the API rejects the flex tier
    (a model or key that does not offer it), the same request is retried once at
    standard tier rather than losing the row.
    """
    use_flex = _gemini_use_flex(key_idx)
    if use_flex:
        payload["service_tier"] = "flex"
    else:
        payload.pop("service_tier", None)

    r = requests.post(url, json=payload,
                      timeout=GEMINI_FLEX_TIMEOUT if use_flex else base_timeout)
    if use_flex and r.status_code == 400 and "service_tier" in r.text.replace("serviceTier", "service_tier"):
        log.warning("Gemini rejected service_tier=flex on key %d — retrying at standard tier",
                    key_idx + 1)
        payload.pop("service_tier", None)
        r = requests.post(url, json=payload, timeout=base_timeout)
    return r


# ── Gemini thinking level ─────────────────────────────────────────────────────
# thinkingLevel is a spend lever on the heavy model — thinking tokens are billed as
# output — but unlike flex it changes the answer, so it cannot be switched on
# silently over a cache full of answers produced without it. Two functions keep the
# request and the cache key in step: thinking_level() decides what is sent, and
# cache_model_id() is the ONLY way a Gemini model string enters a cache key. Fold it
# in one place and no call site can forget it.

def thinking_level(model: str) -> str:
    """The thinkingLevel to send for *model* — set only for the heavy model.

    The heavy model is where the thinking bill is, and the only Gemini model whose
    answers are keyed by ladder_fingerprint(). The PDF and image calls do not consult
    this at all: they are cached by GROBID filenames that name the bare model string,
    so a level applied there would not be named by the key that stores the answer.
    """
    return GEMINI_THINKING_LEVEL if (GEMINI_THINKING_LEVEL and model == GEMINI_HEAVY_MODEL) else ""


def cache_model_id(model: str) -> str:
    """The model string as a cache key names it — with any active thinking level.

    An answer produced at thinking_level=minimal is a different answer from one
    produced at the model's default, so the two must not share a cache entry.
    """
    level = thinking_level(model)
    return f"{model}@thinking={level}" if level else model


# ── Gemini (primary) ──────────────────────────────────────────────────────────

def call_gemini(prompt: str, model: str = GEMINI_MODEL) -> tuple[Optional[dict], str]:
    """
    Call Gemini via the REST API with responseMimeType=application/json.

    Rotates through all keys in GEMINI_API_KEYS when a 429 (quota exhausted)
    is returned — useful when running on multiple free-tier projects.
    Retries once on transient 500/503 within each key.

    Returns (result_dict_or_None, error_description).
    """
    if not GEMINI_API_KEYS:
        log.warning("No GEMINI_API_KEY set — skipping Gemini")
        return None, "no API keys configured"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            # temperature is deliberately NOT set — see the note by
            # JSON_MAX_OUTPUT_TOKENS. gpt-5-mini rejects any explicit value, so
            # pinning it here would make Gemini-coded and OpenAI-coded rows differ
            # by sampling policy as well as by model.
            "responseMimeType": "application/json",
            "maxOutputTokens" : 8192,
            # Note: thinkingConfig is intentionally omitted.
            # Setting thinkingBudget:0 while also using responseMimeType:
            # application/json causes gemini-3-flash-preview to return a
            # non-200 error or empty candidates, so all calls fell through to
            # OpenAI.  Letting the model use its default thinking mode fixes this.
        },
    }

    level = thinking_level(model)
    if level:
        # thinkingLevel (gemini-3): "minimal" buys back most of the output bill on
        # the heavy model. Safe alongside responseMimeType, unlike thinkingBudget:0.
        payload["generationConfig"]["thinkingLevel"] = level

    if GEMINI_USE_FLEX:
        log.debug("Gemini flex inference enabled on paid keys %s (timeout=%ds)",
                  sorted(GEMINI_PAID_KEYS), GEMINI_FLEX_TIMEOUT)

    last_error = "all keys exhausted"
    for key_idx, api_key in enumerate(GEMINI_API_KEYS):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
               f":generateContent?key={api_key}")
        key_label = f"key {key_idx + 1}/{len(GEMINI_API_KEYS)}"

        for attempt in range(2):
            try:
                _throttle("gemini")
                r = _gemini_post(url, payload, key_idx, 90)

                if r.status_code == 429:
                    last_error = f"quota exhausted on {key_label} (429)"
                    print(f"  [Gemini] {key_label} quota exhausted (429) — "
                          f"{'trying next key' if key_idx + 1 < len(GEMINI_API_KEYS) else 'no more keys'}")
                    log.warning("Gemini quota exhausted on %s", key_label)
                    break   # break inner retry loop → next key

                if r.status_code == 404:
                    # Model not found — changing keys won't help; bail out immediately.
                    err_msg = r.json().get("error", {}).get("message", r.text[:200])
                    log.error(
                        "Gemini model not found: %s — update GEMINI_LIGHT_MODEL or "
                        "GEMINI_HEAVY_MODEL in .env. API said: %s", model, err_msg
                    )
                    return None, f"model not found: {model}"

                if r.status_code in (500, 503) and attempt == 0:
                    last_error = f"HTTP {r.status_code} on {key_label} (retrying)"
                    log.debug("Gemini transient %s on %s, retrying…", r.status_code, key_label)
                    time.sleep(3)
                    continue

                if r.status_code != 200:
                    last_error = f"HTTP {r.status_code} on {key_label}: {r.text[:200]}"
                    print(f"  [Gemini] {key_label} HTTP {r.status_code}: {r.text[:400]}")
                    log.warning("Gemini HTTP %s for %s model=%s", r.status_code, key_label, model)
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    break   # non-retryable error on this key → try next

                body = r.json()
                if not body.get("candidates"):
                    blocked = body.get("promptFeedback", {}).get("blockReason", "unknown")
                    last_error = f"no candidates on {key_label} — blockReason={blocked}"
                    print(f"  [Gemini] {key_label} no candidates — blockReason={blocked}")
                    return None, last_error

                if body["candidates"][0].get("finishReason") == "MAX_TOKENS":
                    last_error = "response truncated at maxOutputTokens"
                    log.warning("Gemini response hit maxOutputTokens and was cut off — "
                                "the truncated JSON will fail to parse (model=%s, %s)",
                                model, key_label)
                    return None, last_error

                text = body["candidates"][0]["content"]["parts"][0]["text"]
                # Recorded before parsing: a non-JSON response was still billed.
                _record_tokens("gemini", model, *_gemini_usage(body))
                result = _parse_llm_json(text)
                if result is not None:
                    if key_idx > 0:
                        log.info("Gemini succeeded on %s", key_label)
                    return result, ""

                last_error = f"non-JSON response on {key_label}: {text[:150]}"
                print(f"  [Gemini] {key_label} non-JSON response: {text[:200]}")
                log.warning("Gemini returned non-JSON: %.200s", text)
                return None, last_error

            except Exception as e:
                last_error = f"exception on {key_label} attempt {attempt+1}: {e}"
                print(f"  [Gemini] {key_label} exception (attempt {attempt+1}): {e}")
                log.warning("Gemini call failed on %s (attempt %d): %s", key_label, attempt + 1, e)
                if attempt == 0:
                    time.sleep(3)

    return None, last_error


# ── OpenAI (fallback) ─────────────────────────────────────────────────────────
# Flex inference, the mirror of the Gemini path above: 50% off standard pricing in
# exchange for queueing. Flex is an account-level tier rather than a per-key one, so
# OPENAI_USE_FLEX alone decides it.

_SERVICE_TIER_TEXT = re.compile(r"service[_ ]?tier", re.IGNORECASE)


def _openai_flex_refused(exc: Exception) -> bool:
    """Whether *exc* means "not at flex tier" rather than "the call failed".

    Read off the SDK's structured error fields, never off free text: only two
    shapes say the tier itself was unavailable, and both are answerable by an
    immediate standard-tier call because neither was served or billed.

      * 429 with error code `resource_unavailable` — the flex queue has no
        capacity.
      * 400 naming `service_tier` as the offending parameter — flex is not
        offered for this model or account.

    Everything else is a failed call, not a refused tier. A client-side timeout
    in particular is NOT a refusal: a flex request can be served and billed
    close to OPENAI_FLEX_TIMEOUT with the response lost in transit, so resending
    it immediately would double-bill and record only the second call's usage. It
    goes to the ordinary retry loop like any other transient failure.
    """
    status = getattr(exc, "status_code", None)
    if status not in (400, 429):
        return False

    body = getattr(exc, "body", None)
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        err = {}
    code = getattr(exc, "code", None) or err.get("code")
    param = getattr(exc, "param", None) or err.get("param")

    if status == 429:
        return code == "resource_unavailable"
    # 400: the param field is the authority; the message is a secondary guard for
    # the same shape, where the SDK could not populate param.
    return (param == "service_tier"
            or (not param
                and bool(_SERVICE_TIER_TEXT.search(str(err.get("message") or "")))))


def call_openai(prompt: str, model: str = OPENAI_MODEL,
                reasoning_effort: str = "") -> tuple[Optional[dict], str]:
    """
    Call OpenAI chat completion with response_format=json_object.

    reasoning_effort — passed through only when set, so the reasoning models bill
    hidden thinking at the caller's chosen budget. The screen sends "low": its
    output is a short JSON and the eval that validated the voter pair ran it that
    way. Callers that omit it keep the API default.

    Returns (result_dict_or_None, error_description).
    """
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — skipping OpenAI")
        return None, "OPENAI_API_KEY not configured"

    token_usage.check_openai_budget()

    import openai
    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    # #45: 3 attempts with exponential backoff (1s, 2s) per the api_error contract, so a
    # transient outage does not immediately poison a row after a single failure. call_gemini
    # already retries; this brings the OpenAI fallback to the same contract.
    last_error = "no attempts made"
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}

    def _create(flex: bool):
        """One request, at flex tier or standard. Flex calls queue, so they get
        OPENAI_FLEX_TIMEOUT instead of the client default."""
        tier = ({"service_tier": "flex", "timeout": OPENAI_FLEX_TIMEOUT} if flex else {})
        return client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": JSON_SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=JSON_MAX_OUTPUT_TOKENS,
            **tier,
            **extra,
        )

    # Once a flex request has failed for any reason, the retries run at standard
    # tier: a refused tier will refuse again, and after a timeout a second 900s
    # queue wait is the worst way to find out whether the call went through.
    use_flex = OPENAI_USE_FLEX
    for attempt in range(3):
        try:
            _throttle("openai")
            try:
                response = _create(use_flex)
            except Exception as flex_exc:
                if not use_flex:
                    raise
                use_flex = False
                # The standard-tier call replaces the refused flex call within this
                # attempt rather than consuming one of the three: a tier that was
                # never served is not a transient outage, and nothing was billed.
                # Anything else — a timeout above all — may already have been
                # billed, so it takes the retry loop instead.
                if not _openai_flex_refused(flex_exc):
                    raise
                log.warning("OpenAI would not serve service_tier=flex (%s) — "
                            "retrying at standard tier", flex_exc)
                response = _create(False)
            if response.usage:
                _record_tokens("openai", model,
                               response.usage.prompt_tokens,
                               response.usage.completion_tokens)
                log.debug("OpenAI usage: +%d tokens (day total: %d)",
                          response.usage.total_tokens, token_usage.spent("openai"))
            if response.choices[0].finish_reason == "length":
                log.warning("OpenAI response hit the %d-token cap and was cut off — "
                            "the truncated JSON will fail to parse (model=%s)",
                            JSON_MAX_OUTPUT_TOKENS, model)
                return None, "response truncated at max_completion_tokens"
            result = _parse_llm_json(response.choices[0].message.content)
            return result, ("" if result else "response was not valid JSON")
        except Exception as e:
            last_error = f"exception: {e}"
            log.warning("OpenAI call failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s
    return None, last_error


# ── OpenRouter (OpenAI-compatible alternative LLMs) ──────────────────────────

def call_openrouter(prompt: str, model: str = "") -> tuple[Optional[dict], str]:
    """
    Call any model available on OpenRouter via the OpenAI-compatible API.

    model — OpenRouter model ID e.g. "qwen/qwen3-30b-a3b".
            Defaults to OPENROUTER_HEAVY_MODEL from config.

    Returns (result_dict_or_None, error_description).
    """
    if not OPENROUTER_API_KEY:
        return None, "OPENROUTER_API_KEY not configured"

    import openai
    client = openai.OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    use_model = model or OPENROUTER_HEAVY_MODEL
    try:
        _throttle("openrouter")
        response = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": JSON_SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=JSON_MAX_OUTPUT_TOKENS,
        )
        if response.choices[0].finish_reason == "length":
            log.warning("OpenRouter response hit the %d-token cap and was cut off — "
                        "the truncated JSON will fail to parse (model=%s)",
                        JSON_MAX_OUTPUT_TOKENS, use_model)
            return None, "response truncated at max_tokens"
        if response.usage:
            _record_tokens("openrouter", use_model,
                           response.usage.prompt_tokens,
                           response.usage.completion_tokens)
        result = _parse_llm_json(response.choices[0].message.content)
        return result, ("" if result else "response was not valid JSON")
    except Exception as e:
        log.warning("OpenRouter call failed (model=%s): %s", use_model, e)
        return None, f"exception: {e}"


# ── Unified LLM router ───────────────────────────────────────────────────────

def ladder_fingerprint(gemini_model: str = "", openai_model: str = "",
                       openrouter: bool = True) -> str:
    """Every model a call_llm_ladder with these arguments could be answered by.

    Belongs in a cache key: the ladder falls through to OpenAI and OpenRouter, so
    a key naming only the Gemini model would let one model's answer be replayed as
    another's the next time Gemini is down. The Gemini model goes through
    cache_model_id(), which is where an active thinking level enters the key — every
    heavy-model cache key in the pipeline is built from this fingerprint.
    """
    from .config import GEMINI_LIGHT_MODEL as _LIGHT

    models = [cache_model_id(gemini_model or _LIGHT), openai_model or OPENAI_MODEL]
    if openrouter:
        models.append(OPENROUTER_HEAVY_MODEL)
    return "|".join(models)


def call_llm_ladder(prompt: str, gemini_model: str = "", openai_model: str = "",
                    prefer_openai: bool = False,
                    openrouter: bool = True) -> tuple[Optional[dict], str, str, str]:
    """
    Route a prompt through the configured provider chain and return the first
    successful result, naming the provider that produced it.

    Default order : Gemini -> OpenAI -> OpenRouter (Qwen as last resort).
    prefer_openai : flip to OpenAI -> Gemini -> OpenRouter.
                    Use when Gemini is overloaded (503/429) and OpenAI is preferred.
    openrouter    : False stops the ladder after OpenAI. For calls whose answer is
                    only trustworthy from a strong model.

    gemini_model — Gemini model to use (defaults to GEMINI_LIGHT_MODEL).
    openai_model — OpenAI model to use (defaults to OPENAI_MODEL).

    Returns (result_dict_or_None, provider, model_used, error_description).
    provider is one of gemini | openai | openrouter, or "none" when every
    provider failed; model_used is the exact model string that answered.
    """
    from .config import GEMINI_LIGHT_MODEL as _LIGHT

    g_model = gemini_model or _LIGHT
    o_model = openai_model or OPENAI_MODEL
    errs: dict[str, str] = {}

    order = (("openai", "gemini") if prefer_openai else ("gemini", "openai"))
    for provider in order + ("openrouter",):
        if provider == "openrouter" and (not openrouter or not OPENROUTER_API_KEY):
            continue
        if provider == "gemini":
            result, errs["Gemini"] = call_gemini(prompt, model=g_model)
            model = g_model
        elif provider == "openai":
            result, errs["OpenAI"] = call_openai(prompt, model=o_model)
            model = o_model
        else:
            result, errs["OpenRouter"] = call_openrouter(prompt)
            model = OPENROUTER_HEAVY_MODEL
        if result:
            return result, provider, model, ""

    return None, "none", "", " | ".join(f"{k}: {v}" for k, v in errs.items())


def call_llm(prompt: str, gemini_model: str = "", openai_model: str = "",
             prefer_openai: bool = False) -> tuple[Optional[dict], str, str]:
    """Provider ladder without the provider name — see call_llm_ladder.

    Returns (result_dict_or_None, model_used, error_description).
    model_used is the exact model string that answered, or "" if all providers failed.
    """
    result, _provider, model, err = call_llm_ladder(
        prompt, gemini_model=gemini_model, openai_model=openai_model,
        prefer_openai=prefer_openai)
    return result, model, err


# ── Main dispatcher ───────────────────────────────────────────────────────────

def _validate_targets(raw: list, key_map: dict[str, dict],
                      prompt: str) -> tuple[list[dict], list[str]]:
    """Check every target the model returned against this call's key namespace.

    Nothing the model says about a key is trusted: an invented key is demoted to an
    unmatched target rather than dropped (the paper may still re-test something), a
    repeated key keeps only the first entry, and a quote that is not in the text we
    sent is recorded but not grounds for rejection — parsers reflow whitespace and
    ligatures, so a strict quote gate would discard correct picks.
    """
    sent  = _WS_RE.sub(" ", prompt)
    seen: set[str] = set()
    targets: list[dict] = []
    notes:   list[str]  = []

    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        certain = item.get("match_certain")
        certain = certain is True or str(certain).strip().lower() == "true"
        if key and key not in key_map:
            notes.append(f"invented key {key!r} — target kept as unmatched")
            key, certain = "", False
        if key and key in seen:
            notes.append(f"duplicate key {key!r} — kept the first entry")
            continue
        if key:
            seen.add(key)
        else:
            certain = False
        quote = str(item.get("evidence_quote", "") or "").strip()
        if quote and _WS_RE.sub(" ", quote) not in sent:
            notes.append(f"evidence_quote not found verbatim in the text sent: {quote[:80]!r}")
        targets.append({
            "key":             key or None,
            "match_certain":   certain,
            "target_as_named": str(item.get("target_as_named", "") or "").strip(),
            "study_numbers":   str(item.get("study_numbers", "") or "").strip(),
            "replication_study_numbers":
                               str(item.get("replication_study_numbers", "") or "").strip(),
            "evidence_quote":  quote,
            "record":          key_map.get(key) if key else None,
        })
    return targets, notes


def identify_targets_with_llm(doi_r:          str,
                              study_r:        str,
                              abstract_r:     str,
                              candidates:     list[dict],
                              references:     list[dict],
                              *,
                              pdf_abstract:   str = "",
                              intro:          str = "",
                              methods:        str = "",
                              html_text:      str = "",
                              validator_note: str = "",
                              cache_prefix:   str = "llm",
                              abstract_only:  bool = False,
                              openrouter:     bool = True) -> dict:
    """Ask which previously published study or studies this paper re-tests.

    One function for all three LLM stages of the resolution ladder; what differs is
    the evidence passed in, not the question. Acceptance is `match_certain` on a key
    that exists in THIS call's namespace — the DOI always comes from the mapped
    record, never from the model.

    cache_prefix names the stage as well as the cache: the abstract stage and the
    full-text stage share "llm" and are told apart by abstract_only, the reference
    screen uses "reftarget". Two stages rendering the same prompt for the same paper
    therefore still cache — and are answerable — separately.

    Every parsed answer is cached, a decline included; provider failures are not.
    """
    entries, key_map = assign_target_keys(candidates, references)
    prompt = build_target_prompt(study_r, abstract_r, entries,
                                 pdf_abstract=pdf_abstract, intro=intro,
                                 methods=methods, html_text=html_text,
                                 validator_note=validator_note)
    # The prompt shows the model a key, authors, a year and a title — but the answer
    # is converted to a link through the record that key maps to, and the DOI it
    # carries is not on the page. Two runs whose lists render identically can still
    # map the same key to different records (a DOI backfilled into OpenAlex, a work
    # merged into another id), so the identities go into the key on their own account
    # or the cache replays a stale DOI under a prompt that still looks right.
    identities = "|".join(f"{e['key']}:{e.get('doi') or e.get('openalex_id') or ''}"
                          for e in entries)
    key = content_key(cache_prefix, doi_r or study_r,
                      prompt_version("build_target_prompt"),
                      ladder_fingerprint(GEMINI_HEAVY_MODEL, openrouter=openrouter),
                      abstract_only, identities, prompt)
    cached = read_cache(LLM_CACHE_DIR, key)
    if cached is not None:
        cached.setdefault("llm_source", "cache")
        cached.setdefault("llm_prompt", "")
        cached.setdefault("llm_error",  "")
        cached.setdefault("target_stage", "")
        cached.setdefault("resolved_study_r", "")
        # Entries written before the mapped record was kept on the target resolve
        # their keys here instead. The key namespace is a pure function of
        # (candidates, references), so this call rebuilt the same map; without the
        # repair a cached multi-target answer reaches the adapter record-less and
        # every one of its targets is dropped as unmatched.
        for t in cached.get("targets") or []:
            if t.get("key") and not t.get("record"):
                t["record"] = key_map.get(t["key"])
        return cached

    result, llm_source, llm_model, llm_error = call_llm_ladder(
        prompt, gemini_model=GEMINI_HEAVY_MODEL, openrouter=openrouter)

    base = {
        "resolved"          : False,
        "resolution_method" : "llm_failed",
        "resolved_doi_o"    : "",
        "resolved_title_o"  : "",
        "resolved_year_o"   : None,
        "resolved_author_o" : "",
        "resolved_study_o"  : "",
        "resolved_study_r"  : "",
        "resolution_score"  : 0.0,
        "llm_source"        : "none",
        "llm_model"         : "",
        "llm_confidence"    : "",
        "llm_evidence"      : "",
        "llm_reasoning"     : "",
        "llm_prompt"        : prompt,
        "llm_error"         : llm_error,
        "targets"           : [],
        "target_stage"      : "",
        "unidentified_count": 0,
        "stated_count"      : None,
        "stated_count_unit" : "",
        "multi_target"      : False,
        "target_as_named"   : "",
    }
    if not result:
        return base

    targets, notes = _validate_targets(result.get("targets"), key_map, prompt)

    try:
        unidentified = max(0, int(result.get("unidentified_count") or 0))
    except (TypeError, ValueError):
        notes.append(f"unidentified_count not a number: {result.get('unidentified_count')!r}")
        unidentified = 0

    try:
        stated = int(result.get("stated_count")) if result.get("stated_count") is not None else None
    except (TypeError, ValueError):
        stated = None
    unit = str(result.get("stated_count_unit") or "").strip().lower()

    evidence_notes: list[str] = []
    if unidentified:
        evidence_notes.append(f"unidentified={unidentified}")
    # Only papers/studies can be reconciled against an entry count: a paper stating
    # 28 findings or 36 sites may still be re-testing one original.
    if stated is not None and unit in {"papers", "studies"}:
        if stated != len(targets) + unidentified:
            evidence_notes.append(
                f"stated_count={stated} {unit}, identified={len(targets)}, "
                f"unidentified={unidentified} — does not reconcile")

    first  = targets[0] if targets else None
    single = targets[0] if len(targets) == 1 else None
    record = single["record"] if single and single["match_certain"] else None

    resolved_doi = clean_doi(str(record.get("doi") or "")) if record else ""
    doi_from_title_search = False
    if record and not resolved_doi and record.get("title"):
        # A parsed-PDF reference carries no DOI. Search for it rather than let the
        # model supply one; the pick is then provisional, at ~50% precision.
        from shared.doi_verify import resolve_doi_by_metadata
        hit = resolve_doi_by_metadata(record["title"], record.get("first_author", ""),
                                      record.get("year"), exclude_doi=doi_r)
        if hit:
            resolved_doi = clean_doi(hit.get("doi", "") or "")
            doi_from_title_search = bool(resolved_doi)

    resolved = bool(record) and bool(resolved_doi or record.get("title"))

    # The rung is named whether or not it resolved: a multi-target answer takes its
    # resolution_method from the target count, and the per-target adapter still has to
    # know which stage produced the list to write an honest link_method for the rows.
    stage = ("llm_references"                          if cache_prefix == "reftarget"
             else f"llm_cited_candidates_{llm_source}" if abstract_only
             else f"llm_{llm_source}")

    if not resolved:
        method = "llm_multi_target" if len(targets) > 1 else "llm_no_target"
    elif doi_from_title_search:
        method = f"llm_title_search_{llm_source}"
    else:
        method = stage

    output = {
        **base,
        "resolved"          : resolved,
        "resolution_method" : method,
        "resolved_doi_o"    : resolved_doi,
        "resolved_title_o"  : str(record.get("title") or "") if record else "",
        "resolved_year_o"   : record.get("year") if record else None,
        "resolved_author_o" : str(record.get("first_author") or "") if record else "",
        # Which study inside the accepted original is targeted, in the codebook's
        # "1, 2" form — the same field the multi-original path fills from its own answer.
        "resolved_study_o"  : _clean_study_numbers(
            single["study_numbers"] if record else ""),
        # And which study of the REPLICATION re-tests it — FLoRA's study_r, the
        # counterpart of study_o. Same cleaner: models answer "Study 1, Experiment 2".
        "resolved_study_r"  : _clean_study_numbers(
            single["replication_study_numbers"] if record else ""),
        "resolution_score"  : 1.0 if resolved else 0.0,
        "llm_source"        : llm_source,
        "llm_model"         : llm_model,
        # The acceptance gate is match_certain; llm_confidence exists so
        # run_extract._link_confidence keeps reading one field for every producer.
        "llm_confidence"    : "high" if resolved else "low",
        "llm_evidence"      : "; ".join(filter(None, [
            first["evidence_quote"] if first else "", *evidence_notes])),
        "llm_reasoning"     : " | ".join(filter(None, [
            str(result.get("reasoning", "") or ""), *notes])),
        "llm_error"         : "",
        # The mapped record stays ON the target: a @key is only meaningful against the
        # key_map of the call that offered it, and the per-target adapter in
        # run_extract has neither the candidates nor the parsed references to rebuild
        # one. Records are plain JSON dicts (shared/target_keys._normalise), so the
        # cache is unaffected.
        "targets"           : targets,
        "target_stage"      : stage,
        "unidentified_count": unidentified,
        "stated_count"      : stated,
        "stated_count_unit" : unit,
        "multi_target"      : len(targets) > 1,
        "target_as_named"   : first["target_as_named"] if first else "",
    }

    write_cache(LLM_CACHE_DIR, key, output)
    return output


# ── Gemini with image parts (for PDF reference-page parsing) ──────────────────

def call_gemini_with_images(prompt: str,
                             image_b64_list: list[dict],
                             model: str = GEMINI_MODEL) -> Optional[dict]:
    """
    Call Gemini with inline image parts (base64 PNG/JPEG).

    image_b64_list: [{"mime_type": "image/png", "data": "<base64>"}]

    Requires PyMuPDF (fitz) for rendering — callers must catch ImportError.
    """
    if not GEMINI_API_KEYS:
        return None

    parts: list[dict] = [{"text": prompt}]
    for img in image_b64_list:
        parts.append({"inline_data": {"mime_type": img["mime_type"], "data": img["data"]}})

    payload = {
        "contents"       : [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens" : 4096,
        },
    }

    for key_idx, api_key in enumerate(GEMINI_API_KEYS):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
               f":generateContent?key={api_key}")
        try:
            _throttle("gemini")
            r = _gemini_post(url, payload, key_idx, 120)
            if r.status_code == 429:
                continue
            if r.status_code != 200:
                log.warning("Gemini image call HTTP %s", r.status_code)
                continue
            body = r.json()
            if not body.get("candidates"):
                return None
            _record_tokens("gemini", model, *_gemini_usage(body))
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return _parse_llm_json(text)
        except Exception as e:
            log.warning("Gemini image call failed: %s", e)

    return None


# ── Gemini with inline PDF (for direct PDF reference extraction) ──────────────

def call_gemini_with_pdf(prompt: str,
                          pdf_bytes: bytes,
                          model: str = GEMINI_MODEL) -> Optional[dict]:
    """
    Call Gemini with an inline PDF payload.

    Uses MEDIA_RESOLUTION_LOW to minimise token cost: for native-text PDFs,
    Gemini reads the embedded text directly (not billed as image tokens); for
    scanned PDFs it applies lower-resolution OCR.  Max supported: 50 MB / 1 000 pages.

    Returns a parsed dict or None.
    """
    if not GEMINI_API_KEYS:
        return None

    pdf_b64 = base64.b64encode(pdf_bytes).decode()

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}},
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens" : 4096,
            "mediaResolution" : "MEDIA_RESOLUTION_LOW",
        },
    }

    for key_idx, api_key in enumerate(GEMINI_API_KEYS):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
               f":generateContent?key={api_key}")
        for attempt in range(2):
            try:
                _throttle("gemini")
                r = _gemini_post(url, payload, key_idx, 45)
                if r.status_code == 429:
                    break
                if r.status_code in (500, 503) and attempt == 0:
                    time.sleep(3)
                    continue
                if r.status_code != 200:
                    log.warning("Gemini PDF call HTTP %s on key %d", r.status_code, key_idx + 1)
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    break
                body = r.json()
                if not body.get("candidates"):
                    return None
                _record_tokens("gemini", model, *_gemini_usage(body))
                text = body["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_llm_json(text)
            except Exception as e:
                log.warning("Gemini PDF call failed (key %d, attempt %d): %s",
                            key_idx + 1, attempt + 1, e)
                if attempt == 0:
                    time.sleep(3)

    return None


# ── Study-number cleaning ─────────────────────────────────────────────────────

def _clean_study_number(value) -> str:
    """FLoRA `study_o` for one targeted study: a bare number, or "" if there is none.

    The prompt asks for a number but models answer "Study 2", "Experiment 3a" or 2.
    Everything but the digits (and a trailing letter, which distinguishes Study 3a
    from 3b) is dropped, so that rows collapsed onto one original paper join into
    the codebook's "1, 2" form rather than "Study 1, Experiment 2".
    """
    text = str(value or "").strip()
    if not text:
        return ""
    m = re.search(r"\d+[a-z]?", text, re.IGNORECASE)
    return m.group(0).lower() if m else ""


# How an answer names several studies: "1, 2", "Study 1; Study 4", "2 and 3", "1 & 2".
_STUDY_SPLIT_RE = re.compile(r"[,;&]|\band\b", re.IGNORECASE)
# A range, once the leading word is gone: "1-3", "1–3". Two digits at most, so a year
# or a page range cannot be expanded into a hundred studies.
_STUDY_RANGE_RE = re.compile(r"^(\d{1,2})\s*[-–—]\s*(\d{1,2})$")
_STUDY_WORD_RE  = re.compile(r"^(stud(y|ies)|experiments?|exp\.?)\s*", re.IGNORECASE)


def _clean_study_numbers(value) -> str:
    """FLoRA `study_o` for one target: every study the answer names, as "1, 2".

    The prompt asks for numbers and models answer in prose — "Study 1; Study 4",
    "2 and 3", "Studies 1-3". Splitting on commas alone kept the first number and
    silently dropped the rest, so a two-study target was recorded as a one-study one.
    Duplicates collapse in encounter order, matching the multi-original path.
    """
    numbers: list[str] = []
    for part in _STUDY_SPLIT_RE.split(str(value or "")):
        bare  = _STUDY_WORD_RE.sub("", part.strip())
        span  = _STUDY_RANGE_RE.match(bare)
        if span and int(span.group(1)) < int(span.group(2)):
            numbers.extend(str(n) for n in range(int(span.group(1)),
                                                 int(span.group(2)) + 1))
            continue
        number = _clean_study_number(part)
        if number:
            numbers.append(number)
    return ", ".join(dict.fromkeys(numbers))





# ── Replication screening ────────────────────────────────────────────────────
# Reached when the abstract carries no parseable "(Author, Year)" citation, so
# find_all_candidates() has nothing to match and the row would otherwise drop
# straight to PDF acquisition.
#
# Two questions, deliberately two calls (see below), because they carry very
# different risk: a confident "not a replication" DISCARDS the row, while a
# confident target WRITES a database record. Sharing one confidence field between
# them, as an earlier version did, made a high-confidence negative almost
# inexpressible while the discard path required exactly that.
#
# Q1 also runs on two different models and acts only when they agree. Half the
# rows arriving here are lexical false positives, so the classifier does most of
# the pipeline's filtering work; a single model's high-confidence miss silently
# loses a genuine replication. Disagreement is not an error — it routes the row to
# full text, which is what we would have done anyway.

# Q1's two voters, in call order. Both are required: with one provider the screen
# cannot tell agreement from a lone opinion, so run_extract refuses to start.
# Voter 2 is gpt-5.4-mini on OpenAI direct: on the v3.2 gate sweep this pair
# discards 89% of adjudicated hard negatives with zero settled misses, against 73%
# for Ministral via OpenRouter on the same gate. A model id containing "/" is an
# OpenRouter id, so swapping the env var reroutes the call without a code change.
SCREEN_CLASSIFICATIONS = ("replication", "reproduction", "both", "none", "unclear")
SCREEN_QUALIFYING      = ("replication", "reproduction", "both")

# The v3.2 prompt's category enum, in prompt order — the order the union is joined in.
SCREEN_CATEGORIES = (
    "clearly_declared", "self_retest", "measurement_validation", "context_transfer",
    "incidental_finding", "initial_validation", "tool_benchmark",
    "builds_on_literature", "terminology_only", "about_replication", "other",
)


def screen_voters() -> list[tuple[str, str, str]]:
    """Q1's voters in call order, as (provider, model, env var holding its key).

    The one place the voter pair is configured: it drives the per-vote dispatch, the
    "+".join fingerprint the classification cache is keyed on, and run_extract's
    startup key check. Swapping SCREEN_VOTER2_MODEL therefore reroutes the call, the
    cache key and the required key together — a model id containing "/" is an
    OpenRouter id, anything else is called on OpenAI direct.
    """
    if "/" in SCREEN_VOTER2_MODEL:
        voter2 = ("openrouter", SCREEN_VOTER2_MODEL, "OPENROUTER_API_KEY")
    else:
        voter2 = ("openai", SCREEN_VOTER2_MODEL, "OPENAI_API_KEY")
    return [("gemini", GEMINI_LIGHT_MODEL, "GEMINI_API_KEY"), voter2]


def _classify_once(prompt: str, provider: str, model: str) -> "dict | None":
    """One classification vote on the v3.2 five-field schema."""
    if provider == "gemini":
        result, _ = call_gemini(prompt, model=model)
    elif provider == "openrouter":
        result, _ = call_openrouter(prompt, model=model)
    else:
        result, _ = call_openai(prompt, model=model, reasoning_effort="low")
    if not result:
        return None

    classification = str(result.get("classification", "")).strip().lower()
    if classification not in SCREEN_CLASSIFICATIONS:
        classification = "unclear"
    raw_confident = result.get("confident")
    if isinstance(raw_confident, str):
        confident = raw_confident.strip().lower() == "true"
    else:
        confident = bool(raw_confident)
    raw_categories = result.get("categories")
    categories = [c for c in (str(x).strip().lower()
                              for x in (raw_categories if isinstance(raw_categories, list) else []))
                  if c in SCREEN_CATEGORIES]
    return {
        "classification": classification,
        "confident":      confident,
        "categories":     categories,
        "evidence":       str(result.get("evidence_quote", "") or ""),
        "reasoning":      str(result.get("reasoning", "") or ""),
        "provider":       provider,
        "model":          model,
    }


def screen_gate(votes: list[dict]) -> "str | None":
    """G-softqual, the gate the v3.2 sweep validated: 89% of adjudicated hard
    negatives discarded with zero settled misses.

    Returns "discard", "proceed", or None when fewer than two voters answered (an
    incomplete screen is an API failure, not a verdict).

    Discard when every vote is "none" at any confidence, or when at least one voter
    said "none" confidently and every other vote is a qualifying-or-unclear answer
    the voter explicitly declined to stand behind. Everything else proceeds — a
    confident "none" against a confident qualifying answer is a real split, and
    false inclusions are cheap where false discards are not.
    """
    if len(votes) < 2:
        return None
    is_none = [v["classification"] == "none" for v in votes]
    if all(is_none):
        return "discard"
    if not any(n and v["confident"] for n, v in zip(is_none, votes)):
        return "proceed"
    soft = all(n or (v["classification"] in SCREEN_QUALIFYING + ("unclear",)
                     and not v["confident"])
               for n, v in zip(is_none, votes))
    return "discard" if soft else "proceed"


def _screen_record_type(votes: list[dict]) -> str:
    """The paper type the screen settled on, for the `type` column and the outcome
    vocabulary. Both voters agreeing on a qualifying label wins; a "both" answer or
    a replication-vs-reproduction split falls back to the first qualifying voter in
    call order (Gemini). "both" maps to "replication" because such a paper collects
    new data, and replication outcome vocabulary applies to it; the raw
    classifications stay visible in the votes.
    """
    quals = [v["classification"] for v in votes if v["classification"] in SCREEN_QUALIFYING]
    if not quals:
        return ""
    # Agreement and the fallback pick the same element: quals is in call order, so
    # quals[0] is Gemini's answer whenever Gemini gave a qualifying one.
    return "replication" if quals[0] == "both" else quals[0]


def _screen_categories(votes: list[dict]) -> list[str]:
    """Deduplicated union of both voters' categories, in the prompt's enum order."""
    seen = {c for v in votes for c in v["categories"]}
    return [c for c in SCREEN_CATEGORIES if c in seen]


def classify_replication(doi_r: str, study_r: str, abstract_r: str) -> dict:
    """Q1 alone: two models judge whether this paper is the kind of study the
    database collects, and screen_gate() turns their two votes into one decision.

    Split out of screen_references_with_llm so Stage 3 can ask the question as its
    front door — before match-type classification, the resolution ladder, the PDF
    and outcome coding — rather than after paying for all of them. The target pick
    (Q2) stays in screen_references_with_llm, at the point in the ladder where the
    reference list has been fetched, and reuses this verdict instead of re-voting.

    Returns:
      screen_verdict      — "discard" or "proceed" (empty on an incomplete screen)
      screen_classification — the combined qualifying label, or "none"/"unclear"
      record_type         — "replication"/"reproduction", empty when neither voter
                            gave a qualifying answer
      categories          — union of both voters' categories, in enum order
      votes / llm_*       — who voted what, for the reviewer of a set-aside row
      resolution_method   — llm_refscreen_declined on a complete screen;
                            llm_refscreen_partial (one vote) / llm_refscreen_failed
                            (none) when the screen did not complete. An incomplete
                            screen is an API failure, not a verdict, so it is
                            returned uncached — a re-run must be able to succeed.
    """
    voters     = screen_voters()
    cls_prompt = build_classify_prompt(study_r, abstract_r)
    # The voter pair is part of the verdict — the two models disagree often enough
    # that this is the question the audit measured a model effect on — so both
    # models are in the key alongside the prompt version and the text they see.
    key = content_key("classify", doi_r or study_r,
                      prompt_version("build_classify_prompt"),
                      "+".join(cache_model_id(model) for _, model, _ in voters),
                      cls_prompt)
    cached = read_cache(LLM_CACHE_DIR, key)
    if cached is not None:
        return cached

    out = {
        "resolution_method": "llm_refscreen_declined",
        "screen_verdict": "", "screen_classification": "unclear",
        "record_type": "", "categories": [], "votes": [],
        "llm_source": "", "llm_model": "", "llm_evidence": "",
        "llm_reasoning": "", "llm_prompt": "", "llm_error": "",
    }

    out["llm_prompt"] = cls_prompt
    votes = [v for v in (_classify_once(cls_prompt, p, m) for p, m, _ in voters) if v]

    # Keep the individual votes: the gate's decision is not reviewable without
    # knowing who said what.
    out["votes"] = [{k: v[k] for k in
                     ("provider", "classification", "confident", "categories", "reasoning")}
                    for v in votes]
    out["llm_source"] = "+".join(v["provider"] for v in votes)
    out["llm_model"]  = "+".join(v["model"] for v in votes)
    out["llm_evidence"]  = votes[0]["evidence"] if votes else ""
    out["llm_reasoning"] = " | ".join(f"{v['provider']}: {v['reasoning']}" for v in votes)

    # A missing vote is an API failure, not a verdict. Reporting it as a normal
    # result would file the row as a screen outcome and corrupt the discard rate,
    # and caching it would freeze one transient failure into a permanent one.
    # Return uncached so a re-run can screen the row properly.
    if len(votes) < 2:
        answered = {v["provider"] for v in votes}
        out["resolution_method"] = ("llm_refscreen_partial" if votes
                                    else "llm_refscreen_failed")
        out["llm_error"] = "classifier failed: " + ", ".join(
            p for p, _, _ in voters if p not in answered)
        return out

    out["screen_verdict"] = screen_gate(votes)
    out["record_type"]    = _screen_record_type(votes)
    out["categories"]     = _screen_categories(votes)
    labels = {v["classification"] for v in votes}
    out["screen_classification"] = (out["record_type"] or
                                    (labels.pop() if len(labels) == 1 else "unclear"))

    write_cache(LLM_CACHE_DIR, key, out)
    return out


# The target-pick half of screen_references_with_llm's return value, before a
# reference has been picked — the resolver keys every ladder stage returns.
_UNPICKED_TARGET = {
    "resolved": False, "resolution_method": "llm_refscreen_declined",
    "resolved_doi_o": "", "resolved_title_o": "", "resolved_year_o": None,
    "resolved_author_o": "", "resolved_study_o": "", "resolved_study_r": "",
    "resolution_score": 0.0,
    "llm_confidence": "", "target_description": "",
    "targets": [], "multi_target": False, "target_stage": "",
    "unidentified_count": 0,
}


def screen_references_with_llm(doi_r: str, study_r: str, abstract_r: str,
                               refs: list[dict],
                               classification: "dict | None" = None,
                               candidates: "list[dict] | None" = None) -> dict:
    """Identify the paper's target among its references, given the Q1 verdict.

    classification — the verdict from classify_replication(). Stage 3 runs the
    classification at its front door and threads the result in here, so the two
    votes are made once per paper. When it is absent (a caller that has no verdict
    yet, e.g. the batch tools) the classification runs here.

    candidates and refs go into ONE keyed namespace: a work the author-year matcher
    already found is usually in the reference list too, and offering it twice under
    two numbers made "the third one" ambiguous.

    The return value is the union of two contracts, in this order: the target
    pick's keys (_UNPICKED_TARGET below) and, over them, every key
    classify_replication() returns. The classification wins on the one key they
    share, resolution_method, so an incomplete screen is reported as incomplete
    rather than as a target this function declined to pick.

    A reference is accepted as the target only when the model marks it
    match_certain; llm_confidence mirrors that gate for run_extract, which reads one
    confidence field for every producer.
    """
    out = {**_UNPICKED_TARGET,
           **(classification or classify_replication(doi_r, study_r, abstract_r))}

    if out["screen_classification"] in SCREEN_QUALIFYING and refs:
        # The pick is cached separately from the classification: the two halves are
        # now decided at different points in the pipeline, and one cache holding
        # both would be written before the second half had run.
        # No OpenRouter rung: a wrong original is worse than an unresolved one, so
        # this pick stops at the two strong providers.
        pick = identify_targets_with_llm(doi_r, study_r, abstract_r,
                                         candidates or [], refs,
                                         cache_prefix="reftarget", openrouter=False)
        out.update({
            "llm_confidence":     pick["llm_confidence"],
            "target_description": pick["target_as_named"],
            "targets":            pick["targets"],
            "multi_target":       pick["multi_target"],
            # The list survives this rung even when the pick declined a single link:
            # the adapter writes one row per target, and the stage names the rung so
            # those rows carry the link_method they were actually resolved at.
            "target_stage":       pick["target_stage"],
            "unidentified_count": pick["unidentified_count"],
            "resolved_study_r":   pick["resolved_study_r"],
        })
        if pick["resolved"]:
            # The link is this call's decision, not Q1's, so the row is attributed to
            # the model that picked the reference and carries its justifying quote.
            out.update({
                "resolved":          True,
                "resolution_method": pick["resolution_method"],
                "resolved_doi_o":    pick["resolved_doi_o"],
                "resolved_title_o":  pick["resolved_title_o"],
                "resolved_year_o":   pick["resolved_year_o"],
                "resolved_author_o": pick["resolved_author_o"],
                "resolved_study_o":  pick["resolved_study_o"],
                "resolution_score":  pick["resolution_score"],
                "llm_source":        pick["llm_source"],
                "llm_model":         pick["llm_model"],
                "llm_evidence":      pick["llm_evidence"] or out["llm_evidence"],
                "llm_reasoning":     " | ".join(filter(None, [
                    out["llm_reasoning"],
                    f"target ({pick['llm_source']}): {pick['llm_reasoning']}".strip(),
                ])),
            })

    return out
