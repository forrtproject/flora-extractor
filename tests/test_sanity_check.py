"""Tests for extract/sanity_check.py — the integrity REPORT over the exported CSV.

The pass moves nothing: `extract/export.py:partition` quarantines rows as it writes
them, through the same `classify_row`. What is tested here is therefore the rule
list — which bucket each problem row falls in, in which order — plus the two
`--deep` buckets, which need a network lookup and so can only be decided here.

Where a row physically LANDS is tested in tests/test_extract_export.py, against the
code that writes it.
"""
import pandas as pd

import extract.sanity_check as sc
from shared.schema import EXTRACTED_COLS


def _write(path, rows):
    df = pd.DataFrame(rows)
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    df[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")


def test_each_problem_row_is_flagged_for_its_bucket(tmp_path, monkeypatch):
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
        # A mismatch is a doi_o that points at the wrong paper, so the row HAS one:
        # without it the row is malformed and demoted to target_pending instead.
        {"doi_r": "10.1/mis", "doi_o": "10.2/o5", "outcome": "success",
         "doi_o_verification": "mismatch", "openalex_id_r": "W4", "link_method": "llm_cited_candidates"},
        {"doi_r": "10.1/tp", "outcome": "cannot_be_determined",
         "link_method": "target_pending", "openalex_id_r": "W5"},
        {"doi_r": "10.7287/peerj.2068v0.1/reviews/1", "doi_o": "10.2/o6", "outcome": "success",
         "doi_o_verification": "verified", "openalex_id_r": "W6", "link_method": "llm_cited_candidates"},
    ])

    s = sc.run_sanity_check(ex, deep=False)

    assert s["flagged"]["not_a_replication"] == 1
    assert s["flagged"]["non_article"] == 1   # the peer-review DOI
    assert s["flagged"]["self_link"] == 1
    assert s["flagged"]["doi_mismatch"] == 1
    assert s["flagged"]["target_pending"] == 1
    # Two rows belong in this file: the clean one and the cannot_be_determined one.
    assert s["rows_clean"] == 2
    assert s["cannot_be_determined_kept"] == 1
    # And the report wrote nothing.
    assert set(p.name for p in tmp_path.iterdir()) == {"extracted.csv"}


def test_cannot_be_determined_is_never_a_set_aside(tmp_path, monkeypatch):
    """A linked original with an undecidable outcome is a real record awaiting full
    text, so it belongs in extracted.csv."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [{"doi_r": "10.1/a", "doi_o": "10.2/o", "outcome": "cannot_be_determined",
                 "doi_o_verification": "verified", "openalex_id_r": "W1"}])
    s = sc.run_sanity_check(ex)
    assert not any(s["flagged"].values())
    assert s["cannot_be_determined_kept"] == 1


def test_the_report_writes_nothing(tmp_path, monkeypatch):
    """The export is the only thing that partitions rows; this pass only counts."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [{"doi_r": "10.1/a", "outcome": "not_a_replication", "openalex_id_r": "W1"}])
    before = ex.read_bytes()
    s = sc.run_sanity_check(ex)
    assert s["flagged"]["not_a_replication"] == 1
    assert ex.read_bytes() == before
    assert not (tmp_path / "not_a_replication.csv").exists()


def test_deep_flags_a_fabricated_doi(tmp_path, monkeypatch):
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
    s = sc.run_sanity_check(ex, deep=True)
    assert s["flagged"]["fabricated_doi_o"] == 1, "only the unresolvable doi_o"
    assert s["rows_clean"] == 1


def test_deep_flags_non_study_work_types(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
    types = {"10.5281/zenodo.99999": "dataset",
             "10.7554/elife.12345.041": "peer-review",
             "10.1/study": "journal-article",
             "10.2/untyped": ""}
    monkeypatch.setattr(sc, "fetch_doi_metadata", lambda d: {"type": types.get(d, "")})
    rows = [{"doi_r": d, "doi_o": "10.9/o", "outcome": "success",
             "doi_o_verification": "verified", "openalex_id_r": f"W{i}",
             "link_method": "llm_references"} for i, d in enumerate(types)]

    ex = tmp_path / "extracted.csv"
    _write(ex, rows)
    # Without --deep the work type is never looked up, so the bucket does not exist.
    assert sc.run_sanity_check(ex, deep=False)["flagged"].get("non_article_type") is None

    s = sc.run_sanity_check(ex, deep=True)
    assert s["flagged"]["non_article_type"] == 2
    assert s["rows_clean"] == 2


def test_blank_doi_r_rows_are_counted_separately(tmp_path, monkeypatch):
    """Two rows sharing "no identifier" are two papers, not one."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [
        {"doi_r": "", "title_r": "A", "outcome": "not_a_replication", "openalex_id_r": "W10"},
        {"doi_r": "", "title_r": "B", "outcome": "not_a_replication", "openalex_id_r": "W11"},
    ])
    s = sc.run_sanity_check(ex)
    assert s["flagged"]["not_a_replication"] == 2


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

    s = sc.run_sanity_check(ex, deep=False)

    assert s["flagged"]["screen_disagreement"] == 1
    assert s["flagged"]["not_a_replication"] == 1


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

    s = sc.run_sanity_check(ex, deep=False)

    assert s["flagged"]["target_pending"] == 1
    assert s["flagged"]["not_a_replication"] == 1


def test_pooled_search_rows_belong_in_the_file(tmp_path, monkeypatch):
    """A pooled-search link imports since the 2026-08-08 promotion (98-99% across
    two cross-vendor triages); what stays out is a link the pipeline itself cannot
    stand behind, like a disputed keyed record."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [
        {"doi_r": "10.1/keep", "doi_o": "10.2/landmark", "outcome": "failure",
         "doi_o_verification": "verified", "openalex_id_r": "W1",
         "link_method": "llm_title_search"},
        {"doi_r": "10.1/disp", "doi_o": "10.2/other", "outcome": "pending",
         "doi_o_verification": "verified", "openalex_id_r": "W2",
         "link_method": "keyed_link_disputed"},
    ])

    s = sc.run_sanity_check(ex, deep=False)

    assert s["flagged"]["keyed_link_disputed"] == 1
    assert s["rows_clean"] == 1


def test_a_malformed_row_is_reported_as_what_it_is(tmp_path, monkeypatch):
    """A resolved link_method with no doi_o claims a target it cannot name. It is
    demoted before the rules, exactly as `export.partition` demotes it, so it counts
    as target_pending rather than as a resolved row."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    _write(ex, [{"doi_r": "10.1/bad", "doi_o": "", "outcome": "success",
                 "openalex_id_r": "W1", "link_method": "llm_fulltext"}])
    s = sc.run_sanity_check(ex, deep=False)
    assert s["flagged"]["target_pending"] == 1
    assert s["rows_clean"] == 0
