"""
schema.py — CSV column definitions for all pipeline stages.

This is the contract between pipeline stages.
Never rename or remove a column without updating this file and notifying all teams.

Usage:
    from shared.schema import CANDIDATES_COLS, FILTERED_COLS, EXTRACTED_COLS, VALIDATED_COLS
"""

# ── Stage 1 output: candidates.csv ───────────────────────────────────────────
CANDIDATES_COLS = [
    "doi_r",          # str   — DOI, cleaned (no https://doi.org/ prefix)
    "title_r",        # str   — paper title
    "abstract_r",     # str   — abstract text
    "year_r",         # int   — publication year
    "authors_r",      # str   — semicolon-separated author list
    "journal_r",      # str   — journal name
    "url_r",          # str   — open access URL if available
    "openalex_id_r",  # str   — OpenAlex work ID (e.g. W2741809807)
    "source",         # str   — openalex | bob_reed | i4r | score | semantic_scholar
]

# ── Stage 2 output: filtered.csv ─────────────────────────────────────────────
# All CANDIDATES_COLS + the following:
FILTER_ADDED_COLS = [
    "filter_status",       # str   — replication | reproduction | false_positive | needs_review
    "filter_method",       # str   — rule_based | llm | both
    "filter_evidence",     # str   — phrase that triggered classification
    "filter_confidence",   # float — 0.0–1.0
    "is_replication",           # bool  — True if confirmed replication
    "is_reproduction",          # bool  — True if confirmed reproduction
    "original_match_type",      # str   — single_original | multiple_match | multiple_original
    "original_match_confidence",# float — 0.0–1.0 confidence of match type classification
]
FILTERED_COLS = CANDIDATES_COLS + FILTER_ADDED_COLS

# ── Stage 3 output: extracted.csv ────────────────────────────────────────────
# All FILTERED_COLS + the following:
EXTRACT_ADDED_COLS = [
    "doi_o",               # str   — original study DOI
    "title_o",             # str   — original study title
    "year_o",              # int   — original study publication year
    "authors_o",           # str   — original study authors
    "link_method",         # str   — author_year_match | llm_abstract | llm_fulltext | target_pending
    "link_evidence",       # str   — quote or pattern used for linking
    "link_confidence",     # float — 0.0–1.0
    "outcome",             # str   — success | failure | mixed | uninformative | pending
    "outcome_phrase",      # str   — supporting quote from the paper
    "outcome_confidence",  # float — 0.0–1.0
    "out_quote_source",    # str   — abstract | fulltext | title
    "type",                # str   — replication | reproduction
    "original_rank",       # int   — 1 for single; 1,2,3... for multi-original
    "n_originals",         # int   — total originals in this paper (1 for single)
]
EXTRACTED_COLS = FILTERED_COLS + EXTRACT_ADDED_COLS

# ── Stage 4 output: validated.csv ────────────────────────────────────────────
# All EXTRACTED_COLS + the following:
VALIDATE_ADDED_COLS = [
    "validation_status",  # str  — confirmed | rejected | pending | needs_review
    "vote_count",         # int  — total votes received
    "confirm_votes",      # int  — confirm votes
    "reject_votes",       # int  — reject votes
    "validator_notes",    # str  — aggregated reviewer comments
]
VALIDATED_COLS = EXTRACTED_COLS + VALIDATE_ADDED_COLS

# ── Valid values for categorical columns ─────────────────────────────────────

FILTER_STATUS_VALUES = {"replication", "reproduction", "false_positive", "needs_review"}

ORIGINAL_MATCH_TYPE_VALUES = {"single_original", "multiple_match", "multiple_original"}

LINK_METHOD_VALUES = {"author_year_match", "llm_abstract", "llm_fulltext", "target_pending"}

OUTCOME_VALUES = {"success", "failure", "mixed", "uninformative", "pending"}

TYPE_VALUES = {"replication", "reproduction"}

VALIDATION_STATUS_VALUES = {"confirmed", "rejected", "pending", "needs_review"}

SOURCE_VALUES = {"openalex", "bob_reed", "i4r", "score", "semantic_scholar"}

# ── Default empty row builders ────────────────────────────────────────────────

def empty_candidates_row() -> dict:
    return {col: "" for col in CANDIDATES_COLS}

def empty_filter_row() -> dict:
    return {col: "" for col in FILTERED_COLS}

def empty_extract_row() -> dict:
    return {col: "" for col in EXTRACTED_COLS}

def empty_validated_row() -> dict:
    return {col: "" for col in VALIDATED_COLS}

# ── Schema validation helper ──────────────────────────────────────────────────

def validate_csv_columns(df_columns: list, stage: str) -> list[str]:
    """
    Check that a DataFrame has all required columns for the given stage.
    Returns list of missing column names (empty list = OK).

    Usage:
        missing = validate_csv_columns(list(df.columns), "filtered")
        if missing:
            raise ValueError(f"Missing columns: {missing}")
    """
    required = {
        "candidates": CANDIDATES_COLS,
        "filtered":   FILTERED_COLS,
        "extracted":  EXTRACTED_COLS,
        "validated":  VALIDATED_COLS,
    }.get(stage, [])

    return [c for c in required if c not in df_columns]
