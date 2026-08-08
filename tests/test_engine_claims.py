"""Tests for the engine's Postgres client (issue #146 M2) — all HTTP is mocked.

One test per seam: configuration, the claim RPC's payload and return, conflict
mapping, paged reads, the client-side response_state rule, and the sizing
arithmetic. The SQL in db/migrations/0001_engine_baseline.sql is not exercised
here — there is no Postgres in CI.
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest

from filter.engine import sizing
from filter.engine.claims import (CLAIM_TTL_HOURS, PENDING_UPLOAD, UPLOADED,
                                  ClaimLeaseLost, ClaimsError,
                                  ClaimConflict, ClaimExpiryUnsupported,
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
    sent = post.call_args.kwargs["json"]
    lease = sent.pop("p_expires_at")
    assert sent == {
        "p_release_id": "rel-abc",
        "p_tier": "screen_expensive",
        "p_items": [{"work_id": 2001, "pile": "screen_expensive"},
                    {"work_id": 2002, "pile": "screen_cheap"}],
        "p_meta": {"batch": "wave-1"},
    }
    # The claim is a lease: hours ahead, because an LLM run takes hours.
    ahead = datetime.datetime.fromisoformat(lease) - datetime.datetime.now(
        datetime.timezone.utc)
    assert 0 < ahead.total_seconds() <= CLAIM_TTL_HOURS * 3600 + 5


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


def test_subtraction_ignores_expired_claims():
    """A killed run's claim must stop holding its works — W3.

    The filter is server-side, so what is asserted is that the query asks for
    unexpired claims at all: without it a stranded claim subtracts its works from
    every later batch forever.
    """
    with patch("filter.engine.claims.requests.get",
               return_value=_response(200, [])) as get:
        _client().claimed_work_ids("rel-abc", "screen_expensive")

    params = get.call_args.kwargs["params"]
    assert params["engine_claims.status"] == "eq.active"
    cutoff = params["engine_claims.expires_at"]
    assert cutoff.startswith("gt.")
    # The cutoff is now, so a lease that has run out is on the excluded side.
    asked = datetime.datetime.fromisoformat(cutoff[3:])
    assert abs((asked - datetime.datetime.now(datetime.timezone.utc)).total_seconds()) < 5


def test_unmigrated_database_names_the_migration():
    """Migration 0004 must be run first, and the failure has to say so."""
    body = ('{"code":"PGRST202","message":"Could not find the function '
            'public.engine_claim_batch(p_expires_at, p_items, ...)"}')
    with patch("filter.engine.claims.requests.post",
               return_value=_response(404, None, body)):
        with pytest.raises(ClaimExpiryUnsupported) as excinfo:
            _client().claim("rel-abc", "screen_expensive", [(1, "screen_expensive")])

    assert "0004_claim_expiry.sql" in str(excinfo.value)


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


def test_release_claim_cli_uses_the_run_completion_path(capsys):
    """`release-claim` ends a claim through the same RPC a finishing run calls."""
    from filter.engine.cli import main

    client = MagicMock()
    client.active_claims.return_value = [
        {"id": "c-1", "tier": "screen_expensive", "status": "active",
         "created_at": "2026-08-01T00:00:00+00:00",
         "expires_at": "2026-08-01T06:00:00+00:00"}]
    client.claim_item_count.return_value = 500

    with patch("filter.engine.claims.ClaimsClient", return_value=client):
        assert main(["release-claim"]) == 0
        listing = capsys.readouterr().out
        # Without --yes nothing is ended.
        with pytest.raises(SystemExit):
            main(["release-claim", "--claim", "c-1"])
        client.release_claim.assert_not_called()
        assert main(["release-claim", "--claim", "c-1", "--yes"]) == 0

    assert "c-1" in listing and "EXPIRED" in listing and "500" in listing
    client.release_claim.assert_called_once_with("c-1", "failed")


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


def test_renew_claim_extends_the_lease_and_maps_lease_lost():
    """The heartbeat goes to engine_renew_claim; an expired lease is a named stop."""
    client = _client()
    new_end = "2026-08-06T12:45:00+00:00"
    with patch("filter.engine.claims.requests.post",
               return_value=_response(200, new_end)) as post:
        got = client.renew_claim("claim-1", ttl_seconds=45 * 60)
    assert got == new_end
    url, = post.call_args.args
    assert url.endswith("/rest/v1/rpc/engine_renew_claim")
    sent = post.call_args.kwargs["json"]
    assert sent["p_claim_id"] == "claim-1"
    lease = datetime.datetime.fromisoformat(sent["p_expires_at"])
    ahead = lease - datetime.datetime.now(datetime.timezone.utc)
    assert datetime.timedelta(minutes=40) < ahead < datetime.timedelta(minutes=50)

    lost = _response(400, text="claim_lease_lost: claim-1 expired at …")
    lost.text = "claim_lease_lost: claim-1 expired at …"
    with patch("filter.engine.claims.requests.post", return_value=lost):
        with pytest.raises(ClaimLeaseLost):
            client.renew_claim("claim-1")

    stale = _response(400, text="claim_not_active: claim-1 is already complete")
    stale.text = "claim_not_active: claim-1 is already complete"
    with patch("filter.engine.claims.requests.post", return_value=stale):
        with pytest.raises(ClaimsError):
            client.renew_claim("claim-1")


def test_record_verdict_sends_payload_only_when_given():
    """A screen verdict against a pre-0005 database must still insert."""
    client = _client()
    with patch("filter.engine.claims.requests.post",
               return_value=_response(201, [{"id": "v-1"}])) as post:
        client.record_verdict(claim_id="c", work_id=1, tier="screen_cheap",
                              verdict="none", response_state=PENDING_UPLOAD)
    assert "payload" not in post.call_args.kwargs["json"]

    with patch("filter.engine.claims.requests.post",
               return_value=_response(201, [{"id": "v-2"}])) as post:
        client.record_verdict(claim_id="c", work_id=1, tier="extract",
                              verdict="resolved", response_state=PENDING_UPLOAD,
                              payload={"targets": []})
    assert post.call_args.kwargs["json"]["payload"] == {"targets": []}


def test_verdicts_selects_payload_only_when_asked():
    """The screen's read stays lean; the extract export pays for payloads."""
    client = _client()
    page = _response(200, [])
    with patch("filter.engine.claims.requests.get", return_value=page) as get:
        client.verdicts("screen_expensive")
    assert "payload" not in get.call_args.kwargs["params"]["select"]

    with patch("filter.engine.claims.requests.get", return_value=page) as get:
        client.verdicts("extract", with_payload=True)
    select = get.call_args.kwargs["params"]["select"]
    assert "payload" in select and "prompt_hash" in select


class TestNulsNeverReachPostgres:
    """Postgres text/jsonb cannot hold U+0000; PostgREST answers 22P05.

    NULs arrive in parsed PDF text and ride into `quote` and the extract tier's
    stored payload. On 2026-08-07 one such byte returned HTTP 400 from
    `engine_verdicts` and, because a verdict write is fatal, aborted a 1,325-work
    campaign at work 84.
    """

    def test_a_nul_in_a_nested_payload_is_stripped_before_the_post(self):
        client = _client()
        with patch("filter.engine.claims.requests.post") as post:
            post.return_value = _response(payload=[{"id": "v1"}])
            client.record_verdict(
                claim_id="c1", work_id=7, tier="extract", verdict="resolved",
                quote="ends with a nul\x00",
                payload={"targets": [{"evidence": "text\x00more",
                                      "n": 3, "ok": True}]})
        sent = post.call_args.kwargs["json"]
        assert sent["quote"] == "ends with a nul"
        assert sent["payload"]["targets"][0]["evidence"] == "textmore"
        # Non-strings pass through untouched.
        assert sent["payload"]["targets"][0]["n"] == 3
        assert sent["payload"]["targets"][0]["ok"] is True
