"""One test per seam: each concern rule fires, and clears when the cause is gone."""
from unittest.mock import patch

import pandas as pd
import pytest

from validate.app import create_app


@pytest.fixture
def client():
    return create_app({"TESTING": True}).test_client()


_CLEAN = {"rows": 10, "rows_clean": 10, "flagged": {}, "chronology_errors": 0,
          "cannot_be_determined_kept": 0, "blank_doi_r": 0,
          "duplicate_pair_ids": 0, "unregistered_doi_o": 0}

_NO_STORE = (None, {"source": "routing store", "state": "absent", "release_id": None,
                    "as_of": None, "machine": None, "reason": "no store"})
_NO_STATS = ({}, {"source": "stats.json", "state": "absent", "release_id": None,
                  "as_of": None, "machine": None, "reason": "none"})
_NO_TOKENS = ({}, {"source": "token_usage.json", "state": "absent", "release_id": None,
                   "as_of": None, "machine": None, "reason": "none"})
_NO_CSV = (None, {"source": "extracted.csv", "state": "absent", "release_id": None,
                  "as_of": None, "machine": None, "reason": "none"})


def _ids(payload):
    return {c["id"]: c for c in payload["concerns"]}


def test_api_error_rows_fire_with_the_redo_command(client):
    summary = {**_CLEAN, "flagged": {"api_error": 2}}
    with patch("validate.routes.api_concerns.run_sanity_check", return_value=summary), \
         patch("validate.routes.api_concerns.sources.stats_json", return_value=_NO_STATS), \
         patch("validate.routes.api_concerns.sources.filtered_stats", return_value=_NO_STORE), \
         patch("validate.routes.api_concerns.sources.token_usage_record", return_value=_NO_TOKENS),          patch("validate.routes.api_concerns.sources.extracted_csv", return_value=_NO_CSV):
        payload = client.get("/api/dashboard/concerns").get_json()

    concern = _ids(payload)["api_error"]
    assert concern["count"] == 2
    assert "--redo" in concern["command"]


def test_clean_run_reports_no_concerns(client):
    with patch("validate.routes.api_concerns.run_sanity_check", return_value=_CLEAN), \
         patch("validate.routes.api_concerns.sources.stats_json", return_value=_NO_STATS), \
         patch("validate.routes.api_concerns.sources.filtered_stats", return_value=_NO_STORE), \
         patch("validate.routes.api_concerns.sources.token_usage_record", return_value=_NO_TOKENS),          patch("validate.routes.api_concerns.sources.extracted_csv", return_value=_NO_CSV):
        payload = client.get("/api/dashboard/concerns").get_json()

    assert [c for c in payload["concerns"] if c["count"]] == []


def test_stale_stats_json_release_raises_a_provenance_concern(client):
    """Regression test for the bug that motivated the redesign."""
    stats = ({}, {"source": "stats.json", "state": "cached",
                  "release_id": "f7e4667b6c46", "machine": "lukaswallrich",
                  "as_of": "2026-08-16T21:06:14", "reason": None})
    store = (object(), {"source": "routing store", "state": "live",
                        "release_id": "16d370746b45", "as_of": None,
                        "machine": None, "reason": None})
    with patch("validate.routes.api_concerns.run_sanity_check", return_value=_CLEAN), \
         patch("validate.routes.api_concerns.sources.stats_json", return_value=stats), \
         patch("validate.routes.api_concerns.sources.filtered_stats", return_value=store), \
         patch("validate.routes.api_concerns.sources.token_usage_record", return_value=_NO_TOKENS),          patch("validate.routes.api_concerns.sources.extracted_csv", return_value=_NO_CSV):
        payload = client.get("/api/dashboard/concerns").get_json()

    concern = _ids(payload)["provenance_mismatch"]
    assert concern["count"] == 1
    assert "lukaswallrich" in concern["note"]
    assert "f7e4667b6c46" in concern["note"]


def test_no_local_release_cannot_raise_a_false_provenance_mismatch(client):
    """Without a store there is nothing to compare against — absence is not a mismatch."""
    stats = ({}, {"source": "stats.json", "state": "cached",
                  "release_id": "f7e4667b6c46", "machine": "lukaswallrich",
                  "as_of": "2026-08-16T21:06:14", "reason": None})
    with patch("validate.routes.api_concerns.run_sanity_check", return_value=_CLEAN), \
         patch("validate.routes.api_concerns.sources.stats_json", return_value=stats), \
         patch("validate.routes.api_concerns.sources.filtered_stats", return_value=_NO_STORE), \
         patch("validate.routes.api_concerns.sources.token_usage_record", return_value=_NO_TOKENS),          patch("validate.routes.api_concerns.sources.extracted_csv", return_value=_NO_CSV):
        payload = client.get("/api/dashboard/concerns").get_json()

    assert _ids(payload)["provenance_mismatch"]["count"] == 0


def test_an_outcome_of_api_error_is_a_concern_even_when_filed_correctly(client):
    """sanity_check's api_error bucket counts MISFILED rows; these are filed right."""
    df = pd.DataFrame({"outcome": ["successful", "api_error", "api_error"]})
    live = {"source": "extracted.csv", "state": "live", "release_id": None,
            "as_of": None, "machine": None, "reason": None}
    with patch("validate.routes.api_concerns.run_sanity_check", return_value=_CLEAN),          patch("validate.routes.api_concerns.sources.stats_json", return_value=_NO_STATS),          patch("validate.routes.api_concerns.sources.filtered_stats", return_value=_NO_STORE),          patch("validate.routes.api_concerns.sources.token_usage_record", return_value=_NO_TOKENS),          patch("validate.routes.api_concerns.sources.extracted_csv", return_value=(df, live)):
        payload = client.get("/api/dashboard/concerns").get_json()

    assert _ids(payload)["outcome_api_error"]["count"] == 2


def test_stats_from_another_machine_are_flagged_without_needing_a_store(client):
    """The release comparison needs a local store; this check does not."""
    stats = ({}, {"source": "stats.json", "state": "cached", "release_id": None,
                  "machine": "someone-else", "as_of": None, "reason": None})
    with patch("validate.routes.api_concerns.run_sanity_check", return_value=_CLEAN),          patch("validate.routes.api_concerns.sources.stats_json", return_value=stats),          patch("validate.routes.api_concerns.sources.filtered_stats", return_value=_NO_STORE),          patch("validate.routes.api_concerns.sources.token_usage_record", return_value=_NO_TOKENS),          patch("validate.routes.api_concerns.sources.extracted_csv", return_value=_NO_CSV):
        payload = client.get("/api/dashboard/concerns").get_json()

    concern = _ids(payload)["foreign_stats"]
    assert concern["count"] == 1
    assert "someone-else" in concern["note"]
