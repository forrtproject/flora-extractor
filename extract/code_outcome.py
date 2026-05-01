"""
code_outcome.py — Keyword + LLM outcome extraction for Stage 3.

Classifies replication outcome as: success | failure | mixed | uninformative | pending

Public API:
    extract_outcome(doi_r, abstract_r, fulltext, title_r) → dict
"""
import re
from typing import Optional

from shared.config import log
from shared.llm_client import call_gemini, call_openai
from shared.utils import cache_key

# Keyword patterns for fast classification before LLM
_SUCCESS_PHRASES = re.compile(
    r"\b(successfully replicated|replication succeeded|results replicated"
    r"|confirmed the original|supported the original|consistent with the original"
    r"|replication was successful)\b",
    re.IGNORECASE,
)

_FAILURE_PHRASES = re.compile(
    r"\b(failed to replicate|replication failed|could not replicate"
    r"|did not replicate|no support for the original|inconsistent with the original"
    r"|results did not hold)\b",
    re.IGNORECASE,
)

_MIXED_PHRASES = re.compile(
    r"\b(partially replicated|mixed results|partial replication"
    r"|some support|qualifications|nuanced)\b",
    re.IGNORECASE,
)


def extract_outcome(doi_r:      str,
                    abstract_r: str,
                    fulltext:   str = "",
                    title_r:    str = "") -> dict:
    """
    Extract replication outcome from available text.

    Returns:
        {
          "outcome":            "success" | "failure" | "mixed" | "uninformative" | "pending",
          "outcome_phrase":     str,   # supporting quote
          "outcome_confidence": float, # 0.0–1.0
          "out_quote_source":   str,   # "abstract" | "fulltext" | "title"
        }
    """
    # TODO: implement keyword + LLM outcome extraction
    raise NotImplementedError("extract_outcome is not yet implemented")
