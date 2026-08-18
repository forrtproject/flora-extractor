"""One seam: the Supabase routes keep their paths and payloads after the move."""
from unittest.mock import patch

import pytest

from validate.app import create_app


@pytest.fixture
def client():
    return create_app({"TESTING": True}).test_client()


def test_supabase_stats_route_survives_the_move(client):
    with patch("validate.routes.api_validation.supa.get_validation_stats",
               return_value={"validated": 42}):
        payload = client.get("/api/dashboard/supabase-stats").get_json()
    assert payload["validated"] == 42


def test_drilldown_passes_its_query_arguments_through(client):
    with patch("validate.routes.api_validation.supa.get_drilldown_page",
               return_value={"rows": []}) as fake:
        client.get("/api/dashboard/supabase-drilldown"
                   "?page=3&outcome_filter=failed&check_filter=type")
    fake.assert_called_once_with(3, "failed", "type")


def test_stats_report_records_with_all_three_votes_not_just_filled_slots():
    """`total_judgements` counts slots: 1,055 of them across far fewer records.

    Every record has three slots (two humans, the LLM), so the slot count reads as
    far more work than has been done. The per-record answer is what a reader wants.
    """
    from validate.app import create_app

    stats = {"total": 1829, "total_judgements": 1055, "validated": 254}
    analytics = {"slots_filled": {0: 1241, 1: 119, 2: 139, 3: 330}, "both_humans": 469}
    with patch("validate.routes.api_validation.supa.get_validation_stats",
               return_value=stats), \
         patch("validate.routes.api_validation.supa.get_validation_analytics",
               return_value=analytics):
        payload = create_app({"TESTING": True}).test_client() \
            .get("/api/dashboard/supabase-stats").get_json()

    assert payload["records_fully_voted"] == 330
    assert payload["records_both_humans"] == 469
    assert payload["records_by_votes"]["0"] == 1241
    assert payload["total_judgements"] == 1055      # kept, but no longer the headline


def test_stats_survive_analytics_being_unavailable():
    """Supabase analytics can error independently; the KPIs must still render."""
    from validate.app import create_app

    with patch("validate.routes.api_validation.supa.get_validation_stats",
               return_value={"total": 5}), \
         patch("validate.routes.api_validation.supa.get_validation_analytics",
               return_value={"error": "supabase_not_configured"}):
        payload = create_app({"TESTING": True}).test_client() \
            .get("/api/dashboard/supabase-stats").get_json()

    assert payload["total"] == 5
    assert "records_fully_voted" not in payload
