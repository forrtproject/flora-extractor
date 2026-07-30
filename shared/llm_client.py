"""
llm_client.py — the pipeline's LLM calls: provider ladder, original-study
identification, and the two-model replication screen.

Provider ladder: Gemini → OpenAI → OpenRouter, in call_llm_ladder(). Each provider
is rate-limited against its own last-call timestamp.

Public API:
    identify_original_with_llm(doi_r, study_r, abstract_r, pattern,
                                candidates, sections) → dict
"""
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

from .config import (
    GEMINI_API_KEYS, GEMINI_MODEL, GEMINI_LIGHT_MODEL, GEMINI_HEAVY_MODEL,
    GEMINI_USE_FLEX, GEMINI_FLEX_TIMEOUT, GEMINI_PAID_KEYS, GEMINI_RATE_SEC,
    LLM_CACHE_DIR,
    OPENAI_API_KEY, OPENAI_MODEL, OPENAI_RATE_SEC,
    OPENROUTER_API_KEY, OPENROUTER_HEAVY_MODEL, OPENROUTER_RATE_SEC,
    SCREEN_VOTER2_MODEL,
    log,
)
from . import token_counter
from .cache import content_key, read_cache, write_cache
from .prompts import (
    JSON_SYSTEM_MESSAGE,
    build_classify_prompt, build_identification_prompt,
    build_multi_original_prompt, build_target_prompt, prompt_version,
)
from .schema import OUTCOME_CATEGORIES
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

# ── OpenAI session-level token guardrail ─────────────────────────────────────
# Tracks tokens consumed via call_openai() within the current process.
# When usage crosses OPENAI_WARN_TOKENS, the pipeline pauses and asks the user
# whether to continue.  Override the threshold via env var (in tokens):
#   OPENAI_WARN_TOKENS=9000000
_openai_tokens_session: int = 0
_openai_limit_prompted: bool = False   # ensure we only prompt once
_openai_disabled:       bool = False   # set to True when user answers N
_OPENAI_WARN_THRESHOLD: int = int(os.getenv("OPENAI_WARN_TOKENS", "8000000"))


def _track_openai_tokens(n_tokens: int) -> None:
    """Add n_tokens to the session counter and prompt the user if the threshold is crossed."""
    global _openai_tokens_session, _openai_limit_prompted, _openai_disabled
    _openai_tokens_session += n_tokens
    if _openai_limit_prompted or _openai_tokens_session < _OPENAI_WARN_THRESHOLD:
        return
    _openai_limit_prompted = True
    used_m     = _openai_tokens_session / 1_000_000
    thresh_m   = _OPENAI_WARN_THRESHOLD  / 1_000_000
    print(f"\n{'=' * 62}")
    print(f"  OpenAI token guardrail: {used_m:.1f}M tokens used this session")
    print(f"  (threshold: {thresh_m:.0f}M — set OPENAI_WARN_TOKENS to change)")
    print(f"  Continue using OpenAI for remaining rows?")
    print(f"  Y = keep going   N = disable OpenAI (Gemini-only for rest of run)")
    print(f"{'=' * 62}")
    try:
        answer = input("  Your choice [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("n", "no"):
        _openai_disabled = True
        log.info("OpenAI disabled by user at %.1fM tokens", used_m)
        print("  OpenAI disabled. Remaining rows will use Gemini only.\n")
    else:
        log.info("User confirmed continuing OpenAI at %.1fM tokens", used_m)
        print("  Continuing with OpenAI.\n")


# ── Gemini key-0 (paid) session-level token guardrail ────────────────────────
# Only active when GEMINI_WARN_TOKENS is set to a positive integer (default: 0 = off).
# Tracks tokens on key 0 only (the paid key). Keys 1+ are free-tier and unaffected.
# When usage crosses the threshold the pipeline pauses and asks whether to continue.
_gemini_key0_tokens_session: int  = 0
_gemini_key0_limit_prompted: bool = False
_gemini_key0_disabled:       bool = False
_GEMINI_WARN_THRESHOLD: int = int(os.getenv("GEMINI_WARN_TOKENS", "0"))


def _track_gemini_key0_tokens(n_tokens: int) -> None:
    """Add n_tokens for key-0 and prompt the user if the threshold is crossed."""
    global _gemini_key0_tokens_session, _gemini_key0_limit_prompted, _gemini_key0_disabled
    if not _GEMINI_WARN_THRESHOLD:
        return
    _gemini_key0_tokens_session += n_tokens
    if _gemini_key0_limit_prompted or _gemini_key0_tokens_session < _GEMINI_WARN_THRESHOLD:
        return
    _gemini_key0_limit_prompted = True
    used_m   = _gemini_key0_tokens_session / 1_000_000
    thresh_m = _GEMINI_WARN_THRESHOLD / 1_000_000
    print(f"\n{'=' * 62}")
    print(f"  Gemini key-0 guardrail: {used_m:.1f}M tokens used this session")
    print(f"  (threshold: {thresh_m:.0f}M — set GEMINI_WARN_TOKENS to change)")
    print(f"  Continue using Gemini key-0 (paid) for remaining rows?")
    print(f"  Y = keep going   N = skip key-0 (free-tier keys only for rest of run)")
    print(f"{'=' * 62}")
    try:
        answer = input("  Your choice [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("n", "no"):
        _gemini_key0_disabled = True
        log.info("Gemini key-0 disabled by user at %.1fM tokens", used_m)
        print("  Gemini key-0 disabled. Remaining rows will use free-tier keys only.\n")
    else:
        log.info("User confirmed continuing Gemini key-0 at %.1fM tokens", used_m)
        print("  Continuing with Gemini key-0.\n")


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

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(0))
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

    if GEMINI_USE_FLEX:
        log.debug("Gemini flex inference enabled on paid keys %s (timeout=%ds)",
                  sorted(GEMINI_PAID_KEYS), GEMINI_FLEX_TIMEOUT)

    last_error = "all keys exhausted"
    for key_idx, api_key in enumerate(GEMINI_API_KEYS):
        if key_idx == 0 and _gemini_key0_disabled:
            log.debug("Gemini key-0 disabled by guardrail — skipping to free-tier keys")
            continue
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

                text   = body["candidates"][0]["content"]["parts"][0]["text"]
                result = _parse_llm_json(text)
                if result is not None:
                    n_tok = int((body.get("usageMetadata") or {}).get("totalTokenCount", 0))
                    token_counter.record("gemini", n_tok)
                    if key_idx == 0:
                        _track_gemini_key0_tokens(n_tok)
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

def call_openai(prompt: str, model: str = OPENAI_MODEL) -> tuple[Optional[dict], str]:
    """
    Call OpenAI chat completion with response_format=json_object.
    Returns (result_dict_or_None, error_description).
    """
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — skipping OpenAI")
        return None, "OPENAI_API_KEY not configured"

    if _openai_disabled:
        return None, "OpenAI disabled — token limit reached and user declined to continue"

    import openai
    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    # #45: 3 attempts with exponential backoff (1s, 2s) per the api_error contract, so a
    # transient outage does not immediately poison a row after a single failure. call_gemini
    # already retries; this brings the OpenAI fallback to the same contract.
    last_error = "no attempts made"
    for attempt in range(3):
        try:
            _throttle("openai")
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": JSON_SYSTEM_MESSAGE},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                max_completion_tokens=JSON_MAX_OUTPUT_TOKENS,
            )
            if response.usage:
                _track_openai_tokens(response.usage.total_tokens)
                token_counter.record("openai", response.usage.total_tokens)
                log.debug("OpenAI usage: +%d tokens (session total: %d)",
                          response.usage.total_tokens, _openai_tokens_session)
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
        result = _parse_llm_json(response.choices[0].message.content)
        if result and response.usage:
            token_counter.record("openrouter", response.usage.total_tokens)
        return result, ("" if result else "response was not valid JSON")
    except Exception as e:
        log.warning("OpenRouter call failed (model=%s): %s", use_model, e)
        return None, f"exception: {e}"


# ── Unified LLM router ───────────────────────────────────────────────────────

def call_llm_ladder(prompt: str, gemini_model: str = "", openai_model: str = "",
                    prefer_openai: bool = False) -> tuple[Optional[dict], str, str, str]:
    """
    Route a prompt through the configured provider chain and return the first
    successful result, naming the provider that produced it.

    Default order : Gemini -> OpenAI -> OpenRouter (Qwen as last resort).
    prefer_openai : flip to OpenAI -> Gemini -> OpenRouter.
                    Use when Gemini is overloaded (503/429) and OpenAI is preferred.

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
        if provider == "openrouter" and not OPENROUTER_API_KEY:
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

def identify_original_with_llm(doi_r:          str,
                                 study_r:        str,
                                 abstract_r:     str,
                                 pattern:        str,
                                 candidates:     list[dict],
                                 sections:       dict,
                                 html_text:      str = "",
                                 validator_note: str = "",
                                 abstract_only:  bool = False) -> dict:
    """
    Identify the original study via LLM.

    html_text — extracted landing-page text as full-text substitute.

    Order: the shared ladder — Gemini → OpenAI → OpenRouter, the last skipped
    without an OPENROUTER_API_KEY.

    The cache key is the rendered prompt — the candidates, the parsed sections and
    the validator note all reach the model through it — plus the prompt version and
    the model that will answer. Keying on the DOI alone, as this did, meant the
    abstract-stage call and the full-text call collided, and a paper whose PDF had
    since been parsed replayed the answer given when only the abstract was known.

    Every answer the model gives is cached, including "no identifiable original":
    a decline is a result, and caching only successes made every declined full-text
    call repay its API cost on every re-run. API failures are still not cached.
    """
    prompt     = build_identification_prompt(study_r, abstract_r, pattern,
                                             candidates, sections,
                                             html_text=html_text,
                                             validator_note=validator_note)
    key = content_key("llm", doi_r,
                      prompt_version("build_identification_prompt"),
                      GEMINI_HEAVY_MODEL, abstract_only, prompt)
    cached = read_cache(LLM_CACHE_DIR, key)
    if cached is not None:
        cached.setdefault("llm_source", "cache")
        cached.setdefault("llm_prompt", "")
        cached.setdefault("llm_error",  "")
        return cached

    result, llm_source, llm_model, llm_error = call_llm_ladder(
        prompt, gemini_model=GEMINI_HEAVY_MODEL)

    _empty = {
        "resolved"          : False,
        "resolution_method" : "llm_failed",
        "resolved_doi_o"    : "",
        "resolved_title_o"  : "",
        "resolved_year_o"   : None,
        "resolved_author_o" : "",
        "resolution_score"  : 0.0,
        "llm_source"        : "none",
        "llm_model"         : "",
        "llm_confidence"    : "",
        "llm_evidence"      : "",
        "llm_reasoning"     : "",
        "llm_prompt"        : prompt,
        "llm_error"         : llm_error,
    }

    if not result:
        return _empty

    cand_num       = result.get("selected_candidate_number")
    resolved_doi   = ""
    resolved_title = (result.get("selected_title")        or "").strip()
    resolved_year  = result.get("selected_year")
    resolved_auth  = (result.get("selected_first_author") or "").strip()

    if cand_num is not None:
        try:
            idx = int(cand_num) - 1
            if 0 <= idx < len(candidates):
                c = candidates[idx]
                resolved_doi   = c.get("doi", "")
                resolved_title = resolved_title or c.get("title",        "")
                resolved_year  = resolved_year  or c.get("year")
                resolved_auth  = resolved_auth  or c.get("first_author", "")
        except (ValueError, TypeError):
            pass

    # When the original is not in the candidate list, resolve DOI from title+author
    # via CrossRef/OpenAlex rather than trusting any DOI the LLM may have fabricated.
    doi_from_title_search = False
    if not resolved_doi and resolved_title:
        from shared.doi_verify import resolve_doi_by_metadata
        hit = resolve_doi_by_metadata(
            resolved_title, resolved_auth, resolved_year,
            exclude_doi=doi_r,
        )
        if hit:
            resolved_doi = hit.get("doi", "")
            # Provenance matters: this DOI was NOT taken from the reference list, it
            # was searched for by title. Every doi_o mismatch in the 2026-07 audit came
            # from here, so downstream must be able to tell these apart.
            doi_from_title_search = bool(resolved_doi)

    resolved = bool(resolved_title)

    confidence_map = {"high": 1.0, "medium": 0.6, "low": 0.3}
    conf_str   = result.get("confidence", "low")
    conf_score = confidence_map.get(conf_str, 0.3)

    output = {
        "resolved"          : resolved,
        # llm_no_target: LLM ran successfully but concluded no identifiable original exists.
        # Distinct from llm_failed (all API calls errored) and llm_fulltext (original found).
        "resolution_method" : (
            f"llm_title_search_{llm_source}" if (resolved and doi_from_title_search) else
            (f"llm_cited_candidates_{llm_source}" if abstract_only else f"llm_{llm_source}")
        ) if resolved else "llm_no_target",
        "resolved_doi_o"    : resolved_doi,
        "resolved_title_o"  : resolved_title,
        "resolved_year_o"   : resolved_year,
        "resolved_author_o" : resolved_auth,
        "resolution_score"  : conf_score,
        "llm_source"        : llm_source,
        "llm_model"         : llm_model,
        "llm_confidence"    : conf_str,
        "llm_evidence"      : result.get("evidence",  ""),
        "llm_reasoning"     : result.get("reasoning", ""),
        "llm_prompt"        : prompt,
        "llm_response"      : json.dumps(result, ensure_ascii=False) if result else "",
        "llm_error"         : "",
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
                text = body["candidates"][0]["content"]["parts"][0]["text"]
                return _parse_llm_json(text)
            except Exception as e:
                log.warning("Gemini PDF call failed (key %d, attempt %d): %s",
                            key_idx + 1, attempt + 1, e)
                if attempt == 0:
                    time.sleep(3)

    return None


# ── Multi-original dispatcher ─────────────────────────────────────────────────

def identify_all_originals_with_llm(doi_r:        str,
                                      study_r:      str,
                                      abstract_r:   str,
                                      candidates:   list[dict],
                                      sections:     dict,
                                      html_text:    str = "",
                                      force_multi:  bool = False) -> dict:
    """
    Identify ALL original studies in a multi-target replication paper.

    Returns:
        {
          "resolved": bool,
          "is_false_positive": bool,
          "n_originals": int,
          "originals": [{"rank", "title", "doi", "first_author", "year",
                          "evidence", "confidence", "candidate_number"}],
          "llm_source": str,
          "llm_reasoning": str,
        }
    """
    prompt = build_multi_original_prompt(study_r, abstract_r, candidates,
                                          sections,
                                          html_text=html_text,
                                          force_multi=force_multi)
    # force_multi reaches the model through the prompt, so it separates the two
    # variants' entries by itself — no cache bypass needed.
    key = content_key("multi", doi_r,
                      prompt_version("build_multi_original_prompt"),
                      GEMINI_HEAVY_MODEL, prompt)
    cached = read_cache(LLM_CACHE_DIR, key)
    if cached is not None:
        cached.setdefault("llm_source", "cache")
        return cached

    _empty = {
        "resolved"         : False,
        "is_false_positive": False,
        "n_originals"      : 0,
        "originals"        : [],
        "llm_source"       : "none",
        "llm_reasoning"    : "",
    }

    result, llm_source, llm_model, _err = call_llm_ladder(
        prompt, gemini_model=GEMINI_HEAVY_MODEL)
    if not result:
        return _empty

    raw_originals = result.get("originals", [])
    originals = []
    for o in raw_originals:
        if not isinstance(o, dict):
            continue
        # If candidate_number given, fill missing fields from candidate list.
        cand_num = o.get("candidate_number")
        cand_doi = ""
        if cand_num is not None:
            try:
                idx = int(cand_num) - 1
                if 0 <= idx < len(candidates):
                    c = candidates[idx]
                    cand_doi = c.get("doi", "") or ""
                    o.setdefault("title", c.get("title",        ""))
                    o.setdefault("year",  c.get("year"))
                    o.setdefault("first_author_surname", c.get("first_author", ""))
            except (ValueError, TypeError):
                pass
        # Never trust a DOI the LLM emitted (the prompt no longer asks for one).
        # Use the selected candidate's verified OpenAlex DOI when there was one;
        # otherwise resolve from title+author+year via CrossRef/OpenAlex. This
        # mirrors the single-original path in identify_original_with_llm().
        title_o  = str(o.get("title", "") or "")
        author_o = str(o.get("first_author_surname", "") or "")
        year_o   = o.get("year")
        resolved_doi = cand_doi
        if not resolved_doi and title_o:
            from shared.doi_verify import resolve_doi_by_metadata
            hit = resolve_doi_by_metadata(title_o, author_o, year_o, exclude_doi=doi_r)
            if hit:
                resolved_doi = hit.get("doi", "") or ""
        raw_outcome = str(o.get("outcome", "cannot_be_determined") or "cannot_be_determined").lower()
        if raw_outcome not in OUTCOME_CATEGORIES:
            raw_outcome = "cannot_be_determined"
        originals.append({
            "rank"             : o.get("rank", len(originals) + 1),
            "title"            : title_o,
            "doi"              : str(resolved_doi or ""),
            "first_author"     : author_o,
            "year"             : o.get("year"),
            "evidence"         : str(o.get("evidence",        "") or ""),
            "confidence"       : str(o.get("confidence", "low") or "low"),
            "candidate_number" : cand_num,
            "outcome"          : raw_outcome,
            "outcome_evidence" : str(o.get("outcome_evidence", "") or ""),
        })

    n_originals = len(originals)
    # When force_multi=True the rule already confirmed this is multi-target;
    # never trust is_false_positive from the LLM in that case.
    if force_multi:
        is_false_positive = False
    else:
        is_false_positive = bool(result.get("is_false_positive", n_originals <= 1))

    output = {
        "resolved"         : n_originals > 0,
        "is_false_positive": is_false_positive,
        "n_originals"      : n_originals,
        "originals"        : originals,
        "llm_source"       : llm_source,
        "llm_model"        : llm_model,
        "llm_reasoning"    : str(result.get("reasoning", "") or ""),
    }

    # A run that found no originals is cached too: it is the model's answer, not a
    # failure, and re-asking it every run costs a heavy-model call per paper.
    write_cache(LLM_CACHE_DIR, key, output)
    return output



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
# Voter 2 runs on OpenRouter (Ministral 14B by default) rather than OpenAI: on
# adjudicated hard cases the pair discards 89% of true negatives against gpt-5-mini's
# 25%, losing no genuine replication, because a non-Google lineage errs elsewhere
# than the Gemini first voter does.
SCREEN_PROVIDERS = ("gemini", "openrouter")


def _screen_model(provider: str) -> str:
    return GEMINI_LIGHT_MODEL if provider == "gemini" else SCREEN_VOTER2_MODEL


def _classify_once(prompt: str, provider: str) -> "dict | None":
    """One classification vote. provider is 'gemini' or 'openrouter'."""
    if provider == "gemini":
        result, _ = call_gemini(prompt, model=GEMINI_LIGHT_MODEL)
    else:
        result, _ = call_openrouter(prompt, model=SCREEN_VOTER2_MODEL)
    if not result:
        return None
    return {
        "is_replication": str(result.get("is_replication", "unclear")).strip().lower(),
        "confidence":     str(result.get("confidence", "")).strip().lower(),
        "evidence":       str(result.get("evidence_quote", "") or ""),
        "reasoning":      str(result.get("reasoning", "") or ""),
        "provider":       provider,
    }


def classify_replication(doi_r: str, study_r: str, abstract_r: str) -> dict:
    """Q1 alone: do two models agree that this paper is a replication or reproduction?

    Split out of screen_references_with_llm so Stage 3 can ask the question as its
    front door — before match-type classification, the resolution ladder, the PDF
    and outcome coding — rather than after paying for all of them. The target pick
    (Q2) stays in screen_references_with_llm, at the point in the ladder where the
    reference list has been fetched, and reuses this verdict instead of re-voting.

    Returns:
      is_replication      — the agreed label, or "unclear" when the models disagree
      models_agree        — whether both votes matched
      classification_confidence — the weaker of the two votes' confidences
      votes / llm_*       — who voted what, for the reviewer of a set-aside row
      resolution_method   — llm_refscreen_declined on a complete screen;
                            llm_refscreen_partial (one vote) / llm_refscreen_failed
                            (none) when the screen did not complete. An incomplete
                            screen is an API failure, not a verdict, so it is
                            returned uncached — a re-run must be able to succeed.
    """
    cls_prompt = build_classify_prompt(study_r, abstract_r)
    # The voter pair is part of the verdict — the two models disagree often enough
    # that this is the question the audit measured a model effect on — so both
    # models are in the key alongside the prompt version and the text they see.
    key = content_key("classify", doi_r or study_r,
                      prompt_version("build_classify_prompt"),
                      "+".join(_screen_model(p) for p in SCREEN_PROVIDERS),
                      cls_prompt)
    cached = read_cache(LLM_CACHE_DIR, key)
    if cached is not None:
        return cached

    out = {
        "resolution_method": "llm_refscreen_declined",
        "is_replication": "unclear", "models_agree": False, "votes": [],
        "classification_confidence": "",
        "llm_source": "", "llm_model": "", "llm_evidence": "",
        "llm_reasoning": "", "llm_prompt": "", "llm_error": "",
    }

    out["llm_prompt"] = cls_prompt
    votes = [v for v in (_classify_once(cls_prompt, p) for p in SCREEN_PROVIDERS) if v]

    # Keep the individual votes: a disagreement row is set aside for human review,
    # and "the models disagreed" is not reviewable without knowing who said what.
    out["votes"] = [{k: v[k] for k in ("provider", "is_replication", "confidence", "reasoning")}
                    for v in votes]
    out["llm_source"] = "+".join(v["provider"] for v in votes)
    out["llm_model"]  = "+".join(_screen_model(v["provider"]) for v in votes)
    out["llm_evidence"]  = votes[0]["evidence"] if votes else ""
    out["llm_reasoning"] = " | ".join(f"{v['provider']}: {v['reasoning']}" for v in votes)

    # A missing vote is an API failure, not a verdict. Reporting it as a normal
    # result would file the row as a two-model disagreement and corrupt the
    # agreement rate, and caching it would freeze one transient failure into a
    # permanent one. Return uncached so a re-run can screen the row properly.
    if len(votes) < 2:
        answered = {v["provider"] for v in votes}
        out["resolution_method"] = ("llm_refscreen_partial" if votes
                                    else "llm_refscreen_failed")
        out["llm_error"] = "classifier failed: " + ", ".join(
            p for p in SCREEN_PROVIDERS if p not in answered)
        return out

    labels = {v["is_replication"] for v in votes}
    out["models_agree"] = len(votes) == 2 and len(labels) == 1
    if out["models_agree"]:
        out["is_replication"] = votes[0]["is_replication"]
        # The weaker vote governs: acting on a discard needs both models sure.
        order = {"high": 2, "medium": 1, "low": 0, "": 0}
        out["classification_confidence"] = min(
            (v["confidence"] for v in votes), key=lambda c: order.get(c, 0))
    else:
        # Disagreement is informative, not an error — the row is set aside for review.
        out["is_replication"] = "unclear"

    write_cache(LLM_CACHE_DIR, key, out)
    return out


def screen_references_with_llm(doi_r: str, study_r: str, abstract_r: str,
                               refs: list[dict],
                               classification: "dict | None" = None) -> dict:
    """Identify the paper's target among its references, given the Q1 verdict.

    classification — the verdict from classify_replication(). Stage 3 runs the
    classification at its front door and threads the result in here, so the two
    votes are made once per paper. When it is absent (a caller that has no verdict
    yet, e.g. the batch tools) the classification runs here.

    Returns the standard resolver dict, the classification fields, and
    llm_confidence — the TARGET call's confidence, empty when no target call was
    made. A reference is accepted as the target only at confidence == "high".
    """
    out = {
        "resolved": False, "resolution_method": "llm_refscreen_declined",
        "resolved_doi_o": "", "resolved_title_o": "", "resolved_year_o": None,
        "resolved_author_o": "", "resolution_score": 0.0,
        "llm_confidence": "", "target_description": "",
    }
    out.update(classification or classify_replication(doi_r, study_r, abstract_r))

    if out["is_replication"] == "yes" and refs:
        # The pick is cached separately from the classification: the two halves are
        # now decided at different points in the pipeline, and one cache holding
        # both would be written before the second half had run.
        # The reference list is in the key via the rendered prompt: a re-fetched
        # or re-parsed list changes which numbered reference the pick refers to,
        # so replaying the previous pick would point at a different paper.
        tgt_prompt = build_target_prompt(study_r, abstract_r, refs)
        tgt_key = content_key("reftarget", doi_r or study_r,
                              prompt_version("build_target_prompt"),
                              GEMINI_HEAVY_MODEL, tgt_prompt)
        cached = read_cache(LLM_CACHE_DIR, tgt_key)
        if cached is not None:
            result, tgt_source, tgt_model = cached["result"], cached["source"], cached["model"]
        else:
            result, tgt_source, tgt_model, _ = call_llm_ladder(
                tgt_prompt, gemini_model=GEMINI_HEAVY_MODEL)
            if result:
                write_cache(LLM_CACHE_DIR, tgt_key,
                            {"result": result, "source": tgt_source,
                             "model": tgt_model})
        if result:
            out["llm_confidence"]     = str(result.get("confidence", "")).strip().lower()
            out["target_description"] = str(result.get("target_description", "") or "").strip()
            num = result.get("target_number")
            if num is not None and out["llm_confidence"] == "high":
                try:
                    ref = refs[int(num) - 1]
                except (ValueError, TypeError, IndexError):
                    ref = None
                if ref is not None:
                    # The link is this call's decision, not Q1's, so the row is
                    # attributed to the model that picked the reference and
                    # carries the quote that justifies the pick.
                    out.update({
                        "resolved":          True,
                        "resolution_method": "llm_references",
                        "resolved_doi_o":    clean_doi(ref.get("doi", "") or ""),
                        "resolved_title_o":  ref.get("title", "") or "",
                        "resolved_year_o":   ref.get("publication_year") or ref.get("year"),
                        "resolved_author_o": ref.get("first_author", "") or "",
                        "resolution_score":  1.0,
                        "llm_source":        tgt_source,
                        "llm_model":         tgt_model,
                        "llm_evidence":      (str(result.get("evidence_quote", "") or "").strip()
                                              or out["llm_evidence"]),
                        "llm_reasoning":     " | ".join(filter(None, [
                            out["llm_reasoning"],
                            f"target ({tgt_source}): {result.get('reasoning', '') or ''}".strip(),
                        ])),
                    })

    return out
