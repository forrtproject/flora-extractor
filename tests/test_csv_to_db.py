"""Tests for extract/csv_to_db.py — the Supabase client is stubbed, no live calls.

The `supabase` package is not a test dependency, so we inject a lightweight stub
module before importing extract.csv_to_db. All DB interaction goes through a
FakeClient that records the chained calls and returns canned data.
"""
import sys
import types
from unittest.mock import patch

import pandas as pd
import pytest


# --- Stub the `supabase` package so extract.csv_to_db imports without the real lib ---
if "supabase" not in sys.modules:
    stub = types.ModuleType("supabase")
    stub.create_client = lambda url, key: None  # overridden per-test via patch
    stub.Client = object  # only used as a type annotation
    sys.modules["supabase"] = stub

import extract.csv_to_db as csv_to_db  # noqa: E402


class _FakeExecuteResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    """Chainable stand-in for supabase-py's query builder.

    Records every insert (as (table_name, payload)) into the shared call_log so
    tests can assert insert ORDER. Select+range returns pages from select_pages.
    """

    def __init__(self, name, call_log, select_pages):
        self.name = name
        self.call_log = call_log
        self.select_pages = select_pages
        self._range = None
        self._pending_insert = None

    def select(self, _cols):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def execute(self):
        if self._pending_insert is not None:
            self.call_log.append((self.name, self._pending_insert))
            self._pending_insert = None
            return _FakeExecuteResult([])
        # select path (record_metadata pagination)
        start, end = self._range if self._range else (0, 9999)
        page_size = end - start + 1
        rows = self.select_pages.get(self.name, [])
        return _FakeExecuteResult(rows[start:start + page_size])


class _FakeClient:
    def __init__(self, select_pages=None):
        self.call_log = []
        self.select_pages = select_pages or {}

    def table(self, name):
        return _FakeTable(name, self.call_log, self.select_pages)


# --------------------------------------------------------------------------- #
# 1. Pagination across >1000 rows                                             #
# --------------------------------------------------------------------------- #
def test_load_existing_pair_ids_paginates_past_1000():
    """_load_existing_pair_ids must page through >1000 rows, not stop at the cap."""
    total = 2500
    rows = [{"pair_id": f"p{i}"} for i in range(total)]
    client = _FakeClient(select_pages={"record_metadata": rows})

    result = csv_to_db._load_existing_pair_ids(client)

    assert len(result) == total
    assert "p0" in result and "p2499" in result


def test_load_existing_pair_ids_exact_multiple_of_page_size():
    """When the row count is an exact multiple of 1000, the loop still terminates
    (a full final page triggers one more fetch that comes back empty)."""
    rows = [{"pair_id": f"p{i}"} for i in range(2000)]
    client = _FakeClient(select_pages={"record_metadata": rows})

    result = csv_to_db._load_existing_pair_ids(client)

    assert len(result) == 2000


# --------------------------------------------------------------------------- #
# 2. Disjoint skip accounting                                                 #
# --------------------------------------------------------------------------- #
def _run_and_capture(df, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")
    csv_path = tmp_path / "extracted.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    csv_to_db.run_import(csv_path, dry_run=True)
    return capsys.readouterr().out


def test_skip_accounting_is_disjoint(capsys, monkeypatch, tmp_path):
    """Each skipped row lands in exactly one bucket; buckets + resolved == total."""
    df = pd.DataFrame([
        # resolved (imported)
        {"filter_status": "replication", "link_method": "author_year_match_legacy",
         "doi_r": "10.1/a", "doi_o": "10.2/a", "pair_id": "pa"},
        # false_positive that ALSO has no_original_found — must count as FP only,
        # never double-counted or subtracted into a negative "other" bucket.
        {"filter_status": "false_positive", "link_method": "no_original_found",
         "doi_r": "10.1/b", "doi_o": "", "pair_id": "pb"},
        # genuine no_original_found (a real replication, LLM found no original)
        {"filter_status": "replication", "link_method": "no_original_found",
         "doi_r": "10.1/c", "doi_o": "", "pair_id": "pc"},
        # other pending
        {"filter_status": "replication", "link_method": "target_pending",
         "doi_r": "10.1/d", "doi_o": "", "pair_id": "pd"},
        # plain false_positive
        {"filter_status": "false_positive", "link_method": "author_year_match_legacy",
         "doi_r": "10.1/e", "doi_o": "", "pair_id": "pe"},
    ])

    out = _run_and_capture(df, capsys, monkeypatch, tmp_path)

    assert "Resolved (import):  1" in out
    assert "false_positive:     2" in out          # both FP rows, incl. the no_orig one
    assert "no_original_found:  1" in out           # only the non-FP no_orig row
    assert "target_pending / api_error / other: 1" in out


def test_skip_buckets_sum_to_total(capsys, monkeypatch, tmp_path):
    """Parse the printed counts and confirm resolved + all skip buckets == len(df)."""
    df = pd.DataFrame([
        {"filter_status": "replication", "link_method": "llm_abstract",
         "doi_r": "10.1/a", "doi_o": "10.2/a", "pair_id": "pa"},
        {"filter_status": "false_positive", "link_method": "no_original_found",
         "doi_r": "10.1/b", "doi_o": "", "pair_id": "pb"},
        {"filter_status": "replication", "link_method": "no_original_found",
         "doi_r": "10.1/c", "doi_o": "", "pair_id": "pc"},
        {"filter_status": "reproduction", "link_method": "api_error",
         "doi_r": "10.1/d", "doi_o": "", "pair_id": "pd"},
    ])

    out = _run_and_capture(df, capsys, monkeypatch, tmp_path)

    import re
    resolved = int(re.search(r"Resolved \(import\):\s+(\d+)", out).group(1))
    fp = int(re.search(r"false_positive:\s+(\d+)", out).group(1))
    no_orig = int(re.search(r"no_original_found:\s+(\d+)", out).group(1))
    other = int(re.search(r"other:\s+(\d+)", out).group(1))

    assert resolved + fp + no_orig + other == len(df)
    assert min(resolved, fp, no_orig, other) >= 0


# --------------------------------------------------------------------------- #
# 3. Insert order — record_metadata (dedup anchor) last                       #
# --------------------------------------------------------------------------- #
def test_insert_order_record_metadata_last(monkeypatch, tmp_path):
    """record_metadata must be inserted AFTER unvalidated and validation_queue so a
    partial failure never leaves a dedup anchor that skips the pair on re-run."""
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")

    df = pd.DataFrame([
        {"filter_status": "replication", "link_method": "author_year_match_legacy",
         "doi_r": "10.1/a", "doi_o": "10.2/a", "pair_id": "pa"},
        {"filter_status": "reproduction", "link_method": "llm_fulltext",
         "doi_r": "10.1/b", "doi_o": "10.2/b", "pair_id": "pb"},
    ])
    csv_path = tmp_path / "extracted.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    fake = _FakeClient(select_pages={"record_metadata": []})  # empty DB → import both

    with patch.object(csv_to_db, "create_client", return_value=fake):
        csv_to_db.run_import(csv_path, dry_run=False)

    inserted_tables = [name for name, _ in fake.call_log]

    # Two records × three tables
    assert inserted_tables.count("record_metadata") == 2
    assert inserted_tables.count("unvalidated") == 2
    assert inserted_tables.count("validation_queue") == 2

    # For each record's triplet, record_metadata comes last
    for i in range(0, len(inserted_tables), 3):
        triplet = inserted_tables[i:i + 3]
        assert triplet[-1] == "record_metadata", triplet
        assert set(triplet[:2]) == {"unvalidated", "validation_queue"}


# --------------------------------------------------------------------------- #
# 5. FLoRA gate — never re-validate a replication FLoRA already has            #
# --------------------------------------------------------------------------- #
def _resolved_df():
    return pd.DataFrame([
        {"filter_status": "replication", "link_method": "author_year_match_legacy",
         "doi_r": "10.1037/per0000041", "doi_o": "10.2/x", "pair_id": "p_in_flora"},
        {"filter_status": "replication", "link_method": "author_year_match_legacy",
         "doi_r": "10.9/novel", "doi_o": "10.2/y", "pair_id": "p_new"},
    ])


def test_flora_rows_are_not_imported(capsys, monkeypatch, tmp_path):
    """A doi_r already in FLoRA must be gated out before the Supabase insert."""
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")
    monkeypatch.setattr(csv_to_db, "default_flora_skip_dois",
                        lambda: {"10.1037/per0000041"})
    csv_path = tmp_path / "extracted.csv"
    _resolved_df().to_csv(csv_path, index=False, encoding="utf-8-sig")

    csv_to_db.run_import(csv_path, dry_run=True)
    out = capsys.readouterr().out

    assert "Resolved (import):  1" in out
    assert "already in FLoRA:   1" in out


def test_flora_gate_can_be_disabled(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")
    monkeypatch.setattr(csv_to_db, "default_flora_skip_dois",
                        lambda: {"10.1037/per0000041"})
    csv_path = tmp_path / "extracted.csv"
    _resolved_df().to_csv(csv_path, index=False, encoding="utf-8-sig")

    csv_to_db.run_import(csv_path, dry_run=True, skip_flora=False)
    out = capsys.readouterr().out

    assert "Resolved (import):  2" in out


def test_flora_gate_blocks_the_actual_insert(monkeypatch, tmp_path):
    """Not just the count — the gated row must never reach an insert() call."""
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "fake-key")
    monkeypatch.setattr(csv_to_db, "default_flora_skip_dois",
                        lambda: {"10.1037/per0000041"})
    csv_path = tmp_path / "extracted.csv"
    _resolved_df().to_csv(csv_path, index=False, encoding="utf-8-sig")

    fake = _FakeClient(select_pages={"record_metadata": []})
    with patch.object(csv_to_db, "create_client", return_value=fake):
        csv_to_db.run_import(csv_path, dry_run=False)

    payloads = [str(p) for _, p in fake.call_log]
    assert not any("10.1037/per0000041" in p for p in payloads)
    assert any("10.9/novel" in p for p in payloads)


# --------------------------------------------------------------------------- #
# 6. url_o derivation — never point confidently at the wrong paper             #
# --------------------------------------------------------------------------- #
def test_url_o_uses_doi_when_verified():
    row = {"doi_o": "10.2/orig", "doi_o_verification": "verified", "title_o": "T"}
    assert csv_to_db._derive_url_o(row) == "https://doi.org/10.2/orig"


def test_url_o_not_emitted_for_unverified_doi():
    """A mismatched DOI must NOT become a confident doi.org link — that sends a
    validator to the wrong paper, which is worse than giving them a search."""
    row = {"doi_o": "10.2/wrong", "doi_o_verification": "mismatch",
           "title_o": "The Original Work"}
    url = csv_to_db._derive_url_o(row)
    assert "doi.org/10.2/wrong" not in url
    assert "openalex.org" in url and "The" in url


def test_url_o_falls_back_to_search_when_no_doi():
    """Preprints / old papers legitimately have no DOI — give a resolvable search
    link rather than an empty cell."""
    row = {"doi_o": "", "doi_o_verification": "no_doi", "title_o": "A Titled Work"}
    url = csv_to_db._derive_url_o(row)
    assert url.startswith("https://openalex.org/works?search=")
    assert "Titled" in url


def test_url_o_empty_when_nothing_to_point_at():
    assert csv_to_db._derive_url_o({"doi_o": "", "doi_o_verification": "", "title_o": ""}) == ""


def test_url_o_accepts_corrected_doi():
    row = {"doi_o": "10.2/fixed", "doi_o_verification": "corrected", "title_o": "T"}
    assert csv_to_db._derive_url_o(row) == "https://doi.org/10.2/fixed"
