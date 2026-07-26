"""Tests for extract/sanity_check.py — the post-extraction integrity pass."""
import pandas as pd

import extract.sanity_check as sc
from shared.schema import EXTRACTED_COLS


def _write(path, rows):
    df = pd.DataFrame(rows)
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    df[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")


def test_moves_not_a_replication_and_flags_the_rest(tmp_path, monkeypatch):
    ex = tmp_path / "extracted.csv"
    nar = tmp_path / "not_a_replication.csv"
    monkeypatch.setattr(sc, "NOT_A_REP_PATH", nar)

    _write(ex, [
        # normal, clean row
        {"doi_r": "10.1/r1", "doi_o": "10.2/o1", "year_r": "2020", "year_o": "2015",
         "outcome": "failure", "doi_o_verification": "verified", "openalex_id_r": "W1",
         "pair_id": "p1"},
        # not_a_replication → must move out
        {"doi_r": "10.1/r2", "doi_o": "", "outcome": "not_a_replication",
         "openalex_id_r": "W2", "pair_id": "p2"},
        # self-link (doi_o == doi_r) → flagged
        {"doi_r": "10.1/r3", "doi_o": "10.1/r3", "year_r": "2019", "year_o": "2010",
         "outcome": "success", "doi_o_verification": "verified", "openalex_id_r": "W3",
         "pair_id": "p3"},
        # chronology + unverified (hallucinated-DOI shape) → flagged
        {"doi_r": "10.1/r4", "doi_o": "10.9/fake", "year_r": "2014", "year_o": "2021",
         "outcome": "success", "doi_o_verification": "no_metadata", "openalex_id_r": "W4",
         "pair_id": "p4"},
    ])

    s = sc.run_sanity_check(ex, move=True)

    assert s["not_a_replication_moved"] == 1
    assert s["self_links"] == 1
    assert s["chronology_errors"] == 1
    assert s["unverified_doi_o"] == 1

    out = pd.read_csv(ex, dtype=str, keep_default_na=False)
    assert "not_a_replication" not in set(out["outcome"]), "moved row must leave extracted.csv"
    assert len(out) == 3

    moved = pd.read_csv(nar, dtype=str, keep_default_na=False)
    assert (moved["outcome"] == "not_a_replication").all()
    assert "10.1/r2" in set(moved["doi_r"])


def test_no_move_reports_without_mutating(tmp_path, monkeypatch):
    ex = tmp_path / "extracted.csv"
    monkeypatch.setattr(sc, "NOT_A_REP_PATH", tmp_path / "nar.csv")
    _write(ex, [{"doi_r": "10.1/a", "outcome": "not_a_replication", "openalex_id_r": "W1"}])

    s = sc.run_sanity_check(ex, move=False)

    assert s["not_a_replication_moved"] == 0
    out = pd.read_csv(ex, dtype=str, keep_default_na=False)
    assert list(out["outcome"]) == ["not_a_replication"], "row must remain when move=False"


def test_blank_doi_r_rows_do_not_collapse_on_move(tmp_path, monkeypatch):
    """Rows with blank doi_r all share pair_id md5('|'); dedup must key on
    openalex_id_r so distinct papers are not merged into one."""
    ex = tmp_path / "extracted.csv"
    nar = tmp_path / "not_a_replication.csv"
    monkeypatch.setattr(sc, "NOT_A_REP_PATH", nar)
    _write(ex, [
        {"doi_r": "", "title_r": "Paper A", "outcome": "not_a_replication", "openalex_id_r": "W10"},
        {"doi_r": "", "title_r": "Paper B", "outcome": "not_a_replication", "openalex_id_r": "W11"},
    ])

    s = sc.run_sanity_check(ex, move=True)
    assert s["not_a_replication_moved"] == 2
    moved = pd.read_csv(nar, dtype=str, keep_default_na=False)
    assert len(moved) == 2, "two distinct DOI-less papers must both survive the move"
