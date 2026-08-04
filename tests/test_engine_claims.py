"""Tests for the engine's Postgres client (issue #146 M2) — all HTTP is mocked.

One test per seam: configuration, the claim RPC's payload and return, conflict
mapping, paged reads, the client-side response_state rule, and the sizing
arithmetic. The SQL in db/migrations/0001_engine_baseline.sql is not exercised
here — there is no Postgres in CI.
"""
from unittest.mock import MagicMock, patch

import pytest

from filter.engine import sizing
from filter.engine.claims import (PENDING_UPLOAD, UPLOADED, ClaimConflict,
                                  ClaimsClient, ClaimsNotConfigured)


def _client() -> ClaimsClient:
    return ClaimsClient(url="https://fake.supabase.co", key="fake-key")


def _response(status: int = 200, payload=None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.content = b"x" if payload is not None else b""
    resp.json.return_value = payload
    return resp


def test_unconfigured_client_raises(monkeypatch):
    """No SUPABASE_URL means no state authority — the engine must not run unclaimed."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(ClaimsNotConfigured):
        ClaimsClient()


def test_claim_posts_rpc_payload_and_returns_uuid():
    """The claim goes to the server-side RPC verbatim — never select-then-insert."""
    claim_id = "11111111-2222-3333-4444-555555555555"
    with patch("filter.engine.claims.requests.post",
               return_value=_response(200, claim_id)) as post:
        got = _client().claim("rel-abc", "screen_expensive",
                              [(2001, "screen_expensive"), (2002, "screen_cheap")],
                              meta={"batch": "wave-1"})

    assert got == claim_id
    url, = post.call_args.args
    assert url.endswith("/rest/v1/rpc/engine_claim_batch")
    assert post.call_args.kwargs["json"] == {
        "p_release_id": "rel-abc",
        "p_tier": "screen_expensive",
        "p_items": [{"work_id": 2001, "pile": "screen_expensive"},
                    {"work_id": 2002, "pile": "screen_cheap"}],
        "p_meta": {"batch": "wave-1"},
    }


def test_conflict_maps_to_claim_conflict():
    """A 409 from the RPC's unique_violation names the tier that already holds them."""
    body = ('{"message":"claim_conflict: 3 of 500 works already held by an '
            'active screen_cheap claim"}')
    with patch("filter.engine.claims.requests.post",
               return_value=_response(409, None, body)):
        with pytest.raises(ClaimConflict) as excinfo:
            _client().claim("rel-abc", "screen_cheap", [(1, "screen_cheap")])

    assert excinfo.value.tier == "screen_cheap"
    assert "claim_conflict" in str(excinfo.value)


def test_claimed_work_ids_pages_with_deterministic_order():
    """PostgREST caps a page; paging an unordered result set repeats or skips rows."""
    first_page = [{"work_id": i, "claim_id": "c"} for i in range(1000)]
    second_page = [{"work_id": 1000, "claim_id": "c"}]
    with patch("filter.engine.claims.requests.get",
               side_effect=[_response(200, first_page),
                            _response(200, second_page)]) as get:
        ids = _client().claimed_work_ids("rel-abc", "screen_expensive")

    assert ids == set(range(1001))
    assert get.call_count == 2
    params = get.call_args_list[0].kwargs["params"]
    assert params["order"] == "work_id.asc,claim_id.asc"
    assert params["engine_claims.status"] == "eq.active"
    assert params["engine_claims.tier"] == "eq.screen_expensive"
    # Second page asks for the next window, not the same one.
    assert get.call_args_list[1].kwargs["headers"]["Range"] == "1000-1999"


def test_record_verdict_enforces_response_state():
    """§4 ordering: 'uploaded' means the blob is on HF and has a hash naming it."""
    client = _client()
    with pytest.raises(ValueError):
        client.record_verdict(claim_id="c", work_id=1, tier="screen_cheap",
                              verdict="none", response_state="whatever")
    with pytest.raises(ValueError):
        client.record_verdict(claim_id="c", work_id=1, tier="screen_cheap",
                              verdict="none", response_state=UPLOADED)

    with patch("filter.engine.claims.requests.post",
               return_value=_response(201, [{"id": "v-1"}])) as post:
        vid = client.record_verdict(claim_id="c", work_id=1, tier="screen_cheap",
                                    verdict="none", response_state=PENDING_UPLOAD)
    assert vid == "v-1"
    assert post.call_args.kwargs["json"]["response_state"] == PENDING_UPLOAD
    assert post.call_args.kwargs["json"]["response_hash"] is None


def test_sizing_is_deterministic_and_scales_linearly():
    """The projection is a measurement of synthetic rows, not an RNG draw."""
    a = sizing.estimate(1_000_000, 0.1, dsn="")
    b = sizing.estimate(1_000_000, 0.1, dsn="")
    assert a == b
    assert a["method"] == "model"
    # A claim_item is a uuid + a bigint + a short pile string plus tuple and index
    # overhead: order 100 B, not 10 B and not 1 kB.
    assert 60 < a["claim_items_bytes_per_row"] < 250
    assert 300 < a["verdict_bytes_per_row"] < 900

    ten_x = sizing.estimate(10_000_000, 0.1, dsn="")
    assert ten_x["total_mb"] == pytest.approx(a["total_mb"] * 10, rel=1e-3)
    # More verdicts per row costs more, and the free-tier capacity falls.
    heavier = sizing.estimate(1_000_000, 1.0, dsn="")
    assert heavier["total_mb"] > a["total_mb"]
    assert heavier["free_tier_row_capacity"] < a["free_tier_row_capacity"]
    assert sizing.estimate(1000, 0.1, dsn="")["fits_free_tier"] is True
