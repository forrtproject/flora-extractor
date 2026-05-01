"""
rule_filter.py — Rule-based classifier for Stage 2.

Rules (from RULEBOOK.md):
  - Paper must have an explicit replication phrase AND a specific author-year citation
  - Vague phrases ("we replicate prior findings on X") → false_positive
  - original_match_type=multiple_original when 2+ distinct cited author-year patterns AND
    explicit multi-study language ("Study 1", "Study 2", "Experiments 1-3")

Public API:
    apply_rule_filter(df) → pd.DataFrame  (adds FILTER_ADDED_COLS)
"""
import re

import pandas as pd

from shared.config import log
from shared.openalex_client import extract_author_year_patterns
from shared.utils import clean_doi

# Explicit replication phrases (strong signal)
_REPLICATION_PHRASES = re.compile(
    r"\b(direct replication of|close replication of|conceptual replication of"
    r"|we replicated|we aimed to replicate|this replication|this study replicates"
    r"|replication study of|reproduction of)\b",
    re.IGNORECASE,
)

# Multi-study language (needed for original_match_type = multiple_original)
_MULTI_STUDY_PHRASES = re.compile(
    r"\b(study 1|study 2|study 3|experiment 1|experiment 2|experiments 1"
    r"|studies 1|studies 2)\b",
    re.IGNORECASE,
)


def apply_rule_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add filter columns to *df* based on rule-based classification.

    Sets filter_status to:
      replication   — explicit replication phrase + author-year citation found
      reproduction  — explicit reproduction phrase found
      false_positive — vague/no specific target
      needs_review  — uncertain; requires LLM pass
    """
    # TODO: implement full rule filter
    raise NotImplementedError("apply_rule_filter is not yet implemented")
