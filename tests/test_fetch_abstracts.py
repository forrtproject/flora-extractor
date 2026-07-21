"""
Tests for search.fetch_abstracts — OpenAlex batch join fix and the Scopus tier.

All HTTP is mocked; no live API calls are made.
"""
import json

import pytest

from search import fetch_abstracts as fa


class DummyResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise fa.requests.HTTPError(f"HTTP {self.status_code}")


# ---------------------------------------------------------------------------
# OpenAlex batch join — regression for the full-URL vs bare-id mismatch bug
# ---------------------------------------------------------------------------

def test_openalex_batch_matches_full_url_ids(monkeypatch):
    """openalex_id_r stores full URLs; the response 'id' is a bare W-id.
    The join must still match and return the abstract keyed by the full URL."""
    payload = {
        "results": [
            {
                "id": "https://openalex.org/W2889412410",
                "abstract_inverted_index": {
                    "This": [0], "is": [1], "the": [2], "abstract": [3],
                },
            }
        ]
    }

    captured = {}

    def fake_get(url, timeout, **kwargs):
        captured["url"] = url
        return DummyResponse(payload)

    monkeypatch.setattr(fa._SESSION, "get", fake_get)

    full_url = "https://openalex.org/W2889412410"
    result = fa._fetch_openalex_batch([full_url])

    # Keyed by the exact input string (the full URL), not the bare id.
    assert result[full_url] == "This is the abstract"
    # Filter is queried with the bare id form.
    assert "ids.openalex:W2889412410" in captured["url"]
    assert "https://openalex.org/W2889412410" not in captured["url"].split("filter=")[1]


def test_openalex_batch_missing_work_stays_none(monkeypatch):
    """An id with no matching result stays None (a genuine miss)."""
    monkeypatch.setattr(fa._SESSION, "get", lambda url, timeout: DummyResponse({"results": []}))
    result = fa._fetch_openalex_batch(["https://openalex.org/W999"])
    assert result == {"https://openalex.org/W999": None}


# ---------------------------------------------------------------------------
# OpenAlex miss recovery — --retry-openalex-misses
# ---------------------------------------------------------------------------

def test_drop_openalex_misses(monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    fa.ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")

    hit = "oa:https://openalex.org/W1"     # real abstract cached — keep
    miss = "oa:https://openalex.org/W2"    # poisoned '__none__' — drop
    doi_line = "doi:10.1/x"                # non-OA — keep

    fa._write_abstract_cache(hit, "A real abstract")
    fa._write_abstract_cache(miss, "__none__")
    fa.CHECKPOINT_PATH.write_text(f"{hit}\n{miss}\n{doi_line}\n", encoding="utf-8")

    dropped = fa._drop_openalex_misses()

    assert dropped == 1
    remaining = fa.CHECKPOINT_PATH.read_text(encoding="utf-8").split()
    assert hit in remaining
    assert doi_line in remaining
    assert miss not in remaining
    # Poisoned cache file is cleared so the fixed batch phase re-fetches it.
    assert not fa._cache_path(miss).exists()
    assert fa._cache_path(hit).exists()


# ---------------------------------------------------------------------------
# Scopus parsing + quota handling
# ---------------------------------------------------------------------------

def test_scopus_parse_abstract():
    payload = {
        "abstracts-retrieval-response": {
            "coredata": {"dc:description": "  A Scopus abstract.  "}
        }
    }
    assert fa._parse_scopus_abstract(payload) == "A Scopus abstract."


def test_scopus_parse_strips_tags_and_handles_missing():
    tagged = {"abstracts-retrieval-response": {"coredata": {"dc:description": "<p>Body</p>"}}}
    assert fa._parse_scopus_abstract(tagged) == "Body"
    assert fa._parse_scopus_abstract({}) is None
    assert fa._parse_scopus_abstract(
        {"abstracts-retrieval-response": {"coredata": {}}}
    ) is None


def test_scopus_fetch_success(monkeypatch):
    payload = {"abstracts-retrieval-response": {"coredata": {"dc:description": "Found it"}}}
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout, headers: DummyResponse(payload))
    abstract, exhausted = fa._fetch_scopus_abstract("10.1/x", "KEY")
    assert abstract == "Found it"
    assert exhausted is False


def test_scopus_fetch_404_is_clean_miss(monkeypatch):
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout, headers: DummyResponse({}, status_code=404))
    abstract, exhausted = fa._fetch_scopus_abstract("10.1/x", "KEY")
    assert abstract is None
    assert exhausted is False


def test_scopus_fetch_quota_exhausted_via_header(monkeypatch):
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        fa._SESSION, "get",
        lambda url, timeout, headers: DummyResponse(
            {}, status_code=429, headers={"X-RateLimit-Remaining": "0"}
        ),
    )
    abstract, exhausted = fa._fetch_scopus_abstract("10.1/x", "KEY")
    assert abstract is None
    assert exhausted is True


def test_scopus_fetch_persistent_429_exhausts(monkeypatch):
    """A 429 without a remaining=0 header retries, then gives up as exhausted."""
    calls = {"n": 0}
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)

    def fake_get(url, timeout, headers):
        calls["n"] += 1
        return DummyResponse({}, status_code=429, headers={})

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    abstract, exhausted = fa._fetch_scopus_abstract("10.1/x", "KEY")
    assert abstract is None
    assert exhausted is True
    assert calls["n"] == 3  # retried 3× per repo convention


# ---------------------------------------------------------------------------
# Phase 4 integration — quota stop leaves later rows retryable
# ---------------------------------------------------------------------------

def test_phase4_stops_on_quota_and_leaves_rows_retryable(monkeypatch, tmp_path):
    import pandas as pd

    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    fa.ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(fa, "CANDIDATES_PATH", tmp_path / "candidates.csv")
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa, "_parquet_path", lambda name: tmp_path / "missing.parquet")
    monkeypatch.setattr(fa, "_dc_refresh", lambda name: None)
    monkeypatch.setenv("ELSEVIER_API_KEY", "KEY")
    monkeypatch.setenv("S2_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", ""],
        "doi_r": ["10.1/a", "10.1/b"],
        "openalex_id_r": ["https://openalex.org/W1", "https://openalex.org/W2"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    # OpenAlex + CrossRef find nothing → both rows fall through to Scopus.
    # Scopus: first DOI returns an abstract; second reports quota exhausted.
    scopus_responses = {
        "10.1/a": DummyResponse(
            {"abstracts-retrieval-response": {"coredata": {"dc:description": "First"}}}
        ),
        "10.1/b": DummyResponse({}, status_code=429, headers={"X-RateLimit-Remaining": "0"}),
    }

    def fake_get(url, timeout=None, headers=None):
        if "openalex.org" in url:
            return DummyResponse({"results": []})           # no OA abstracts
        if "crossref.org" in url:
            return DummyResponse({"message": {}})            # no CrossRef abstract
        doi = url.split("/content/abstract/doi/", 1)[-1]     # elsevier by DOI
        return scopus_responses[doi]

    monkeypatch.setattr(fa._SESSION, "get", fake_get)

    fa.run(scopus_limit=9000)

    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert out.loc[0, "abstract_r"] == "First"
    assert out.loc[1, "abstract_r"] == ""  # quota stopped before filling

    # Checkpoint records the filled row but NOT the quota-blocked one, so it retries.
    done = fa.CHECKPOINT_PATH.read_text(encoding="utf-8")
    assert "scopus:10.1/a" in done
    assert "scopus:10.1/b" not in done


# ---------------------------------------------------------------------------
# Scopus priority ordering — --scopus-priority
# ---------------------------------------------------------------------------

def test_load_scopus_priority(tmp_path):
    pf = tmp_path / "priority.txt"
    pf.write_text("# P1 first\n10.1/B\n\nhttps://doi.org/10.1/a\n10.1/b\n", encoding="utf-8")
    ranks = fa._load_scopus_priority(pf)
    # cleaned, deduplicated, file order preserved
    assert ranks == {"10.1/b": 0, "10.1/a": 1}


def test_phase4_priority_file_reorders_quota(monkeypatch, tmp_path):
    import pandas as pd

    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    fa.ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(fa, "CANDIDATES_PATH", tmp_path / "candidates.csv")
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa, "_parquet_path", lambda name: tmp_path / "missing.parquet")
    monkeypatch.setattr(fa, "_dc_refresh", lambda name: None)
    monkeypatch.setenv("ELSEVIER_API_KEY", "KEY")
    monkeypatch.setenv("S2_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", "", ""],
        "doi_r": ["10.1/a", "10.1/b", "10.1/c"],
        "openalex_id_r": ["", "", ""],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    pf = tmp_path / "priority.txt"
    pf.write_text("10.1/c\n", encoding="utf-8")

    called_dois: list = []

    def fake_get(url, timeout=None, headers=None):
        if "crossref.org" in url:
            return DummyResponse({"message": {}})
        doi = url.split("/content/abstract/doi/", 1)[-1]
        called_dois.append(doi)
        return DummyResponse(
            {"abstracts-retrieval-response": {"coredata": {"dc:description": f"Abs {doi}"}}}
        )

    monkeypatch.setattr(fa._SESSION, "get", fake_get)

    # scopus_limit=1: only the priority DOI gets the quota.
    fa.run(scopus_limit=1, scopus_priority=pf)

    assert called_dois == ["10.1/c"]
    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert out.loc[2, "abstract_r"] == "Abs 10.1/c"
    assert out.loc[0, "abstract_r"] == ""
