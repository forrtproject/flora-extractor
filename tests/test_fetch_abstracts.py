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

    def fake_get(url, timeout):
        captured["url"] = url
        return DummyResponse(payload)

    monkeypatch.setattr(fa._OA_SESSION, "get", fake_get)

    full_url = "https://openalex.org/W2889412410"
    result = fa._fetch_openalex_batch([full_url])

    # Keyed by the exact input string (the full URL), not the bare id.
    assert result[full_url] == "This is the abstract"
    # Filter is queried with the bare id form.
    assert "ids.openalex:W2889412410" in captured["url"]
    assert "https://openalex.org/W2889412410" not in captured["url"].split("filter=")[1]


def test_openalex_batch_missing_work_stays_none(monkeypatch):
    """An id with no matching result stays None (a genuine miss)."""
    monkeypatch.setattr(fa._OA_SESSION, "get", lambda url, timeout: DummyResponse({"results": []}))
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
# Session auth isolation — the OpenAlex Bearer key must not leak to CrossRef/S2/Scopus
# ---------------------------------------------------------------------------

def test_openalex_key_does_not_leak_to_shared_session():
    """The shared session (CrossRef/S2/Scopus) must carry no Authorization header;
    only the dedicated OpenAlex session may. A leaked Bearer token makes CrossRef 401."""
    assert "Authorization" not in fa._SESSION.headers
    # If a key is configured, it lives only on the OpenAlex session.
    if fa.OPENALEX_API_KEY:
        assert fa._OA_SESSION.headers.get("Authorization") == f"Bearer {fa.OPENALEX_API_KEY}"


def test_crossref_uses_shared_session_without_auth(monkeypatch):
    """CrossRef fetches must go through the no-auth shared session."""
    captured = {}

    def fake_get(url, timeout=None, **kwargs):
        captured["session_headers"] = dict(fa._SESSION.headers)
        return DummyResponse({"message": {"abstract": "<jats:p>Body</jats:p>"}})

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa._fetch_crossref_abstract("10.1/x")
    assert "Authorization" not in captured["session_headers"]


# ---------------------------------------------------------------------------
# Transient vs definitive misses — the core of this PR
# ---------------------------------------------------------------------------

def _setup_run(monkeypatch, tmp_path):
    """Common run() plumbing: temp cache/checkpoint/candidates, no real sleeps,
    no parquet, no dashboard refresh."""
    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    fa.ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(fa, "CANDIDATES_PATH", tmp_path / "candidates.csv")
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa, "_parquet_path", lambda name: tmp_path / "missing.parquet")
    monkeypatch.setattr(fa, "_dc_refresh", lambda name: None)


def _checkpoint(monkeypatch=None):
    return fa.CHECKPOINT_PATH.read_text(encoding="utf-8") if fa.CHECKPOINT_PATH.exists() else ""


def test_crossref_adds_mailto_and_retries_429_then_ok(monkeypatch):
    """A 429 is transient: retry with backoff, then succeed → ('...', 'ok').
    The request URL carries the polite-pool ?mailto= param."""
    calls = {"n": 0, "url": None}
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)

    def fake_get(url, timeout=None, **kwargs):
        calls["n"] += 1
        calls["url"] = url
        if calls["n"] == 1:
            return DummyResponse({}, status_code=429, headers={"Retry-After": "1"})
        return DummyResponse({"message": {"abstract": "<jats:p>Real body</jats:p>"}})

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    abstract, status = fa._fetch_crossref_abstract("10.1/x")
    assert (abstract, status) == ("Real body", "ok")
    assert calls["n"] == 2  # retried once
    assert "mailto=" in calls["url"]


def test_crossref_persistent_429_is_transient(monkeypatch):
    """Three 429s in a row exhaust retries → (None, 'transient'), NOT a miss."""
    calls = {"n": 0}
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)

    def fake_get(url, timeout=None, **kwargs):
        calls["n"] += 1
        return DummyResponse({}, status_code=429)

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    abstract, status = fa._fetch_crossref_abstract("10.1/x")
    assert (abstract, status) == (None, "transient")
    assert calls["n"] == 3  # 3 attempts per repo convention


def test_crossref_200_no_abstract_is_empty(monkeypatch):
    """HTTP 200 with no abstract field is a DEFINITIVE miss → (None, 'empty')."""
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout=None, **kw: DummyResponse({"message": {}}))
    assert fa._fetch_crossref_abstract("10.1/x") == (None, "empty")


def test_crossref_404_is_empty(monkeypatch):
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout=None, **kw: DummyResponse({}, status_code=404))
    assert fa._fetch_crossref_abstract("10.1/x") == (None, "empty")


def test_crossref_phase_checkpoints_empty_not_transient(monkeypatch, tmp_path):
    """The CrossRef phase checkpoints an 'empty' DOI but leaves a 'transient' one
    un-checkpointed so a later run retries it."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", ""],
        "doi_r": ["10.1/empty", "10.1/transient"],
        "openalex_id_r": ["https://openalex.org/W1", "https://openalex.org/W2"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    # OpenAlex finds nothing → both rows fall through to CrossRef.
    monkeypatch.setattr(fa._OA_SESSION, "get",
                        lambda url, timeout=None: DummyResponse({"results": []}))

    def fake_get(url, timeout=None, **kwargs):
        doi = url.split("/works/", 1)[1].split("?", 1)[0]
        if doi == "10.1/empty":
            return DummyResponse({"message": {}})          # definitive miss
        return DummyResponse({}, status_code=429)          # transient

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa.run(scopus_limit=0)

    done = _checkpoint()
    assert "doi:10.1/empty" in done          # definitive miss is checkpointed
    assert "doi:10.1/transient" not in done  # transient is NOT — it retries next run


def test_crossref_circuit_breaker_stops_phase(monkeypatch, tmp_path):
    """After N consecutive transient failures the phase breaks; DOIs after the
    break are never requested and never checkpointed."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "TRANSIENT_BREAKER_LIMIT", 3)
    monkeypatch.setenv("S2_API_KEY", "")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    dois = [f"10.1/{c}" for c in "abcde"]
    df = pd.DataFrame({
        "abstract_r": [""] * 5,
        "doi_r": dois,
        "openalex_id_r": [f"https://openalex.org/W{i}" for i in range(5)],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    monkeypatch.setattr(fa._OA_SESSION, "get",
                        lambda url, timeout=None: DummyResponse({"results": []}))

    requested = set()

    def fake_get(url, timeout=None, **kwargs):
        doi = url.split("/works/", 1)[1].split("?", 1)[0]
        requested.add(doi)
        return DummyResponse({}, status_code=429)   # everything transient

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa.run(scopus_limit=0)

    # Breaker trips at 3 consecutive transient rows → later DOIs never touched.
    assert "10.1/a" in requested
    assert "10.1/d" not in requested
    assert "10.1/e" not in requested
    assert "doi:" not in _checkpoint()   # nothing checkpointed at all


def test_openalex_whole_batch_failure_not_checkpointed(monkeypatch, tmp_path):
    """A whole-batch HTTP failure returns None and poisons no ids: none of the
    batch's ids are cached or checkpointed, so they all retry next run."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", ""],
        "doi_r": ["", ""],           # no DOI → no CrossRef/S2/Scopus fallback
        "openalex_id_r": ["https://openalex.org/W1", "https://openalex.org/W2"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    monkeypatch.setattr(fa._OA_SESSION, "get",
                        lambda url, timeout=None: DummyResponse({}, status_code=500))
    fa.run(scopus_limit=0)

    assert "oa:" not in _checkpoint()   # batch failure checkpointed nothing
    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert list(out["abstract_r"]) == ["", ""]


def test_openalex_successful_batch_missing_id_is_checkpointed(monkeypatch, tmp_path):
    """A successful batch where a specific id is absent from the response is a
    DEFINITIVE miss for that id — it must be checkpointed (unlike a batch failure)."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", ""],
        "doi_r": ["", ""],
        "openalex_id_r": ["https://openalex.org/W1", "https://openalex.org/W2"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    # Successful response contains only W1; W2 is simply absent.
    payload = {"results": [
        {"id": "https://openalex.org/W1", "abstract_inverted_index": {"Found": [0]}},
    ]}
    monkeypatch.setattr(fa._OA_SESSION, "get",
                        lambda url, timeout=None: DummyResponse(payload))
    fa.run(scopus_limit=0)

    done = _checkpoint()
    assert "oa:https://openalex.org/W1" in done
    assert "oa:https://openalex.org/W2" in done   # absent-in-response = definitive miss
    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert out.loc[0, "abstract_r"] == "Found"
    assert out.loc[1, "abstract_r"] == ""


def test_s2_persistent_429_is_transient_and_not_checkpointed(monkeypatch, tmp_path):
    """S2 429 is transient (was previously a silent miss). The phase leaves it
    un-checkpointed so it retries."""
    import pandas as pd

    # Unit-level: the fetch returns ('', 'transient') style tuple.
    calls = {"n": 0}
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout=None, headers=None: (calls.update(n=calls["n"] + 1)
                        or DummyResponse({}, status_code=429)))
    abstract, status = fa._fetch_s2_abstract("10.1/x", "KEY")
    assert (abstract, status) == (None, "transient")
    assert calls["n"] == 3

    # Phase-level: transient S2 row is not checkpointed as an s2 miss.
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "KEY")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": [""],
        "doi_r": ["10.1/x"],
        "openalex_id_r": ["https://openalex.org/W1"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    monkeypatch.setattr(fa._OA_SESSION, "get",
                        lambda url, timeout=None: DummyResponse({"results": []}))

    def fake_get(url, timeout=None, headers=None, **kwargs):
        if "crossref.org" in url:
            return DummyResponse({"message": {}})            # CrossRef empty (checkpointed)
        return DummyResponse({}, status_code=429)            # S2 transient

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa.run(scopus_limit=0)

    done = _checkpoint()
    assert "doi:10.1/x" in done       # CrossRef definitive miss checkpointed
    assert "s2:10.1/x" not in done    # S2 transient NOT checkpointed — retries next run


# ---------------------------------------------------------------------------
# Streamed worklist + streamed write-back merge (issue #65 — bounded memory)
# ---------------------------------------------------------------------------

def test_run_fills_from_cache_by_key_priority(monkeypatch, tmp_path):
    """End-to-end run() with all HTTP mocked. Each row's abstract is recovered by
    exactly one source; the streamed merge fills the OUTPUT candidates.csv from the
    right cache key, in oa → doi → s2 → scopus priority. A row whose only hit is a
    cached '__none__' stays empty. Column order + utf-8-sig BOM header preserved."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "KEY")
    monkeypatch.setenv("ELSEVIER_API_KEY", "KEY")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "KEY")

    # Five rows exercising each key type + a definitive miss. Column order is
    # deliberately not the identifier-first order so we can assert it survives.
    df = pd.DataFrame({
        "title_r":       ["t0", "t1", "t2", "t3", "t4"],
        "abstract_r":    ["", "", "", "", ""],
        "doi_r":         ["10.1/oa", "10.1/cr", "10.1/s2", "10.1/sc", "10.1/none"],
        "openalex_id_r": ["https://openalex.org/W0", "", "", "", ""],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    def fake_oa_get(url, timeout=None):
        # Row 0 (W0) resolves via OpenAlex; all other OA lookups empty.
        if "W0" in url:
            return DummyResponse({"results": [
                {"id": "https://openalex.org/W0",
                 "abstract_inverted_index": {"OA": [0], "hit": [1]}},
            ]})
        return DummyResponse({"results": []})

    monkeypatch.setattr(fa._OA_SESSION, "get", fake_oa_get)

    def fake_get(url, timeout=None, headers=None, **kwargs):
        if "crossref.org" in url:
            doi = url.split("/works/", 1)[1].split("?", 1)[0]
            if doi == "10.1/cr":
                return DummyResponse({"message": {"abstract": "<jats:p>CR hit</jats:p>"}})
            return DummyResponse({"message": {}})                       # CrossRef miss
        if "semanticscholar.org" in url:
            doi = url.rsplit("DOI:", 1)[1].split("?", 1)[0]
            if doi == "10.1/s2":
                return DummyResponse({"abstract": "S2 hit"})
            return DummyResponse({"abstract": None})                    # S2 miss
        # Elsevier Scopus
        doi = url.split("/content/abstract/doi/", 1)[-1]
        if doi == "10.1/sc":
            return DummyResponse(
                {"abstracts-retrieval-response": {"coredata": {"dc:description": "SC hit"}}}
            )
        return DummyResponse({}, status_code=404)                       # Scopus miss

    monkeypatch.setattr(fa._SESSION, "get", fake_get)

    fa.run(scopus_limit=9000)

    # Header must keep the BOM; column order must be identical to the input.
    raw = fa.CANDIDATES_PATH.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert list(out.columns) == ["title_r", "abstract_r", "doi_r", "openalex_id_r"]
    assert out.loc[0, "abstract_r"] == "OA hit"
    assert out.loc[1, "abstract_r"] == "CR hit"
    assert out.loc[2, "abstract_r"] == "S2 hit"
    assert out.loc[3, "abstract_r"] == "SC hit"
    assert out.loc[4, "abstract_r"] == ""      # only a '__none__' cache hit → stays empty


def test_run_respects_cache_key_priority_over_lower_tiers(monkeypatch, tmp_path):
    """When more than one key is cached for a row, the oa key wins over doi."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r":    [""],
        "doi_r":         ["10.1/x"],
        "openalex_id_r": ["https://openalex.org/W1"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    # Pre-seed both an oa hit and a doi hit; oa must win in the merge.
    fa._write_abstract_cache("oa:https://openalex.org/W1", "OA wins")
    fa._write_abstract_cache("doi:10.1/x", "DOI loses")
    # Both identifiers already checkpointed so no phase re-fetches.
    fa.CHECKPOINT_PATH.write_text("oa:https://openalex.org/W1\ndoi:10.1/x\n", encoding="utf-8")

    monkeypatch.setattr(fa._OA_SESSION, "get",
                        lambda url, timeout=None: DummyResponse({"results": []}))
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda *a, **k: DummyResponse({"message": {}}))

    fa.run(scopus_limit=0)

    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert out.loc[0, "abstract_r"] == "OA wins"


def test_candidates_csv_is_never_read_whole(monkeypatch, tmp_path):
    """Guard against the OOM anti-pattern: every pd.read_csv of candidates.csv must
    pass a chunksize (streamed), never an unchunked full-file read."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r":    ["", ""],
        "doi_r":         ["", ""],
        "openalex_id_r": ["https://openalex.org/W1", "https://openalex.org/W2"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    real_read_csv = pd.read_csv

    def guarded_read_csv(path, *args, **kwargs):
        if str(path) == str(fa.CANDIDATES_PATH):
            assert "chunksize" in kwargs, "candidates.csv must be read in chunks"
        return real_read_csv(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", guarded_read_csv)
    monkeypatch.setattr(fa._OA_SESSION, "get",
                        lambda url, timeout=None: DummyResponse({"results": []}))

    fa.run(scopus_limit=0)   # must not raise the assertion


def test_dry_run_writes_nothing_but_reports_counts(monkeypatch, tmp_path, caplog):
    """--dry-run makes no API calls, rewrites no file, but still counts missing
    rows by identifier type."""
    import logging
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r":    ["", "", "present"],
        "doi_r":         ["10.1/a", "", ""],
        "openalex_id_r": ["https://openalex.org/W1", "https://openalex.org/W2", ""],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")
    before = fa.CANDIDATES_PATH.read_bytes()

    # Any HTTP call under dry-run is a bug.
    def boom(*a, **k):
        raise AssertionError("no API calls under --dry-run")
    monkeypatch.setattr(fa._OA_SESSION, "get", boom)
    monkeypatch.setattr(fa._SESSION, "get", boom)

    with caplog.at_level(logging.INFO):
        fa.run(dry_run=True)

    # File untouched, no tmp left behind, no checkpoint written.
    assert fa.CANDIDATES_PATH.read_bytes() == before
    assert not (fa.CANDIDATES_PATH.parent / (fa.CANDIDATES_PATH.name + ".tmp")).exists()
    assert not fa.CHECKPOINT_PATH.exists()
    text = caplog.text
    assert "Rows missing abstract: 2" in text
    assert "with openalex_id_r: 2" in text
    assert "with doi_r:         1" in text


def test_limit_caps_processing(monkeypatch, tmp_path):
    """--limit N caps the worklist to the first N missing rows: only those rows'
    identifiers are fetched/checkpointed."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setenv("S2_API_KEY", "")
    monkeypatch.setenv("ELSEVIER_API_KEY", "")
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r":    ["", "", "", ""],
        "doi_r":         ["", "", "", ""],
        "openalex_id_r": [f"https://openalex.org/W{i}" for i in range(4)],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    requested = []

    def fake_oa_get(url, timeout=None):
        requested.append(url)
        return DummyResponse({"results": []})

    monkeypatch.setattr(fa._OA_SESSION, "get", fake_oa_get)
    fa.run(limit=2, scopus_limit=0)

    done = _checkpoint()
    assert "oa:https://openalex.org/W0" in done
    assert "oa:https://openalex.org/W1" in done
    assert "oa:https://openalex.org/W2" not in done   # beyond the limit
    assert "oa:https://openalex.org/W3" not in done
    # Only the first two ids ever reached the OpenAlex batch call.
    joined = " ".join(requested)
    assert "W2" not in joined and "W3" not in joined
