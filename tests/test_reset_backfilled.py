"""Tests for filter.reset_backfilled — resetting Stage 2 screening for rows whose
abstract was backfilled after a title-only decision. All CSV I/O uses tmp_path;
no live APIs are touched."""
from __future__ import annotations

import pandas as pd
import pytest

import filter.run_filter as rf
import filter.reset_backfilled as rb


def _write_csv(path, rows: list[dict], cols: list[str]) -> None:
    df = pd.DataFrame(rows).reindex(columns=cols, fill_value="")
    df.to_csv(path, index=False, encoding="utf-8-sig")


_FILT_COLS = ["doi_r", "title_r", "abstract_r", "url_r", "openalex_id_r",
              "filter_status", "filter_method", "filter_evidence", "filter_confidence"]


@pytest.fixture
def paths(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    data_dir.mkdir()
    cache_dir.mkdir()
    index_path = cache_dir / "filtered_index.txt"

    monkeypatch.setattr(rb, "DATA_DIR", data_dir)
    monkeypatch.setattr(rb, "_FILTERED_INDEX_PATH", index_path)
    monkeypatch.setattr(rf, "_FILTERED_INDEX_PATH", index_path)

    return {
        "data": data_dir,
        "filtered": data_dir / "filtered.csv",
        "candidates": data_dir / "candidates.csv",
        "index": index_path,
    }


def _standard_fixture(paths):
    # filtered.csv: A/B decided with empty abstract; C already had an abstract;
    # D has no identifier at all (idx: fallback key — must be ignored).
    filtered_rows = [
        {"doi_r": "10.1/a", "abstract_r": "", "filter_status": "false_positive"},
        {"doi_r": "10.1/b", "abstract_r": "", "filter_status": "false_positive"},
        {"doi_r": "10.1/c", "abstract_r": "had one", "filter_status": "replication"},
        {"doi_r": "", "title_r": "", "url_r": "", "openalex_id_r": "",
         "abstract_r": "", "filter_status": "false_positive"},
    ]
    _write_csv(paths["filtered"], filtered_rows, _FILT_COLS)

    # candidates.csv: only 10.1/b has been backfilled with an abstract.
    candidate_rows = [
        {"doi_r": "10.1/a", "abstract_r": ""},
        {"doi_r": "10.1/b", "abstract_r": "a freshly backfilled abstract"},
        {"doi_r": "10.1/c", "abstract_r": "had one"},
    ]
    _write_csv(paths["candidates"], candidate_rows, _FILT_COLS)


def test_collect_keys_skips_abstracts_and_idless(paths):
    _standard_fixture(paths)
    keys, total = rb.collect_empty_abstract_keys(paths["filtered"])
    assert total == 4
    # C has an abstract (excluded); D has no identifier (empty _row_key, skipped).
    assert keys == {"10.1/a", "10.1/b"}


def test_reset_set_only_includes_backfilled(paths):
    _standard_fixture(paths)
    empty_keys, _ = rb.collect_empty_abstract_keys(paths["filtered"])
    reset = rb.collect_reset_keys(paths["candidates"], empty_keys)
    # Only 10.1/b now has an abstract in candidates.csv.
    assert reset == {"10.1/b"}


def test_dry_run_writes_nothing(paths):
    _standard_fixture(paths)
    before = paths["filtered"].read_bytes()
    assert not paths["index"].exists()

    summary = rb.reset_backfilled(apply=False)

    assert summary["backfilled_rows"] == 1
    assert summary["would_drop"] == 1
    assert "rows_dropped" not in summary
    assert paths["filtered"].read_bytes() == before  # unchanged
    assert not paths["index"].exists()               # not rebuilt


def test_apply_drops_exactly_reset_rows(paths):
    _standard_fixture(paths)
    # Seed a stale index so index_before is reported.
    paths["index"].write_text("10.1/a\n10.1/b\n10.1/c\n", encoding="utf-8")

    summary = rb.reset_backfilled(apply=True)

    assert summary["rows_dropped"] == 1
    assert summary["index_before"] == 3

    # Header keeps the BOM (utf-8-sig) and original column order.
    raw = paths["filtered"].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    out = pd.read_csv(paths["filtered"], dtype=str, encoding="utf-8-sig").fillna("")
    assert list(out.columns) == _FILT_COLS
    # 10.1/b dropped; all others survive.
    assert list(out["doi_r"]) == ["10.1/a", "10.1/c", ""]

    # Index rebuilt from survivors (idless D has no key).
    idx_keys = set(paths["index"].read_text(encoding="utf-8").split())
    assert idx_keys == {"10.1/a", "10.1/c"}
    assert summary["index_after"] == 2


def test_apply_refuses_without_filtered_csv(paths):
    # candidates present, filtered absent.
    _write_csv(paths["candidates"], [{"doi_r": "10.1/a", "abstract_r": "x"}], _FILT_COLS)
    with pytest.raises(FileNotFoundError, match="filtered"):
        rb.reset_backfilled(apply=True)


# ---------------------------------------------------------------------------
# End-to-end: reset then a follow-up run_filter re-screens exactly the reset row.
# ---------------------------------------------------------------------------

def test_followup_run_filter_reprocesses_only_reset_rows(paths, monkeypatch):
    from shared.schema import CANDIDATES_COLS

    monkeypatch.setattr(rf, "DATA_DIR", paths["data"])
    # No live LLM: rows the rule filter can't decide stay as-is.
    monkeypatch.setattr(rf, "_llm_classify", lambda title, abstract: None)

    cand_rows = [
        {"doi_r": "10.1/a", "title_r": "A", "abstract_r": ""},
        {"doi_r": "10.1/b", "title_r": "B", "abstract_r": ""},
        {"doi_r": "10.1/c", "title_r": "C", "abstract_r": "had one all along"},
    ]
    _write_csv(paths["candidates"], cand_rows, CANDIDATES_COLS)

    # Initial screening: all three written with whatever abstract they had.
    rf.run_filter()
    n_initial = len(pd.read_csv(paths["filtered"], dtype=str, encoding="utf-8-sig"))
    assert n_initial == 3

    # Backfill an abstract for 10.1/b in candidates.csv.
    cand_rows[1]["abstract_r"] = "a backfilled abstract for B"
    _write_csv(paths["candidates"], cand_rows, CANDIDATES_COLS)

    # Reset: only 10.1/b (empty-at-filter-time AND now backfilled) should drop.
    summary = rb.reset_backfilled(apply=True)
    assert summary["rows_dropped"] == 1

    after_reset = pd.read_csv(paths["filtered"], dtype=str, encoding="utf-8-sig").fillna("")
    assert set(after_reset["doi_r"]) == {"10.1/a", "10.1/c"}

    # Follow-up run: 10.1/b comes back through (now with abstract); a and c stay skipped.
    rf.run_filter()
    final = pd.read_csv(paths["filtered"], dtype=str, encoding="utf-8-sig").fillna("")
    assert len(final) == 3
    # 10.1/b present exactly once — no duplicate decision.
    assert list(final["doi_r"]).count("10.1/b") == 1
    reprocessed = final[final["doi_r"] == "10.1/b"].iloc[0]
    assert reprocessed["abstract_r"] == "a backfilled abstract for B"
