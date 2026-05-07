"""
Classification labels (filter_status):

  replication    – collects NEW data to test a prior finding
  reproduction   – reanalyses the SAME original data computationally
  false_positive – looks like a replication/reproduction but is not
                   (meta-analysis, methods paper, biological replication, etc.)
  needs_review   – all LLM attempts failed; requires manual inspection

Columns added to the DataFrame:

  filter_status            – classification label (above)
  filter_sort              – one-sentence rationale from the LLM
  filter_method            – "gemini" | "openai" | "cache" | "failed"

Usage:
    import pandas as pd
    from llm_filter import run_llm_filter

    df = pd.read_csv("test_data.csv")
    df = run_llm_filter(df)
    df.to_csv("test_data_filtered.csv", index=False)
"""

from __future__ import annotations

# Load .env FIRST, before config.py is imported
from dotenv import load_dotenv
from pathlib import Path
import os

env_path = Path(__file__).parent / "env"
load_dotenv(env_path, override=True)

print("DEBUG GEMINI:", os.getenv("GEMINI_API_KEY"))
print("DEBUG OPENAI:", os.getenv("OPENAI_API_KEY"))
print("ENV PATH:", env_path)
print("EXISTS:", env_path.exists())


import json
import time
import logging
from typing import Optional

import pandas as pd

# Imports — .py and .env files are in the same folder
from filter.llm_client import call_gemini, call_openai  # (dict|None, str)
from filter.config import LLM_CACHE_DIR, LLM_RATE_SEC, log
from filter.utils import cache_key  # MD5 hex string

logger = logging.getLogger("flora.filter")


# Prompt

_PROMPT_TEMPLATE = """\
You are an expert in scientific replication and reproducibility.

Classify the paper into EXACTLY ONE label:

replication
- Uses NEW data/samples/populations to test whether a prior study's findings hold.
- Must intentionally replicate a specific prior study or experiment.
- Can be direct/close or conceptual.
- Replication must be an explicit study aim, not merely a discussion point or side result.
- Includes secondary-data replications using different data.
- Key criterion: different data from the original study.

reproduction
- Reanalyzes the SAME original data/results from a prior study.
- Focuses on computational reproducibility or robustness of reported findings.
- Key criterion: same original dataset/data source.

false_positive
- NOT actually a replication or reproduction despite similar language.
- Includes:
  - meta-analyses or systematic reviews
  - papers about the replication crisis/research methodology
  - data/code release papers
  - biological replication (cells, DNA, organisms, viruses)
  - robustness/sensitivity checks within the original paper
  - papers mentioning “replication” casually without conducting one

Decision rules:
1. If authors explicitly describe the study as a replication, classify as replication unless clearly false_positive.
2. If authors explicitly describe the study as a reproduction/reproducibility analysis using the original data, classify as reproduction unless clearly false_positive.
3. New data → replication.
4. Same original data → reproduction.
5. false_positive overrides whenever the paper only superficially resembles replication/reproduction.

PAPER TO CLASSIFY

Title:
{study_r}

Abstract:
{abstract_r}

Return ONLY valid JSON:

{{
  "filter_status": "replication" | "reproduction" | "false_positive",
  "filter_sort": "<one-sentence explanation>",
  "filter_evidence": "<short verbatim supporting phrase>"
}}
"""


def _build_prompt(study_r: str, abstract_r: str) -> str:
    return _PROMPT_TEMPLATE.format(
        study_r=(study_r or "(not provided)"),
        abstract_r=(abstract_r or "(not provided)"),
    )


# Cache Helpers


def _cache_path(doi: str) -> Path:
    return LLM_CACHE_DIR / f"filter_{cache_key(doi + '_filter')}.json"


def _read_cache(doi: str) -> Optional[dict]:
    p = _cache_path(doi)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _write_cache(doi: str, result: dict) -> None:
    try:
        _cache_path(doi).write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not write cache for '%s': %s", doi, exc)


# Single-row Classification

_RETRY_DELAYS = [1, 2, 4]  # 3 attempts, exponential backoff in seconds

_EMPTY_RESULT: dict = {
    "filter_status": "needs_review",
    "filter_sort": "Automatic classification failed after 3 retries; manual review required.",
    "filter_method": "failed",
}


def _classify_single(doi: str, study_r: str, abstract_r: str) -> dict:
    """
    Classify one paper. Returns a result dict; never raises.

    Cache key  : cache_key(doi + "_filter")
    Cache hit  → filter_method = "cache", no LLM call
    3 failures → filter_status = "needs_review", api_error populated
    """
    # cache check
    cached = _read_cache(doi)
    if cached is not None:
        logger.debug("Cache hit for DOI '%s'", doi)
        cached["filter_method"] = "cache"
        return cached

    prompt = _build_prompt(study_r, abstract_r)
    last_error = "unknown error"

    for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
        # Gemini first — same precedence as identify_original_with_llm
        result, gemini_err = call_gemini(prompt)
        llm_source = "gemini"

        if result is None:
            logger.debug(
                "Gemini failed (attempt %d, doi='%s'): %s — trying OpenAI",
                attempt,
                doi,
                gemini_err,
            )
            result, openai_err = call_openai(prompt)
            llm_source = "openai"
            last_error = f"Gemini: {gemini_err} | OpenAI: {openai_err}"
        else:
            last_error = ""

        if result is not None and "filter_status" in result:
            out = {
                "filter_status": result["filter_status"],
                "filter_sort": result.get("filter_sort", ""),
                "filter_method": llm_source,
                "filter_evidence": result.get("filter_evidence", ""),
            }
            _write_cache(doi, out)
            return out

        logger.warning(
            "Attempt %d/%d failed for doi='%s': %s",
            attempt,
            len(_RETRY_DELAYS),
            doi,
            last_error,
        )
        if attempt < len(_RETRY_DELAYS):
            time.sleep(delay)

    # all retries exhausted
    logger.error("All retries exhausted for doi='%s'. Marking as needs_review.", doi)
    return {**_EMPTY_RESULT, "api_error": last_error}


# Functions (DataFrame)


def run_llm_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    Run the LLM filter over every row in df.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns: doi_r, study_r, abstract_r

    Returns
    -------
    pd.DataFrame
        Same df with filter columns added/overwritten (see module docstring).
        Rows where all retries failed also get an api_error column.
    """
    required = {"doi_r", "study_r", "abstract_r"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame is missing required columns: {missing}")

    results: list[dict] = []
    last_llm_call_t: float = 0.0
    total = len(df)

    for i, (_, row) in enumerate(df.iterrows(), start=1):
        doi = str(row.get("doi_r") or "")
        study_r = str(row.get("study_r") or "")
        abstract_r = str(row.get("abstract_r") or "")

        # Rate-limit only before a real LLM call, not on cache hits
        if _read_cache(doi) is None:
            elapsed = time.time() - last_llm_call_t
            wait = LLM_RATE_SEC - elapsed
            if wait > 0:
                time.sleep(wait)
            last_llm_call_t = time.time()

        log.info("[%d/%d] Classifying: %s", i, total, doi or study_r[:60])
        results.append(_classify_single(doi, study_r, abstract_r))

    result_df = pd.DataFrame(results, index=df.index)
    for col in result_df.columns:
        df[col] = result_df[col]

    return df
