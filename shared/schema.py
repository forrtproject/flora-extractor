"""
schema.py — CSV column definitions for all pipeline stages.

This is the contract between pipeline stages.
Never rename or remove a column without updating this file and notifying all teams.

Usage:
    from shared.schema import CANDIDATES_COLS, FILTERED_COLS, EXTRACTED_COLS, VALIDATED_COLS
"""

import re
from pathlib import Path

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
    "filter_method",     # str — engine:<release_id> (the filter engine) | screen (Stage 3's
                         #       front door). rule_based/llm/both are historical only.
    "filter_evidence",   # str — phrase or quote that triggered classification
    "filter_confidence", # str — high | medium | low  (categorical, not float)
]
FILTERED_COLS = CANDIDATES_COLS + FILTER_ADDED_COLS

# ── Filter-engine export: filtered.csv + routing provenance (issue #146/#148) ─
# What the pool knew must survive into the materialized artifact: the OpenAlex work
# type and concept hit the row was kept for, and which rule routed it under which
# release. Appended AFTER FILTERED_COLS, so Stage 3 — which reads by name and
# ignores trailing columns — is unaffected.
ENGINE_EXPORT_COLS = [
    "oa_type",           # str — OpenAlex work type from the pool row (`type`)
    "hit_concept",       # str — whether Stage A kept the row on a concept match
    "route_rule",        # str — id of the spec that won the pile ("" when pending)
    "route_precedence",  # str — that spec's precedence
    "matched_rules",     # str — |-joined; match by substring/split, never equality
    "pending_reason",    # str — no_filter_matched (no rule claimed the row) | no_text
                         #       (a text pile downgraded because the abstract is empty)
    "release_id",        # str — the routing release the pile came from
]
# ── The screen verdict, carried from Stage 2 to Stage 3 ──────────────────────
# The two-voter front-door screen runs ONCE, in Stage 2's `screen_expensive` tier
# (`filter/engine/tiers.py`), and its answer travels here. Stage 3 reads these
# columns instead of voting again: `extract/run_extract.py` rebuilds the
# `classify_replication()` dict from them, which is why the vote detail is on the
# row and not just the summary — the pre-PDF title-search rung fires only when
# BOTH voters gave a qualifying answer AND stood behind it, and that gate cannot
# be reconstructed from a record type.
#
# Blank on every row no expensive-tier run settled (an `--as-routed` handoff, a
# hand-made CSV, an `export`ed pile). Stage 3 refuses to start on an input whose
# header lacks them; a row whose values are blank is written `target_pending`.
SCREEN_COLS = [
    "screen_verdict",      # str — discard | proceed; blank = no verdict exists
    "screen_record_type",  # str — replication | reproduction, the screen's own answer
                           #       ("both" already mapped to replication); blank when
                           #       neither voter gave a qualifying label
    "screen_categories",   # str — |-joined union of both voters' category labels;
                           #       multi-valued, so match by substring/split
    "screen_votes",        # str — |-joined <model>=<classification>/<confident|
                           #       unconfident>, in call order. The vote detail the
                           #       gate and the title-search rung are computed from
    "screen_evidence",     # str — every voter's justifying quote, "<model>: <quote>"
                           #       segments joined by " || " (a quote may contain "|")
    "screen_reasoning",    # str — "<provider>: <reasoning>" per voter, " | "-joined
]

ENGINE_EXPORTED_COLS = FILTERED_COLS + ENGINE_EXPORT_COLS + SCREEN_COLS

# ── Stage 3 output: extracted.csv ────────────────────────────────────────────
# All FILTERED_COLS + the following:
EXTRACT_ADDED_COLS = [
    # Original-match type — OBSERVED, not routed. Nothing predicts how many originals
    # a paper targets any more: the target prompt answers it, and these three columns
    # record what came back. multiple_original means the per-target adapter wrote more
    # than one row for the paper, single_original that it wrote one; the confidence is
    # high when the ladder settled on an original and low when the row carries none.
    # multiple_match survives as a legacy value in stored data and is never written.
    "original_match_type",       # str   — single_original | multiple_original (legacy: multiple_match)
    "original_match_confidence", # str   — high | low  (legacy: medium)
    "classify_llm_model",  # str   — legacy; always blank now that no classifier routes the row

    # OpenAlex work IDs — bare form (e.g. W2884670852), not the https://openalex.org/ URL.
    # openalex_id_r above carries the URL form inherited from Stage 1; these two are the
    # canonical bare IDs the validation DB keys on, and oa_work_id_o is only obtainable
    # here because it needs doi_o, which does not exist before Stage 3.
    "oa_work_id_r",        # str   — OpenAlex work ID of the replication paper
    "oa_work_id_o",        # str   — OpenAlex work ID of the original study

    # Replication-side study identifier. FLoRA's `study_r`: WHICH study inside the
    # REPLICATION paper re-tests this original ("1, 2" when several do). Blank when
    # the paper reports a single study or does not say. It is the counterpart of
    # study_o, and it is what makes a multi-original paper readable: without it a
    # reader cannot tell which of the replication's studies each row is about.
    # Filled from the target prompt's replication_study_numbers on both link paths.
    "study_r",             # str   — replicating study number(s) within this paper

    # Original study
    "doi_o",               # str   — original study DOI
    "title_o",             # str   — original study title
    # FLoRA's `study_o`: WHICH study inside the original paper is targeted, as a
    # number, and several numbers when one replication targets several studies from
    # the same paper ("1, 2"). Blank when the original reports a single study or the
    # replication does not say. It is what makes FLoRA's coding level representable —
    # one row per pair of REFERENCES, so several studies within one original paper
    # stay one row, while several original PAPERS are several rows. Both link paths
    # fill it from the LLM's study_numbers — the per-target adapter additionally
    # groups on it, see _collapse_same_paper_originals() in extract/run_extract.py.
    # The validation DB's `study_o` used to hold a title; the validation repo's
    # import now sends this number there and the titles to title_o (issue #103).
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

    # Full-text provenance. A row coded llm_fulltext asserts that a model read the
    # paper; until these two columns existed nothing on the row said WHICH document
    # it read, and an audit of the 2026-07 batch could only reconstruct it from cache
    # timestamps — 105 of 138 llm_fulltext rows turned out to have no artifact at all.
    # Both are blank on every row that never acquired or never parsed a document,
    # which is the honest reading: no document, no provenance.
    "pdf_source",          # str   — acquisition tier that supplied the document
                           #         (arxiv | osf | openalex_oa | unpaywall_pdf |
                           #          semanticscholar | core | europepmc | landing_* |
                           #          serpapi | playwright | openalex_xml); blank when none
    "parse_method",        # str   — winning parser from best_parse_result()
                           #         (openalex_xml | pdfminer | grobid | docpluck |
                           #          opendataloader | markitdown); blank when nothing parsed

    # Outcome
    "outcome",             # str   — success | failure | mixed | descriptive | cannot_be_determined | pending | api_error
                           #         For a reproduction this is DERIVED from the two axes below
                           #         ("computationally reproducible, robust"), so one column reads
                           #         the same way for both record types — the way flora.csv stores it.
    "outcome_phrase",      # str   — supporting quote from the paper
    "outcome_confidence",  # str   — high | medium | low
    "out_quote_source",    # str   — title | abstract | introduction | discussion (or two
                           #         joined by " | "). "fulltext" is the legacy value, written
                           #         while one undifferentiated body block was sent.
    "outcome_reasoning",  # str   — one-sentence LLM note explaining the classification choice

    # Reproduction outcome axes — empty on a replication row. The 4x3 grid is stored
    # as two coded fields, each with its own quote and quote source: a validator was
    # otherwise shown one quote asked to justify two independent judgments, and every
    # downstream analysis had to re-split a joined string to read one axis.
    "outcome_computation",             # str — COMPUTATION_OUTCOME_VALUES
    "outcome_computational_quote",     # str — verbatim passage proving the computation verdict
    "out_quote_computational_source",  # str — as out_quote_source above
    "outcome_robustness",              # str — ROBUSTNESS_OUTCOME_VALUES
    "outcome_robustness_quote",        # str — verbatim passage proving the robustness verdict
    "out_quote_robust_source",         # str — as out_quote_source above
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
#
# These sets are enforced, not documentation. tests/test_schema_roundtrip.py holds
# the checked-in samples in misc/ against FILTER_STATUS_VALUES,
# FILTER_CONFIDENCE_VALUES, ORIGINAL_MATCH_TYPE_VALUES, DOI_VERIFICATION_VALUES,
# TYPE_VALUES and SOURCE_VALUES; LINK_METHOD_VALUES and RESOLVED_LINK_METHODS are
# held against every method the pipeline can emit by
# tests/test_link_method_contract.py; OUTCOME_VALUES is what extract/audit_extracted
# checks stored rows against. A set nothing checks belongs in a comment, not here —
# VALIDATION_STATUS_VALUES was removed for that reason (Stage 4 writes no CSV, and
# the `validation_status` strings this repo actually reads are the FLoRA entry
# sheet's, a different vocabulary).

FILTER_STATUS_VALUES = {"replication", "reproduction", "false_positive", "needs_review"}

FILTER_CONFIDENCE_VALUES = {"high", "medium", "low"}

ORIGINAL_MATCH_TYPE_VALUES = {"single_original", "multiple_match", "multiple_original"}

# Resolved link methods — an original study was identified. The five rule-based
# methods used to collapse into a single "author_year_match" value; they are now
# kept distinct because their reliability differs sharply (e.g.
# single_candidate_after_requery auto-accepts a lone candidate at score 1.0 with no
# semantic check). These are the methods the validation import takes.
#
# single_candidate_after_requery and same_author_year_title_overlap are HELD-ONLY:
# _HELD_ONLY_METHODS in extract/link_original.py stops either from ending the ladder
# while a call that can enumerate targets is still available. The pick is parked and
# restored only when nothing that could enumerate contradicted it — nothing enumerating
# spoke at all, or the one call that did named at most one original and it is the same
# work. So a row carrying either value on disk is one an enumerating call confirmed or
# never got to make, never one that overrode an LLM.
RESOLVED_LINK_METHODS = {
    "citation_context_match",
    "same_author_year_title_overlap",
    "single_candidate_after_requery",
    "title_pattern_match",
    "grobid_ref_match",
    "llm_cited_candidates",
    "llm_fulltext",
    # Stage 4.5: the LLM picked the target from the paper's OpenAlex reference list,
    # accepted only when the model marked the target `match_certain`
    # (see link_original.run_for_doi).
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
    # The cheap discard-only tier (shared/prescreen.py, run by Stage 2 over the
    # screen_cheap pile) ended the row before the validated screen ever voted. Deliberately NOT folded into
    # not_a_replication: that value means the validated pair settled the paper, and an
    # audit of the screen must not have to disentangle a lower-recall tier's discards
    # from it. Quarantined to its own prescreen_discard.csv; excluded from DB import.
    "prescreen_discard",
    # Historical, no longer emitted: the front door's gate has no disagreement
    # terminal state, so a split now proceeds down the ladder. Rows on disk still
    # carry the value, and the export still partitions them by it.
    "screen_disagreement",
    "target_pending", "api_error",
}

# Every file sanity_check parks a row in — quarantine bucket → destination, mirroring
# the rule list in extract/sanity_check.py (several buckets share a file). This map is
# the single place a set-aside destination is named: sanity_check reports through it and
# run_extract's resume reads it, so a new destination cannot be added to one and go
# unclassified by the other (tests/test_link_method_contract.py pins the partition).
SET_ASIDE_DESTINATIONS = {
    "screen_disagreement": "screen_disagreement.csv",
    "non_article": "not_a_replication.csv",
    "non_article_type": "not_a_replication.csv",
    "title_search_provisional": "provisional_title_search.csv",
    "target_pending": "target_pending.csv",
    "prescreen_discard": "prescreen_discard.csv",
    "not_a_replication": "not_a_replication.csv",
    "api_error": "api_error.csv",
    "no_original_found": "no_original_found.csv",
    "self_link": "unresolved_self_links.csv",
    "doi_mismatch": "unresolved_doi_mismatch.csv",
    "fabricated_doi_o": "fabricated_original_doi.csv",
}

# The two destinations a re-run is MEANT to redo, so resume must not count them as
# settled. target_pending is "re-run decides" by construction, and an api_error row
# records a transient provider failure — checkpointing either as settled would turn a
# 503 into a permanent hole in the corpus. They need no flag to reopen: every run does.
REOPENED_SET_ASIDE_FILES = ("target_pending.csv", "api_error.csv")

# What resume treats as settled: every set-aside destination except those two. Each of
# these rows already paid for a screen, a ladder pass and sometimes a PDF; re-running
# them buys the same answer twice.
SETTLED_SET_ASIDE_FILES = tuple(sorted(
    set(SET_ASIDE_DESTINATIONS.values()) - set(REOPENED_SET_ASIDE_FILES)))

# The settled files whose verdicts came from the abstract alone: a changed voter pair
# or prompt would decide them differently. What reopens such a paper is a new SCREENING
# GENERATION — it makes the work claimable again in Stage 2, and a fresh PROCEED verdict
# puts it back in the extract tier's worklist. No flag and no file is involved.
SCREEN_SET_ASIDE_FILES = ("not_a_replication.csv", "screen_disagreement.csv",
                          "prescreen_discard.csv")


def set_aside_dir(csv_path) -> "Path":
    """The directory holding the set-aside CSVs that belong to *csv_path*.

    Set-asides are part of the state of ONE rendered output, not of the data
    directory: they are what the dashboard and the audits read as that file's
    quarantine, and a sandbox render's rows must not be read as production's.
    Production keeps the historical layout (the files sit beside extracted.csv in
    data/); any other output gets its own `<stem>-set-aside/` directory, so
    `extract.export --mode validation --out data/extracted-test.csv` writes
    data/extracted-test-set-aside/.
    """
    p = Path(csv_path)
    if p.name == "extracted.csv":
        return p.parent
    return p.parent / f"{p.stem}-set-aside"


# Link methods whose rows are never doi_o-verified: they carry no target to verify
# (target_pending), no answer at all (api_error), or a verdict the pipeline has
# deliberately stopped spending on (prescreen_discard). run_extract._verify_row and
# extract/audit_dois.py both read this, so an audit cannot re-verify — and re-stamp —
# a row the pipeline leaves alone.
VERIFICATION_SKIP_LINK_METHODS = {"target_pending", "api_error", "prescreen_discard"}

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
    # Emitted when the full-text outcome pass answers record_type_check="neither":
    # the paper does not check the named original at all.
    "not_a_replication",
}

# Reproduction outcomes use a completely different vocabulary from replications.
# A reproduction re-runs the ORIGINAL data/code, so two independent questions apply,
# and each is coded in its own column with its own quote.
#
# Axis 1 — did re-running the analysis produce the original numbers? `technical
# failure` is the case where the reproduction was defeated by the materials (no data,
# no code, an unrunnable workflow), the most common real reproduction outcome in
# economics and the one the old 3x3 grid could not express: such a paper had to be
# recorded either as `computational issues`, asserting a numerical disagreement never
# observed, or as cannot_be_determined, asserting that we could not read the paper.
COMPUTATION_OUTCOME_VALUES = {
    "computationally reproducible",
    "computational issues",
    "technical failure",
    "not checked",
    "cannot_be_determined",
}

# Axis 2 — does the finding survive alternative reasonable specifications?
ROBUSTNESS_OUTCOME_VALUES = {
    "robust",
    "robustness challenges",
    "not checked",
    "cannot_be_determined",
}

# The empty axis block a replication row carries, and the block a "neither" verdict
# clears a reproduction row down to.
EMPTY_OUTCOME_AXES = {
    "outcome_computation": "", "outcome_computational_quote": "",
    "out_quote_computational_source": "",
    "outcome_robustness": "", "outcome_robustness_quote": "",
    "out_quote_robust_source": "",
}

# The derived `outcome` string for a reproduction: the two settled axis values joined
# with ", ", computation first. Either axis at cannot_be_determined derives
# cannot_be_determined instead, so this set holds only the settled combinations.
# Which vocabulary applies is keyed off the row's `type` column — the same way
# flora.csv stores it (one `outcome` column, disambiguated by `type`).
REPRODUCTION_OUTCOME_CATEGORIES = {
    f"{comp}, {rob}"
    for comp in COMPUTATION_OUTCOME_VALUES - {"cannot_be_determined"}
    for rob in ROBUSTNESS_OUTCOME_VALUES - {"cannot_be_determined"}
}


def derive_reproduction_outcome(computation: str, robustness: str) -> str:
    """The stored `outcome` for a reproduction, from its two axis verdicts.

    An unsettled axis makes the whole verdict unsettled — the axis columns still carry
    whatever the other axis settled, but the single `outcome` column a reader sorts on
    must not read as settled when half of it is not. An axis value outside its
    vocabulary is unsettled for the same reason: joining it would mint an `outcome`
    outside REPRODUCTION_OUTCOME_CATEGORIES.
    """
    comp = str(computation or "").strip()
    rob = str(robustness or "").strip()
    if (comp not in COMPUTATION_OUTCOME_VALUES - {"cannot_be_determined"}
            or rob not in ROBUSTNESS_OUTCOME_VALUES - {"cannot_be_determined"}):
        return "cannot_be_determined"
    return f"{comp}, {rob}"

# Pipeline-state markers. These are NOT outcome categories — they record where a
# row sits in the pipeline, never a judgment about the replication result.
#   pending   — row not yet processed by the outcome step
#   api_error — outcome extraction failed after retries
OUTCOME_STATE_MARKERS = {"pending", "api_error"}

# Values the classifier no longer emits but that still exist in stored CSVs. The
# nine strings below are the old 3x3 reproduction grid, retired when axis 1 gained
# `technical failure` and the axes moved into their own columns. The 17 stored
# reproduction rows still carry them until they are re-coded under the new prompts.
OUTCOME_LEGACY_VALUES: set = {
    f"{comp}, {rob}"
    for comp in ("computationally successful", "computational issues",
                 "computation not checked")
    for rob in ("robust", "robustness challenges", "robustness not checked")
}

# Every value that may legitimately appear in the `outcome` CSV column. Validators of
# STORED data (e.g. extract/audit_extracted.py) must check against this, not
# OUTCOME_CATEGORIES — otherwise every legacy row is flagged as non-canonical.
OUTCOME_VALUES = (OUTCOME_CATEGORIES | REPRODUCTION_OUTCOME_CATEGORIES
                  | OUTCOME_STATE_MARKERS | OUTCOME_LEGACY_VALUES)


def _outcome_text(value) -> str:
    """A response field as a string, with JSON null read as absent — a model saying
    "none" for a quote must not be stored as the string "None"."""
    return "" if value is None else str(value)


def normalise_outcome_block(result: dict, record_type: str,
                            has_text: bool) -> dict:
    """The outcome half of an LLM answer as the row shape, in *record_type*'s vocabulary.

    One normaliser for two producers: the per-target block of the combined
    target+outcome call, and the standalone coder's whole response. They name the
    confidence field differently (`outcome_confident` per target, `confident` on the
    standalone answer) and nothing else, so both are read here.

    *has_text* says whether the model was given any of the paper's own body. The two
    check fields are only asked then, so their absence otherwise is not a verdict —
    reading it as one would let a model that saw an abstract veto a row on the methods
    it never read.
    """
    is_repro = str(record_type or "").strip().lower() == "reproduction"
    rtc = _outcome_text(result.get("record_type_check")).strip().lower() if has_text else ""
    target_check = (_outcome_text(result.get("target_check")).strip().lower()
                    if has_text else "")

    axes = dict(EMPTY_OUTCOME_AXES)
    if is_repro:
        computation = _outcome_text(result.get("outcome_computation")).strip()
        robustness = _outcome_text(result.get("outcome_robustness")).strip()
        # An unrecognised axis value is a value the pipeline cannot act on; recording
        # it as unsettled is honest, recording it verbatim is not.
        if computation not in COMPUTATION_OUTCOME_VALUES:
            computation = "cannot_be_determined"
        if robustness not in ROBUSTNESS_OUTCOME_VALUES:
            robustness = "cannot_be_determined"
        axes = {
            "outcome_computation":            computation,
            "outcome_computational_quote":    _outcome_text(result.get("outcome_computational_quote")),
            "out_quote_computational_source": _outcome_text(result.get("out_quote_computational_source")),
            "outcome_robustness":             robustness,
            "outcome_robustness_quote":       _outcome_text(result.get("outcome_robustness_quote")),
            "out_quote_robust_source":        _outcome_text(result.get("out_quote_robust_source")),
        }
        outcome = derive_reproduction_outcome(computation, robustness)
        # A reproduction's evidence lives in the two axis quotes; the shared
        # outcome_phrase would have to be one of them chosen arbitrarily, which is
        # exactly the one-quote-for-two-judgments problem the axes were split to end.
        phrase, source = "", ""
    else:
        outcome = _outcome_text(result.get("outcome", "cannot_be_determined")).lower()
        # Reproductions use the computation/robustness axes, replications the
        # success/failure/... enum. Validating against the wrong one would silently
        # coerce every verdict to cannot_be_determined.
        if outcome not in outcome_categories_for(record_type):
            outcome = "cannot_be_determined"
        phrase = _outcome_text(result.get("outcome_phrase"))
        source = _outcome_text(result.get("out_quote_source"))

    # The veto: a paper the text shows does not check the named original at all.
    if rtc == "neither" or target_check == "no_original":
        outcome = "not_a_replication"
        axes = dict(EMPTY_OUTCOME_AXES)
        phrase, source = "", ""

    confident = result.get("outcome_confident", result.get("confident"))
    if not isinstance(confident, bool):
        confident = _outcome_text(confident).strip().lower() == "true"

    return {
        "outcome":            outcome,
        "outcome_phrase":     phrase,
        "outcome_confidence": "high" if confident else "low",
        "out_quote_source":   source,
        "outcome_reasoning":  _outcome_text(result.get("outcome_reasoning")),
        **axes,
        "record_type_check":  rtc,
        "target_check":       target_check,
    }


def outcome_is_settled(block: dict, record_type: str) -> bool:
    """Whether an outcome block says something the pipeline can record as a verdict.

    This is what decides whether an LLM rung of the resolution ladder may END the row:
    a resolved target whose outcome is unsettled descends towards the full text
    instead. A reproduction has two axes, and either one unresolved is a reason to keep
    reading — treating a settled computation verdict beside an unresolved robustness
    verdict as done would record "not checked" as though the paper had been read to the
    end.
    """
    if not block:
        return False
    if str(record_type or "").strip().lower() == "reproduction":
        return (block.get("outcome_computation") not in ("", None, "cannot_be_determined")
                and block.get("outcome_robustness") not in ("", None, "cannot_be_determined"))
    return block.get("outcome") not in ("", None, "cannot_be_determined", "api_error")


def outcome_categories_for(record_type: str) -> set:
    """The outcome vocabulary valid for a row of this `type`.

    reproduction -> the derived computation/robustness joins; anything else -> the
    replication categories. cannot_be_determined is valid for both, since either
    classifier can fail to reach a verdict.
    """
    if str(record_type or "").strip().lower() == "reproduction":
        return REPRODUCTION_OUTCOME_CATEGORIES | {"cannot_be_determined", "not_a_replication"}
    return OUTCOME_CATEGORIES

# The `type` column may also be EMPTY: when the front-door screen proceeds without a
# qualifying vote and Stage 2 left the row at needs_review, nothing has said what the
# paper is, and the pipeline records that rather than defaulting to replication.
TYPE_VALUES = {"replication", "reproduction"}

# Sources actually produced by the pipeline. #46: bob_reed / i4r were advertised
# here but their fetchers (search/external_lists.py) are never called, so no such
# rows exist — reserved until external_lists is wired into run_search. Both names
# are already in shared.config.CURATED_SOURCES, so those rows will bypass Stage 2's
# keyword filter on the day the fetchers are wired in.
SOURCE_VALUES = {"openalex", "openalex_concept", "openalex_snapshot", "semantic_scholar",
                 "backfill_old_pipeline"}


# ── Year columns ──────────────────────────────────────────────────────────────
# A year is written as a bare integer or as nothing. pandas promotes an integer
# column holding any NaN to float64, and to_csv then renders every value with a
# ".0" suffix, so "2018.0" reaches the CSV and from there Supabase and every
# consumer that compares years as strings (issue #140). Normalising on write is
# the fix; the assertion below is what stops a new float path reappearing.
YEAR_COLS = ("year_r", "year_o")

_FLOAT_YEAR_RE = re.compile(r"^\d+\.0$")


def year_str(value) -> str:
    """Normalise any year representation to a bare integer string or "".

    int/float/str/None/NaN → "2018" or "". A value that is not an integral number
    is passed through stripped rather than discarded: the column's job is to carry
    what is known about the year, and only the float artifact is being removed.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return "" if value != value else str(int(value))  # NaN != NaN
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "<na>"}:
        return ""
    try:
        num = float(text)
    except ValueError:
        return text
    if num != num or num != int(num):
        return text
    return str(int(num))


def assert_no_float_years(row: dict) -> None:
    """Raise if a year column carries the float artifact ("2018.0").

    Loud by design: a violation means a new write path bypassed year_str(), and a
    silently-tolerated one is how 93% of extracted.csv came to hold float years.
    """
    for col in YEAR_COLS:
        val = row.get(col)
        if isinstance(val, str) and _FLOAT_YEAR_RE.match(val):
            raise ValueError(
                f"{col} written as a float year: {val!r} "
                f"(doi_r={row.get('doi_r', '')!r}). Normalise with shared.schema.year_str()."
            )


def make_pair_id(doi_r: str, doi_o: str, oa_work_id_o: str = "",
                 title_o: str = "") -> str:
    """MD5 of the replication-original pair. Full 32-char hex string.

    Some originals genuinely have no registered DOI (books, chapters, pre-DOI-era
    papers), so doi_o stays blank and every DOI-less original of the same
    replication used to hash to the same "doi_r|" — the import dedupes on pair_id
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
