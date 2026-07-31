"""
llm_filter.py — Stage 2 LLM uplift for rows the rule filter couldn't decide.

Only rows with ``filter_status == 'needs_review'`` are sent to the LLM, through the
shared provider ladder with OpenAI first: OpenAI (FILTER_OPENAI_MODEL, default
gpt-5-mini) → Gemini (rotates API keys automatically on 429) → OpenRouter.

Results are cached by hash(title + abstract). Cache key uses the same
cache_key() helper as all other stages so re-runs are free.
"""

from typing import Optional

from shared.cache import content_key, read_cache, write_cache
from shared.config import (
    FILTER_OPENAI_MODEL,
    GEMINI_MODEL,
    LLM_CACHE_DIR,
    log,
)
from shared import token_counter
from shared.llm_client import call_llm
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

    token_counter.set_stage("filter")

    # OpenAI (FILTER_OPENAI_MODEL) first, then Gemini, then OpenRouter — the shared
    # ladder, so this arm gets the same last-resort provider and the same retry
    # behaviour as every other LLM call in the pipeline.
    result, _model, err = call_llm(prompt, gemini_model=GEMINI_MODEL,
                                   openai_model=FILTER_OPENAI_MODEL,
                                   prefer_openai=True)

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
