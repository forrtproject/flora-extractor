"""
llm_client.py — the pipeline's LLM calls: the three provider entry points, target
identification, and the two-model replication screen.

One call, one model, no fallback. call_gemini(), call_openai() and call_openrouter()
each take their model as a required argument and report failure by returning None
with an error string; nothing re-asks another provider. A ladder used to run
Gemini → OpenAI → OpenRouter here, and it made an outage invisible: the row still got
an answer, from a model the caller never chose and the cache key had to over-name.
Any fallback is now the caller's explicit decision, made where the answer is used.
Each provider is rate-limited against its own reserved schedule, which holds under
concurrent callers as well as sequential ones.

Public API:
    resolve_targets_and_outcomes(doi_r, study_r, abstract_r, candidates,
                                 references, *, rung, …) → dict
"""
import base64
import json
import re
import time
from typing import Optional

import requests

from .config import (
    GEMINI_API_KEYS, PDF_PARSE_MODEL, SCREENING_MODEL_1, LINKING_MODEL,
    GEMINI_USE_FLEX, GEMINI_FLEX_TIMEOUT, GEMINI_PAID_KEY_SLOTS, GEMINI_RATE_SEC,
    LINKING_EFFORT, SCREENING_EFFORT_1, SCREENING_EFFORT_2,
    LLM_CACHE_DIR,
    OPENAI_API_KEY, OPENAI_RATE_SEC,
    OPENAI_USE_FLEX, OPENAI_FLEX_TIMEOUT,
    OPENROUTER_API_KEY, OPENROUTER_RATE_SEC,
    SCREENING_MODEL_2,
    log,
)
from . import token_counter, token_usage
from .cache import content_key, read_cache, read_cache_migrating, write_cache
from .rate_limit import throttle
from .prompts import (
    build_classify_prompt, build_repro_target_outcome_prompt,
    build_target_outcome_prompt, prompt_version,
)
from .schema import normalise_outcome_block
from .target_keys import assign_target_keys
from .utils import clean_doi

# Output cap for EVERY call in this module — the three chat entry points and the two
# Gemini document calls. It was 1024, which on a reasoning model (gpt-5-mini) also has
# to cover hidden reasoning tokens — while the outcome prompts ask for a quote of up to
# ~1200 characters, ~300 tokens of visible output before any other field. A truncated
# response is not valid JSON, so it was indistinguishable from a parse failure and got
# retried rather than reported; every call site now logs it explicitly.
#
# One value for all providers, because the cap decides what the model is ABLE to answer:
# Gemini sent 8192 here while OpenAI and OpenRouter sent 4096, so the same screening
# prompt had a different ceiling depending on which voter's id it carried. 4096 is the
# value both metered providers have always run at, and it is what the answers on disk
# need: the longest response in 813 cached answers is 1,723 characters, ~430 tokens.
# The cap is a ceiling, not a spend — a call is billed for the tokens it actually
# produces — so raising it is cheap if a reasoning budget ever pushes a real call into
# it. Truncation is loud when it happens: it is logged and returned as an error, and
# never cached.
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


def _gemini_block_reason(body: dict) -> str:
    """Why this HTTP 200 carried no answer text — "" when it carried one.

    A 200 with nothing to read is never the model DECLINING to find anything: a
    decline arrives as parseable JSON whose list is empty, which is a fact about the
    document. The empty shapes are a prompt blocked before generation (no
    `candidates` at all, `promptFeedback.blockReason` saying why) and a candidate
    blocked or truncated on the way out (`finishReason` SAFETY / RECITATION /
    MAX_TOKENS … with no text part). Neither establishes anything about the
    document, so both must reach the caller as an ERROR: the document callers cache
    what they are told, and a safety block recorded as "this paper has no reference
    list" is permanent and wrong.
    """
    candidates = body.get("candidates") or []
    if not candidates:
        reason = (body.get("promptFeedback") or {}).get("blockReason") or "unknown"
        return f"blocked before generation (blockReason={reason})"
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    if parts and isinstance(parts[0], dict) and parts[0].get("text"):
        return ""
    return ("no response text "
            f"(finishReason={candidates[0].get('finishReason') or 'unknown'})")


# ── Per-provider rate limiting ────────────────────────────────────────────────
# Each provider is throttled against its OWN schedule, immediately before the
# request goes out. Charging the wait to the caller after a successful call — as
# every call site used to — made the delay unconditional even when the next call
# went to a different provider, or was served from cache, or never came.
#
# Callers may be THREADS (the filter engine's tier runners and Stage 3's row loop
# both run a pool), so the wait is taken from the shared reservation queue in
# shared/rate_limit.py rather than from a last-call timestamp this module reads and
# sleeps on — see that module for why the difference matters.

_PROVIDER_RATE_SEC = {
    "gemini":     GEMINI_RATE_SEC,
    "openai":     OPENAI_RATE_SEC,
    "openrouter": OPENROUTER_RATE_SEC,
}


def _throttle(provider: str) -> None:
    """Wait until this provider's next free slot, and reserve the one after it."""
    throttle(provider, _PROVIDER_RATE_SEC.get(provider, 1.0))


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
# keys, so the decision follows which key is in use — GEMINI_PAID_KEY_SLOTS — rather
# than the key's position in the rotation.

def _gemini_use_flex(key_idx: int) -> bool:
    return GEMINI_USE_FLEX and (key_idx + 1) in GEMINI_PAID_KEY_SLOTS


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


# ── Reasoning effort ──────────────────────────────────────────────────────────
# How hard a model is asked to think is a spend lever — reasoning tokens are billed
# as output — but unlike flex it changes the answer, so it cannot be switched on
# silently over a cache full of answers produced without it.
#
# Effort belongs to the CALL SITE, never to the model id. Three call sites name the
# same string today (LINKING_MODEL == OUTCOME_MODEL == SCREENING_MODEL_2), so
# deriving an effort from the id sent linking's "medium" to outcome coding, which
# asked for none, and labelled the screen's key "medium" while it sent "low". Each
# caller now passes its effort to call_model AND the same value to cache_model_id(),
# which is the ONLY way a model string enters a cache key — what was sent and what
# the key says are one value, written once per call site.
#
# The effort is one constant per call site, not one per provider, so it survives a
# model being repointed: each provider is sent the same intent under its own name —
# Gemini's `thinkingLevel`, OpenAI's and OpenRouter's `reasoning_effort`.

def cache_model_id(model: str, effort: str = "") -> str:
    """The model string as a cache key names it — with the effort the call sends.

    An answer produced at effort=minimal is a different answer from one produced at
    the model's default, so the two must not share a cache entry. An empty effort
    leaves the bare model string, so a call site that asks for no reasoning keys the
    same way one that cannot do any does.
    """
    return f"{model}@effort={effort}" if effort else model


# ── Gemini: one request, over every key ───────────────────────────────────────
# The three Gemini entry points below (a text prompt, a prompt plus a PDF, a prompt
# plus rendered page images) differ only in what they SEND and how they READ the
# answer. Everything between those two — key rotation, retries, the status codes, and
# charging the call — is one loop, kept here so the three cannot drift apart.

def _gemini_call(payload: dict, model: str, timeout: int,
                 what: str) -> tuple[Optional[dict], str]:
    """POST *payload* to *model*, rotating keys and retrying, and return the BODY.

    Key rotation and retry are separate axes. A 429 is quota gone on THIS key, so it
    moves to the next key immediately rather than spending the retries; a 5xx, a
    non-200 of any other kind, or an exception is transient and gets 3 attempts at
    1s/2s backoff on the same key — the same shape call_openai and call_openrouter
    run. A 404 is permanent on every key, so it ends the call.

    Tokens are recorded HERE, before the caller has read anything: a 200 was served
    and billed whatever it carried, and a truncated or blocked response is the most
    expensive kind — it ran to the output cap. Returning first would make Gemini's
    recorded spend an undercount of exactly the calls that cost the most.

    *what* names the call in the log lines only. Returns (body_or_None, error).
    """
    if not GEMINI_API_KEYS:
        log.warning("No GEMINI_API_KEY set — skipping Gemini")
        return None, "no API keys configured"

    if GEMINI_USE_FLEX:
        log.debug("Gemini flex inference enabled on paid keys %s (timeout=%ds)",
                  sorted(GEMINI_PAID_KEY_SLOTS), GEMINI_FLEX_TIMEOUT)

    last_error = "all keys exhausted"
    for key_idx, api_key in enumerate(GEMINI_API_KEYS):
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
               f":generateContent?key={api_key}")
        key_label = f"key {key_idx + 1}/{len(GEMINI_API_KEYS)}"

        for attempt in range(3):
            try:
                _throttle("gemini")
                r = _gemini_post(url, payload, key_idx, timeout)

                if r.status_code == 429:
                    last_error = f"quota exhausted on {key_label} (429)"
                    log.warning("Gemini %s quota exhausted on %s — %s", what, key_label,
                                "trying next key"
                                if key_idx + 1 < len(GEMINI_API_KEYS) else "no more keys")
                    break   # break inner retry loop → next key

                if r.status_code == 404:
                    # Model not found — changing keys won't help; bail out immediately.
                    err_msg = r.json().get("error", {}).get("message", r.text[:200])
                    log.error("Gemini model not found: %s — update the model constant "
                              "in shared/config.py. API said: %s", model, err_msg)
                    return None, f"model not found: {model}"

                if r.status_code != 200:
                    last_error = f"HTTP {r.status_code} on {key_label}: {r.text[:200]}"
                    log.warning("Gemini %s HTTP %s for %s model=%s: %.200s",
                                what, r.status_code, key_label, model, r.text)
                    if attempt < 2:
                        time.sleep(2 ** attempt)  # 1s, 2s
                        continue
                    break   # attempts spent on this key → try the next one

                body = r.json()
                _record_tokens("gemini", model, *_gemini_usage(body))
                if key_idx > 0:
                    log.info("Gemini %s succeeded on %s", what, key_label)
                return body, ""

            except Exception as e:
                last_error = f"exception on {key_label} attempt {attempt+1}: {e}"
                log.warning("Gemini %s failed on %s (attempt %d/3): %s",
                            what, key_label, attempt + 1, e)
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s

    return None, last_error


def _gemini_answer_text(body: dict) -> str:
    """The answer text of a Gemini response body, or "" when it carries none."""
    candidates = body.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    return str(parts[0].get("text") or "") if parts else ""


# ── Gemini: JSON chat calls ───────────────────────────────────────────────────

def call_gemini(prompt: str, model: str, *,
                reasoning_effort: str = "") -> tuple[Optional[dict], str]:
    """
    Call Gemini via the REST API with responseMimeType=application/json.

    model is required, as it is on call_openai and call_openrouter. This is a
    text-JSON call, reached through call_model(), which always names the model it
    dispatched on; the default it used to carry was PDF_PARSE_MODEL — the constant
    for a different question entirely — so it could only ever be reached by mistake.

    reasoning_effort is sent as `thinkingLevel` and only when set — it comes from the
    call site, so a Gemini id and an OpenAI id under the same constant think alike.

    Returns (result_dict_or_None, error_description).
    """
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            # temperature is deliberately NOT set — see the note by
            # JSON_MAX_OUTPUT_TOKENS. gpt-5-mini rejects any explicit value, so
            # pinning it here would make Gemini-coded and OpenAI-coded rows differ
            # by sampling policy as well as by model.
            "responseMimeType": "application/json",
            "maxOutputTokens" : JSON_MAX_OUTPUT_TOKENS,
            # Note: thinkingConfig is intentionally omitted.
            # Setting thinkingBudget:0 while also using responseMimeType:
            # application/json causes gemini-3-flash-preview to return a
            # non-200 error or empty candidates, so all calls fell through to
            # OpenAI.  Letting the model use its default thinking mode fixes this.
        },
    }

    if reasoning_effort:
        # thinkingLevel (gemini-3): "minimal" buys back most of the output bill on
        # the heavy model. Safe alongside responseMimeType, unlike thinkingBudget:0.
        payload["generationConfig"]["thinkingLevel"] = reasoning_effort

    body, err = _gemini_call(payload, model, 90, "call")
    if body is None:
        return None, err

    if not body.get("candidates"):
        blocked = body.get("promptFeedback", {}).get("blockReason", "unknown")
        log.warning("Gemini returned no candidates — blockReason=%s", blocked)
        return None, f"no candidates — blockReason={blocked}"

    if body["candidates"][0].get("finishReason") == "MAX_TOKENS":
        log.warning("Gemini response hit maxOutputTokens and was cut off — "
                    "the truncated JSON will fail to parse (model=%s)", model)
        return None, "response truncated at maxOutputTokens"

    text = _gemini_answer_text(body)
    result = _parse_llm_json(text)
    if result is not None:
        return result, ""

    log.warning("Gemini returned non-JSON: %.200s", text)
    return None, f"non-JSON response: {text[:150]}"


# ── OpenAI direct: JSON chat calls ────────────────────────────────────────────
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


def call_openai(prompt: str, model: str,
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
    # transient outage does not immediately poison a row after a single failure. Retrying
    # the same model is the only retry there is — after these three the call has failed.
    last_error = "no attempts made"
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}

    def _create(flex: bool):
        """One request, at flex tier or standard. Flex calls queue, so they get
        OPENAI_FLEX_TIMEOUT instead of the client default."""
        tier = ({"service_tier": "flex", "timeout": OPENAI_FLEX_TIMEOUT} if flex else {})
        return client.chat.completions.create(
            model=model,
            # No system message. Every prompt states its own JSON schema, and
            # response_format already pins the output to one JSON object; the
            # generic instruction that used to ride along was paid for on every
            # OpenAI-served call and was never sent to Gemini at all, so dropping it
            # also makes the three providers ask the same question. See
            # _LEGACY_JSON_SYSTEM_MESSAGE in shared/prompts.py for why the retired
            # text still appears in every prompt_version() hash.
            messages=[{"role": "user", "content": prompt}],
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

def call_openrouter(prompt: str, model: str,
                    reasoning_effort: str = "") -> tuple[Optional[dict], str]:
    """
    Call any model available on OpenRouter via the OpenAI-compatible API.

    model — OpenRouter model ID e.g. "qwen/qwen3-30b-a3b". Required: OpenRouter is
            only ever reached by a caller that named a model living there, so there
            is no house default to fall back on.
    reasoning_effort — passed through only when set, as on OpenAI direct. A model
            that does not reason ignores it; sending nothing keeps the old behaviour
            for the pre-screen's two voters.

    Returns (result_dict_or_None, error_description).
    """
    if not OPENROUTER_API_KEY:
        return None, "OPENROUTER_API_KEY not configured"

    import openai
    client = openai.OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    use_model = model
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    # The same three attempts with 1s/2s backoff call_openai runs, for the same
    # reason: a single transient failure at the provider must not be the answer a
    # row records. OpenRouter had one attempt, so the pre-screen's two voters and
    # any OpenRouter-served screen slot gave up where the OpenAI slot retried —
    # a difference in how hard the pipeline tried, not in what the model thought.
    last_error = "no attempts made"
    for attempt in range(3):
        try:
            _throttle("openrouter")
            response = client.chat.completions.create(
                model=use_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=JSON_MAX_OUTPUT_TOKENS,
                **extra,
            )
            # Before the truncation check: a response cut off at the cap was served
            # and billed in full, and it is the most expensive shape there is.
            if response.usage:
                _record_tokens("openrouter", use_model,
                               response.usage.prompt_tokens,
                               response.usage.completion_tokens)
            if response.choices[0].finish_reason == "length":
                log.warning("OpenRouter response hit the %d-token cap and was cut off — "
                            "the truncated JSON will fail to parse (model=%s)",
                            JSON_MAX_OUTPUT_TOKENS, use_model)
                return None, "response truncated at max_tokens"
            result = _parse_llm_json(response.choices[0].message.content)
            return result, ("" if result else "response was not valid JSON")
        except Exception as e:
            last_error = f"exception: {e}"
            log.warning("OpenRouter call failed (attempt %d/3, model=%s): %s",
                        attempt + 1, use_model, e)
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s
    return None, last_error


# ── Which provider serves a model ────────────────────────────────────────────
# The model constants in config.py are named for the question they answer, not the
# provider that serves them, and they are meant to be swapped by a commit. That only
# works if the provider follows the id: a call site that hardcodes "this one goes to
# Gemini" turns a model swap into a 404 from the wrong API.
#
# This is a dispatcher, not a ladder. It picks the ONE provider that can serve the
# named model; when that provider fails, the call fails.

def provider_for(model: str) -> str:
    """The provider that serves *model*, from the shape of its id.

    A "/" means an OpenRouter id (`vendor/model`), which is also how the screen and
    the pre-screen have always routed their second slot. A leading "gemini" is Google;
    anything else is OpenAI direct. Vendor prefixes are stable enough to route on, and
    an id that reaches the wrong API fails loudly on the first call rather than
    quietly grading rows.
    """
    if "/" in model:
        return "openrouter"
    if model.startswith("gemini"):
        return "gemini"
    return "openai"


def call_model(prompt: str, model: str, *,
               reasoning_effort: str = "") -> tuple[Optional[dict], str, str]:
    """One JSON call to *model*, on whichever provider serves it.

    How hard the model thinks comes from the caller and nowhere else: nothing is
    inferred from the model id, because three call sites name the same id today.
    The effort reaches whichever provider serves it under that provider's own name —
    Gemini's thinkingLevel, OpenAI's and OpenRouter's reasoning_effort — so the same
    constant asks a Gemini id and an OpenAI id to think alike. An empty value sends
    nothing and leaves the provider default.

    Returns (result_dict_or_None, provider, error_description).
    """
    provider = provider_for(model)
    if provider == "gemini":
        result, err = call_gemini(prompt, model=model,
                                  reasoning_effort=reasoning_effort)
    elif provider == "openrouter":
        result, err = call_openrouter(prompt, model=model,
                                      reasoning_effort=reasoning_effort)
    else:
        result, err = call_openai(prompt, model=model,
                                  reasoning_effort=reasoning_effort)
    return result, provider, err


# ── Target identification ────────────────────────────────────────────────────

def _validate_targets(raw: list, key_map: dict[str, dict], prompt: str,
                      record_type: str, has_text: bool
                      ) -> tuple[list[dict], list[str]]:
    """Check every target the model returned against this call's key namespace.

    Nothing the model says about a key is trusted: an invented key is demoted to an
    unmatched target rather than dropped (the paper may still re-test something), a
    repeated key keeps only the first entry, and a quote that is not in the text we
    sent is recorded but not grounds for rejection — parsers reflow whitespace and
    ligatures, so a strict quote gate would discard correct picks.

    The outcome half of each entry goes through the same normaliser the standalone
    coder uses, so an out-of-vocabulary verdict lands as cannot_be_determined rather
    than reaching a row verbatim.
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
            # Coded for every target the model listed, key or no key: which entries
            # become rows is decided downstream, and an outcome for one that does not
            # is simply discarded there.
            "outcome_block":   normalise_outcome_block(item, record_type, has_text),
        })
    return targets, notes


def resolve_targets_and_outcomes(doi_r:       str,
                                 study_r:     str,
                                 abstract_r:  str,
                                 candidates:  list[dict],
                                 references:  list[dict],
                                 *,
                                 record_type: str = "replication",
                                 rung:        str,
                                 pdf_abstract: str = "",
                                 intro:       str = "",
                                 methods:     str = "",
                                 discussion:  str = "",
                                 discussion_provenance: str = "") -> dict:
    """Ask which previously published study or studies this paper re-tests, and what
    it concluded about each.

    One function for all three LLM stages of the resolution ladder; what differs is
    the evidence passed in, not the question. Acceptance is `match_certain` on a key
    that exists in THIS call's namespace — the DOI always comes from the mapped
    record, never from the model. The outcome is a separate judgment on the same
    reading, and it is validated separately: a target the model is sure of may carry
    an outcome the evidence does not settle.

    *rung* names the stage as well as the cache — "abstract", "reftarget" or
    "fulltext". Two rungs rendering the same prompt for the same paper therefore
    still cache, and are answerable, separately.

    *record_type* selects the vocabulary the outcome is coded in, and is part of the
    key: a reproduction gets a different prompt and a different outcome vocabulary,
    so it must not read back a replication-coded entry.

    LINKING_MODEL answers all three rungs, and only it: a wrong original is worse
    than an unresolved one, so an outage at its provider ends the row at llm_failed
    rather than handing the pick to whatever else was reachable.

    Every parsed answer is cached, a decline included; provider failures are not.
    """
    is_repro = str(record_type or "").strip().lower() == "reproduction"
    build = (build_repro_target_outcome_prompt if is_repro
             else build_target_outcome_prompt)
    builder_name = ("build_repro_target_outcome_prompt" if is_repro
                    else "build_target_outcome_prompt")
    entries, key_map = assign_target_keys(candidates, references)
    prompt = build(study_r, abstract_r, entries,
                   pdf_abstract=pdf_abstract, intro=intro, methods=methods,
                   discussion=discussion,
                   discussion_provenance=discussion_provenance)
    # The prompt shows the model a key, authors, a year and a title — but the answer
    # is converted to a link through the record that key maps to, and the DOI it
    # carries is not on the page. Two runs whose lists render identically can still
    # map the same key to different records (a DOI backfilled into OpenAlex, a work
    # merged into another id), so the identities go into the key on their own account
    # or the cache replays a stale DOI under a prompt that still looks right.
    identities = "|".join(f"{e['key']}:{e.get('doi') or e.get('openalex_id') or ''}"
                          for e in entries)
    # A new prefix on purpose. The old "llm"/"reftarget" entries answered a narrower
    # question — a target and nothing else — so replaying one under this call would
    # hand back a resolution with no outcome on it. They stay on disk, unread, and
    # there is deliberately no declared equivalence: the question changed.
    key = content_key("targetoutcome", doi_r or study_r,
                      prompt_version(builder_name),
                      cache_model_id(LINKING_MODEL, LINKING_EFFORT),
                      rung, record_type, identities, prompt)
    cached = read_cache(LLM_CACHE_DIR, key)
    if cached is not None:
        cached.setdefault("llm_source", "cache")
        cached.setdefault("llm_prompt", "")
        cached.setdefault("llm_error",  "")
        cached.setdefault("target_stage", "")
        cached.setdefault("resolved_study_r", "")
        # Future-proofing, not migration: no entry on disk is record-less today,
        # because every key moved when the prompt became the combined one. The repair
        # stays because the output shape can change again WITHOUT the key changing,
        # and the failure mode is silent — a record-less target is dropped as
        # unmatched, so the row loses an original it had already paid to find. The key
        # namespace is a pure function of (candidates, references), so this call
        # rebuilt the same map the cached answer was written against.
        for t in cached.get("targets") or []:
            if t.get("key") and not t.get("record"):
                t["record"] = key_map.get(t["key"])
        return cached

    result, provider, llm_error = call_model(prompt, LINKING_MODEL,
                                             reasoning_effort=LINKING_EFFORT)
    llm_source = provider if result else "none"
    llm_model  = LINKING_MODEL if result else ""

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
        # The accepted link's own outcome, when this call accepted one. The ladder
        # reads it to decide whether the row is finished, and run_extract writes it
        # instead of paying for a second call about the same reading.
        "outcome_block"     : {},
    }
    if not result:
        return base

    targets, notes = _validate_targets(result.get("targets"), key_map, prompt,
                                       record_type, bool(discussion))

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
    stage = ("llm_references"                          if rung == "reftarget"
             else f"llm_cited_candidates_{llm_source}" if rung == "abstract"
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
        "outcome_block"     : (single["outcome_block"] if record else {}),
    }

    write_cache(LLM_CACHE_DIR, key, output)
    return output


# ── Gemini with document parts (for PDF reference extraction) ─────────────────
# The one pair of calls that does NOT go through call_model. They send inline_data
# parts — a PDF or rendered page images — in Gemini's own request shape, which has no
# equivalent in the OpenAI-compatible body the other providers are called with. So
# PDF_PARSE_MODEL is the one model constant that cannot be swapped across providers;
# pointing it elsewhere is a code change here, not a one-line edit in config.py.

if provider_for(PDF_PARSE_MODEL) != "gemini":
    raise RuntimeError(
        f"PDF_PARSE_MODEL is {PDF_PARSE_MODEL!r}, which provider_for() routes to "
        f"{provider_for(PDF_PARSE_MODEL)}. The document calls below build a Gemini "
        "request body and have no other implementation — send a Gemini id, or "
        "write the call for the provider you want."
    )



def call_gemini_with_images(prompt: str,
                             image_b64_list: list[dict],
                             model: str = PDF_PARSE_MODEL
                             ) -> tuple[Optional[dict], str]:
    """
    Call Gemini with inline image parts (base64 PNG/JPEG).

    image_b64_list: [{"mime_type": "image/png", "data": "<base64>"}]

    Returns (result_dict_or_None, error_description) — the same contract as every
    other call in this module. An answer is a parsed JSON object, whose list may be
    empty: THAT is the model having nothing to give, and it is a fact about the
    document. Everything else is an error string, including a safety block
    (`_gemini_block_reason`), which says nothing about the document at all.

    Requires PyMuPDF (fitz) for rendering — callers must catch ImportError.
    """
    parts: list[dict] = [{"text": prompt}]
    for img in image_b64_list:
        parts.append({"inline_data": {"mime_type": img["mime_type"], "data": img["data"]}})

    payload = {
        "contents"       : [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens" : JSON_MAX_OUTPUT_TOKENS,
        },
    }

    body, err = _gemini_call(payload, model, 120, "image call")
    if body is None:
        return None, err
    blocked = _gemini_block_reason(body)
    if blocked:
        return None, blocked
    result = _parse_llm_json(_gemini_answer_text(body))
    return result, ("" if result else "response was not valid JSON")


# ── Gemini with inline PDF (for direct PDF reference extraction) ──────────────

def call_gemini_with_pdf(prompt: str,
                          pdf_bytes: bytes,
                          model: str = PDF_PARSE_MODEL
                          ) -> tuple[Optional[dict], str]:
    """
    Call Gemini with an inline PDF payload.

    Uses MEDIA_RESOLUTION_LOW to minimise token cost: for native-text PDFs,
    Gemini reads the embedded text directly (not billed as image tokens); for
    scanned PDFs it applies lower-resolution OCR.  Max supported: 50 MB / 1 000 pages.

    Returns (result_dict_or_None, error_description), as call_gemini_with_images
    does and for the same reason: "the model read the document and listed nothing"
    and "no answer came back" are different answers to the question the caller is
    asking, and only the first may be recorded against the document.
    """
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
            "maxOutputTokens" : JSON_MAX_OUTPUT_TOKENS,
            "mediaResolution" : "MEDIA_RESOLUTION_LOW",
        },
    }

    body, err = _gemini_call(payload, model, 45, "PDF call")
    if body is None:
        return None, err
    blocked = _gemini_block_reason(body)
    if blocked:
        return None, blocked
    result = _parse_llm_json(_gemini_answer_text(body))
    return result, ("" if result else "response was not valid JSON")


# ── Study-number cleaning ─────────────────────────────────────────────────────

# One study's number inside a part: digits plus an optional trailing letter, which is
# what distinguishes Study 3a from 3b. Everything else in "Study 2" or "Experiment 3a"
# is dropped, so a target's studies read as the codebook's "1, 2".
_STUDY_NUM_RE = re.compile(r"\d+[a-z]?", re.IGNORECASE)
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
        number = _STUDY_NUM_RE.search(part)
        if number:
            numbers.append(number.group(0).lower())
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


def screen_voters() -> list[tuple[str, str, str, str]]:
    """Q1's voters in call order, as (provider, model, env var holding its key, effort).

    The one place the voter pair is configured: it drives the per-vote dispatch, the
    "+".join fingerprint the classification cache is keyed on, and run_extract's
    startup key check. Swapping either model therefore reroutes the call, the cache
    key and the required key together — the provider follows the id, through
    provider_for(), so a slot is not tied to the vendor that happens to fill it.

    The effort rides along per slot rather than as one value for the pair: the two
    voters were evaluated at different rungs, and a vote's effort must reach both the
    request and that voter's component of the key from the same place.
    """
    return [(provider_for(m), m, _PROVIDER_KEY_ENV[provider_for(m)], effort)
            for m, effort in ((SCREENING_MODEL_1, SCREENING_EFFORT_1),
                              (SCREENING_MODEL_2, SCREENING_EFFORT_2))]


_PROVIDER_KEY_ENV = {
    "gemini":     "GEMINI_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _classify_once(prompt: str, model: str, effort: str) -> "dict | None":
    """One classification vote on the v3.2 five-field schema, at this voter's effort.

    The provider is not passed in: it follows the model id through call_model, and
    the vote is labelled with the provider that actually served it. Passing one
    alongside the id would let a caller label a vote with a provider that never saw
    the prompt. The effort IS passed in, from the same screen_voters() slot that
    names the model, because it is what the key claims the vote was produced at.
    """
    result, provider, _err = call_model(prompt, model, reasoning_effort=effort)
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


# A quote is a verbatim sentence from a title or abstract and may contain "|", so the
# single pipe `screen_votes` joins on is not a safe separator for one.
SCREEN_EVIDENCE_SEP = " || "


def format_screen_evidence(votes: list[dict]) -> str:
    """Every voter's justifying quote, in call order, as `<model>: <quote>` segments.

    Both quotes travel rather than the first voter's alone: the gate is a decision of
    the pair, and half the evidence is not something a reviewer can act on. The model
    prefix is there because this string is read by a human validator with no other
    attribution beside it.

    The vote dicts reach here from two places that name the same field differently — a
    fresh `classify_replication()` vote carries `evidence`, a vote rebuilt from the
    verdict rows carries `quote` — so both names are read. A vote with no quote
    contributes no segment; a legacy cached screen, whose votes predate the per-vote
    quote, therefore yields the empty string rather than a row of bare prefixes.
    """
    parts = []
    for vote in votes:
        quote = str(vote.get("quote") or vote.get("evidence") or "").strip()
        if not quote:
            continue
        model = str(vote.get("model") or "").strip()
        parts.append(f"{model}: {quote}" if model else quote)
    return SCREEN_EVIDENCE_SEP.join(parts)


# Declared cache equivalences for the classify key (issue #171). The declaration is a
# statement about ONE pair of voters: the equivalence holds only while the screen is
# still asking exactly the models and efforts whose answers are on disk. The equivalent
# pair is therefore named explicitly, and the legacy keys are offered only when the
# voter id this call built equals it. A wildcard here would be much worse than no
# equivalence at all: swapping a voter or an effort to RE-SCREEN the corpus would serve
# every legacy entry straight back from the old pair, re-filed under the new pair's key
# with a cache_migrated note naming voters that never saw the row — a rescreen that
# completes instantly, costs nothing and changes no verdict.
#
# Everything else about the key is unchanged, so an equivalence also stops matching by
# itself the moment the prompt moves: the legacy key is rebuilt from the CURRENT prompt
# version and prompt text, and only the voter component is substituted.
_CLASSIFY_EQUIVALENT_VOTER_ID = ("gemini-3.5-flash-lite@effort=minimal"
                                 "+gpt-5.4-mini@effort=low")

# Both declared entries name the same computation as _CLASSIFY_EQUIVALENT_VOTER_ID: the
# Gemini voter at the API default — no thinkingLevel in the request, which for
# gemini-3.5-flash-lite is "minimal" — and the GPT voter at an explicit "low", which is
# exactly what SCREENING_EFFORT_1 / SCREENING_EFFORT_2 send. Only the LABEL differed.
#
#   "gemini-3.5-flash-lite+gpt-5.4-mini"  — the unlabelled pair, before the key named
#   an effort at all. This is what the entries on disk are under (4,767 of the 4,817
#   local classify entries when this was written).
#
#   "…@effort=medium"  — the label dcc0ba6 briefly derived from the model id, because
#   the voter id matched LINKING_MODEL. That value was never sent to any provider; it
#   is registered because a collaborator's cache pulled from HF may carry it.
_CLASSIFY_LEGACY_KEY_PARTS = (
    "gemini-3.5-flash-lite+gpt-5.4-mini",
    "gemini-3.5-flash-lite+gpt-5.4-mini@effort=medium",
)


def _classify_keys(doi_r: str, study_r: str, abstract_r: str) -> tuple[str, list[str], dict]:
    """`(key, legacy_keys, migrate_note)` for one classify call's cache entry.

    Split out so the cache can be READ without any possibility of a call being
    made — `cached_classification()` below — while the key stays defined exactly
    once. The voter pair is part of the key because the two models disagree often
    enough that this is the question the audit measured a model effect on.
    """
    voters = screen_voters()
    cls_prompt = build_classify_prompt(study_r, abstract_r)
    pv = prompt_version("build_classify_prompt")
    voter_id = "+".join(cache_model_id(model, effort)
                        for _, model, _, effort in voters)
    key = content_key("classify", doi_r or study_r, pv, voter_id, cls_prompt)
    # The equivalence was declared for one pair of voters; a run screening any other
    # pair is asking a question those cached answers do not answer, and must pay for it.
    legacy = ([content_key("classify", doi_r or study_r, pv, part, cls_prompt)
               for part in _CLASSIFY_LEGACY_KEY_PARTS]
              if voter_id == _CLASSIFY_EQUIVALENT_VOTER_ID else [])
    return key, legacy, {"prompt_version": pv, "voters": voter_id}


def cached_classification(doi_r: str, study_r: str, abstract_r: str) -> "dict | None":
    """The stored `classify_replication()` answer for this row, or None. Never calls.

    The one read-only door onto the screen's cache. `filter/engine/handoff.py` uses
    it to put the two descriptive fields the verdict table does not hold — the
    category union and the voters' reasoning — onto the row Stage 3 reads. A miss
    is not an error and must never become a call: the handoff is a materialising
    step over rows that were already paid for, and a cache miss there means the
    row travels with those two columns blank, not that the screen runs again.
    """
    key, legacy, note = _classify_keys(doi_r, study_r, abstract_r)
    cached = read_cache_migrating(LLM_CACHE_DIR, key, legacy, note)
    return cached if isinstance(cached, dict) else None


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
    key, legacy, note = _classify_keys(doi_r, study_r, abstract_r)
    cached = read_cache_migrating(LLM_CACHE_DIR, key, legacy, note)
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
    votes = [v for v in (_classify_once(cls_prompt, m, eff)
                         for _p, m, _e, eff in voters) if v]

    # Keep the individual votes: the gate's decision is not reviewable without
    # knowing who said what.
    # The quote and the model ride along per vote: the evidence a reviewer reads is
    # one line per voter, and neither `llm_evidence` nor a "+"-joined model string can
    # be split back into whose quote was whose.
    out["votes"] = [{k: v[k] for k in
                     ("provider", "model", "classification", "confident", "categories",
                      "evidence", "reasoning")}
                    for v in votes]
    out["llm_source"] = "+".join(v["provider"] for v in votes)
    out["llm_model"]  = "+".join(v["model"] for v in votes)
    out["llm_evidence"]  = format_screen_evidence(votes)
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
            p for p, _, _, _ in voters if p not in answered)
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
    "unidentified_count": 0, "outcome_block": {},
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
        pick = resolve_targets_and_outcomes(doi_r, study_r, abstract_r,
                                            candidates or [], refs,
                                            record_type=(out["record_type"]
                                                         or "replication"),
                                            rung="reftarget")
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
            "outcome_block":      pick["outcome_block"],
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
