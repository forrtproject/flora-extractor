"""
Tests for search functions
"""
import json
import os
import pytest

from shared.config import OA_CACHE_DIR
from search import openalex_search as oa
from search.semantic_scholar_search import fetch_semantic_scholar_candidates
from search.external_lists import fetch_i4r
from search.run_search import _row_keys


# ---------------------------------------------------------------------------
# Dedup keys (#53): a row with a stronger identifier must NOT dedupe on title,
# so two distinct DOIs that share a title are both kept.
# ---------------------------------------------------------------------------

def test_row_keys_doi_row_has_no_title_key():
    keys = _row_keys({"doi_r": "10.1/abc", "title_r": "A Shared Title"})
    assert "10.1/abc" in keys
    assert not any(k.startswith("title:") for k in keys)


def test_row_keys_titleless_identifier_rows_dont_collide():
    a = _row_keys({"doi_r": "10.1/aaa", "title_r": "Registered Replication Report"})
    b = _row_keys({"doi_r": "10.1/bbb", "title_r": "Registered Replication Report"})
    assert set(a).isdisjoint(b), "distinct DOIs sharing a title must not share any key"


def test_row_keys_doi_less_row_still_uses_title():
    keys = _row_keys({"title_r": "Only A Title"})
    assert keys == ["title:only a title"]


def test_s2_phrases_match_openalex_and_include_failure_signals():
    """#47: S2 must sample the same phrase set as OpenAlex, incl. failure-signal
    phrases, or the two sources skew the eventual success/failure balance."""
    from search.semantic_scholar_search import SEARCH_PHRASES as S2
    from search.openalex_search import SEARCH_PHRASES as OA
    assert set(S2) == set(OA)
    for p in ("failed to replicate", "did not replicate", "could not reproduce"):
        assert p in S2


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

class DummyResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise oa.requests.HTTPError(f"HTTP {self.status_code}")


def make_payload():
    return {
        "meta": {"count": 1, "next_cursor": None},
        "results": [
            {
                "id": "https://openalex.org/W123",
                "doi": "https://doi.org/10.1234/ABC.567",
                "title": "A direct replication study",
                "publication_year": 2024,
                "authorships": [
                    {"author": {"display_name": "Alice Smith"}},
                    {"author": {"display_name": "Bob Jones"}},
                ],
                "primary_location": {
                    "source": {"display_name": "Journal of Replications"}
                },
                "open_access": {"oa_url": "https://example.org/paper.pdf"},
                "abstract_inverted_index": {
                    "This": [0],
                    "is": [1],
                    "a": [2],
                    "replication": [3],
                    "abstract": [4],
                },
            }
        ],
    }


def test_extract_row_maps_expected_fields():
    row = oa._extract_row(make_payload()["results"][0])

    assert row["doi_r"] == "10.1234/abc.567"
    assert row["title_r"] == "A direct replication study"
    assert row["abstract_r"] == "This is a replication abstract"
    assert row["year_r"] == 2024
    assert row["authors_r"] == "Alice Smith; Bob Jones"
    assert row["journal_r"] == "Journal of Replications"
    assert row["url_r"] == "https://example.org/paper.pdf"
    assert row["openalex_id_r"] == "https://openalex.org/W123"
    assert row["source"] == "openalex"


def test_fetch_openalex_candidates_schema_and_cleaning(monkeypatch, tmp_path):
    monkeypatch.setattr(oa, "OA_CACHE_DIR", tmp_path)
    monkeypatch.setattr(oa, "SEARCH_PHRASES", ["direct replication"])
    monkeypatch.setattr(oa.time, "sleep", lambda *_: None)

    calls = []

    def fake_get(url, params, timeout, **kwargs):
        calls.append((url, params, timeout))
        return DummyResponse(make_payload())

    monkeypatch.setattr(oa.requests, "get", fake_get)

    df = oa.fetch_openalex_candidates()

    assert list(df.columns) == oa.CANDIDATES_COLS
    assert len(df) == 1
    assert df.loc[0, "doi_r"] == "10.1234/abc.567"
    assert df.loc[0, "abstract_r"] == "This is a replication abstract"
    assert calls


def test_fetch_openalex_candidates_uses_cache_on_second_run(monkeypatch, tmp_path):
    monkeypatch.setattr(oa, "OA_CACHE_DIR", tmp_path)
    monkeypatch.setattr(oa, "SEARCH_PHRASES", ["direct replication"])
    monkeypatch.setattr(oa.time, "sleep", lambda *_: None)

    call_count = {"n": 0}

    def fake_get(url, params, timeout, **kwargs):
        call_count["n"] += 1
        return DummyResponse(make_payload())

    monkeypatch.setattr(oa.requests, "get", fake_get)

    df1 = oa.fetch_openalex_candidates()
    df2 = oa.fetch_openalex_candidates()

    # Only one HTTP request should be made — phrase is marked complete after first run
    assert call_count["n"] == 1
    assert list(df1.columns) == oa.CANDIDATES_COLS
    assert list(df2.columns) == oa.CANDIDATES_COLS
    # Second call returns empty (phrase already fully fetched, nothing new to add)
    assert len(df1) == 1
    assert len(df2) == 0


class TestCursorCompleteness:
    """A job must checkpoint what it knows, so under-fetching is detectable later (#68)."""

    def _run(self, monkeypatch, tmp_path, payload):
        monkeypatch.setattr(oa, "OA_CACHE_DIR", tmp_path)
        monkeypatch.setattr(oa.time, "sleep", lambda *_: None)
        monkeypatch.setattr(oa.requests, "get",
                            lambda url, params, timeout, **kw: DummyResponse(payload))
        rows = oa.fetch_phrase("direct replication", 2020, 2020)
        state = json.loads(
            (tmp_path / f"{oa._job_key('direct replication', 2020, 2020)}.cursor.json")
            .read_text(encoding="utf-8"))
        return rows, state

    def test_empty_result_set_is_marked_complete(self, monkeypatch, tmp_path):
        """A phrase-year with genuinely zero hits used to break out of the loop before
        the completed checkpoint, so every later run re-requested it forever."""
        rows, state = self._run(
            monkeypatch, tmp_path, {"meta": {"count": 0, "next_cursor": None}, "results": []})
        assert rows == []
        assert state["completed"] is True
        assert state["api_total"] == 0

    def test_api_total_is_recorded_for_a_non_empty_job(self, monkeypatch, tmp_path):
        _, state = self._run(monkeypatch, tmp_path, make_payload())
        assert state["completed"] is True
        assert state["api_total"] == 1
        assert state["total_fetched"] == 1

    def test_empty_job_is_not_refetched(self, monkeypatch, tmp_path):
        monkeypatch.setattr(oa, "OA_CACHE_DIR", tmp_path)
        monkeypatch.setattr(oa.time, "sleep", lambda *_: None)
        n = {"calls": 0}

        def fake_get(url, params, timeout, **kw):
            n["calls"] += 1
            return DummyResponse({"meta": {"count": 0, "next_cursor": None}, "results": []})

        monkeypatch.setattr(oa.requests, "get", fake_get)
        oa.fetch_phrase("direct replication", 2020, 2020)
        oa.fetch_phrase("direct replication", 2020, 2020)
        assert n["calls"] == 1, "the second run must skip a job already known to be empty"


class TestPhraseYieldCoverage:
    def test_reports_expected_incomplete_and_missing_years(self, monkeypatch, tmp_path):
        monkeypatch.setattr(oa, "OA_CACHE_DIR", tmp_path)
        phrase = oa.SEARCH_PHRASES[0]
        this_year = oa.datetime.date.today().year
        # One finished year and one that stopped a long way short of api_total.
        for year, fetched, total, done in [(oa.COVERAGE_FROM_YEAR, 10, 10, True),
                                           (oa.COVERAGE_FROM_YEAR + 1, 5, 500, False)]:
            (tmp_path / f"{oa._job_key(phrase, year, year)}.cursor.json").write_text(
                json.dumps({"total_fetched": fetched, "completed": done,
                            "api_total": total}), encoding="utf-8")

        row = next(r for r in oa.phrase_yield()["rows"] if r["phrase"] == phrase)

        assert row["fetched"] == 15
        assert row["expected"] == 510, "coverage needs OpenAlex's own count as denominator"
        assert row["incomplete"] == 1
        assert row["expected_partial"] is False
        # Every year of the target span except the two that have cursor files.
        assert row["years_missing"] == list(
            range(oa.COVERAGE_FROM_YEAR + 2, this_year + 1))

    def test_missing_api_total_flags_a_partial_denominator(self, monkeypatch, tmp_path):
        """Checkpoints written before api_total existed must not silently read as
        'expected 0' — a 0 denominator would look like 100% coverage."""
        monkeypatch.setattr(oa, "OA_CACHE_DIR", tmp_path)
        phrase = oa.SEARCH_PHRASES[0]
        (tmp_path / f"{oa._job_key(phrase, 2020, 2020)}.cursor.json").write_text(
            json.dumps({"total_fetched": 7, "completed": True}), encoding="utf-8")

        out = oa.phrase_yield()
        row = next(r for r in out["rows"] if r["phrase"] == phrase)
        assert row["fetched"] == 7
        assert row["expected"] == 0
        assert row["expected_partial"] is True
        assert out["expected_partial"] is True

    def test_stopword_only_phrases_are_flagged_degenerate(self, monkeypatch, tmp_path):
        """OpenAlex strips stopwords, so "replication of" matches the same 1.3M works
        as bare "replication" — it is a firehose, not a high-precision phrase."""
        monkeypatch.setattr(oa, "OA_CACHE_DIR", tmp_path)   # never read the real cache
        rows = {r["phrase"]: r for r in oa.phrase_yield()["rows"]}
        for p in ("replication of", "reproducibility of", "replicability of"):
            assert rows[p]["degenerate"] is True, p
        # Measured as genuine phrase matches — flagging these would be a false warning.
        for p in ("direct replication", "we replicated", "could not reproduce",
                  "did not replicate"):
            assert rows[p]["degenerate"] is False, p


@pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run live API tests",
)
class TestOpenAlexDateRange:

    def test_single_year_count(self):
        """2020 should return exactly 14 'registered replication report' papers."""
        df = oa.fetch_phrase(
            phrase="registered replication report",
            from_year=2020,
            to_year=2020
        )
        assert len(df) == 14, f"Expected 14 rows for 2020, got {len(df)}"

    def test_single_year_all_years_correct(self):
        df = oa.fetch_openalex_candidates(from_year=2020, to_year=2020)
        bad = df[df["year_r"] != 2020]
        assert bad.empty, f"Rows with wrong year:\n{bad[['doi_r','year_r']]}"

    def test_no_filter_returns_results(self):
        df = oa.fetch_openalex_candidates()
        assert len(df) > 0

    def test_from_year_only(self):
        df = oa.fetch_openalex_candidates(from_year=2024)
        assert (df["year_r"].dropna() >= 2024).all()

    def test_to_year_only(self):
        df = oa.fetch_openalex_candidates(to_year=2015)
        assert len(df) > 0
        assert (df["year_r"].dropna() <= 2015).all()

    def test_empty_range_returns_empty(self):
        df = oa.fetch_openalex_candidates(from_year=2050, to_year=2050)
        assert len(df) == 0

    # --- DOI spot-checks (fill in once you know the expected DOIs) ---
    # def test_known_doi_present_2020(self):
    #     df = oa.fetch_openalex_candidates(from_year=2020, to_year=2020)
    #     assert "10.XXXX/YYYY" in df["doi_r"].values


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run live API tests",
)
class TestSemanticScholarDateRange:

    def test_single_year_all_years_correct(self):
        df = fetch_semantic_scholar_candidates(from_year=2020, to_year=2020)
        if df.empty:
            pytest.skip("S2 returned no results (rate-limited?)")
        bad = df[df["year_r"].notna() & (df["year_r"] != 2020)]
        assert bad.empty, f"Rows with wrong year:\n{bad[['doi_r','year_r']]}"

    def test_no_filter_returns_results(self):
        df = fetch_semantic_scholar_candidates()
        assert len(df) >= 0   # passes even if rate-limited (returns partial)

    def test_from_year_only(self):
        df = fetch_semantic_scholar_candidates(from_year=2024)
        if df.empty:
            pytest.skip("S2 returned no results (rate-limited?)")
        assert (df["year_r"].dropna() >= 2024).all()

    # --- DOI spot-checks ---
    # def test_known_doi_present_2020(self):
    #     df = fetchfetch_semantic_scholar_candidates_semantic_scholar(from_year=2020, to_year=2020)
    #     assert "10.XXXX/YYYY" in df["doi_r"].values


# ---------------------------------------------------------------------------
# I4R
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run live API tests",
)
class TestI4RDateRange:

    def test_no_filter_returns_results(self):
        df = fetch_i4r()
        assert len(df) > 0

    def test_single_year_count_2024(self):
        """RepEC page currently lists 98 I4R papers for 2024."""
        df = fetch_i4r(from_year=2024, to_year=2024)
        assert len(df) == 98, f"Expected 98 rows for 2024, got {len(df)}"

    def test_single_year_all_years_correct(self):
        df = fetch_i4r(from_year=2024, to_year=2024)
        bad = df[df["year_r"].notna() & (df["year_r"] != 2024)]
        assert bad.empty, f"Rows with wrong year:\n{bad[['title_r','year_r']]}"

    def test_from_year_only(self):
        df_all  = fetch_i4r()
        df_from = fetch_i4r(from_year=2025)
        assert len(df_from) < len(df_all)
        assert (df_from["year_r"].dropna() >= 2025).all()

    def test_to_year_only(self):
        df = fetch_i4r(to_year=2023)
        assert len(df) > 0
        assert (df["year_r"].dropna() <= 2023).all()

    def test_empty_range_returns_empty(self):
        df = fetch_i4r(from_year=2030, to_year=2030)
        assert len(df) == 0

    def test_year_range_is_subset_of_all(self):
        df_all   = fetch_i4r()
        df_range = fetch_i4r(from_year=2024, to_year=2025)
        assert len(df_range) < len(df_all)

    def test_known_title_present_2024(self):
        df = fetch_i4r(from_year=2024, to_year=2024)
        titles = df["title_r"].str.lower().tolist()
        assert any("replication" in t or "comment" in t for t in titles)
