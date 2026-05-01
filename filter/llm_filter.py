"""
llm_filter.py — LLM-based classifier for Stage 2 (uncertain cases only).

Only called for rows where filter_status == "needs_review".
Uses Gemini (primary) → OpenAI (fallback).

Public API:
    apply_llm_filter(df) → pd.DataFrame
"""
import time

import pandas as pd

from shared.config import GEMINI_MODEL, log
from shared.llm_client import call_gemini, call_openai
from shared.utils import cache_key

# TODO: implement LLM filter for needs_review cases


def apply_llm_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    Run LLM classification on rows with filter_status == "needs_review".
    Updates filter_status, filter_method, filter_confidence in-place.
    """
    # TODO: implement full LLM filter
    raise NotImplementedError("apply_llm_filter is not yet implemented")
