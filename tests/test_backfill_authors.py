"""backfill_authors: which rows it decides to CORRECT, and what it sends.

It reads the exported CSV and writes the verdicts that CSV is rendered from, so the
seams that matter are the ones that decide whether a row is touched at all — a dry
run must send nothing, an already-correct row must not be corrected, and a lookup
that failed must leave the row alone rather than stamping a degraded value over real
data. The OpenAlex side is `_build_ref_o`, which is mocked here; the module imports
it by name, so the patch target is the module's own binding.
"""
import csv
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from extract import backfill_authors
from shared.schema import EXTRACTED_COLS, make_pair_id


def _write_csv(path, rows: list[dict]) -> None:
    """A real extracted.csv: every schema column, blank where the row says nothing."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXTRACTED_COLS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in EXTRACTED_COLS})


def _pair(doi_r: str, doi_o: str) -> str:
    return make_pair_id(doi_r, doi_o)


@pytest.fixture()
def csv_path(tmp_path):
    path = tmp_path / "extracted.csv"
    _write_csv(path, [
        # stale: OpenAlex knows more authors than the row records
        {"doi_r": "10.1000/r1", "doi_o": "10.1000/o1", "title_o": "Original One",
         "year_o": "1999", "authors_o": "Smith", "ref_o": "Smith · 1999",
         "pair_id": _pair("10.1000/r1", "10.1000/o1"), "link_method": "llm_abstract"},
        # already filled with exactly what OpenAlex returns
        {"doi_r": "10.1000/r2", "doi_o": "10.1000/o2", "title_o": "Original Two",
         "year_o": "2001", "authors_o": "Jones; Lee", "ref_o": "Jones & Lee (2001).",
         "bibtex_ref_o": "@article{jones2001}",
         "pair_id": _pair("10.1000/r2", "10.1000/o2"), "link_method": "llm_abstract"},
        # no doi_o and no title_o: nothing to look up by
        {"doi_r": "10.1000/r3", "doi_o": "", "title_o": "",
         "pair_id": _pair("10.1000/r3", ""), "link_method": "target_pending"},
        # a title, but the ladder found no original — skipped by link_method
        {"doi_r": "10.1000/r4", "doi_o": "", "title_o": "Ghost Original",
         "pair_id": _pair("10.1000/r4", ""), "link_method": "no_original_found"},
    ])
    return path


_ANSWERS = {
    "10.1000/o1": ("Smith, A., Brown, B., & Chen, C. (1999). Original One.",
                   "Smith, A.; Brown, B.; Chen, C.", "@article{smith1999}"),
    "10.1000/o2": ("Jones & Lee (2001).", "Jones; Lee", "@article{jones2001}"),
}


def _fake_build_ref_o(doi_o, fallback_author="", fallback_year="", title_o=""):
    return _ANSWERS[doi_o]


def test_dry_run_reports_the_change_and_writes_nothing(csv_path, capsys):
    before = csv_path.read_bytes()
    with patch.object(backfill_authors, "_build_ref_o", side_effect=_fake_build_ref_o):
        patches = backfill_authors.backfill(csv_path, apply=False)
    assert csv_path.read_bytes() == before
    out = capsys.readouterr().out
    assert "1 rows changed out of 4 total" in out
    assert "Dry-run" in out
    assert list(patches) == [_pair("10.1000/r1", "10.1000/o1")]


def test_the_stale_row_is_corrected_and_the_others_are_left_alone(csv_path):
    """The correction names the stored target by pair id and carries all three
    columns the reference builder settles together."""
    sent = MagicMock(return_value={"works": 1, "rows": 1, "unmatched": [],
                                   "claim": "c-fix"})
    before = csv_path.read_bytes()
    with patch.object(backfill_authors, "_build_ref_o", side_effect=_fake_build_ref_o), \
         patch("extract.audit_dois.apply_corrections", sent):
        backfill_authors.backfill(csv_path, apply=True)

    assert csv_path.read_bytes() == before, "the exported file is not this tool's to write"
    patches = sent.call_args[0][0]
    assert list(patches) == [_pair("10.1000/r1", "10.1000/o1")]
    assert patches[_pair("10.1000/r1", "10.1000/o1")] == {
        "authors_o": "Smith, A.; Brown, B.; Chen, C.",
        "ref_o": "Smith, A., Brown, B., & Chen, C. (1999). Original One.",
        "bibtex_ref_o": "@article{smith1999}"}


def test_rows_with_nothing_to_look_up_are_skipped(csv_path):
    with patch.object(backfill_authors, "_build_ref_o",
                      side_effect=_fake_build_ref_o) as build:
        backfill_authors.backfill(csv_path)
    assert [call.args[0] for call in build.call_args_list] == ["10.1000/o1", "10.1000/o2"]


def test_a_lookup_failure_leaves_the_row_as_it_was(csv_path):
    """A failed OpenAlex call must not overwrite real authors with a degraded value."""
    def _boom(doi_o, *args, **kwargs):
        if doi_o == "10.1000/o1":
            raise RuntimeError("openalex 503")
        return _ANSWERS[doi_o]

    with patch.object(backfill_authors, "_build_ref_o", side_effect=_boom):
        patches = backfill_authors.backfill(csv_path)
    assert patches == {}


def test_target_doi_restricts_the_run_to_one_row(csv_path):
    with patch.object(backfill_authors, "_build_ref_o",
                      side_effect=_fake_build_ref_o) as build:
        patches = backfill_authors.backfill(csv_path, target_doi="10.1000/o2")
    assert [call.args[0] for call in build.call_args_list] == ["10.1000/o2"]
    assert patches == {}, "that row already says what OpenAlex says"


def test_a_row_with_no_pair_id_is_skipped_rather_than_guessed_at(tmp_path):
    """The pair id is how a correction names the stored target it is about."""
    path = tmp_path / "extracted.csv"
    _write_csv(path, [{"doi_r": "10.1000/r1", "doi_o": "10.1000/o1",
                       "title_o": "Original One", "year_o": "1999",
                       "authors_o": "Smith", "pair_id": "",
                       "link_method": "llm_abstract"}])
    with patch.object(backfill_authors, "_build_ref_o", side_effect=_fake_build_ref_o):
        assert backfill_authors.backfill(path) == {}


def test_a_missing_file_is_reported_rather_than_created(tmp_path):
    missing = tmp_path / "nope.csv"
    assert backfill_authors.backfill(missing, apply=True) == {}
    assert not missing.exists()
