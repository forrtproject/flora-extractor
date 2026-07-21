"""Tests for validate/ monitoring app routes.

Stale tests for the old SQLite voting infrastructure (import_csv, models, vote,
export routes) were removed when that infrastructure was stripped. Stale tests
for validate/routes/pipeline.py were removed when that module was deleted (it
was never registered as a blueprint in validate/app.py's read-only redesign,
so these routes had been 404ing since 2026-06-09). Supabase client tests are
in test_supabase_client.py.
"""


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


# ── New dashboard/check route tests ───────────────────────────────────────────

def test_check_route_accessible(client):
    """New /check page must exist."""
    rv = client.get("/check")
    assert rv.status_code == 200

def test_dashboard_still_works(client):
    rv = client.get("/dashboard")
    assert rv.status_code == 200
