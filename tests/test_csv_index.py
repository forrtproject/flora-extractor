"""
Tests for shared/csv_index.py — the one dedup+index implementation Stage 1 and
Stage 2 now share (previously a copy each, neither of them tested).
"""

import pandas as pd

from shared.csv_index import KeyIndex, dedup_csv
from shared.row_key import row_keys


def _index(tmp_path) -> KeyIndex:
    return KeyIndex(tmp_path / "index.txt", row_keys, "Test")


def test_dedup_csv_drops_duplicates_rewrites_file_and_rebuilds_index(tmp_path):
    csv_path = tmp_path / "rows.csv"
    pd.DataFrame([
        {"doi_r": "10.1/aaa", "openalex_id_r": "", "url_r": "", "title_r": "First"},
        {"doi_r": "10.1/AAA", "openalex_id_r": "", "url_r": "", "title_r": "Dup by DOI"},
        {"doi_r": "", "openalex_id_r": "W1", "url_r": "", "title_r": "Second"},
        {"doi_r": "", "openalex_id_r": "", "url_r": "", "title_r": ""},   # no key — always kept
    ]).to_csv(csv_path, index=False, encoding="utf-8-sig")

    index = _index(tmp_path)
    before, after = dedup_csv(csv_path, index)

    assert (before, after) == (4, 3)
    kept = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    assert kept["title_r"].tolist() == ["First", "Second", ""]
    assert index.load() == {"10.1/aaa", "oa:W1"}


def test_dedup_csv_dry_run_leaves_the_file_alone(tmp_path):
    csv_path = tmp_path / "rows.csv"
    pd.DataFrame([
        {"doi_r": "10.1/aaa", "openalex_id_r": "", "url_r": "", "title_r": "First"},
        {"doi_r": "10.1/aaa", "openalex_id_r": "", "url_r": "", "title_r": "Dup"},
    ]).to_csv(csv_path, index=False, encoding="utf-8-sig")
    original = csv_path.read_bytes()

    before, after = dedup_csv(csv_path, _index(tmp_path), dry_run=True)

    assert (before, after) == (2, 1)
    assert csv_path.read_bytes() == original
    assert not (tmp_path / "index.txt").exists()


def test_dedup_csv_catches_a_collision_on_any_key_not_just_the_primary(tmp_path):
    # A later row with a NEW doi but an already-seen OpenAlex id is the same paper;
    # the incremental merge rejects it on any key, so the rebuild must too.
    csv_path = tmp_path / "rows.csv"
    pd.DataFrame([
        {"doi_r": "", "openalex_id_r": "W1", "url_r": "", "title_r": "First"},
        {"doi_r": "10.1/new", "openalex_id_r": "W1", "url_r": "", "title_r": "Same paper, DOI added"},
    ]).to_csv(csv_path, index=False, encoding="utf-8-sig")

    before, after = dedup_csv(csv_path, _index(tmp_path))

    assert (before, after) == (2, 1)
    kept = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    assert kept["title_r"].tolist() == ["First"]
