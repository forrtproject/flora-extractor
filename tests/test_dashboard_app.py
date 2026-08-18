"""One seam: the page renders and every band API is registered."""
import pytest

from validate.app import create_app


@pytest.fixture
def client():
    return create_app({"TESTING": True}).test_client()


def test_dashboard_page_renders(client):
    assert client.get("/dashboard").status_code == 200


@pytest.mark.parametrize("route", [
    "/api/dashboard/flow", "/api/dashboard/analysis",
    "/api/dashboard/token-usage", "/api/dashboard/concerns",
])
def test_band_apis_are_registered(client, route):
    assert client.get(route).status_code == 200


def test_the_retired_csv_stats_endpoint_is_gone(client):
    """Its work is split across api_flow and api_analysis, each with provenance."""
    assert client.get("/api/dashboard/csv-stats").status_code == 404
