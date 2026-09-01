"""api_stage3.py — Stage 3 read out of the code and the CSV it rendered.

The same split as the other two stage APIs. What Stage 3 ASKS — the ladder's steps,
which link methods count as resolved, the two outcome vocabularies, the models behind
each call site — is read from `shared/schema.py`, `shared/config.py` and
`extract/link_original.py`. What Stage 3 PRODUCED is counted off `data/extracted.csv`
live on every request (a few hundred KB, so it can be), and arrives with provenance.

One thing is hand-written and says so: `_LADDER`, the ORDER of the steps. The ladder is
control flow inside `run_for_doi()` — a sequence of early returns, not a list a reader
can import — so the order is transcribed here and each step is keyed to the
`link_method` it writes. That key is what makes the transcription checkable: every
method named below must be in `LINK_METHOD_VALUES`, and `_ladder()` marks any that is
not, so a renamed method shows up on the page instead of quietly describing a step
that no longer exists.
"""
from typing import Any

from flask import Blueprint, jsonify

from validate import sources

stage3_bp = Blueprint("stage3_api", __name__)

# The resolution ladder in the order `run_for_doi()` tries it, cheapest first. Each
# step names the `link_method` it writes, which is how the live counts below attach to
# it and how `_ladder()` proves the transcription still matches the schema.
_LADDER = [
    ("title_pattern_match", "Title pattern",
     "The paper's own title states the target — \"A replication of Smith (2009)\". "
     "Free: no API call, no model.", "free"),
    ("citation_context_match", "Citation context",
     "A deterministic rule scores the paper's citations: which one does the text "
     "point at when it says what it replicated?", "free"),
    ("llm_cited_candidates", "Abstract over candidates",
     "The linking model reads the abstract against the paper's candidate list and "
     "names the target — and codes the outcome in the same call.", "one model call"),
    ("llm_references", "Reference-list pick",
     "The model chooses the target from the paper's OpenAlex reference list. Accepted "
     "only when it marks the target match_certain.", "one model call"),
    ("llm_title_search", "Pre-PDF title search",
     "Only when both screen voters were qualifying AND confident. CrossRef and "
     "OpenAlex title hits are pooled and the model adjudicates, with decline offered "
     "first-class.", "10x an OpenAlex filter query, plus a model call"),
    ("llm_author_year_search", "Author-and-year search",
     "The same pooled, adjudicated shape, shortlisted by author and year instead of "
     "title.", "10x an OpenAlex filter query, plus a model call"),
    ("llm_fulltext", "Full text",
     "The document waterfall acquires the paper, the parser that scores best wins, and "
     "the model reads the body — the only step that can see the methods and the "
     "closing sections.", "a download, a parse and a long model call"),
]

# Where a row goes when it is not shipped. Keyed by destination file so the page can
# say what each one MEANS rather than listing fourteen statuses.
_SET_ASIDE_COPY = {
    "target_pending.csv": (
        "Nothing settled the target. The ladder ran out of steps, or an outage stopped "
        "it below an accepted link. Does NOT settle the work — but a current-generation "
        "target_pending rests until a new generation or an explicit --redo reopens it, "
        "because re-running the same evidence buys the same answer."),
    "api_error.csv": (
        "A provider failed after its retries. Never a verdict about the paper — the "
        "next run retries it immediately."),
    "no_original_found.csv": (
        "The ladder read the paper and concluded it names no identifiable original."),
    "unidentified_original.csv": (
        "The paper names an original the pipeline cannot identify: no DOI on the "
        "record, none recoverable, no OpenAlex id. The link is kept for a human, but a "
        "validation pair cannot be keyed on a title."),
    "keyed_link_disputed.csv": (
        "A second, cold model call was shown only the study, the quoted evidence and "
        "the chosen record, and confidently said that is not the named target. The "
        "link, the outcome and both readings are kept for a human."),
    "search_link_unconfirmed.csv": (
        "A pooled-search link whose confirmation grade was short of clearly_target. "
        "The grade sets link_confidence and is appended to link_evidence; the row is "
        "held back rather than dropped."),
    "not_a_replication.csv": (
        "The screen or the outcome coder concluded this is not a replication or "
        "reproduction at all."),
    "prescreen_discard.csv": (
        "Historical: the cheap discard-only tier dropped it. That tier is dormant, so "
        "nothing writes here now."),
    "screen_disagreement.csv": (
        "Historical: a terminal state that no longer exists. Rows on disk are still "
        "routed by the value."),
    "unresolved_self_links.csv": "The resolved original is the replication itself.",
    "unresolved_doi_mismatch.csv":
        "Verification found doi_o pointing at different metadata and could not correct it.",
    "unregistered_original_doi.csv": "The original's DOI resolves to no registry record.",
}


def _ladder() -> list[dict]:
    """The steps, each checked against the schema's own method vocabulary."""
    from shared.schema import LINK_METHOD_VALUES, RESOLVED_LINK_METHODS

    return [{"method": method, "name": name, "blurb": blurb, "cost": cost,
             "resolved": method in RESOLVED_LINK_METHODS,
             "known": method in LINK_METHOD_VALUES}
            for method, name, blurb, cost in _LADDER]


def _vocabularies() -> dict:
    """The three outcome scales, read from the schema that normalises against them."""
    from shared.schema import (COMPUTATION_OUTCOME_VALUES, OUTCOME_CATEGORIES,
                               ROBUSTNESS_OUTCOME_VALUES)

    return {
        "replication": sorted(OUTCOME_CATEGORIES),
        "computation": sorted(COMPUTATION_OUTCOME_VALUES),
        "robustness": sorted(ROBUSTNESS_OUTCOME_VALUES),
    }


def _models() -> dict:
    """One model per call site, named for the question it answers."""
    from shared import config as C

    return {
        "calls": [
            {"site": "LINKING_MODEL", "asks": "which original did this paper re-test?",
             "model": C.LINKING_MODEL, "effort": C.LINKING_EFFORT},
            {"site": "OUTCOME_MODEL", "asks": "what was the outcome?",
             "model": C.OUTCOME_MODEL, "effort": C.OUTCOME_EFFORT},
            {"site": "PDF_PARSE_MODEL", "asks": "what does this document say?",
             "model": C.PDF_PARSE_MODEL, "effort": ""},
        ],
        "workers": C.EXTRACT_WORKERS,
    }


def _methods() -> dict:
    """Which link methods ship, which are quarantined, and where a held row goes."""
    from shared.schema import (PROVISIONAL_LINK_METHODS, REOPENED_SET_ASIDE_FILES,
                               RESOLVED_LINK_METHODS, SET_ASIDE_DESTINATIONS)

    files: dict[str, list[str]] = {}
    for status, filename in SET_ASIDE_DESTINATIONS.items():
        files.setdefault(filename, []).append(status)
    reopened = set(REOPENED_SET_ASIDE_FILES)
    return {
        "resolved": sorted(RESOLVED_LINK_METHODS),
        "provisional": sorted(PROVISIONAL_LINK_METHODS),
        "set_aside": [{"file": f, "statuses": sorted(s),
                       "settles": f not in reopened,
                       "meaning": _SET_ASIDE_COPY.get(f, "")}
                      for f, s in sorted(files.items())],
    }


def _counts(df) -> dict:
    """Live distributions off the rendered CSV — what the ladder actually did."""
    wanted = ("link_method", "link_confidence", "pdf_source", "parse_method",
              "type", "doi_o_verification", "original_match_type")
    out: dict[str, Any] = {"rows": int(len(df))}
    for column in wanted:
        if column not in df.columns:
            continue
        series = df[column].fillna("").astype(str).str.strip().replace("", "(none)")
        out[column] = {str(k): int(v) for k, v in series.value_counts().items()}
    return out


@stage3_bp.route("/api/stage3")
def api_stage3():
    """The ladder, the vocabularies, the models, and what they produced here."""
    from extract.link_original import EXTRACT_LADDER_VERSION, OUTCOME_DESCENT

    payload: dict[str, Any] = {
        "ladder": _ladder(),
        "ladder_version": EXTRACT_LADDER_VERSION,
        "outcome_descent": bool(OUTCOME_DESCENT),
        "vocabularies": _vocabularies(),
        "models": _models(),
        "methods": _methods(),
        "counts": {},
    }
    df, prov = sources.extracted_csv()
    payload["provenance"] = prov
    if df is not None:
        payload["counts"] = _counts(df)
    return jsonify(payload)
