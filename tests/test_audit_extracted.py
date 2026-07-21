"""Tests for extract/audit_extracted.py and the csv_to_db --audit-report gate.

Synthetic DataFrames / CSVs only; the Supabase client is mocked. No live calls.
"""
from unittest.mock import MagicMock, patch

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
        "link_method": "llm_abstract",
        "link_confidence": "high",
        "doi_o_verification": "verified",
        "outcome": "success",
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

@pytest.mark.parametrize("verification", ["mismatch", "not_found", "no_metadata",
                                          "no_doi", "api_error", "skipped", ""])
def test_doi_o_unverified_fires(verification):
    fired = _checks_fired([_clean_row(doi_o_verification=verification)])
    assert "doi_o_unverified" in fired
    assert _severity_of([_clean_row(doi_o_verification=verification)],
                        "doi_o_unverified") == BLOCKER


@pytest.mark.parametrize("verification", ["verified", "corrected"])
def test_doi_o_verified_ok(verification):
    assert "doi_o_unverified" not in _checks_fired(
        [_clean_row(doi_o_verification=verification)])


def test_self_link_fires():
    row = _clean_row(doi_o="10.1/REPL")  # differs only by case → clean_doi equal
    assert "self_link" in _checks_fired([row])


def test_self_link_not_fired_when_distinct():
    assert "self_link" not in _checks_fired([_clean_row()])


def test_self_link_not_fired_when_doi_o_empty():
    assert "self_link" not in _checks_fired([_clean_row(doi_o="")])


def test_duplicate_pair_id_fires_on_both_rows():
    rows = [_clean_row(pair_id="dup", doi_r="10.1/a"),
            _clean_row(pair_id="dup", doi_r="10.1/b")]
    report = audit_dataframe(pd.DataFrame(rows))
    dup = [r for r in report if r["check"] == "duplicate_pair_id"]
    assert len(dup) == 2
    assert all(r["severity"] == BLOCKER for r in dup)


def test_duplicate_pair_id_not_fired_when_unique():
    rows = [_clean_row(pair_id="a", doi_r="10.1/a"),
            _clean_row(pair_id="b", doi_r="10.1/b")]
    assert "duplicate_pair_id" not in _checks_fired(rows)


@pytest.mark.parametrize("field,value", [
    ("outcome", "pending"), ("outcome", "api_error"),
    ("link_method", "target_pending"), ("link_method", "api_error"),
    ("link_method", "no_original_found"),
])
def test_unresolved_stage_fires(field, value):
    assert "unresolved_stage" in _checks_fired([_clean_row(**{field: value})])


@pytest.mark.parametrize("field", ["title_r", "title_o", "abstract_r"])
def test_missing_display_field_fires(field):
    report = audit_dataframe(pd.DataFrame([_clean_row(**{field: ""})]))
    hits = [r for r in report if r["check"] == "missing_display_field"]
    assert hits and hits[0]["severity"] == BLOCKER
    assert field in hits[0]["detail"]


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
    row = _clean_row(outcome="cannot_be_determined")
    assert "outcome_not_canonical" in _checks_fired([row])
    assert _severity_of([row], "outcome_not_canonical") == WARNING


def test_outcome_canonical_ok():
    for good in ("success", "failure", "mixed", "uninformative", "descriptive"):
        assert "outcome_not_canonical" not in _checks_fired([_clean_row(outcome=good)])


def test_quote_exact_substring_ok():
    assert "quote_not_in_abstract" not in _checks_fired([_clean_row(
        abstract_r="The study found a clear effect here.",
        outcome_phrase="found a clear effect")])


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


def test_low_link_confidence_fires():
    assert "low_link_confidence" in _checks_fired([_clean_row(link_confidence="low")])


def test_low_outcome_confidence_fires():
    assert "low_outcome_confidence" in _checks_fired(
        [_clean_row(outcome_confidence="low")])


def test_multi_original_consistent_ok():
    rows = [_clean_row(pair_id="p1", doi_r="10.1/multi", doi_o="10.2/o1",
                       original_rank="1", n_originals="2"),
            _clean_row(pair_id="p2", doi_r="10.1/multi", doi_o="10.2/o2",
                       original_rank="2", n_originals="2")]
    assert "multi_original_inconsistent" not in _checks_fired(rows)


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


def test_audit_file_only_doi_filters(tmp_path):
    df = pd.DataFrame([_clean_row(doi_r="10.1/keep", doi_o_verification="mismatch"),
                       _clean_row(pair_id="other", doi_r="10.1/drop",
                                  doi_o_verification="mismatch")])
    csv = tmp_path / "e.csv"
    df.to_csv(csv, index=False, encoding="utf-8-sig")
    report_rows, _ = audit_file(csv, report_path=tmp_path / "r.csv",
                                only_doi="10.1/keep")
    assert {r["doi_r"] for r in report_rows} == {"10.1/keep"}


def test_blocked_pair_ids_reads_only_blockers(tmp_path):
    report = tmp_path / "r.csv"
    pd.DataFrame([
        {"pair_id": "b1", "doi_r": "x", "check": "self_link",
         "severity": BLOCKER, "detail": ""},
        {"pair_id": "w1", "doi_r": "y", "check": "low_link_confidence",
         "severity": WARNING, "detail": ""},
    ]).to_csv(report, index=False, encoding="utf-8-sig")
    assert blocked_pair_ids(report) == {"b1"}


def test_only_warnings_no_blockers(tmp_path):
    rows = [_clean_row(link_confidence="low")]
    _, counts = audit_file(
        _write_csv(tmp_path, rows), report_path=tmp_path / "r.csv")
    assert not any(sev == BLOCKER for (_c, sev) in counts)


def _write_csv(tmp_path, rows) -> "object":
    csv = tmp_path / "extracted.csv"
    pd.DataFrame(rows).to_csv(csv, index=False, encoding="utf-8-sig")
    return csv


# ── csv_to_db integration ────────────────────────────────────────────────────

class _FakeTable:
    def __init__(self, name, store):
        self.name, self.store = name, store

    def select(self, *args):
        exec_mock = MagicMock()
        exec_mock.execute.return_value = MagicMock(data=[])  # no existing pair_ids
        return exec_mock

    def insert(self, payload):
        self.store.setdefault(self.name, []).append(payload)
        exec_mock = MagicMock()
        exec_mock.execute.return_value = MagicMock(data=payload)
        return exec_mock


class _FakeClient:
    def __init__(self):
        self.store: dict[str, list] = {}

    def table(self, name):
        return _FakeTable(name, self.store)


def _resolved_row(**overrides) -> dict:
    row = _clean_row(**overrides)
    row["filter_status"] = "replication"
    return row


def _run_import(tmp_path, rows, monkeypatch, audit_report=None):
    import extract.csv_to_db as mod
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")
    csv = tmp_path / "extracted.csv"
    pd.DataFrame(rows).to_csv(csv, index=False, encoding="utf-8-sig")
    fake = _FakeClient()
    with patch.object(mod, "create_client", return_value=fake):
        mod.run_import(csv, audit_report=audit_report)
    return fake


def test_csv_to_db_imports_all_without_audit(tmp_path, monkeypatch):
    rows = [_resolved_row(pair_id="p1", doi_r="10.1/a"),
            _resolved_row(pair_id="p2", doi_r="10.1/b")]
    fake = _run_import(tmp_path, rows, monkeypatch)
    assert len(fake.store["unvalidated"]) == 2


def test_csv_to_db_skips_blocked_pair_id(tmp_path, monkeypatch):
    rows = [_resolved_row(pair_id="good", doi_r="10.1/a"),
            _resolved_row(pair_id="blocked", doi_r="10.1/b")]
    report = tmp_path / "audit.csv"
    pd.DataFrame([{"pair_id": "blocked", "doi_r": "10.1/b", "check": "self_link",
                   "severity": BLOCKER, "detail": ""}]).to_csv(
        report, index=False, encoding="utf-8-sig")

    fake = _run_import(tmp_path, rows, monkeypatch, audit_report=report)
    imported = {r["doi_r"] for r in fake.store["unvalidated"]}
    assert imported == {"10.1/a"}


def test_csv_to_db_warning_only_report_imports_all(tmp_path, monkeypatch):
    rows = [_resolved_row(pair_id="p1", doi_r="10.1/a"),
            _resolved_row(pair_id="p2", doi_r="10.1/b")]
    report = tmp_path / "audit.csv"
    pd.DataFrame([{"pair_id": "p1", "doi_r": "10.1/a", "check": "low_link_confidence",
                   "severity": WARNING, "detail": ""}]).to_csv(
        report, index=False, encoding="utf-8-sig")
    fake = _run_import(tmp_path, rows, monkeypatch, audit_report=report)
    assert len(fake.store["unvalidated"]) == 2
