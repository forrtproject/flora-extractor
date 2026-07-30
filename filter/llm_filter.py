"""
llm_filter.py — Stage 2 LLM uplift for rows the rule filter couldn't decide.

Only rows with ``filter_status == 'needs_review'`` are sent to the LLM.
Primary model: OpenAI (FILTER_OPENAI_MODEL, default gpt-5-mini).
Fallback model: Gemini (rotates API keys automatically on 429).

Results are cached by hash(title + abstract). Cache key uses the same
cache_key() helper as all other stages so re-runs are free.
"""

import time
from typing import Optional

from shared.cache import content_key, read_cache, write_cache
from shared.config import (
    FILTER_OPENAI_MODEL,
    GEMINI_API_KEYS, GEMINI_MODEL,
    LLM_CACHE_DIR, LLM_RATE_SEC,
    OPENAI_API_KEY, log,
)
from shared import token_counter
from shared.llm_client import call_gemini, call_openai
from shared.prompts import build_filter_prompt, prompt_version

VALID_STATUSES   = {"replication", "reproduction", "false_positive", "needs_review"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def classify_with_llm(title: str, abstract: str) -> Optional[dict]:
    """Return a dict with filter_status, filter_confidence, filter_evidence, or None on hard failure.

    Primary: OpenAI (FILTER_OPENAI_MODEL).  Fallback: Gemini (rotates keys on 429).
    Results are cached on the rendered prompt, its version and the model that
    answers it, in LLM_CACHE_DIR.
    """
    prompt = build_filter_prompt(title, abstract)
    cache_id = content_key("filter", "", prompt_version("build_filter_prompt"),
                           FILTER_OPENAI_MODEL, GEMINI_MODEL, prompt)
    cached = read_cache(LLM_CACHE_DIR, cache_id)
    if cached is not None:
        return cached

    result = None
    err    = "no API keys configured"
    token_counter.set_stage("filter")

    # Primary: OpenAI gpt-5-mini (FILTER_OPENAI_MODEL default)
    if OPENAI_API_KEY:
        result, err = call_openai(prompt, model=FILTER_OPENAI_MODEL)
        time.sleep(LLM_RATE_SEC)

    # Fallback: Gemini (call_gemini rotates through all GEMINI_API_KEYS on 429)
    if result is None and GEMINI_API_KEYS:
        result, err = call_gemini(prompt, model=GEMINI_MODEL)
        time.sleep(LLM_RATE_SEC)

    if result is None:
        log.warning("LLM filter: classification failed (%s)", err)
        return None

    status = str(result.get("filter_status") or "").strip().lower()
    if status not in VALID_STATUSES:
        log.warning("LLM filter: invalid filter_status %r — coercing to needs_review", status)
        status = "needs_review"

    confidence = str(result.get("confidence") or "").strip().lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    # filter_evidence takes the verbatim quote if present; falls back to the one-sentence rationale
    evidence = str(
        result.get("filter_evidence") or result.get("reasoning") or ""
    ).strip()[:240]

    out = {
        "filter_status":     status,
        "filter_confidence": confidence,
        "filter_evidence":   evidence,
    }
    write_cache(LLM_CACHE_DIR, cache_id, out)
    return out
