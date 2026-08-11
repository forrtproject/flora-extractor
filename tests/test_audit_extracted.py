"""Tests for extract/audit_extracted.py — the read-only pre-validation audit.

Synthetic DataFrames / CSVs only. No live calls.
"""
import pandas as pd
import pytest

from extract.audit_extracted import (
    BLOCKER, WARNING, audit_dataframe, audit_file, blocked_pair_ids,
)


def _clean_row(**overrides) -> dict:
    """A row that trips no checks. Override fields to make a check fire."""
    row = {
        "pair_id": "pid-clean",
        "doi_r": "10.1/repl",
        "title_r": "A Replication of Something",
        "abstract_r": "We replicated the effect and found strong support for it.",
        "year_r": "2020",
        "doi_o": "10.2/orig",
        "title_o": "The Original Study",
        "year_o": "2010",
        "link_method": "llm_cited_candidates",
        "link_confidence": "high",
        "doi_o_verification": "verified",
        "outcome": "successful",
        "outcome_phrase": "found strong support for it",
        "outcome_confidence": "high",
        "out_quote_source": "abstract",
        "original_rank": "1",
        "n_originals": "1",
    }
    row.update(overrides)
    return row


def _checks_fired(rows: list[dict]) -> set[str]:
    df = pd.DataFrame(rows)
    return {r["check"] for r in audit_dataframe(df)}


def _severity_of(rows: list[dict], check: str) -> str:
    df = pd.DataFrame(rows)
    for r in audit_dataframe(df):
        if r["check"] == check:
            return r["severity"]
    raise AssertionError(f"{check} did not fire")


# ── Clean baseline ───────────────────────────────────────────────────────────

def test_clean_row_fires_nothing():
    assert _checks_fired([_clean_row()]) == set()


# ── BLOCKER checks ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("verification,blocks", [
    # Only "verified" and "corrected" are a checked link…
    ("verified",  False),
    ("corrected", False),
    # …everything else — including a blank field — is not.
    ("mismatch",  True),
    ("",          True),
])
def test_doi_o_must_be_verified(verification, blocks):
    rows = [_clean_row(doi_o_verification=verification)]
    if blocks:
        assert _severity_of(rows, "doi_o_unverified") == BLOCKER
    else:
        assert "doi_o_unverified" not in _checks_fired(rows)


@pytest.mark.parametrize("work_id", ["W2003152982", "https://openalex.org/W2003152982"])
def test_no_doi_with_openalex_id_is_not_a_blocker(work_id):
    """Books, chapters and pre-DOI-era papers have no DOI to verify. The row is
    still identifiable — and clickable — through its OpenAlex work id, in either
    the bare or the URL form."""
    row = _clean_row(doi_o="", doi_o_verification="no_doi",
                     oa_work_id_o=work_id, title_o="Gender Advertisements")
    assert _checks_fired([row]) == set()


def test_no_doi_without_openalex_id_still_blocks():
    row = _clean_row(doi_o="", doi_o_verification="no_doi", oa_work_id_o="")
    assert _severity_of([row], "doi_o_unverified") == BLOCKER


def test_openalex_id_does_not_rescue_other_verification_values():
    row = _clean_row(doi_o_verification="mismatch", oa_work_id_o="W2003152982")
    assert _severity_of([row], "doi_o_unverified") == BLOCKER


def test_self_link_fires():
    row = _clean_row(doi_o="10.1/REPL")  # differs only by case → clean_doi equal
    assert "self_link" in _checks_fired([row])


def test_self_link_by_work_id_fires_on_a_no_doi_row():
    """A no_doi row is identified by its work id, so a self-link there is invisible
    to the DOI comparison."""
    row = _clean_row(doi_o="", doi_o_verification="no_doi",
                     oa_work_id_o="W999", oa_work_id_r="https://openalex.org/W999")
    assert _severity_of([row], "self_link") == BLOCKER


def test_duplicate_pair_id_fires_on_both_rows():
    rows = [_clean_row(pair_id="dup", doi_r="10.1/a"),
            _clean_row(pair_id="dup", doi_r="10.1/b")]
    report = audit_dataframe(pd.DataFrame(rows))
    dup = [r for r in report if r["check"] == "duplicate_pair_id"]
    assert len(dup) == 2
    assert all(r["severity"] == BLOCKER for r in dup)


@pytest.mark.parametrize("field,value", [
    ("outcome", "pending"),                 # the outcome half never ran
    ("link_method", "target_pending"),      # the link half never resolved
])
def test_unresolved_stage_fires(field, value):
    assert "unresolved_stage" in _checks_fired([_clean_row(**{field: value})])


def test_missing_display_field_fires():
    """A validator cannot judge a row whose display fields are blank; the detail
    names the field that is missing."""
    report = audit_dataframe(pd.DataFrame([_clean_row(title_o="")]))
    hits = [r for r in report if r["check"] == "missing_display_field"]
    assert hits and hits[0]["severity"] == BLOCKER
    assert "title_o" in hits[0]["detail"]


# ── WARNING checks ───────────────────────────────────────────────────────────

def test_original_postdates_replication_fires():
    assert "original_postdates_replication" in _checks_fired(
        [_clean_row(year_r="2010", year_o="2015")])


def test_original_postdates_within_tolerance_ok():
    # one year after is tolerated (in-press ordering)
    assert "original_postdates_replication" not in _checks_fired(
        [_clean_row(year_r="2010", year_o="2011")])


def test_original_postdates_nonnumeric_skipped():
    assert "original_postdates_replication" not in _checks_fired(
        [_clean_row(year_r="in press", year_o="2015")])


def test_outcome_not_canonical_fires():
    # NB: use a value that is genuinely absent from schema.OUTCOME_VALUES.
    # "cannot_be_determined" was non-canonical when this check was written but is
    # now a valid outcome, so it must not be used as the negative example here.
    row = _clean_row(outcome="totally_bogus_outcome")
    assert "outcome_not_canonical" in _checks_fired([row])
    assert _severity_of([row], "outcome_not_canonical") == WARNING


def test_cannot_be_determined_is_canonical():
    """Regression: cannot_be_determined is a valid outcome and must not be flagged."""
    row = _clean_row(outcome="cannot_be_determined")
    assert "outcome_not_canonical" not in _checks_fired([row])


def test_quote_whitespace_case_normalized_ok():
    assert "quote_not_in_abstract" not in _checks_fired([_clean_row(
        abstract_r="The study   found a CLEAR   effect here.",
        outcome_phrase="Found a Clear Effect")])


def test_quote_fuzzy_threshold_ok():
    # near-verbatim quote (minor word difference) passes via partial_ratio >= 85
    assert "quote_not_in_abstract" not in _checks_fired([_clean_row(
        abstract_r="Participants showed a significant reduction in anxiety scores.",
        outcome_phrase="showed a significant reduction in anxiety score")])


def test_quote_genuine_miss_fires():
    row = _clean_row(
        abstract_r="This paper is about photosynthesis in tomato plants.",
        outcome_phrase="the replication failed to reproduce the priming effect")
    assert "quote_not_in_abstract" in _checks_fired([row])
    assert _severity_of([row], "quote_not_in_abstract") == WARNING


def test_quote_not_checked_when_source_not_abstract():
    assert "quote_not_in_abstract" not in _checks_fired([_clean_row(
        out_quote_source="fulltext",
        outcome_phrase="something not present in the abstract at all here")])


def test_joined_quote_checked_against_the_source_in_the_same_position():
    """A two-passage quote lists its two sources in the same order. Testing the joined
    string against the abstract flagged every one of them, because the fulltext half
    was never in the abstract to begin with."""
    assert "quote_not_in_abstract" not in _checks_fired([_clean_row(
        abstract_r="The study found a clear effect here.",
        outcome_phrase="found a clear effect | and the discussion said something else",
        out_quote_source="abstract | fulltext")])


def test_reproduction_axis_quotes_are_checked_too():
    fired = _checks_fired([_clean_row(
        abstract_r="Re-running the code returned the reported coefficients.",
        outcome_phrase="", out_quote_source="",
        outcome_computational_quote="returned the reported coefficients",
        out_quote_computational_source="abstract",
        outcome_robustness_quote="a sentence found nowhere in this abstract at all",
        out_quote_robust_source="abstract")])
    assert "quote_not_in_abstract" in fired


def test_quote_source_count_mismatch_fires_both_ways():
    """A passage with no source of its own, or a source with no passage, leaves part of
    the quote unaudited — silently dropping the surplus said nothing about it."""
    more_quotes = _clean_row(
        abstract_r="The study found a clear effect here.",
        outcome_phrase="found a clear effect | and a second passage entirely",
        out_quote_source="abstract")
    more_sources = _clean_row(
        abstract_r="The study found a clear effect here.",
        outcome_phrase="found a clear effect",
        out_quote_source="abstract | fulltext")
    for row in (more_quotes, more_sources):
        assert "quote_source_count_mismatch" in _checks_fired([row])
        assert _severity_of([row], "quote_source_count_mismatch") == WARNING
    # The pairs that do align are still checked.
    assert "quote_not_in_abstract" in _checks_fired([_clean_row(
        abstract_r="This paper is about photosynthesis in tomato plants.",
        outcome_phrase="the priming effect did not replicate | a second passage",
        out_quote_source="abstract")])
    assert "quote_source_count_mismatch" not in _checks_fired([_clean_row()])


@pytest.mark.parametrize("field,check", [
    ("link_confidence", "low_link_confidence"),
    ("outcome_confidence", "low_outcome_confidence"),
])
def test_low_confidence_fires(field, check):
    assert check in _checks_fired([_clean_row(**{field: "low"})])


def test_multi_original_bad_ranks_fires():
    rows = [_clean_row(pair_id="p1", doi_r="10.1/multi", doi_o="10.2/o1",
                       original_rank="1", n_originals="2"),
            _clean_row(pair_id="p2", doi_r="10.1/multi", doi_o="10.2/o2",
                       original_rank="3", n_originals="2")]  # rank 3 not in 1..2
    fired = [r for r in audit_dataframe(pd.DataFrame(rows))
             if r["check"] == "multi_original_inconsistent"]
    assert len(fired) == 2 and all(r["severity"] == WARNING for r in fired)


def test_multi_original_n_disagrees_fires():
    rows = [_clean_row(pair_id="p1", doi_r="10.1/multi", doi_o="10.2/o1",
                       original_rank="1", n_originals="2"),
            _clean_row(pair_id="p2", doi_r="10.1/multi", doi_o="10.2/o2",
                       original_rank="2", n_originals="3")]  # n_originals disagree
    assert "multi_original_inconsistent" in _checks_fired(rows)


# ── audit_file / report / exit accounting ────────────────────────────────────

def test_audit_file_writes_report_and_counts(tmp_path):
    df = pd.DataFrame([_clean_row(),
                       _clean_row(pair_id="bad", doi_r="10.1/x",
                                  doi_o_verification="mismatch")])
    csv = tmp_path / "extracted.csv"
    df.to_csv(csv, index=False, encoding="utf-8-sig")
    report = tmp_path / "report.csv"

    report_rows, counts = audit_file(csv, report_path=report)
    assert report.exists()
    written = pd.read_csv(report)
    assert list(written.columns) == ["pair_id", "doi_r", "check", "severity", "detail"]
    assert counts[("doi_o_unverified", BLOCKER)] == 1

    # --doi narrows the audit to one replication.
    report_rows, _ = audit_file(csv, report_path=report, only_doi="10.1/x")
    assert {r["doi_r"] for r in report_rows} == {"10.1/x"}


def test_blocked_pair_ids_reads_only_blockers(tmp_path):
    report = tmp_path / "r.csv"
    pd.DataFrame([
        {"pair_id": "b1", "doi_r": "x", "check": "self_link",
         "severity": BLOCKER, "detail": ""},
        {"pair_id": "w1", "doi_r": "y", "check": "low_link_confidence",
         "severity": WARNING, "detail": ""},
    ]).to_csv(report, index=False, encoding="utf-8-sig")
    assert blocked_pair_ids(report) == {"b1"}


def test_audit_tolerates_a_frame_without_the_optional_columns():
    """An extracted.csv written before oa_work_id_o and the reproduction-axis quote
    columns existed still has to audit: the row checks read those fields defensively,
    so their absence is not an error — and a no_doi row with no work id to fall back
    on still blocks."""
    optional = ("oa_work_id_o", "oa_work_id_r", "outcome_computational_quote",
                "out_quote_computational_source", "outcome_robustness_quote",
                "out_quote_robust_source")
    df = pd.DataFrame([_clean_row(), _clean_row(pair_id="nd", doi_r="10.1/nd", doi_o="",
                                                doi_o_verification="no_doi")])
    assert not any(c in df.columns for c in optional)

    report = audit_dataframe(df)
    assert {r["check"] for r in report if r["pair_id"] == "pid-clean"} == set()
    assert [r["severity"] for r in report if r["check"] == "doi_o_unverified"] == [BLOCKER]
