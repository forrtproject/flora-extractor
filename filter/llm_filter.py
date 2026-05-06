"""
llm_filter.py — Stage 2 LLM uplift for rows the rule filter couldn't decide.

Only rows with ``filter_status == 'needs_review'`` are sent to the LLM. The
prompt asks Gemini for a JSON verdict (replication / reproduction /
false_positive / needs_review) plus a short evidence quote and a confidence
band. Results are cached by hash(title + abstract).

If no GEMINI_API_KEY (and no OPENAI_API_KEY fallback) is set, this module is
a no-op: it leaves the rule-based ``needs_review`` rows untouched and logs a
warning. That keeps offline runs working and avoids fake "LLM verdicts."
"""

import time
from typing import Optional

import pandas as pd

from shared.cache import read_cache, write_cache
from shared.config import GEMINI_API_KEYS, GEMINI_MODEL, LLM_CACHE_DIR, LLM_RATE_SEC, OPENAI_API_KEY, log
from shared.llm_client import call_gemini, call_openai
from shared.utils import cache_key

VALID_STATUSES = {"replication", "reproduction", "false_positive", "needs_review"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def _build_prompt(title: str, abstract: str) -> str:
    return (
        "You are classifying whether an academic paper reports a replication or "
        "reproduction study of a previously-published finding.\n\n"
        "Possible labels:\n"
        '  - "replication": the paper conducts a new empirical study that targets a specific, '
        "named prior finding (look for an explicit author–year reference and an explicit "
        '"replicate"/"replication"/"reproduce"/"reproduction" claim).\n'
        '  - "reproduction": the paper re-analyses or re-runs the original analysis on the same '
        "or similar data (sometimes called computational reproduction).\n"
        '  - "false_positive": the paper uses replication/reproduction terminology in a non-'
        "scholarly sense (DNA replication, code/data replication, replication fork, etc.) "
        "OR uses the term loosely without a specific target.\n"
        '  - "needs_review": you genuinely cannot tell from title + abstract.\n\n'
        f"Title: {title!r}\n"
        f"Abstract: {abstract!r}\n\n"
        "Reply with JSON exactly matching this schema:\n"
        '  {"filter_status": "replication"|"reproduction"|"false_positive"|"needs_review",\n'
        '   "filter_confidence": "high"|"medium"|"low",\n'
        '   "filter_evidence": "<short quote ≤120 chars from title or abstract>"}\n'
    )


def classify_with_llm(title: str, abstract: str) -> Optional[dict]:
    """Return a dict with the three filter fields, or None on hard failure."""
    cache_id = cache_key(f"filter|{title}|{abstract}")
    cached = read_cache(LLM_CACHE_DIR, cache_id)
    if cached is not None:
        return cached

    prompt = _build_prompt(title, abstract)
    result, err = (None, "no LLM keys configured")
    if GEMINI_API_KEYS:
        result, err = call_gemini(prompt, model=GEMINI_MODEL)
        time.sleep(LLM_RATE_SEC)
    if result is None and OPENAI_API_KEY:
        result, err = call_openai(prompt)
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
    evidence = (result.get("filter_evidence") or "").strip()[:240]

    out = {
        "filter_status": status,
        "filter_confidence": confidence,
        "filter_evidence": evidence,
    }
    write_cache(LLM_CACHE_DIR, cache_id, out)
    return out


def apply_llm_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Run the LLM only on rows that the rule filter left as ``needs_review``."""
    if "filter_status" not in df.columns:
        log.warning("LLM filter: rule filter must run first; nothing to do")
        return df

    if not GEMINI_API_KEYS and not OPENAI_API_KEY:
        skipped = (df["filter_status"] == "needs_review").sum()
        if skipped:
            log.warning(
                "LLM filter: no API keys configured — leaving %d needs_review rows untouched",
                int(skipped),
            )
        return df

    target_idx = df.index[df["filter_status"] == "needs_review"]
    if len(target_idx) == 0:
        log.info("LLM filter: no needs_review rows; skipping")
        return df

    log.info("LLM filter: classifying %d rows", len(target_idx))
    for i, idx in enumerate(target_idx, 1):
        row = df.loc[idx]
        title = str(row.get("title_r") or "")
        abstract = str(row.get("abstract_r") or "")
        verdict = classify_with_llm(title, abstract)
        if verdict is None:
            continue
        df.at[idx, "filter_status"] = verdict["filter_status"]
        df.at[idx, "filter_confidence"] = verdict["filter_confidence"]
        prior = str(df.at[idx, "filter_evidence"] or "")
        df.at[idx, "filter_evidence"] = (
            f"{prior} | llm:{verdict['filter_evidence']}" if prior else f"llm:{verdict['filter_evidence']}"
        )
        prior_method = str(df.at[idx, "filter_method"] or "")
        df.at[idx, "filter_method"] = "both" if prior_method == "rule_based" else "llm"
        if i % 25 == 0:
            log.info("  llm-filtered %d/%d", i, len(target_idx))

    counts = df["filter_status"].value_counts().to_dict()
    log.info("LLM filter done: %s", counts)
    return df
