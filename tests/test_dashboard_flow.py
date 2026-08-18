"""One test per seam: the funnel degrades honestly, and counts no_text correctly."""
from unittest.mock import patch

import pandas as pd
import pytest

from validate.app import create_app


@pytest.fixture
def client():
    return create_app({"TESTING": True}).test_client()


_ABSENT_STORE = ({}, {"source": "routing store", "state": "absent",
                        "release_id": None, "as_of": None, "machine": None,
                        "reason": "no routing store on this machine — "
                                  "`python -m filter.engine route`"})
_LIVE_CSV = {"source": "extracted.csv", "state": "live", "release_id": None,
             "as_of": "2026-08-18T09:00:00", "machine": None, "reason": None}


def _stages(payload):
    return {s["id"]: s for s in payload["stages"]}


def test_missing_routing_store_says_so_instead_of_reporting_zero(client):
    df = pd.DataFrame({"doi_r": ["10.1/a"], "outcome": ["successful"]})
    with patch("validate.routes.api_flow.sources.filtered_stats", return_value=_ABSENT_STORE), \
         patch("validate.routes.api_flow.sources.extracted_csv", return_value=(df, _LIVE_CSV)):
        payload = client.get("/api/dashboard/flow").get_json()

    piles = _stages(payload)["release_piles"]
    assert piles["count"] is None                      # NOT 0
    assert "filter.engine route" in piles["provenance"]["reason"]


def test_rendered_rows_come_from_the_csv_even_without_a_store(client):
    df = pd.DataFrame({"doi_r": ["10.1/a", ""], "outcome": ["successful", "failed"]})
    with patch("validate.routes.api_flow.sources.filtered_stats", return_value=_ABSENT_STORE), \
         patch("validate.routes.api_flow.sources.extracted_csv", return_value=(df, _LIVE_CSV)):
        payload = client.get("/api/dashboard/flow").get_json()

    assert _stages(payload)["rendered"]["count"] == 2
    assert payload["completeness"]["blank_doi_r"] == 1


def test_no_text_is_counted_from_pending_reason_not_from_a_pile_name(client):
    """A work that matched a screening rule but had no abstract is recoverable coverage.

    route.py stores it as pile='pending' with pending_reason='no_text', so counting
    it needs the reason column — there is no 'pending/no_text' pile.
    """
    from filter.engine.store import open_store
    con = open_store(":memory:")
    con.execute("INSERT INTO routing VALUES "
                "(1,'pending','no_text','r',1,['r'],'e','rel1'),"
                "(2,'pending','no_text','r',1,['r'],'e','rel1'),"
                "(3,'pending',NULL,'r',1,['r'],'e','rel1'),"
                "(4,'screen_expensive',NULL,'r',1,['r'],'e','rel1'),"
                "(5,'discard',NULL,'r',1,['r'],'e','rel1')")
    piles = {"available": True, "release_id": "rel1", "total": 5,
             "by_pile": {"discard": 1, "pending": 3, "screen_expensive": 1},
             "release_created_at": "2026-08-11T21:13:13+00:00"}
    prov = {"source": "routing store", "state": "live", "release_id": "rel1",
            "as_of": "2026-08-11T21:13:13+00:00", "machine": None, "reason": None}

    with patch("validate.routes.api_flow.sources.filtered_stats", return_value=(piles, prov)),          patch("validate.routes.api_flow.sources.routing_store", return_value=(con, prov)),          patch("validate.routes.api_flow.sources.extracted_csv",
               return_value=(pd.DataFrame({"doi_r": ["10.1/a"]}), _LIVE_CSV)):
        payload = client.get("/api/dashboard/flow").get_json()

    assert payload["completeness"]["no_text"] == 2
    assert payload["completeness"]["by_pile"] == {"discard": 1, "pending": 3,
                                                  "screen_expensive": 1}
    assert _stages(payload)["release_piles"]["count"] == 5
    assert _stages(payload)["screened"]["count"] == 1


def test_set_aside_piles_are_counted_from_the_shared_destination_map(client, tmp_path,
                                                                     monkeypatch):
    """A destination the export deletes (nothing to write) is an empty pile, not absent."""
    import validate.routes.api_flow as flow

    monkeypatch.setattr(flow, "DATA_DIR", tmp_path)
    (tmp_path / "target_pending.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

    with patch("validate.routes.api_flow.sources.filtered_stats", return_value=_ABSENT_STORE), \
         patch("validate.routes.api_flow.sources.extracted_csv",
               return_value=(pd.DataFrame({"doi_r": ["10.1/a"]}), _LIVE_CSV)):
        payload = client.get("/api/dashboard/flow").get_json()

    target = payload["set_aside"]["target_pending.csv"]
    assert target["rows"] == 2
    assert target["title"]                       # a human name, not the filename
    assert "target_pending" in target["statuses"]
    # A destination the export deletes is an empty pile, not an absent one.
    assert payload["set_aside"]["api_error.csv"]["rows"] == 0


def test_a_raising_pool_reaches_the_panel_as_a_state_not_a_500(client):
    """Ported from the retired csv-stats tests: a 500 here takes the dashboard down."""
    with patch("shared.dashboard_cache.pool_totals", side_effect=RuntimeError("bad footer")), \
         patch("validate.routes.api_flow.sources.filtered_stats", return_value=_ABSENT_STORE), \
         patch("validate.routes.api_flow.sources.extracted_csv",
               return_value=(pd.DataFrame({"doi_r": ["10.1/a"]}), _LIVE_CSV)):
        response = client.get("/api/dashboard/flow")

    assert response.status_code == 200
    pool = _stages(response.get_json())["pool"]
    assert pool["count"] is None
    assert "bad footer" in pool["provenance"]["reason"]


def test_a_partial_pool_is_named_as_partial(tmp_path, monkeypatch, client):
    """A resumed pull leaves a pool that otherwise reads as a good smaller corpus."""
    import validate.routes.api_flow as flow

    (tmp_path / "_pool_provenance.json").write_text(
        '{"expected_files": 2246, "recorded_at": "2026-08-18T06:58:41+00:00"}',
        encoding="utf-8")
    partial = {"total": 10, "files": 3, "bytes": 1, "unreadable": 0,
               "pool_dir": str(tmp_path)}

    with patch("shared.dashboard_cache.pool_totals", return_value=partial), \
         patch("validate.routes.api_flow.sources.filtered_stats", return_value=_ABSENT_STORE), \
         patch("validate.routes.api_flow.sources.extracted_csv",
               return_value=(pd.DataFrame({"doi_r": ["10.1/a"]}), _LIVE_CSV)):
        payload = client.get("/api/dashboard/flow").get_json()

    prov = _stages(payload)["pool"]["provenance"]
    assert "partial pool: 3 of 2,246" in prov["reason"]
    assert prov["as_of"] == "2026-08-18T06:58:41+00:00"


def test_every_set_aside_pile_explains_itself(client):
    """A count with no definition is not actionable: each pile says what it means."""
    from shared.schema import SET_ASIDE_DESTINATIONS

    with patch("validate.routes.api_flow.sources.filtered_stats", return_value=_ABSENT_STORE),          patch("validate.routes.api_flow.sources.extracted_csv",
               return_value=(pd.DataFrame({"doi_r": ["10.1/a"]}), _LIVE_CSV)):
        sets = client.get("/api/dashboard/flow").get_json()["set_aside"]

    assert set(sets) == set(SET_ASIDE_DESTINATIONS.values())
    for filename, spec in sets.items():
        assert spec["why"], f"{filename} has no explanation"
        assert spec["statuses"], f"{filename} names no status that writes it"


def test_a_row_with_no_abstract_is_counted_like_a_row_with_no_doi(client):
    """The screen and every abstract-stage rung read abstract_r; a blank one is a gap."""
    df = pd.DataFrame({"doi_r": ["10.1/a", "", "10.1/c"],
                       "abstract_r": ["text", "text", ""]})
    with patch("validate.routes.api_flow.sources.filtered_stats", return_value=_ABSENT_STORE),          patch("validate.routes.api_flow.sources.extracted_csv", return_value=(df, _LIVE_CSV)):
        c = client.get("/api/dashboard/flow").get_json()["completeness"]

    assert c["blank_doi_r"] == 1
    assert c["blank_abstract_r"] == 1
