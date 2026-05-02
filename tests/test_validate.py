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
