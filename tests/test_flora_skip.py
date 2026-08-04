"""
The already-in-FLoRA skip list (shared/flora_skip.py).

Stage 3 must never re-extract, and the validation hand-off must never re-validate,
a replication FLoRA already holds. Two sources feed it: the entry sheet, where only
an adjudicated validation_status counts, and flora.csv, the published database,
every row of which is by definition already in FLoRA.
"""
import pandas as pd
import pytest

from shared.flora_skip import load_flora_skip_dois


def _csv(tmp_path, name, rows):
    path = tmp_path / name
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path


@pytest.mark.parametrize("status,skipped", [
    # Regression: 'validated - chosen' was omitted, so those rows leaked through to
    # extraction and on to validation (e.g. 10.1037/per0000041).
    ("validated - chosen", True),
    ("validated - unchanged", True),
    ("validated - changed", True),
    ("validated - discarded", True),
    # These are in flight and genuinely still need the pipeline.
    ("", False),
    ("help needed", False),
    ("on hold", False),
    ("awaiting validation", False),
])
def test_only_an_adjudicated_entry_sheet_row_is_skipped(tmp_path, status, skipped):
    sheet = _csv(tmp_path, "sheet.csv",
                 [{"doi_r": "10.1/row", "validation_status": status}])
    assert load_flora_skip_dois(sheet, None) == ({"10.1/row"} if skipped else set())


@pytest.mark.parametrize("sheet_rows,flora_rows,expected", [
    # flora.csv has no validation_status: both DOI columns are skipped wholesale,
    # and a blank contributes nothing.
    (None, [{"doi_r": "10.2/a", "doi_r_alt": ""},
            {"doi_r": "10.2/b", "doi_r_alt": "10.2/b-alt"},
            {"doi_r": "", "doi_r_alt": ""}],
     {"10.2/a", "10.2/b", "10.2/b-alt"}),
    # Both sources are unioned, each still applying its own rule.
    ([{"doi_r": "10.1/chosen", "validation_status": "validated - chosen"},
      {"doi_r": "10.1/blank", "validation_status": ""}],
     [{"doi_r": "10.2/a", "doi_r_alt": ""}],
     {"10.1/chosen", "10.2/a"}),
])
def test_the_two_sources_are_unioned(tmp_path, sheet_rows, flora_rows, expected):
    sheet = _csv(tmp_path, "sheet.csv", sheet_rows) if sheet_rows else None
    flora = _csv(tmp_path, "flora.csv", flora_rows) if flora_rows else None
    assert load_flora_skip_dois(sheet, flora) == expected


def test_missing_files_are_non_fatal(tmp_path):
    """One unreadable source must never silently disable the whole skip list."""
    assert load_flora_skip_dois(tmp_path / "nope.csv",
                                tmp_path / "also-nope.csv") == set()
