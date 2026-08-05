"""Tests for validation-table supersession (#146 M5) — all HTTP is mocked.

One test per seam: the routing diff, the lineage csv_to_db writes, the affected-
record lookup (paging + validated/unvalidated split), the dry-run's silence, and
what a live run does and does not write.
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from filter.engine import supersede as sup
from filter.engine.store import open_store

# extract.csv_to_db imports the `supabase` package, which is not a test dependency.
if "supabase" not in sys.modules:
    _stub = types.ModuleType("supabase")
    _stub.create_client = lambda url, key: None
    _stub.Client = object
    sys.modules["supabase"] = _stub

import extract.csv_to_db as csv_to_db  # noqa: E402


def _store_with(rows):
    """An in-memory routing store holding `(work_id, pile, release_id)` rows."""
    con = open_store(":memory:")
    for work_id, pile, release_id in rows:
        con.execute(
            "INSERT INTO routing VALUES (?, ?, '', ?, 100, [], '', ?)",
            [work_id, pile, f"rule-{pile}", release_id])
    return con


def _response(status: int = 200, payload=None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.json.return_value = payload
    return resp


# ── the routing diff ─────────────────────────────────────────────────────────

def test_diff_releases_finds_rerouted_works_only():
    """A pile that changed is a supersession; an unchanged one, and a work only one
    release routed, are not."""
    con = _store_with([
        (11, "screen_expensive", "R1"), (11, "discard", "R2"),   # re-routed
        (22, "screen_cheap", "R1"), (22, "screen_cheap", "R2"),  # unchanged
        (33, "screen_cheap", "R1"),                              # gone in R2
        (44, "screen_cheap", "R2"),                              # new in R2
    ])
    diffs = sup.diff_releases(con, "R1", "R2")

    assert [d["work_id"] for d in diffs] == [11]
    assert diffs[0]["old_pile"] == "screen_expensive"
    assert diffs[0]["new_pile"] == "discard"
    assert diffs[0]["new_rule"] == "rule-discard"
    assert sup.supersession_kind("screen_expensive", "discard") == "withdrawal"
    assert sup.supersession_kind("screen_cheap", "screen_expensive") == "reroute"


# ── the lineage csv_to_db writes ─────────────────────────────────────────────

def test_metadata_row_carries_work_id_and_release_id():
    """Both halves of the lineage reach record_metadata, from their own sources.

    `work_id` comes off the row; `release_id` does NOT, and never could —
    `EXTRACTED_COLS` excludes the engine's export columns, so a Stage 3 row has
    no release to read. The release names the whole handoff (one per
    `filtered.csv.manifest.json`), so it is an argument to the import, not a
    column: provenance is linked by `work_id`, not copied per row.
    """
    row = pd.Series({
        "pair_id": "p1",
        "openalex_id_r": "https://openalex.org/W2741809807",
    })
    out = csv_to_db._build_metadata_row("rec-1", row, release_id="rel-abc")
    assert out["work_id"] == 2741809807
    assert out["release_id"] == "rel-abc"

    # A release carried on the row is ignored — it cannot be there legitimately.
    stowaway = pd.Series({**row.to_dict(), "release_id": "rel-from-row"})
    assert csv_to_db._build_metadata_row("rec-1", stowaway)["release_id"] is None


def test_metadata_row_lineage_is_null_when_the_row_predates_the_engine():
    """Unparseable/absent ids are null, not an error and not an empty string:
    reconciliation must be able to see that a row has no engine lineage."""
    out = csv_to_db._build_metadata_row("rec-2", pd.Series({
        "pair_id": "p2", "openalex_id_r": "not-an-openalex-id",
    }))
    assert out["work_id"] is None
    assert out["release_id"] is None

    empty = csv_to_db._build_metadata_row("rec-3", pd.Series({"pair_id": "p3"}))
    assert empty["work_id"] is None and empty["release_id"] is None


# ── the affected-record lookup ───────────────────────────────────────────────

def test_affected_lookup_pages_and_splits_validated_from_unvalidated(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")

    # record_metadata answers in two pages (the second short); `validated` names
    # one of the records as final.
    page1 = [{"record_id": f"r{i}", "work_id": 11, "release_id": "R1"}
             for i in range(1000)]
    page2 = [{"record_id": "rX", "work_id": 22, "release_id": "R1"}]
    validated = [{"record_id": "rX"}]

    metadata_pages = [page1, page2]

    def _fake_get(url, **kwargs):
        if url.endswith("/validated"):
            wanted = kwargs["params"]["record_id"]
            return _response(200, [r for r in validated if f'"{r["record_id"]}"' in wanted])
        return _response(200, metadata_pages.pop(0) if metadata_pages else [])

    with patch("filter.engine.supersede.requests.get", side_effect=_fake_get) as get:
        affected = sup.affected_validation_records([11, 22, 33])

    assert set(affected) == {11, 22}
    assert len(affected[11]["record_ids"]) == 1000
    assert affected[11]["validated"] == []
    assert affected[22]["validated"] == ["rX"] and affected[22]["unvalidated"] == []
    # Paging is ordered, or PostgREST may repeat/skip rows between pages.
    assert get.call_args_list[0].kwargs["params"]["order"] == "record_id"
    assert get.call_args_list[0].kwargs["params"]["work_id"] == "in.(11,22,33)"


# ── writing (and not writing) ────────────────────────────────────────────────

def _diffs_and_affected():
    diffs = [{"work_id": 11, "old_pile": "screen_expensive", "new_pile": "discard",
              "old_rule": "a", "new_rule": "b"},
             {"work_id": 99, "old_pile": "screen_cheap", "new_pile": "screen_expensive",
              "old_rule": "a", "new_rule": "c"}]
    affected = {11: {"record_ids": ["r1", "r2"], "validated": ["r1"],
                     "unvalidated": ["r2"]}}
    return diffs, affected


def test_dry_run_writes_nothing():
    diffs, affected = _diffs_and_affected()
    client = MagicMock()
    result = sup.supersede(client, diffs, affected, reason="spec edit", actor="me")

    client.record_supersession.assert_not_called()
    assert result["dry_run"] is True and result["written"] == []
    assert result["works_changed"] == 2 and result["works_affected"] == 1
    assert result["records_named"] == 2
    assert result["validated_records"] == 1 and result["unvalidated_records"] == 1


def test_run_writes_one_lineage_row_per_affected_work_and_no_validation_write():
    """A work with no already-sent record gets no row; the affected one gets exactly
    one, naming its record_ids. Nothing touches unvalidated/validated/queue."""
    diffs, affected = _diffs_and_affected()
    with patch("filter.engine.claims.requests.post",
               return_value=_post_response([{"id": "s-1"}])) as post:
        from filter.engine.claims import ClaimsClient
        client = ClaimsClient(url="https://fake.supabase.co", key="fake-key")
        result = sup.supersede(client, diffs, affected, reason="spec edit",
                               actor="me", old_release_id="R1", new_release_id="R2",
                               dry_run=False)

    assert result["written"] == ["s-1"]
    assert post.call_count == 1
    url, = post.call_args.args
    assert url.endswith("/rest/v1/engine_supersessions")
    assert post.call_args.kwargs["json"] == {
        "work_id": 11, "old_release_id": "R1", "new_release_id": "R2",
        "kind": "withdrawal", "reason": "spec edit",
        "affected_record_ids": ["r1", "r2"], "actor": "me",
    }
    # The immutability rule of #146 §5, asserted rather than assumed.
    posted_paths = [call.args[0] for call in post.call_args_list]
    assert not any(t in path for path in posted_paths
                   for t in ("unvalidated", "validated", "validation_queue"))


def _post_response(payload):
    resp = MagicMock()
    resp.status_code = 201
    resp.text = ""
    resp.content = b"[]"
    resp.json.return_value = payload
    return resp


def test_supersession_kind_is_validated_by_the_client():
    from filter.engine.claims import ClaimsClient
    client = ClaimsClient(url="https://fake.supabase.co", key="fake-key")
    with pytest.raises(ValueError):
        client.record_supersession(work_id=1, kind="unrouted", affected_record_ids=[])
