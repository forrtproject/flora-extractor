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
    "filter_method",     # str — rule_based | screen; llm/both are historical
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
    "classify_llm_model",  # str   — exact model that classified original_match_type (blank when a rule fired or the LLM failed)

    # OpenAlex work IDs — bare form (e.g. W2884670852), not the https://openalex.org/ URL.
    # openalex_id_r above carries the URL form inherited from Stage 1; these two are the
    # canonical bare IDs the validation DB keys on, and oa_work_id_o is only obtainable
    # here because it needs doi_o, which does not exist before Stage 3.
    "oa_work_id_r",        # str   — OpenAlex work ID of the replication paper
    "oa_work_id_o",        # str   — OpenAlex work ID of the original study

    # Original study
    "doi_o",               # str   — original study DOI
    "title_o",             # str   — original study title
    # FLoRA's `study_o`: WHICH study inside the original paper is targeted, as a
    # number, and several numbers when one replication targets several studies from
    # the same paper ("1, 2"). Blank when the original reports a single study or the
    # replication does not say. It is what makes FLoRA's coding level representable —
    # one row per pair of REFERENCES, so several studies within one original paper
    # stay one row, while several original PAPERS are several rows. Only the
    # multi-original path fills it — see _collapse_same_paper_originals() in
    # extract/run_extract.py, which does the grouping.
    # NOT the same field as the validation DB's `study_o`, which holds a title —
    # see extract/csv_to_db.py.
    "study_o",             # str   — target study number(s) within the original paper
    "year_o",              # int   — original study publication year
    "authors_o",           # str   — original study authors, semicolon-separated APA names (e.g. "Bransford, J. D.; Franks, J. J.")
    "ref_o",               # str   — full APA-style citation fetched from OpenAlex after doi_o resolved
    "bibtex_ref_o",        # str   — BibTeX entry for the original study (@article or @misc)
    "bibtex_ref_r",        # str   — BibTeX entry for the replication/reproduction paper (@article or @misc)

    # Linking
    "link_method",         # str   — see LINK_METHOD_VALUES below
    "link_evidence",       # str   — quote or pattern used for linking
    "link_confidence",     # str   — high | medium | low
    "link_llm_model",      # str   — exact model used for DOI resolution (e.g. gemini-2.0-flash)
    "screen_categories",   # str   — |-joined union of the front-door screen's category
                           #         labels (SCREEN_CATEGORIES in shared/llm_client.py);
                           #         multi-valued, so filter it by substring, never equality
    "doi_o_verification",  # str   — verified | corrected | mismatch | no_doi | not_found | no_metadata | api_error | skipped

    # Outcome
    "outcome",             # str   — success | failure | mixed | descriptive | cannot_be_determined | pending | api_error
    "outcome_phrase",      # str   — supporting quote from the paper
    "outcome_confidence",  # str   — high | medium | low
    "out_quote_source",    # str   — abstract | title | fulltext
    "outcome_reasoning",  # str   — one-sentence LLM note explaining the classification choice
    "outcome_llm_model",   # str   — exact model that coded the outcome ("keyword" for the
                           #         rule-based fallback, blank when no verdict was made);
                           #         differs from link_llm_model when a provider falls over mid-run

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
    "llm_cited_candidates",
    "llm_fulltext",
    # Stage 4.5: the LLM picked the target from the paper's OpenAlex reference list,
    # accepted only at confidence == "high" (see link_original.run_for_doi).
    "llm_references",
}

LINK_METHOD_VALUES = RESOLVED_LINK_METHODS | {
    # PROVISIONAL, not resolved. The DOI came from a CrossRef/OpenAlex title search
    # because the LLM named an original that was NOT in the candidate/reference list —
    # the only link method whose answer is not picked from a bounded candidate set, so
    # a plausible-sounding title can be matched against the whole literature. A
    # hand-check of the 2026-07-28 batch put precision near 50%, and the errors are
    # invisible to DOI verification: the DOI really does resolve to the named title,
    # it is simply not the paper's target. These rows are quarantined by sanity_check
    # for human confirmation and are NOT imported for validation.
    "llm_title_search",
    # Legacy rows written before the granular split, remapped by
    # tools/migrate_link_methods.py — they cannot be disaggregated retroactively.
    "author_year_match_legacy",
    # LLM ran with full context but concluded no identifiable original study exists.
    # These papers are likely Stage 2 false positives or self-replications; exclude from DB import.
    "no_original_found",
    # Screen verdicts that end the row without a target. sanity_check quarantines
    # both; exclude from DB import.
    "not_a_replication",
    # Historical, no longer emitted: the front door's gate has no disagreement
    # terminal state, so a split now proceeds down the ladder. Rows on disk still
    # carry the value and --rescreen must keep reopening them.
    "screen_disagreement",
    "target_pending", "api_error",
}

DOI_VERIFICATION_VALUES = {
    "verified", "corrected", "mismatch", "no_doi",
    "not_found", "no_metadata", "api_error", "skipped",
}

# The canonical outcome enum. This is the single source of truth for the
# outcome categories a classifier may emit — code_outcome and run_extract both
# import OUTCOME_CATEGORIES rather than defining their own copies.
#
# The five substantive values mirror the FLoRA codebook's dropdown. Two of them were
# missing until the rule-alignment pass:
#   uninformative — the FLoRA category for a replication whose AUTHORS say it cannot
#     speak to the original (underpowered, failed at the design level). It had been
#     retired as legacy and folded into cannot_be_determined, which conflated a
#     property of the paper with a failure of our extraction: merged, the database
#     could not tell a null-informative replication from an unread one, and every
#     "share we could not code" figure silently counted correctly-coded papers.
#   statistically_successful_but_flawed — FLoRA's category for "we replicated the
#     effect using the original methods, but show those methods do not test the
#     hypothesis". Without it such papers code as `success`, which is the reading
#     FLoRA created the category to avoid.
OUTCOME_CATEGORIES = {
    "success", "failure", "mixed", "descriptive",
    "statistically_successful_but_flawed",
    "uninformative",
    "cannot_be_determined",
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

# Values the classifier no longer emits but that still exist in stored CSVs.
# Empty since `uninformative` was restored as a live category above — the stored
# rows carrying it are now valid under the current enum rather than tolerated
# exceptions to it. Kept as a named set so the next retirement has somewhere to go.
OUTCOME_LEGACY_VALUES: set = set()

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


def make_pair_id(doi_r: str, doi_o: str, oa_work_id_o: str = "",
                 title_o: str = "") -> str:
    """MD5 of the replication-original pair. Full 32-char hex string.

    Some originals genuinely have no registered DOI (books, chapters, pre-DOI-era
    papers), so doi_o stays blank and every DOI-less original of the same
    replication used to hash to the same "doi_r|" — csv_to_db dedupes on pair_id
    and silently dropped all but one. Fall back to the OpenAlex work id, then the
    title, so those rows keep distinct identities.

    Calling this with only (doi_r, doi_o) and a non-blank doi_o is byte-identical
    to the pre-fallback hash, which is what keeps pair_ids already imported into
    the validation DB stable.

    *oa_work_id_o* and *title_o* are passed ONLY by the multi-original writer. 129
    existing rows have a blank doi_o and a blank oa_work_id_o, and are already keyed
    on md5("doi_r|") in the validation DB; feeding them either fallback would silently
    re-key every one of them into a duplicate import. They are all single-original,
    where neither fallback buys anything: a single-original row has exactly one
    original, and rows for different replications already differ by doi_r. Only
    several DOI-less originals of ONE replication need disambiguating, and that is
    exactly the multi-original case.
    """
    import hashlib
    import re
    from shared.utils import bare_work_id

    second = doi_o or ""
    if not second:
        work_id = bare_work_id(oa_work_id_o)
        if work_id:
            second = f"oa:{work_id}"
        elif str(title_o or "").strip():
            second = "t:" + re.sub(r"\s+", " ", str(title_o).strip().lower())
    return hashlib.md5(f"{doi_r}|{second}".encode()).hexdigest()

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
