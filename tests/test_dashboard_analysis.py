"""One test per seam: outcome vocabularies stay apart; token spend splits by provider."""
from unittest.mock import patch

import pandas as pd
import pytest

from validate.app import create_app


@pytest.fixture
def client():
    return create_app({"TESTING": True}).test_client()


_LIVE = {"source": "extracted.csv", "state": "live", "release_id": None,
         "as_of": "2026-08-18T09:00:00", "machine": None, "reason": None}


def test_reproduction_outcomes_are_not_summed_into_the_replication_bars(client):
    """They are different vocabularies; one distribution over both would be invented."""
    df = pd.DataFrame({
        "type":    ["replication", "replication", "reproduction"],
        "outcome": ["successful", "failed", "computationally reproducible, robust"],
        "doi_r":   ["10.1/a", "10.1/b", "10.1/c"],
        "link_method": ["llm_references"] * 3,
        "link_confidence": ["high"] * 3,
        "doi_o_verification": ["verified"] * 3,
    })
    with patch("validate.routes.api_analysis.sources.extracted_csv", return_value=(df, _LIVE)):
        payload = client.get("/api/dashboard/analysis").get_json()

    assert payload["by_outcome_replication"] == {"successful": 1, "failed": 1}
    assert payload["by_outcome_reproduction"] == {"computationally reproducible, robust": 1}


def test_analysis_absent_csv_reports_its_reason(client):
    absent = {"source": "extracted.csv", "state": "absent", "release_id": None,
              "as_of": None, "machine": None,
              "reason": "not rendered yet — `python -m extract.export --release <id>`"}
    with patch("validate.routes.api_analysis.sources.extracted_csv", return_value=(None, absent)):
        payload = client.get("/api/dashboard/analysis").get_json()

    assert payload["rows"] == 0
    assert "extract.export" in payload["provenance"]["reason"]


def test_token_usage_keeps_provider_and_model_apart_with_in_out_split(client):
    """A model id does not name its provider: gpt via OpenRouter is not OpenAI spend."""
    record = {"2026-08-17": {"openai": {"gpt-5.6-luna": {"in": 100, "out": 20}},
                             "openrouter": {"deepseek/deepseek-v4-flash": {"in": 5, "out": 1}}}}
    prov = {"source": "token_usage.json", "state": "live", "release_id": None,
            "as_of": None, "machine": None, "reason": None}
    with patch("validate.routes.api_analysis.sources.token_usage_record",
               return_value=(record, prov)):
        payload = client.get("/api/dashboard/token-usage").get_json()

    top = payload["rows"][0]
    assert top["provider"] == "openai" and top["model"] == "gpt-5.6-luna"
    assert top["in"] == 100 and top["out"] == 20 and top["total"] == 120
    assert payload["total"] == 126
    assert payload["days"][0]["day"] == "2026-08-17"


def test_token_usage_sums_one_model_across_days(client):
    record = {"2026-08-16": {"openai": {"gpt-5.6-luna": {"in": 10, "out": 2}}},
              "2026-08-17": {"openai": {"gpt-5.6-luna": {"in": 5,  "out": 3}}}}
    prov = {"source": "token_usage.json", "state": "live", "release_id": None,
            "as_of": None, "machine": None, "reason": None}
    with patch("validate.routes.api_analysis.sources.token_usage_record",
               return_value=(record, prov)):
        payload = client.get("/api/dashboard/token-usage").get_json()

    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["in"] == 15 and payload["rows"][0]["out"] == 5
    assert [d["day"] for d in payload["days"]] == ["2026-08-17", "2026-08-16"]


def test_reproduction_axes_are_a_grid_not_a_flat_list(client):
    """The joined outcome hides which axis failed; the grid is the honest shape."""
    df = pd.DataFrame({
        "type": ["reproduction"] * 3 + ["replication"],
        "outcome_computation": ["computationally reproducible", "computational issues",
                                "computationally reproducible", ""],
        "outcome_robustness": ["robust", "robustness challenges", "robust", ""],
        "outcome": ["x", "y", "x", "successful"],
    })
    with patch("validate.routes.api_analysis.sources.extracted_csv", return_value=(df, _LIVE)):
        ax = client.get("/api/dashboard/analysis").get_json()["repro_axes"]

    assert ax["total"] == 3
    row = ax["computation"].index("computationally reproducible")
    col = ax["robustness"].index("robust")
    assert ax["rows"][row][col] == 2          # the two that share both axes
    assert "" not in ax["computation"]        # the replication row is not in the grid


def test_a_count_can_be_resolved_to_the_rows_behind_it(client):
    """An api_error count that cannot name its rows is not actionable."""
    df = pd.DataFrame({
        "doi_r": ["10.1/a", "10.1/b", "10.1/c"],
        "doi_o_verification": ["verified", "api_error", "verified"],
        "title_r": ["A", "B", "C"],
    })
    with patch("validate.routes.api_analysis.sources.extracted_csv", return_value=(df, _LIVE)):
        d = client.get(
            "/api/dashboard/rows?field=doi_o_verification&value=api_error").get_json()

    assert d["total"] == 1
    assert d["rows"][0]["doi_r"] == "10.1/b"


def test_an_unknown_column_is_refused_rather_than_answered_empty(client):
    """An empty result and 'no such column' must not look the same."""
    df = pd.DataFrame({"doi_r": ["10.1/a"]})
    with patch("validate.routes.api_analysis.sources.extracted_csv", return_value=(df, _LIVE)):
        res = client.get("/api/dashboard/rows?field=nope&value=x")

    assert res.status_code == 400
    assert "nope" in res.get_json()["error"]
