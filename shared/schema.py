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
    "source",         # str   — openalex | bob_reed | i4r | semantic_scholar | ...
    "ref_r",          # str   — "Surname · Year · Journal" — built at search time
]

# ── Stage 2 output: filtered.csv ─────────────────────────────────────────────
# All CANDIDATES_COLS + the following:
FILTER_ADDED_COLS = [
    "filter_status",     # str — replication | reproduction | false_positive | needs_review
    "filter_method",     # str — rule_based | llm | both
    "filter_evidence",   # str — phrase or quote that triggered classification
    "filter_confidence", # str — high | medium | low  (categorical, not float)
]
FILTERED_COLS = CANDIDATES_COLS + FILTER_ADDED_COLS

# ── Stage 3 output: extracted.csv ────────────────────────────────────────────
# All FILTERED_COLS + the following:
EXTRACT_ADDED_COLS = [
    # Original-match type — determined by Stage 3 as its first routing step
    "original_match_type",       # str   — single_original | multiple_match | multiple_original
    "original_match_confidence", # str   — high | medium | low

    # Original study
    "doi_o",               # str   — original study DOI
    "title_o",             # str   — original study title
    "year_o",              # int   — original study publication year
    "authors_o",           # str   — original study authors, semicolon-separated APA names (e.g. "Bransford, J. D.; Franks, J. J.")
    "ref_o",               # str   — full APA-style citation fetched from OpenAlex after doi_o resolved
    "bibtex_ref_o",        # str   — BibTeX entry for the original study (@article or @misc)
    "bibtex_ref_r",        # str   — BibTeX entry for the replication/reproduction paper (@article or @misc)

    # Linking
    "link_method",         # str   — citation_context_match | same_author_year_title_overlap | single_candidate_after_requery | title_pattern_match | grobid_ref_match | llm_abstract | llm_fulltext | no_original_found | target_pending | api_error | author_year_match_legacy
    "link_evidence",       # str   — quote or pattern used for linking
    "link_confidence",     # str   — high | medium | low
    "link_llm_model",      # str   — exact model used for DOI resolution (e.g. gemini-2.0-flash)
    "doi_o_verification",  # str   — verified | corrected | mismatch | no_doi | not_found | no_metadata | api_error | skipped

    # Outcome
    "outcome",             # str   — success | failure | mixed | descriptive | cannot_be_determined | pending | api_error
    "outcome_phrase",      # str   — supporting quote from the paper
    "outcome_confidence",  # str   — high | medium | low
    "out_quote_source",    # str   — abstract | title | fulltext
    "outcome_reasoning",  # str   — one-sentence LLM note explaining the classification choice

    # Record type and multi-original bookkeeping
    "type",                # str   — replication | reproduction
    "original_rank",       # int   — 1 for single; 1,2,3... for multi-original papers
    "n_originals",         # int   — total originals in this paper (1 for single)
]
# pair_id is placed first so it is the leading identifier in extracted.csv.
# Value: md5(doi_r + "|" + doi_o).hexdigest() — full 32-char hex in the CSV;
# the UI displays only the first 3 characters as a compact visual tag.
EXTRACTED_COLS = ["pair_id"] + FILTERED_COLS + EXTRACT_ADDED_COLS

# ── Stage 4 output: validated.csv ────────────────────────────────────────────
# All EXTRACTED_COLS + the following:
VALIDATE_ADDED_COLS = [
    "validation_status",  # str — confirmed | rejected | pending | needs_review
    "vote_count",         # int — total votes received
    "confirm_votes",      # int — confirm votes
    "reject_votes",       # int — reject votes
    "validator_notes",    # str — aggregated reviewer comments
    # Reviewer corrections — blank means the extracted value was accepted unchanged
    "validated_doi_o",    # str — reviewer-corrected original DOI (blank = accepted)
    "validated_outcome",  # str — reviewer-corrected outcome (blank = accepted)
]
VALIDATED_COLS = EXTRACTED_COLS + VALIDATE_ADDED_COLS

# ── Valid values for categorical columns ─────────────────────────────────────

FILTER_STATUS_VALUES = {"replication", "reproduction", "false_positive", "needs_review"}

FILTER_CONFIDENCE_VALUES = {"high", "medium", "low"}

ORIGINAL_MATCH_TYPE_VALUES = {"single_original", "multiple_match", "multiple_original"}

# Resolved link methods — an original study was identified. The five rule-based
# methods used to collapse into a single "author_year_match" value; they are now
# kept distinct because their reliability differs sharply (e.g.
# single_candidate_after_requery auto-accepts a lone candidate at score 1.0 with no
# semantic check). These are the methods csv_to_db imports for validation.
RESOLVED_LINK_METHODS = {
    "citation_context_match",
    "same_author_year_title_overlap",
    "single_candidate_after_requery",
    "title_pattern_match",
    "grobid_ref_match",
    "llm_abstract",
    "llm_fulltext",
    # DOI came from a CrossRef/OpenAlex title search because the LLM named an
    # original that was NOT in the candidate/reference list. Kept distinct from
    # llm_fulltext: every doi_o mismatch found in the 2026-07 audit came from this
    # path, so it must stay filterable rather than blend into candidate-derived links.
    "llm_title_search",
}

LINK_METHOD_VALUES = RESOLVED_LINK_METHODS | {
    # Legacy rows written before the granular split, remapped by
    # tools/migrate_link_methods.py — they cannot be disaggregated retroactively.
    "author_year_match_legacy",
    # LLM ran with full context but concluded no identifiable original study exists.
    # These papers are likely Stage 2 false positives or self-replications; exclude from DB import.
    "no_original_found",
    "target_pending", "api_error",
}

DOI_VERIFICATION_VALUES = {
    "verified", "corrected", "mismatch", "no_doi",
    "not_found", "no_metadata", "api_error", "skipped",
}

# The canonical outcome enum. This is the single source of truth for the
# outcome categories a classifier may emit — code_outcome and run_extract both
# import OUTCOME_CATEGORIES rather than defining their own copies.
OUTCOME_CATEGORIES = {
    "success", "failure", "mixed", "descriptive", "cannot_be_determined",
    # Emitted when the classifier judges is_genuine_attempt=false: the text does not
    # describe a real attempt to replicate/reproduce the named original at all.
    "not_a_replication",
}

# Reproduction outcomes use a completely different vocabulary from replications.
# A reproduction re-runs the ORIGINAL data/code, so two independent questions apply:
#   1. did the computation reproduce?  computationally successful | computational
#      issues | computation not checked
#   2. does the result survive alternative specifications?  robust |
#      robustness challenges | robustness not checked
# The full 3x3 grid below matches the FLoRA entry form's dropdown. Which vocabulary
# applies is keyed off the row's `type` column — the same way flora.csv stores it
# (one `outcome` column, disambiguated by `type`).
REPRODUCTION_OUTCOME_CATEGORIES = {
    "computationally successful, robust",
    "computationally successful, robustness challenges",
    "computationally successful, robustness not checked",
    "computational issues, robust",
    "computational issues, robustness challenges",
    "computational issues, robustness not checked",
    "computation not checked, robust",
    "computation not checked, robustness challenges",
    "computation not checked, robustness not checked",
}

# Pipeline-state markers. These are NOT outcome categories — they record where a
# row sits in the pipeline, never a judgment about the replication result.
#   pending   — row not yet processed by the outcome step
#   api_error — outcome extraction failed after retries
OUTCOME_STATE_MARKERS = {"pending", "api_error"}

# Values the classifier no longer emits but that still exist in stored CSVs:
#   uninformative — predates cannot_be_determined (outcome-coding unification)
OUTCOME_LEGACY_VALUES = {"uninformative"}

# Every value that may legitimately appear in the `outcome` CSV column. Validators of
# STORED data (e.g. extract/audit_extracted.py) must check against this, not
# OUTCOME_CATEGORIES — otherwise every legacy row is flagged as non-canonical.
OUTCOME_VALUES = (OUTCOME_CATEGORIES | REPRODUCTION_OUTCOME_CATEGORIES
                  | OUTCOME_STATE_MARKERS | OUTCOME_LEGACY_VALUES)


def outcome_categories_for(record_type: str) -> set:
    """The outcome vocabulary valid for a row of this `type`.

    reproduction -> the 3x3 computation/robustness grid; anything else -> the
    replication categories. cannot_be_determined is valid for both, since either
    classifier can fail to reach a verdict.
    """
    if str(record_type or "").strip().lower() == "reproduction":
        return REPRODUCTION_OUTCOME_CATEGORIES | {"cannot_be_determined", "not_a_replication"}
    return OUTCOME_CATEGORIES

TYPE_VALUES = {"replication", "reproduction"}

VALIDATION_STATUS_VALUES = {"confirmed", "rejected", "pending", "needs_review"}

# Sources actually produced by the pipeline. #46: bob_reed / i4r were advertised
# here but their fetchers (search/external_lists.py) are never called, so no such
# rows exist — reserved until external_lists is wired into run_search.
SOURCE_VALUES = {"openalex", "openalex_concept", "semantic_scholar", "backfill_old_pipeline"}

# ── Default empty row builders ────────────────────────────────────────────────

def empty_candidates_row() -> dict:
    return {col: "" for col in CANDIDATES_COLS}

def empty_filter_row() -> dict:
    return {col: "" for col in FILTERED_COLS}

def empty_extract_row() -> dict:
    return {col: "" for col in EXTRACTED_COLS}


def make_pair_id(doi_r: str, doi_o: str) -> str:
    """MD5 of the replication-original DOI pair. Full 32-char hex string."""
    import hashlib
    return hashlib.md5(f"{doi_r}|{doi_o}".encode()).hexdigest()

def empty_validated_row() -> dict:
    return {col: "" for col in VALIDATED_COLS}

# ── Schema validation helper ──────────────────────────────────────────────────

def validate_csv_columns(df_columns: list, stage: str) -> list[str]:
    """
    Check that a DataFrame has all required columns for a given stage.
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
