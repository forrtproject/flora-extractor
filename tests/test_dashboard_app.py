"""One seam: the page renders and every band API is registered."""
import subprocess
import sys

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


def test_the_screen_panel_does_not_import_the_llm_client():
    """The dashboard's deployment does not install the pipeline's dependencies, so the
    panel reads the decision voter's cut-offs from config."""
    code = ("import sys; from validate.routes.api_stage2 import _screen; "
            "assert _screen()['decision']['confident_at'] == 0.9; "
            "assert 'shared.llm_client' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True)
