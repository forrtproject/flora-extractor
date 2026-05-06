"""
rule_filter.py — Stage 2 rule-based classifier.

Per RULEBOOK §Filter:
    - A paper passes as ``replication`` only when both an explicit replication
      phrase AND a specific author-year citation are present.
    - Vague phrases like "we replicate prior findings on X" become
      ``false_positive`` because no target study is named.
    - When only an exclusion pattern (DNA replication, code/data replication,
      replication fork/origin/stress/timing) fires, the row is ``false_positive``
      with high confidence.
    - When a phrase is present but no author-year cite is found, the row is
      ``needs_review`` for the LLM stage to decide.
    - If only reproduction-flavoured phrases fire, the status is ``reproduction``
      with the same author-year cite gating.

Adds the FILTER_ADDED_COLS to ``df`` (filter_status, filter_method,
filter_evidence, filter_confidence). ``filter_method`` is always
``rule_based`` here; ``llm_filter`` may overwrite it to ``llm`` or ``both``
later.
"""

import pandas as pd

from shared.config import log
from shared.openalex_client import extract_author_year_patterns
from shared.schema import FILTER_ADDED_COLS

from filter.phrase_detection import (
    find_replication_phrase,
    has_replication_phrase,
    is_non_scholarly_context,
    is_reproduction_only,
)


def _classify_row(title: str, abstract: str, year: int | None) -> dict:
    """Return the four FILTER_ADDED_COLS values for one candidate row."""
    title = title or ""
    abstract = abstract or ""
    text = f"{title}\n{abstract}".strip()

    # Hard-exclude non-scholarly contexts (DNA replication, code/data, etc.)
    excl = is_non_scholarly_context(text)
    if excl:
        return {
            "filter_status": "false_positive",
            "filter_method": "rule_based",
            "filter_evidence": f"exclusion:{excl}",
            "filter_confidence": "high",
        }

    if not has_replication_phrase(text):
        return {
            "filter_status": "false_positive",
            "filter_method": "rule_based",
            "filter_evidence": "no replication phrase detected",
            "filter_confidence": "high",
        }

    phrase = find_replication_phrase(text) or ""
    is_repro = is_reproduction_only(text)
    base_status = "reproduction" if is_repro else "replication"

    # Specific-target gate: at least one author–year citation must be present.
    cited = extract_author_year_patterns(text, max_year=year) if year else extract_author_year_patterns(text)
    if not cited:
        return {
            "filter_status": "needs_review",
            "filter_method": "rule_based",
            "filter_evidence": f"phrase:{phrase!s}; no author-year cite",
            "filter_confidence": "medium",
        }

    sample_cite = (
        cited[0].get("raw", "")
        if isinstance(cited[0], dict)
        else str(cited[0])
    )
    return {
        "filter_status": base_status,
        "filter_method": "rule_based",
        "filter_evidence": f"phrase:{phrase!s}; cite:{sample_cite}",
        "filter_confidence": "high",
    }


def classify_row(row: dict) -> dict:
    """Return FILTER_ADDED_COLS values for a single candidate row dict."""
    title    = str(row.get("title_r")    or "")
    abstract = str(row.get("abstract_r") or "")
    year_val = row.get("year_r")
    try:
        year = int(year_val) if year_val and str(year_val).strip() else None
    except (ValueError, TypeError):
        year = None
    return _classify_row(title, abstract, year)


def apply_rule_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Add FILTER_ADDED_COLS to ``df`` using rule-based classification."""
    if df.empty:
        for col in FILTER_ADDED_COLS:
            if col not in df.columns:
                df[col] = ""
        return df

    out_rows: list[dict] = []
    for _, row in df.iterrows():
        title = str(row.get("title_r") or "")
        abstract = str(row.get("abstract_r") or "")
        year_val = row.get("year_r")
        try:
            year = int(year_val) if pd.notna(year_val) and str(year_val).strip() else None
        except (ValueError, TypeError):
            year = None
        out_rows.append(_classify_row(title, abstract, year))

    additions = pd.DataFrame(out_rows, index=df.index)
    for col in FILTER_ADDED_COLS:
        df[col] = additions[col]

    counts = df["filter_status"].value_counts().to_dict()
    log.info("Rule filter: %s", counts)
    return df
