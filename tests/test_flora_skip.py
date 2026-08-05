"""
The already-in-FLoRA skip list (shared/flora_skip.py).

Stage 3 must never re-extract, and the validation hand-off must never re-validate,
a replication FLoRA already holds. Two sources feed it: the entry sheet, where only
an adjudicated validation_status counts, and flora.csv, the published database,
every row of which is by definition already in FLoRA. Both also name OSF records by
URL rather than DOI, so both contribute the DOI those URLs resolve to.

Tested against `shared.flora_skip` directly rather than through `run_extract`'s
alias: `csv_to_db` is the other consumer, and the contract belongs to neither.
"""
import pandas as pd
import pytest

from shared.flora_skip import (
    ENTRY_SHEET_NAME,
    FLORA_CSV_NAME,
    SkipListUnreadable,
    default_flora_skip_dois,
    load_flora_skip_dois,
)


def _sheet(tmp_path, rows):
    p = tmp_path / "sheet.csv"
    pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
    return p


def _sheet_at(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _flora(tmp_path, rows):
    p = tmp_path / "flora.csv"
    pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8-sig")
    return p


def test_validated_chosen_is_skipped(tmp_path):
    """Regression: 'validated - chosen' was omitted, so those rows leaked
    through to extraction and on to validation (e.g. 10.1037/per0000041)."""
    sheet = _sheet(tmp_path, [
        {"doi_r": "10.1/chosen",    "validation_status": "validated - chosen"},
        {"doi_r": "10.1/unchanged", "validation_status": "validated - unchanged"},
        {"doi_r": "10.1/changed",   "validation_status": "validated - changed"},
    ])
    assert load_flora_skip_dois(sheet, None) == {
        "10.1/chosen", "10.1/unchanged", "10.1/changed"}


def test_unvalidated_statuses_not_skipped(tmp_path):
    sheet = _sheet(tmp_path, [
        {"doi_r": "10.1/blank",    "validation_status": ""},
        {"doi_r": "10.1/help",     "validation_status": "help needed"},
        {"doi_r": "10.1/hold",     "validation_status": "on hold"},
        {"doi_r": "10.1/awaiting", "validation_status": "awaiting validation"},
    ])
    assert load_flora_skip_dois(sheet, None) == set()


def test_flora_csv_skipped_wholesale(tmp_path):
    """flora.csv has no validation_status — everything in it is already in FLoRA."""
    flora = _flora(tmp_path, [
        {"doi_r": "10.2/a", "alt_identifier_r": ""},
        {"doi_r": "10.2/b", "alt_identifier_r": "10.2/b-alt"},
        {"doi_r": "",       "alt_identifier_r": ""},
    ])
    assert load_flora_skip_dois(None, flora) == {"10.2/a", "10.2/b", "10.2/b-alt"}


def test_both_sources_are_unioned(tmp_path):
    sheet = _sheet(tmp_path, [
        {"doi_r": "10.1/chosen", "validation_status": "validated - chosen"},
        {"doi_r": "10.1/blank",  "validation_status": ""},
    ])
    flora = _flora(tmp_path, [{"doi_r": "10.2/a", "alt_identifier_r": ""}])
    assert load_flora_skip_dois(sheet, flora) == {"10.1/chosen", "10.2/a"}


def test_missing_files_are_non_fatal(tmp_path):
    """One unreadable source must never silently disable the whole skip list."""
    assert load_flora_skip_dois(tmp_path / "nope.csv",
                                tmp_path / "also-nope.csv") == set()


def test_an_osf_record_named_only_by_url_is_still_skipped(tmp_path):
    """FLoRA identifies an OSF record by URL five times more often than by DOI
    — over flora.csv, 366 rows by URL against 51 by DOI. The Reproducibility
    Project rows are the case that found it: all 92 carry the aggregate Science
    paper in `doi_r` and the individual replication's OSF page in `url_r`, so a
    DOI-keyed skip list re-extracts every one of them."""
    flora = _flora(tmp_path, [
        {"doi_r": "10.1126/science.aac4716", "alt_identifier_r": "",
         "url_r": "https://osf.io/su6bm"},
        {"doi_r": "", "alt_identifier_r": "", "url_r": "http://osf.io/XSE7Q/"},
        {"doi_r": "", "alt_identifier_r": "", "url_r": "https://example.com/paper"},
    ])
    got = load_flora_skip_dois(None, flora)
    assert "10.17605/osf.io/su6bm" in got
    assert "10.17605/osf.io/xse7q" in got          # case and trailing slash
    assert got == {"10.1126/science.aac4716", "10.17605/osf.io/su6bm",
                   "10.17605/osf.io/xse7q"}


def test_the_entry_sheet_contributes_osf_urls_only_when_validated(tmp_path):
    sheet = _sheet(tmp_path, [
        {"doi_r": "10.1/chosen", "validation_status": "validated - chosen",
         "url_r": "https://osf.io/aaaaa"},
        {"doi_r": "10.1/blank", "validation_status": "",
         "url_r": "https://osf.io/bbbbb"},
    ])
    assert load_flora_skip_dois(sheet, None) == {
        "10.1/chosen", "10.17605/osf.io/aaaaa"}


def test_an_unreadable_file_stops_the_run(tmp_path):
    """Absent and unreadable are different facts. An empty skip list is
    indistinguishable downstream from "nothing to skip", and the cost of that
    mistake is re-extracting and re-importing records already adjudicated — so a
    file that is present and cannot be parsed raises instead of warning."""
    flora = tmp_path / "flora.csv"
    flora.write_text('doi_r,alt_identifier_r\n"unterminated,\n', encoding="utf-8")
    with pytest.raises(SkipListUnreadable) as exc:
        load_flora_skip_dois(None, flora)
    assert str(flora) in str(exc.value)


def test_an_entry_sheet_without_its_status_column_stops_the_run(tmp_path):
    """A sheet with no validation_status cannot say which rows are validated; reading
    it as "none of them" silently disables the skip list."""
    sheet = _sheet(tmp_path, [{"doi_r": "10.1/a"}])
    with pytest.raises(SkipListUnreadable):
        load_flora_skip_dois(sheet, None)


def test_default_skip_list_reads_the_two_standard_names(tmp_path):
    """Both consumers come through default_flora_skip_dois, so the filenames live in
    one place — run_extract used to spell them out itself."""
    _sheet_at(tmp_path / ENTRY_SHEET_NAME,
              [{"doi_r": "10.1/chosen", "validation_status": "validated - chosen"}])
    _sheet_at(tmp_path / FLORA_CSV_NAME,
              [{"doi_r": "10.2/a", "alt_identifier_r": ""}])
    assert default_flora_skip_dois(tmp_path) == {"10.1/chosen", "10.2/a"}
