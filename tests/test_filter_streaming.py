"""
Tests for the streamed (chunk-bounded) Stage-2 filter in filter/run_filter.py,
plus the shared dedup+index implementation in shared/csv_index.py that Stage 1
and Stage 2 both build on.

The refactor moved classification + writing inside the 50k-row chunk loop so a
default run never loads the whole candidates.csv into memory.  The load-bearing
invariant these tests protect is that the resume keys written to
cache/filtered_index.txt are byte-for-byte identical to what the previous
pd.concat(chunks, ignore_index=True) implementation produced — in particular the
idx:<n> fallback used for rows that carry no doi/openalex/url/title, whose key is
their 0-based position among surviving rows in read order.  If the keys drift, a
resumed run against an existing index would reprocess or duplicate rows.
"""

from unittest.mock import patch

import pandas as pd
import pytest

import filter.run_filter as rf
from shared.csv_index import KeyIndex, dedup_csv
from shared.row_key import primary_key, row_keys
from shared.schema import CANDIDATES_COLS


def _blank_row(**over) -> dict:
    row = {c: "" for c in CANDIDATES_COLS}
    row["year_r"] = "2020"
    row["source"] = "openalex"
    row.update(over)
    return row


def _write_candidates(path, rows: list[dict]) -> None:
    df = pd.DataFrame(rows).reindex(columns=CANDIDATES_COLS, fill_value="")
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _expected_keys_old_style(candidates_path, from_year=None, to_year=None,
                             source=None) -> list[str]:
    """Reproduce the pre-refactor key computation: read in chunks, apply the
    year/source filters, concat with ignore_index=True, then key each row by
    primary_key() or idx:<concat_index>.  Returns keys in write order."""
    def _year_int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    chunks = []
    for chunk in pd.read_csv(candidates_path, dtype=str, encoding="utf-8-sig",
                             chunksize=50_000, low_memory=False):
        chunk = chunk.fillna("").reindex(columns=CANDIDATES_COLS, fill_value="")
        if from_year is not None or to_year is not None:
            years = chunk["year_r"].apply(_year_int)
            mask = pd.Series(True, index=chunk.index)
            if from_year is not None:
                mask &= years.apply(lambda y: y is not None and y >= from_year)
            if to_year is not None:
                mask &= years.apply(lambda y: y is not None and y <= to_year)
            chunk = chunk[mask]
        if source is not None:
            chunk = chunk[chunk["source"].str.lower() == source.lower()]
        if not chunk.empty:
            chunks.append(chunk)

    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=CANDIDATES_COLS)
    keys = []
    for row_idx, row in df.iterrows():
        key = primary_key(row)
        if not key:
            key = f"idx:{row_idx}"
        keys.append(key)
    return keys


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(rf, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rf.FILTERED_INDEX, "path", tmp_path / "filtered_index.txt")


def _read_index(tmp_path) -> list[str]:
    idx_path = tmp_path / "filtered_index.txt"
    if not idx_path.exists():
        return []
    return [ln for ln in idx_path.read_text(encoding="utf-8").splitlines() if ln]


# Rows: a mix of doi-bearing, oa-only, url-only, title-only, and fully id-less
# rows so every primary_key branch AND the idx:<n> fallback are exercised.
_ROWS = [
    _blank_row(doi_r="10.1/aaa", title_r="Replication of Foo"),
    _blank_row(openalex_id_r="W123", title_r="Repro of Bar"),
    _blank_row(url_r="http://ex.org/x", title_r="Some paper"),
    _blank_row(title_r="Title Only Paper"),
    _blank_row(),                                   # fully id-less  → idx:4
    _blank_row(doi_r="10.1/bbb", title_r="Another"),
    _blank_row(),                                   # fully id-less  → idx:6
    _blank_row(year_r="1999", title_r="Old id-less"),  # id-less, older year
]


@pytest.mark.parametrize("from_year", [None, 2000])
def test_streamed_keys_match_old_concat(tmp_path, monkeypatch, from_year):
    """Streamed keys == old concat-ignore_index keys, with and without a year
    filter. With a filter dropping rows, idx:<n> must count position among
    *surviving* rows — exactly as pd.concat(ignore_index=True) did."""
    _patch_paths(monkeypatch, tmp_path)
    _write_candidates(tmp_path / "candidates.csv", _ROWS)

    expected = _expected_keys_old_style(tmp_path / "candidates.csv",
                                        from_year=from_year)
    # Force small chunks so the multi-chunk path is exercised on a tiny file.
    with patch.object(rf.pd, "read_csv", _chunked_read(3)):
        rf.run_filter(from_year=from_year)

    assert _read_index(tmp_path) == expected
    # idx fallback must be present for the two fully id-less rows; the 1999
    # id-less row is filtered out at from_year=2000, so survivors keep their
    # positions among survivors (idx:4, idx:6), not global positions.
    assert "idx:4" in expected and "idx:6" in expected


def test_resume_no_reprocess_no_duplicate(tmp_path, monkeypatch):
    """A second run over the same candidates writes nothing new and leaves the
    index unchanged — including the id-less idx:<n> rows."""
    _patch_paths(monkeypatch, tmp_path)
    _write_candidates(tmp_path / "candidates.csv", _ROWS)

    first = rf.run_filter()                  # returns count of new rows written
    index_after_first = _read_index(tmp_path)
    second = rf.run_filter()
    index_after_second = _read_index(tmp_path)

    assert first == len(_ROWS)
    assert second == 0                           # nothing reprocessed
    assert index_after_first == index_after_second
    assert len(index_after_first) == len(set(index_after_first))  # no duplicates


def test_resume_after_limit_continues_cleanly(tmp_path, monkeypatch):
    """Run 1 stops at --limit; run 2 processes exactly the remainder, and the
    final index equals a single unlimited run's index (order preserved)."""
    _patch_paths(monkeypatch, tmp_path)
    _write_candidates(tmp_path / "candidates.csv", _ROWS)

    expected = _expected_keys_old_style(tmp_path / "candidates.csv")
    rf.run_filter(limit=3)
    after_limit = _read_index(tmp_path)
    rf.run_filter()
    final = _read_index(tmp_path)

    assert after_limit == expected[:3]
    assert final == expected
    assert len(final) == len(set(final))


def test_needs_review_rows_are_written_through(tmp_path, monkeypatch):
    """Stage 2 no longer escalates undecidable rows to an LLM. A `needs_review`
    verdict is written and indexed like any other, and Stage 3's front-door screen
    is what decides it."""
    _patch_paths(monkeypatch, tmp_path)
    _write_candidates(tmp_path / "candidates.csv", _ROWS)

    wrote = rf.run_filter()
    out = pd.read_csv(tmp_path / "filtered.csv", dtype=str).fillna("")

    assert wrote == len(_ROWS)
    assert set(out["filter_method"]) == {"rule_based"}
    assert len(_read_index(tmp_path)) == len(_ROWS)
    # A second run reprocesses nothing: every row, needs_review included, is indexed.
    assert rf.run_filter() == 0


# ── shared/csv_index.py: the dedup+index implementation both stages share ────

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
        {"doi_r": "10.1/new", "openalex_id_r": "W1", "url_r": "",
         "title_r": "Same paper, DOI added"},
    ]).to_csv(csv_path, index=False, encoding="utf-8-sig")

    before, after = dedup_csv(csv_path, _index(tmp_path))

    assert (before, after) == (2, 1)
    kept = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    assert kept["title_r"].tolist() == ["First"]


def _chunked_read(chunksize: int):
    """Return a read_csv replacement that forces a small chunksize so the
    multi-chunk streaming path runs on tiny test files (real chunksize is 50k)."""
    real = pd.read_csv

    def _reader(*args, **kwargs):
        if "chunksize" in kwargs:
            kwargs["chunksize"] = chunksize
        return real(*args, **kwargs)

    return _reader
