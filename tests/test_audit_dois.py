"""Tests for extract/audit_dois.py — the retroactive doi_o audit.

The audit READS the exported CSV and WRITES the verdicts that CSV is rendered from,
so what it produces is a set of per-row corrections keyed by pair id
(`_patches`), not a rewritten file. `verify_and_correct` and the reference builder
are mocked; no live calls.
"""
from unittest.mock import MagicMock, patch

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


def _run(tmp_path, overrides: dict, verdict: dict) -> tuple[dict, dict]:
    """`(the row's correction, the status counts)` for one audited row.

    Dry-run: the corrections are what `--apply` would send to
    `tier.supersede_targets`, and asserting on them is asserting on what would be
    written. The pair id they are keyed on is the row's AS READ.
    """
    csv = tmp_path / "extracted.csv"
    row = {**_ROW, **overrides}
    pd.DataFrame([row]).to_csv(csv, index=False, encoding="utf-8-sig")
    with patch("extract.audit_dois.verify_and_correct", return_value=verdict), \
         patch("extract.audit_dois._build_ref_o", return_value=("r", "a", "b")):
        counts = audit_file(csv, report_path=tmp_path / "report.csv")
    patches = counts.pop("_patches")
    return patches.get(str(row["pair_id"]), {}), counts


def test_no_doi_with_a_work_id_is_preserved(tmp_path):
    """verify_and_correct only searches for DOIs, so a genuinely DOI-less original
    always comes back not_found — overwriting no_doi would block the row."""
    patch_, counts = _run(tmp_path, {}, _NOT_FOUND)
    assert "doi_o_verification" not in patch_
    assert counts.get("no_doi") == 1
    assert "not_found" not in counts, "the summary must not claim a change it did not make"


def test_no_doi_without_a_work_id_becomes_not_found(tmp_path):
    """Nothing identifies the original, so not_found is the honest status."""
    patch_, counts = _run(tmp_path, {"oa_work_id_o": ""}, _NOT_FOUND)
    assert patch_["doi_o_verification"] == "not_found"
    assert counts.get("not_found") == 1


def test_a_row_that_never_claimed_no_doi_gets_not_found(tmp_path):
    patch_, _ = _run(tmp_path, {"doi_o_verification": "verified", "doi_o": "10.2/orig"},
                     _NOT_FOUND)
    assert patch_["doi_o_verification"] == "not_found"


def test_a_correction_keys_pair_id_on_the_doi_alone(tmp_path):
    corrected = {"doi_o_verification": "corrected", "doi_o": "10.2/right",
                 "evidence_note": "DOI filled from metadata search"}
    patch_, _ = _run(tmp_path, {}, corrected)
    assert patch_["doi_o"] == "10.2/right"
    assert patch_["pair_id"] == make_pair_id("10.1/repl", "10.2/right")


def test_a_correction_clears_the_stale_work_id(tmp_path):
    """The id was resolved from the superseded DOI and may describe another work."""
    corrected = {"doi_o_verification": "corrected", "doi_o": "10.2/right",
                 "evidence_note": "DOI corrected"}
    patch_, _ = _run(tmp_path, {}, corrected)
    assert patch_["oa_work_id_o"] == ""
    assert "oa_work_id_o cleared" in patch_["link_evidence"]


def test_a_mismatch_blanks_the_doi_as_the_pipeline_does(tmp_path):
    """`_verify_row` drops a mismatched doi_o and everything derived from it. The
    audit used to leave it in place, so an audited row reached
    unresolved_doi_mismatch.csv still carrying the discredited DOI."""
    mismatch = {"doi_o_verification": "mismatch", "doi_o": "10.2/wrong",
                "evidence_note": "metadata describes a different paper"}
    patch_, counts = _run(tmp_path, {
        "doi_o": "10.2/wrong", "doi_o_verification": "verified",
        "pair_id": make_pair_id("10.1/repl", "10.2/wrong")}, mismatch)
    assert counts.get("mismatch") == 1
    assert patch_["doi_o"] == ""
    assert patch_["bibtex_ref_o"] == ""
    assert patch_["oa_work_id_o"] == ""
    assert patch_["pair_id"] == make_pair_id("10.1/repl", "")
    assert patch_["link_confidence"] == "low"
    assert "title_o" not in patch_, \
        "the title/author/year claim stays, so the row can still be reviewed"


def test_apply_supersedes_the_stored_verdict_rather_than_editing_the_csv(tmp_path):
    """The export is the CSV's only writer, so a correction written into the file
    would be gone at the next render. It goes to the verdict the row comes from."""
    csv = tmp_path / "extracted.csv"
    pd.DataFrame([_ROW]).to_csv(csv, index=False, encoding="utf-8-sig")
    before = csv.read_bytes()
    corrected = {"doi_o_verification": "corrected", "doi_o": "10.2/right",
                 "evidence_note": "DOI filled from metadata search"}
    sent = MagicMock(return_value={"works": 1, "rows": 1, "unmatched": [],
                                   "claim": "c-fix"})

    with patch("extract.audit_dois.verify_and_correct", return_value=corrected), \
         patch("extract.audit_dois._build_ref_o", return_value=("r", "a", "b")), \
         patch("extract.audit_dois.apply_corrections", sent):
        audit_file(csv, apply=True, report_path=tmp_path / "report.csv")

    assert csv.read_bytes() == before, "the audit must not touch the exported file"
    patches = sent.call_args[0][0]
    assert list(patches) == [_ROW["pair_id"]], \
        "corrections are keyed on the pair id the row was READ under"
    assert patches[_ROW["pair_id"]]["doi_o"] == "10.2/right"
