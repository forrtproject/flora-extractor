"""Tests for extract/sanity_check.py — the post-extraction quarantine pass."""
import pandas as pd

import extract.sanity_check as sc
from shared.schema import EXTRACTED_COLS


def _write(path, rows):
    df = pd.DataFrame(rows)
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    df[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")


def test_each_problem_row_moves_to_its_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [
        {"doi_r": "10.1/keep", "doi_o": "10.2/o", "year_r": "2020", "year_o": "2015",
         "outcome": "failure", "doi_o_verification": "verified", "openalex_id_r": "W0",
         "link_method": "llm_cited_candidates", "pair_id": "p0"},
        {"doi_r": "10.1/cbd", "doi_o": "10.2/o2", "outcome": "cannot_be_determined",
         "doi_o_verification": "verified", "openalex_id_r": "W1", "link_method": "llm_cited_candidates"},
        {"doi_r": "10.1/nar", "outcome": "not_a_replication", "openalex_id_r": "W2"},
        {"doi_r": "10.1/self", "doi_o": "10.1/self", "outcome": "success",
         "doi_o_verification": "verified", "openalex_id_r": "W3", "link_method": "llm_cited_candidates"},
        {"doi_r": "10.1/mis", "doi_o": "", "outcome": "success",
         "doi_o_verification": "mismatch", "openalex_id_r": "W4", "link_method": "llm_cited_candidates"},
        {"doi_r": "10.1/tp", "outcome": "cannot_be_determined",
         "link_method": "target_pending", "openalex_id_r": "W5"},
        {"doi_r": "10.7287/peerj.2068v0.1/reviews/1", "doi_o": "10.2/o6", "outcome": "success",
         "doi_o_verification": "verified", "openalex_id_r": "W6", "link_method": "llm_cited_candidates"},
    ])

    s = sc.run_sanity_check(ex, move=True, deep=False)

    assert s["moved"]["not_a_replication"] == 1
    assert s["moved"]["non_article"] == 1
    assert s["moved"]["self_link"] == 1
    assert s["moved"]["doi_mismatch"] == 1
    assert s["moved"]["target_pending"] == 1
    out = pd.read_csv(ex, dtype=str, keep_default_na=False)
    # kept: the clean row + the cannot_be_determined row (2)
    assert set(out["doi_r"]) == {"10.1/keep", "10.1/cbd"}
    assert s["cannot_be_determined_kept"] == 1

    def rows(name):
        return set(pd.read_csv(tmp_path / name, dtype=str, keep_default_na=False)["doi_r"])
    nar = rows("not_a_replication.csv")
    assert "10.1/nar" in nar
    assert "10.7287/peerj.2068v0.1/reviews/1" in nar  # non-article routed here too
    assert "10.1/self" in rows("unresolved_self_links.csv")
    assert "10.1/mis" in rows("unresolved_doi_mismatch.csv")
    assert "10.1/tp" in rows("target_pending.csv")


def test_cannot_be_determined_is_never_moved(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [{"doi_r": "10.1/a", "doi_o": "10.2/o", "outcome": "cannot_be_determined",
                 "doi_o_verification": "verified", "openalex_id_r": "W1"}])
    sc.run_sanity_check(ex, move=True)
    out = pd.read_csv(ex, dtype=str, keep_default_na=False)
    assert list(out["outcome"]) == ["cannot_be_determined"]
    assert not (tmp_path / "cannot_be_determined.csv").exists(), "must not create/move CBD"


def test_report_only_moves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [{"doi_r": "10.1/a", "outcome": "not_a_replication", "openalex_id_r": "W1"}])
    s = sc.run_sanity_check(ex, move=False)
    assert s["moved"]["not_a_replication"] == 1  # counted
    out = pd.read_csv(ex, dtype=str, keep_default_na=False)
    assert list(out["outcome"]) == ["not_a_replication"]  # but not moved


def test_deep_quarantines_fabricated_doi(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
    # 10.9/fake resolves nowhere (404), 10.2/real resolves (302).
    monkeypatch.setattr(sc, "_doi_is_registered", lambda d: "real" in d)
    ex = tmp_path / "extracted.csv"
    _write(ex, [
        {"doi_r": "10.1/f", "doi_o": "10.9/fake", "outcome": "success",
         "doi_o_verification": "no_metadata", "openalex_id_r": "W1", "link_method": "llm_fulltext"},
        {"doi_r": "10.1/r", "doi_o": "10.2/real", "outcome": "success",
         "doi_o_verification": "no_metadata", "openalex_id_r": "W2", "link_method": "llm_fulltext"},
    ])
    s = sc.run_sanity_check(ex, move=True, deep=True)
    assert s["moved"]["fabricated_doi_o"] == 1
    out = pd.read_csv(ex, dtype=str, keep_default_na=False)
    assert set(out["doi_r"]) == {"10.1/r"}, "only the unresolvable doi_o is quarantined"
    fab = pd.read_csv(tmp_path / "fabricated_original_doi.csv", dtype=str, keep_default_na=False)
    assert "10.1/f" in set(fab["doi_r"])


def test_deep_quarantines_non_study_work_types(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
    types = {"10.7910/dvn/deposit": "dataset",
             "10.7554/elife.12345.041": "peer-review",
             "10.1/study": "journal-article",
             "10.2/untyped": ""}
    monkeypatch.setattr(sc, "fetch_doi_metadata", lambda d: {"type": types.get(d, "")})
    rows = [{"doi_r": d, "doi_o": "10.9/o", "outcome": "success",
             "doi_o_verification": "verified", "openalex_id_r": f"W{i}",
             "link_method": "llm_references"} for i, d in enumerate(types)]

    ex = tmp_path / "extracted.csv"
    _write(ex, rows)
    assert sc.run_sanity_check(ex, move=True, deep=False)["moved"].get("non_article_type") is None
    assert len(pd.read_csv(ex, dtype=str, keep_default_na=False)) == 4

    _write(ex, rows)
    s = sc.run_sanity_check(ex, move=True, deep=True)
    assert s["moved"]["non_article_type"] == 2
    out = pd.read_csv(ex, dtype=str, keep_default_na=False)
    assert set(out["doi_r"]) == {"10.1/study", "10.2/untyped"}
    nar = set(pd.read_csv(tmp_path / "not_a_replication.csv", dtype=str,
                          keep_default_na=False)["doi_r"])
    assert nar == {"10.7910/dvn/deposit", "10.7554/elife.12345.041"}


def test_blank_doi_r_rows_do_not_collapse(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [
        {"doi_r": "", "title_r": "A", "outcome": "not_a_replication", "openalex_id_r": "W10"},
        {"doi_r": "", "title_r": "B", "outcome": "not_a_replication", "openalex_id_r": "W11"},
    ])
    s = sc.run_sanity_check(ex, move=True)
    assert s["moved"]["not_a_replication"] == 2
    moved = pd.read_csv(tmp_path / "not_a_replication.csv", dtype=str, keep_default_na=False)
    assert len(moved) == 2


def test_disagreement_beats_the_outcome_rule(tmp_path, monkeypatch):
    """audit B6: not_a_replication.csv is read as "both classifiers agreed this is
    not a replication". A row where they DISAGREED must never land there, whatever
    outcome was once coded against it."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [
        {"doi_r": "10.1/dis", "outcome": "not_a_replication", "openalex_id_r": "W1",
         "link_method": "screen_disagreement"},
        {"doi_r": "10.1/nar", "outcome": "not_a_replication", "openalex_id_r": "W2",
         "link_method": "not_a_replication"},
    ])

    s = sc.run_sanity_check(ex, move=True, deep=False)

    assert s["moved"]["screen_disagreement"] == 1
    assert s["moved"]["not_a_replication"] == 1
    dis = pd.read_csv(tmp_path / "screen_disagreement.csv", dtype=str, keep_default_na=False)
    nar = pd.read_csv(tmp_path / "not_a_replication.csv", dtype=str, keep_default_na=False)
    assert set(dis["doi_r"]) == {"10.1/dis"}
    assert set(nar["doi_r"]) == {"10.1/nar"}


def test_an_unresolved_row_routes_on_its_link_not_its_outcome(tmp_path, monkeypatch):
    """The abstract pass can answer not_a_replication on a row whose target was never
    resolved. Such a row is awaiting a target, not a settled finding, so it belongs in
    target_pending.csv — not_a_replication.csv is read as the pipeline's verdict."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [
        {"doi_r": "10.1/tp", "outcome": "not_a_replication", "openalex_id_r": "W1",
         "out_quote_source": "abstract", "link_method": "target_pending"},
        {"doi_r": "10.1/nar", "outcome": "not_a_replication", "openalex_id_r": "W2",
         "link_method": "not_a_replication"},
    ])

    s = sc.run_sanity_check(ex, move=True, deep=False)

    assert s["moved"]["target_pending"] == 1
    assert s["moved"]["not_a_replication"] == 1
    tp = pd.read_csv(tmp_path / "target_pending.csv", dtype=str, keep_default_na=False)
    nar = pd.read_csv(tmp_path / "not_a_replication.csv", dtype=str, keep_default_na=False)
    assert set(tp["doi_r"]) == {"10.1/tp"}
    assert set(nar["doi_r"]) == {"10.1/nar"}


def test_provisional_title_search_rows_are_set_aside(tmp_path, monkeypatch):
    """A title-search link is ~50% precise and its failure mode is invisible to
    doi_o_verification, so a verified-looking row must still leave extracted.csv
    for human confirmation rather than sit there looking resolved (audit D2)."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [
        {"doi_r": "10.1/keep", "doi_o": "10.2/o", "outcome": "failure",
         "doi_o_verification": "verified", "openalex_id_r": "W0",
         "link_method": "llm_cited_candidates"},
        {"doi_r": "10.1/prov", "doi_o": "10.2/landmark",
         "outcome": "cannot_be_determined", "doi_o_verification": "verified",
         "openalex_id_r": "W1", "link_method": "llm_title_search"},
    ])

    s = sc.run_sanity_check(ex, move=True, deep=False)

    assert s["moved"]["title_search_provisional"] == 1
    out = pd.read_csv(ex, dtype=str, keep_default_na=False)
    assert set(out["doi_r"]) == {"10.1/keep"}
    moved = pd.read_csv(tmp_path / "provisional_title_search.csv",
                        dtype=str, keep_default_na=False)
    assert set(moved["doi_r"]) == {"10.1/prov"}
