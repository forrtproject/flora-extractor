"""Tests for validate/ monitoring app routes.

Stale tests for the old SQLite voting infrastructure (import_csv, models, vote,
export routes) were removed when that infrastructure was stripped. The pipeline
routes are tested below; Supabase client tests are in test_supabase_client.py.
"""
from pathlib import Path

import pandas as pd
import pytest
import json
from unittest.mock import patch


def test_placeholder():
    assert True


def test_set_name_stores_session(app):
    """POST /set-name stores reviewer_id in session and redirects."""
    with app.test_client() as c:
        rv = c.post("/set-name", data={"name": "Rohan"}, follow_redirects=False)
        assert rv.status_code == 302
        with c.session_transaction() as sess:
            assert sess["reviewer_id"] == "Rohan"


def test_dashboard_accessible_without_name(app):
    """Dashboard is reachable without setting a reviewer name."""
    with app.test_client() as c:
        rv = c.get("/dashboard")
        assert rv.status_code == 200


# ── pipeline route tests ───────────────────────────────────────────────────────


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
    assert data["rows"][0]["resolved"] is True


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
    assert data["all_candidates_json"] == []
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
