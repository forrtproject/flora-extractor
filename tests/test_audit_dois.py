"""Tests for extract/audit_dois.py — the retroactive doi_o audit.

verify_and_correct and the reference builder are mocked; no live calls.
"""
from unittest.mock import patch

import pandas as pd
import pytest

from extract.audit_dois import audit_file
from shared.schema import make_pair_id

_ROW = {
    "doi_r": "10.1/repl", "title_r": "A Replication", "doi_o": "",
    "title_o": "Gender Advertisements", "authors_o": "Goffman", "year_o": "1979",
    "link_method": "llm_fulltext", "link_confidence": "high",
    "doi_o_verification": "no_doi", "oa_work_id_o": "W123",
    "pair_id": make_pair_id("10.1/repl", ""), "ref_o": "old ref",
    "bibtex_ref_o": "@book{old}", "link_evidence": "",
}

_NOT_FOUND = {"doi_o_verification": "not_found", "doi_o": "",
              "evidence_note": "No DOI found for resolved title/author"}


def _run(tmp_path, overrides: dict, verdict: dict) -> tuple[pd.DataFrame, dict]:
    csv = tmp_path / "extracted.csv"
    row = {**_ROW, **overrides}
    pd.DataFrame([row]).to_csv(csv, index=False, encoding="utf-8-sig")
    with patch("extract.audit_dois.verify_and_correct", return_value=verdict), \
         patch("extract.audit_dois._build_ref_o", return_value=("r", "a", "b")):
        counts = audit_file(csv, apply=True, report_path=tmp_path / "report.csv")
    return pd.read_csv(csv, dtype=str, encoding="utf-8-sig").fillna(""), counts


def test_no_doi_with_a_work_id_is_preserved(tmp_path):
    """verify_and_correct only searches for DOIs, so a genuinely DOI-less original
    always comes back not_found — overwriting no_doi would block the row."""
    df, counts = _run(tmp_path, {}, _NOT_FOUND)
    assert df.at[0, "doi_o_verification"] == "no_doi"
    assert counts.get("no_doi") == 1
    assert "not_found" not in counts, "the summary must not claim a change it did not make"


def test_no_doi_without_a_work_id_becomes_not_found(tmp_path):
    """Nothing identifies the original, so not_found is the honest status."""
    df, counts = _run(tmp_path, {"oa_work_id_o": ""}, _NOT_FOUND)
    assert df.at[0, "doi_o_verification"] == "not_found"
    assert counts.get("not_found") == 1


def test_a_row_that_never_claimed_no_doi_gets_not_found(tmp_path):
    df, _ = _run(tmp_path, {"doi_o_verification": "verified", "doi_o": "10.2/orig"},
                 _NOT_FOUND)
    assert df.at[0, "doi_o_verification"] == "not_found"


def test_a_correction_keys_pair_id_on_the_doi_alone(tmp_path):
    corrected = {"doi_o_verification": "corrected", "doi_o": "10.2/right",
                 "evidence_note": "DOI filled from metadata search"}
    df, _ = _run(tmp_path, {}, corrected)
    assert df.at[0, "doi_o"] == "10.2/right"
    assert df.at[0, "pair_id"] == make_pair_id("10.1/repl", "10.2/right")


def test_a_correction_clears_the_stale_work_id(tmp_path):
    """The id was resolved from the superseded DOI and may describe another work."""
    corrected = {"doi_o_verification": "corrected", "doi_o": "10.2/right",
                 "evidence_note": "DOI corrected"}
    df, _ = _run(tmp_path, {}, corrected)
    assert df.at[0, "oa_work_id_o"] == ""
    assert "oa_work_id_o cleared" in df.at[0, "link_evidence"]
