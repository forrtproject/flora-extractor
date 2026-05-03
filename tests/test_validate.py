from pathlib import Path

import pandas as pd
import pytest


def test_placeholder():
    assert True


# ── import_csv tests ──────────────────────────────────────────────────────────

def test_import_csv_maps_columns(app, tmp_path):
    """import_csv maps study_r→title_r, resolved_doi_o→doi_o, etc."""
    from validate.import_csv import import_csv
    from validate.models import Replication

    csv_file = tmp_path / "test.csv"
    pd.DataFrame([{
        "doi_r": "https://doi.org/10.1234/test",
        "study_r": "A replication study",
        "resolved_doi_o": "https://doi.org/10.9999/orig",
        "resolved_title_o": "The original study",
        "user_val_status": "",
        "flora_validation_status": "confirmed",
    }]).to_csv(csv_file, index=False)

    with app.app_context():
        count = import_csv(csv_path=csv_file)
        assert count == 1
        rep = Replication.query.first()
        assert rep.doi_r == "10.1234/test"
        assert rep.title_r == "A replication study"
        assert rep.doi_o == "10.9999/orig"
        assert rep.title_o == "The original study"
        assert rep.validation_status == "pending"
        assert rep.flora_status == "confirmed"
        assert rep.original_rank == 1
        assert rep.n_originals == 1


def test_import_csv_idempotent(app, tmp_path):
    """Re-running import_csv updates rows; row count stays the same."""
    from validate.import_csv import import_csv
    from validate.models import Replication

    csv_file = tmp_path / "test.csv"
    pd.DataFrame([{
        "doi_r": "10.1234/test",
        "study_r": "Original title",
        "resolved_doi_o": "10.9999/orig",
        "resolved_title_o": "Original study",
        "user_val_status": "",
        "flora_validation_status": "",
    }]).to_csv(csv_file, index=False)

    with app.app_context():
        import_csv(csv_path=csv_file)
        assert Replication.query.count() == 1

        pd.DataFrame([{
            "doi_r": "10.1234/test",
            "study_r": "Updated title",
            "resolved_doi_o": "10.9999/orig",
            "resolved_title_o": "Original study",
            "user_val_status": "",
            "flora_validation_status": "",
        }]).to_csv(csv_file, index=False)

        import_csv(csv_path=csv_file)
        assert Replication.query.count() == 1
        rep = Replication.query.first()
        assert rep.title_r == "Updated title"


# ── vote tests ────────────────────────────────────────────────────────────────

def _seed_replication(db, **kwargs):
    from validate.models import Replication
    defaults = dict(doi_r="10.1/test", original_rank=1, n_originals=1,
                    title_r="Test study", title_o="Original", doi_o="10.2/orig")
    defaults.update(kwargs)
    rep = Replication(**defaults)
    db.session.add(rep)
    db.session.commit()
    return rep


def test_vote_confirm_updates_status(client, db, app):
    """Two confirm votes → validation_status = confirmed."""
    with app.app_context():
        rep = _seed_replication(db)
        rep_id = rep.id

    rv = client.post("/vote", json={"replication_id": rep_id, "vote": "confirm", "comment": ""})
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["validation_status"] != "confirmed"

    with client.session_transaction() as sess:
        sess["reviewer_id"] = "second_reviewer"

    rv = client.post("/vote", json={"replication_id": rep_id, "vote": "confirm", "comment": ""})
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["validation_status"] == "confirmed"
    assert data["confirm_votes"] == 2


def test_vote_needs_review_overrides(client, db, app):
    """Any needs_review vote → status = needs_review regardless of other votes."""
    with app.app_context():
        rep = _seed_replication(db, doi_r="10.1/nr")
        rep_id = rep.id

    client.post("/vote", json={"replication_id": rep_id, "vote": "confirm", "comment": ""})

    with client.session_transaction() as sess:
        sess["reviewer_id"] = "reviewer2"

    rv = client.post("/vote", json={"replication_id": rep_id, "vote": "needs_review", "comment": "Check this"})
    data = rv.get_json()
    assert data["validation_status"] == "needs_review"


def test_vote_duplicate_updates_existing(client, db, app):
    """Same reviewer voting twice updates their vote, not creates a new row."""
    with app.app_context():
        rep = _seed_replication(db, doi_r="10.1/dup")
        rep_id = rep.id

    client.post("/vote", json={"replication_id": rep_id, "vote": "confirm", "comment": ""})
    rv = client.post("/vote", json={"replication_id": rep_id, "vote": "reject", "comment": "Changed mind"})
    data = rv.get_json()
    assert data["confirm_votes"] == 0
    assert data["reject_votes"] == 1


def test_set_name_stores_session(app):
    """POST /set-name stores reviewer_id in session."""
    with app.test_client() as c:
        rv = c.post("/set-name", data={"name": "Rohan"}, follow_redirects=False)
        assert rv.status_code == 302
        with c.session_transaction() as sess:
            assert sess["reviewer_id"] == "Rohan"


# ── dashboard / export tests ──────────────────────────────────────────────────

def test_dashboard_stats_structure(client, db, app):
    """GET /api/dashboard/stats returns required keys."""
    with app.app_context():
        _seed_replication(db, doi_r="10.1/a", validation_status="confirmed",
                          vote_count=2, confirm_votes=2)
        _seed_replication(db, doi_r="10.1/b", original_rank=1, validation_status="pending",
                          vote_count=0, confirm_votes=0)

    rv = client.get("/api/dashboard/stats")
    assert rv.status_code == 200
    d = rv.get_json()
    for key in ("total", "confirmed", "rejected", "needs_review",
                "pending", "in_progress", "total_votes", "avg_votes", "reviewer_count"):
        assert key in d, f"missing key: {key}"
    assert d["total"] == 2
    assert d["confirmed"] == 1


def test_export_csv(client, db, app):
    """POST /api/export/download?format=csv returns CSV with confirmed rows."""
    with app.app_context():
        _seed_replication(db, doi_r="10.1/export", validation_status="confirmed")

    rv = client.post("/api/export/download", json={"format": "csv"})
    assert rv.status_code == 200
    assert b"10.1/export" in rv.data


def test_export_minimal(client, db, app):
    """Minimal CSV export has exactly 5 columns."""
    with app.app_context():
        _seed_replication(db, doi_r="10.1/min", validation_status="confirmed")

    rv = client.post("/api/export/download", json={"format": "minimal"})
    assert rv.status_code == 200
    lines = rv.data.decode("utf-8-sig").strip().split("\n")
    header_cols = lines[0].split(",")
    assert len(header_cols) == 5


# ── pipeline route tests ───────────────────────────────────────────────────────

import json
from unittest.mock import patch


def _pipeline_df(overrides=None):
    """Minimal flora_all.csv-shaped DataFrame for pipeline route tests."""
    row = {
        "doi_r": "10.1037/test001",
        "study_r": "A test replication study",
        "year_r": "2024",
        "match_status": "multiple_matches",
        "resolution_method": "llm_gemini",
        "outcome": "successful",
        "resolved_title_o": "The original study",
        "resolved_doi_o": "10.1037/orig001",
        "all_candidates_json": "",
        "grobid_refs_json": "",
    }
    # Fill every other expected column with ""
    extras = [
        "flora_validation_status", "user_val_status", "abstract_r",
        "resolved_year_o", "resolved_author_o", "resolution_score",
        "llm_source", "llm_confidence", "llm_evidence", "llm_reasoning",
        "llm_prompt", "llm_error", "flora_ref_o", "flora_doi_o", "flora_study_o",
        "flora_outcome", "flora_outcome_quote", "flora_out_quote_source",
        "flora_ref_r", "flora_url_r", "flora_abstract_r", "flora_prep_notes",
        "n_candidates", "openalex_id_r", "pdf_url", "pdf_source", "pdf_serve_url",
        "pdf_ok", "pdf_url_tried", "grobid_status", "n_grobid_refs",
        "grobid_abstract", "grobid_intro", "grobid_methods", "ref_r", "ref_o",
        "llm_study_type", "match_source", "outcome_quote", "out_quote_source",
        "outcome_confidence", "outcome_original_confirmed", "outcome_original_study",
        "outcome_reasoning", "quote_validated", "quote_similarity",
        "pathway_source", "abstract_source", "author_year_pattern_r",
        "multi_target", "fred_match_type", "readiness_level",
    ]
    for k in extras:
        row.setdefault(k, "")
    if overrides:
        row.update(overrides)
    return pd.DataFrame([row])


def test_pipeline_list_returns_all_rows(client):
    with patch("validate.routes.pipeline._load_csv", return_value=_pipeline_df()):
        rv = client.get("/api/pipeline/list")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["total"] == 1
    assert data["rows"][0]["doi_r"] == "10.1037/test001"
    assert data["rows"][0]["resolved"] is True  # resolved_doi_o is non-empty


def test_pipeline_list_filters_by_outcome(client):
    with patch("validate.routes.pipeline._load_csv", return_value=_pipeline_df()):
        rv = client.get("/api/pipeline/list?outcome=failed")
    assert rv.get_json()["total"] == 0


def test_pipeline_list_filters_by_method(client):
    with patch("validate.routes.pipeline._load_csv", return_value=_pipeline_df()):
        rv = client.get("/api/pipeline/list?method=llm_openai")
    assert rv.get_json()["total"] == 0


def test_pipeline_list_search_match(client):
    with patch("validate.routes.pipeline._load_csv", return_value=_pipeline_df()):
        rv = client.get("/api/pipeline/list?q=test+replication")
    assert rv.get_json()["total"] == 1


def test_pipeline_list_search_no_match(client):
    with patch("validate.routes.pipeline._load_csv", return_value=_pipeline_df()):
        rv = client.get("/api/pipeline/list?q=nonexistent")
    assert rv.get_json()["total"] == 0


def test_pipeline_detail_returns_full_row(client):
    with patch("validate.routes.pipeline._load_csv", return_value=_pipeline_df()):
        rv = client.get("/api/pipeline/detail?doi=10.1037/test001")
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["study_r"] == "A test replication study"
    assert data["all_candidates_json"] == []   # empty string parsed → []
    assert data["grobid_refs_json"] == []


def test_pipeline_detail_parses_candidates_json(client):
    cands = [{"doi": "10.test/1", "title": "Orig", "year": 1981,
              "first_author": "Smith", "match_year_exact": True, "cited_pattern": "Smith (1981)"}]
    df = _pipeline_df({"all_candidates_json": json.dumps(cands)})
    with patch("validate.routes.pipeline._load_csv", return_value=df):
        rv = client.get("/api/pipeline/detail?doi=10.1037/test001")
    assert rv.get_json()["all_candidates_json"] == cands


def test_pipeline_detail_not_found(client):
    with patch("validate.routes.pipeline._load_csv", return_value=_pipeline_df()):
        rv = client.get("/api/pipeline/detail?doi=10.9999/nope")
    assert rv.status_code == 404


def test_pipeline_list_missing_csv(client):
    with patch("validate.routes.pipeline._load_csv", return_value=None):
        rv = client.get("/api/pipeline/list")
    assert rv.status_code == 404


def test_pipeline_detail_missing_doi(client):
    rv = client.get("/api/pipeline/detail")
    assert rv.status_code == 400


# ── import_csv new-column mapping test ────────────────────────────────────────

def test_import_csv_maps_new_columns(app, tmp_path):
    """import_csv correctly maps the new flora_all.csv columns."""
    from validate.import_csv import import_csv
    from validate.models import Replication

    csv_file = tmp_path / "flora_all.csv"
    pd.DataFrame([{
        "doi_r": "10.1234/newcols",
        "study_r": "Replication of X",
        "abstract_r": "Abstract text here.",
        "resolved_doi_o": "10.9999/orig",
        "resolved_title_o": "The Original Study",
        "resolved_year_o": "1998",
        "resolved_author_o": "Smith, J.",
        "resolution_method": "llm_gemini",
        "llm_evidence": "cites Smith (1998)",
        "llm_confidence": "high",
        "outcome": "successful",
        "outcome_quote": "replication succeeded with d=0.45",
        "outcome_confidence": "high",
        "user_val_status": "",
        "flora_validation_status": "confirmed",
    }]).to_csv(csv_file, index=False)

    with app.app_context():
        import_csv(csv_path=csv_file)
        rep = Replication.query.filter_by(doi_r="10.1234/newcols").first()
        assert rep is not None
        assert rep.abstract_r == "Abstract text here."
        assert rep.link_method == "llm_gemini"
        assert rep.link_evidence == "cites Smith (1998)"
        assert rep.link_confidence == "high"
        assert rep.outcome == "successful"
        assert rep.outcome_phrase == "replication succeeded with d=0.45"
        assert rep.outcome_confidence == "high"
        assert rep.year_o == 1998
        assert rep.authors_o == "Smith, J."
