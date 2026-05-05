"""
code_outcome.py — Keyword + LLM outcome extraction for Stage 3.

Pass 1: keyword scan on title → abstract → fulltext (first high-confidence hit wins).
Pass 2: LLM call (Gemini → OpenAI) when keyword pass returns no confident match.

Public API:
    extract_outcome(doi_r, abstract_r, fulltext, title_r) → dict
"""
import json
import re
import time
from typing import Optional

from shared.config import GEMINI_LIGHT_MODEL, LLM_CACHE_DIR, LLM_RATE_SEC, log
from shared.llm_client import call_llm
from shared.utils import cache_key

# ── Keyword patterns (Pass 1) ─────────────────────────────────────────────────
# Failure is checked before success to avoid "failed to replicate" hitting success.

_FAILURE = re.compile(
    r"\b("
    r"failed to replicate|replication failed|could not replicate"
    r"|did not replicate|not replicated|no support for the original"
    r"|inconsistent with (?:the )?(?:original|prior)"
    r"|results did not (?:hold|replicate)|null result"
    r"|no evidence|no significant (?:effect|difference)"
    r"|failed to reproduce|did not reproduce"
    r")\b",
    re.IGNORECASE,
)

_SUCCESS = re.compile(
    r"\b("
    r"successfully replicated|replication succeeded|results (?:were )?replicated"
    r"|confirmed the (?:original|findings?|results?|effect)"
    r"|supported the original"
    r"|consistent with (?:the )?(?:original|prior)"
    r"|replication was successful|effect was reproduced"
    r"|was (?:successfully )?replicated|replicated successfully"
    r")\b"
    r"|(?<!\w)replicated(?!\w)",   # bare "replicated" as low-priority catch-all
    re.IGNORECASE,
)

_MIXED = re.compile(
    r"\b("
    r"partially replicated|mixed results?|partial replication"
    r"|some but not all|some (?:but not all|support)"
    r"|nuanced|qualified support"
    r"|smaller (?:effect|than original)|reduced (?:effect|magnitude)"
    r")\b",
    re.IGNORECASE,
)

_DESCRIPTIVE = re.compile(
    r"\b("
    r"adapted (?:the|this) (?:method|procedure|paradigm)"
    r"|in a (?:different|new) (?:context|sample|culture|population)"
    r"|not intended to test|not a direct test"
    r")\b",
    re.IGNORECASE,
)

_VALID_OUTCOMES = {"success", "failure", "mixed", "uninformative", "descriptive"}


def _keyword_scan(text: str, source: str) -> Optional[dict]:
    """Return a result dict if a keyword pattern matches, else None.

    Check order: failure → mixed → success → descriptive.
    Mixed is checked before success so that "partially replicated" resolves
    to mixed rather than triggering the broad bare-"replicated" success pattern.
    """
    m = _FAILURE.search(text)
    if m:
        return {"outcome": "failure", "outcome_phrase": m.group(0),
                "outcome_confidence": "high", "out_quote_source": source}
    m = _MIXED.search(text)
    if m:
        return {"outcome": "mixed", "outcome_phrase": m.group(0),
                "outcome_confidence": "medium", "out_quote_source": source}
    m = _SUCCESS.search(text)
    if m:
        return {"outcome": "success", "outcome_phrase": m.group(0),
                "outcome_confidence": "high", "out_quote_source": source}
    m = _DESCRIPTIVE.search(text)
    if m:
        return {"outcome": "descriptive", "outcome_phrase": m.group(0),
                "outcome_confidence": "medium", "out_quote_source": source}
    return None


def _llm_outcome(doi_r: str, title_r: str, abstract_r: str, fulltext: str) -> dict:
    """LLM-based outcome extraction. Result cached per doi_r."""
    cache_file = LLM_CACHE_DIR / f"outcome_{cache_key(doi_r)}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    abstract_snip = (abstract_r[:1000] + "…") if len(abstract_r) > 1000 else abstract_r
    text_snip = (fulltext[:800] + "…") if len(fulltext) > 800 else fulltext

    prompt = (
        "You are a research methodology expert. Classify the replication outcome.\n\n"
        f"TITLE: {title_r}\n"
        f"ABSTRACT: {abstract_snip or '(not available)'}\n"
        f"FULLTEXT EXCERPT: {text_snip or '(not available)'}\n\n"
        "Outcome values:\n"
        "- success: replication confirmed the original finding\n"
        "- failure: replication failed to find the original effect\n"
        "- mixed: some aspects replicated, others did not\n"
        "- uninformative: cannot determine from available text\n"
        "- descriptive: adapted methods in a new context without testing the original claim\n\n"
        "Respond with ONLY this JSON:\n"
        '{"outcome": "<value>", "outcome_phrase": "<supporting quote, max 2 sentences>", '
        '"outcome_confidence": "<high|medium|low>", "out_quote_source": "<abstract|fulltext|title>"}'
    )

    result, model_used, _ = call_llm(prompt, gemini_model=GEMINI_LIGHT_MODEL)
    if result:
        time.sleep(LLM_RATE_SEC)

    _fallback = {"outcome": "uninformative", "outcome_phrase": "",
                 "outcome_confidence": "low", "out_quote_source": "", "llm_model": ""}
    if not result:
        log.warning("[%s] outcome LLM failed — marking uninformative", doi_r)
        return _fallback

    outcome = str(result.get("outcome", "uninformative")).lower()
    if outcome not in _VALID_OUTCOMES:
        outcome = "uninformative"

    output = {
        "outcome":            outcome,
        "outcome_phrase":     str(result.get("outcome_phrase",    "") or ""),
        "outcome_confidence": str(result.get("outcome_confidence", "low") or "low"),
        "out_quote_source":   str(result.get("out_quote_source",  "") or ""),
        "llm_model":          model_used,
        "llm_prompt":         prompt,  # stored for debug view in Extract tab
    }
    with cache_file.open("w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    return output


def extract_outcome(doi_r: str,
                    abstract_r: str,
                    fulltext: str = "",
                    title_r: str = "") -> dict:
    """
    Extract replication outcome from available text.

    Returns:
        {
          "outcome":            str,  # success | failure | mixed | uninformative | descriptive
          "outcome_phrase":     str,  # supporting quote
          "outcome_confidence": str,  # high | medium | low
          "out_quote_source":   str,  # abstract | fulltext | title
        }
    """
    # Title scan — only act on high-confidence hits (avoid false triggers like "replication of X")
    if title_r:
        hit = _keyword_scan(title_r, "title")
        if hit and hit["outcome_confidence"] == "high":
            return hit

    # Abstract scan — accept any hit
    if abstract_r:
        hit = _keyword_scan(abstract_r, "abstract")
        if hit:
            return hit

    # Fulltext scan — only act on high-confidence hits
    if fulltext:
        hit = _keyword_scan(fulltext[:3000], "fulltext")
        if hit and hit["outcome_confidence"] == "high":
            return hit

    # LLM pass for uninformative or absent keyword matches
    return _llm_outcome(doi_r, title_r, abstract_r, fulltext)
