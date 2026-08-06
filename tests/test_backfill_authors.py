"""backfill_authors: a write path straight over extracted.csv.

It rewrites authors_o/ref_o/bibtex_ref_o in place, so the seams that matter are
the ones that decide whether a row is TOUCHED at all — a dry run must write
nothing, an already-correct row must not be rewritten, and a lookup that failed
must leave the row exactly as it was rather than stamping a degraded value over
real data. The OpenAlex side is `_build_ref_o`, which is mocked here; the module
imports it by name, so the patch target is the module's own binding.
"""
import csv
from unittest.mock import patch

import pandas as pd
import pytest

from extract import backfill_authors
from shared.schema import EXTRACTED_COLS, validate_csv_columns


def _write_csv(path, rows: list[dict]) -> None:
    """A real extracted.csv: every schema column, blank where the row says nothing."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXTRACTED_COLS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in EXTRACTED_COLS})


def _read(path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")


@pytest.fixture()
def csv_path(tmp_path):
    path = tmp_path / "extracted.csv"
    _write_csv(path, [
        # stale: OpenAlex knows more authors than the row records
        {"doi_r": "10.1000/r1", "doi_o": "10.1000/o1", "title_o": "Original One",
         "year_o": "1999", "authors_o": "Smith", "ref_o": "Smith · 1999",
         "link_method": "llm_abstract"},
        # already filled with exactly what OpenAlex returns
        {"doi_r": "10.1000/r2", "doi_o": "10.1000/o2", "title_o": "Original Two",
         "year_o": "2001", "authors_o": "Jones; Lee", "ref_o": "Jones & Lee (2001).",
         "bibtex_ref_o": "@article{jones2001}", "link_method": "llm_abstract"},
        # no doi_o and no title_o: nothing to look up by
        {"doi_r": "10.1000/r3", "doi_o": "", "title_o": "",
         "link_method": "target_pending"},
        # a title, but the ladder found no original — skipped by link_method
        {"doi_r": "10.1000/r4", "doi_o": "", "title_o": "Ghost Original",
         "link_method": "no_original_found"},
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
        backfill_authors.backfill(csv_path, apply=False)
    assert csv_path.read_bytes() == before
    out = capsys.readouterr().out
    assert "1 rows changed out of 4 total" in out
    assert "Dry-run" in out


def test_apply_fills_the_stale_row_and_leaves_the_others_alone(csv_path):
    with patch.object(backfill_authors, "_build_ref_o", side_effect=_fake_build_ref_o):
        backfill_authors.backfill(csv_path, apply=True)
    df = _read(csv_path).set_index("doi_r")
    assert df.at["10.1000/r1", "authors_o"] == "Smith, A.; Brown, B.; Chen, C."
    assert df.at["10.1000/r1", "ref_o"].startswith("Smith, A., Brown, B.")
    assert df.at["10.1000/r1", "bibtex_ref_o"] == "@article{smith1999}"
    # Already correct: rewritten to the same bytes, so unchanged either way.
    assert df.at["10.1000/r2", "authors_o"] == "Jones; Lee"
    # No doi_o and no title_o: never looked up.
    assert df.at["10.1000/r3", "authors_o"] == ""


def test_rows_with_nothing_to_look_up_are_skipped(csv_path):
    with patch.object(backfill_authors, "_build_ref_o",
                      side_effect=_fake_build_ref_o) as build:
        backfill_authors.backfill(csv_path, apply=True)
    assert [call.args[0] for call in build.call_args_list] == ["10.1000/o1", "10.1000/o2"]


def test_a_lookup_failure_leaves_the_row_as_it_was(csv_path):
    """A failed OpenAlex call must not overwrite real authors with a degraded value."""
    def _boom(doi_o, *args, **kwargs):
        if doi_o == "10.1000/o1":
            raise RuntimeError("openalex 503")
        return _ANSWERS[doi_o]

    with patch.object(backfill_authors, "_build_ref_o", side_effect=_boom):
        backfill_authors.backfill(csv_path, apply=True)
    df = _read(csv_path).set_index("doi_r")
    assert df.at["10.1000/r1", "authors_o"] == "Smith"
    assert df.at["10.1000/r1", "ref_o"] == "Smith · 1999"
    assert df.at["10.1000/r1", "bibtex_ref_o"] == ""


def test_target_doi_restricts_the_write_to_one_row(csv_path):
    with patch.object(backfill_authors, "_build_ref_o",
                      side_effect=_fake_build_ref_o) as build:
        backfill_authors.backfill(csv_path, apply=True, target_doi="10.1000/o2")
    assert [call.args[0] for call in build.call_args_list] == ["10.1000/o2"]
    assert _read(csv_path).set_index("doi_r").at["10.1000/r1", "authors_o"] == "Smith"


def test_the_write_round_trips_every_schema_column_as_utf_8_sig(csv_path):
    with patch.object(backfill_authors, "_build_ref_o", side_effect=_fake_build_ref_o):
        backfill_authors.backfill(csv_path, apply=True)
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    df = _read(csv_path)
    assert list(df.columns) == EXTRACTED_COLS
    assert not validate_csv_columns(list(df.columns), "extracted")
    assert len(df) == 4


def test_a_missing_file_is_reported_rather_than_created(tmp_path):
    missing = tmp_path / "nope.csv"
    backfill_authors.backfill(missing, apply=True)
    assert not missing.exists()
