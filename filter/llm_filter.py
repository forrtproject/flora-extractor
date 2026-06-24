"""
llm_filter.py — Stage 2 LLM uplift for rows the rule filter couldn't decide.

Only rows with ``filter_status == 'needs_review'`` are sent to the LLM.
Primary model: OpenAI (FILTER_OPENAI_MODEL, default gpt-5.4-mini).
Fallback model: Gemini (rotates API keys automatically on 429).

Results are cached by hash(title + abstract). Cache key uses the same
cache_key() helper as all other stages so re-runs are free.
"""

import time
from typing import Optional

from shared.cache import read_cache, write_cache
from shared.config import (
    FILTER_OPENAI_MODEL,
    GEMINI_API_KEYS, GEMINI_MODEL,
    LLM_CACHE_DIR, LLM_RATE_SEC,
    OPENAI_API_KEY, log,
)
from shared import token_counter
from shared.llm_client import call_gemini, call_openai
from shared.utils import cache_key

VALID_STATUSES   = {"replication", "reproduction", "false_positive", "needs_review"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def _build_prompt(title: str, abstract: str) -> str:
    return (
        "You are an expert in scientific replication and reproducibility.\n\n"
        "Classify the paper into EXACTLY ONE label:\n\n"
        "replication\n"
        "- Uses NEW data/samples/populations to test whether a prior study's findings hold.\n"
        "- Must intentionally replicate a specific prior study or experiment.\n"
        "- Can be direct/close or conceptual.\n"
        "- Replication must be an explicit study aim, not merely a discussion point or side result.\n"
        "- Includes secondary-data replications using different data.\n"
        "- Key criterion: different data from the original study.\n\n"
        "reproduction\n"
        "- Reanalyzes the SAME original data/results from a prior study.\n"
        "- Focuses on computational reproducibility or robustness of reported findings.\n"
        "- Key criterion: same original dataset/data source.\n\n"
        "false_positive\n"
        "- NOT actually a replication or reproduction despite similar language.\n"
        "- Includes:\n"
        "  - meta-analyses or systematic reviews\n"
        "  - papers about the replication crisis/research methodology\n"
        "  - data/code release papers\n"
        "  - biological replication (cells, DNA, organisms, viruses)\n"
        "  - robustness/sensitivity checks within the original paper\n"
        "  - papers mentioning 'replication' casually without conducting one\n\n"
        "Decision rules:\n"
        "1. If authors explicitly describe the study as a replication, classify as replication "
        "unless clearly false_positive.\n"
        "2. If authors explicitly describe using the original data for reproducibility, classify "
        "as reproduction unless clearly false_positive.\n"
        "3. New data → replication.\n"
        "4. Same original data → reproduction.\n"
        "5. false_positive overrides whenever the paper only superficially resembles "
        "replication/reproduction.\n\n"
        "PAPER TO CLASSIFY\n\n"
        f"Title:\n{title!r}\n\n"
        f"Abstract:\n{abstract!r}\n\n"
        "Return ONLY valid JSON:\n\n"
        "{\n"
        '  "filter_status": "replication" | "reproduction" | "false_positive",\n'
        '  "filter_confidence": "high" | "medium" | "low",\n'
        '  "filter_evidence": "<short verbatim supporting phrase ≤120 chars>",\n'
        '  "filter_sort": "<one-sentence explanation>"\n'
        "}"
    )


def classify_with_llm(title: str, abstract: str) -> Optional[dict]:
    """Return a dict with filter_status, filter_confidence, filter_evidence, or None on hard failure.

    Primary: OpenAI (FILTER_OPENAI_MODEL).  Fallback: Gemini (rotates keys on 429).
    Results cached by hash(title + abstract) in LLM_CACHE_DIR.
    """
    cache_id = cache_key(f"filter|{title}|{abstract}")
    cached = read_cache(LLM_CACHE_DIR, cache_id)
    if cached is not None:
        return cached

    prompt = _build_prompt(title, abstract)
    result = None
    err    = "no API keys configured"
    token_counter.set_stage("filter")

    # Primary: OpenAI gpt-5.4-mini
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

    confidence = str(result.get("filter_confidence") or "").strip().lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    # filter_evidence takes the verbatim quote if present; falls back to the one-sentence rationale
    evidence = str(
        result.get("filter_evidence") or result.get("filter_sort") or ""
    ).strip()[:240]

    out = {
        "filter_status":     status,
        "filter_confidence": confidence,
        "filter_evidence":   evidence,
    }
    write_cache(LLM_CACHE_DIR, cache_id, out)
    return out
