"""
Tests for search.fetch_abstracts — OpenAlex batch join fix and the Scopus tier.

All HTTP is mocked; no live API calls are made.
"""
import json

import pytest

from search import fetch_abstracts as fa


class DummyResponse:
    def __init__(self, payload=None, status_code=200, headers=None, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = headers or {}
        # The batch fetchers log resp.text[:200] on a >=400 body, so a real
        # response attribute has to exist or the error path itself raises.
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise fa.requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def no_scopus_key(monkeypatch):
    """Blank the Elsevier key so Phase 5 (Scopus) is skipped and the test exercises
    only the phases above it."""
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "")


# ---------------------------------------------------------------------------
# OpenAlex batch join — regression for the full-URL vs bare-id mismatch bug
# ---------------------------------------------------------------------------

def test_openalex_batch_matches_full_url_ids(monkeypatch):
    """openalex_id_r stores full URLs; the response 'id' is a bare W-id.
    The join must still match and return the abstract keyed by the full URL.
    An id absent from a SUCCESSFUL response is a genuine miss, mapped to None."""
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
    result = fa._fetch_openalex_batch([full_url, "https://openalex.org/W999"])

    # Keyed by the exact input string (the full URL), not the bare id.
    assert result[full_url] == "This is the abstract"
    assert result["https://openalex.org/W999"] is None
    # Filter is queried with the bare id form.
    assert "ids.openalex:W2889412410" in captured["url"]
    assert "https://openalex.org/W2889412410" not in captured["url"].split("filter=")[1]


# ---------------------------------------------------------------------------
# OpenAlex miss recovery — --retry-openalex-misses
# ---------------------------------------------------------------------------

def test_drop_openalex_misses(monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    fa.ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(fa, "FOUND_INDEX_PATH", tmp_path / "found.txt")

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

def test_scopus_parse_strips_tags_and_handles_missing():
    tagged = {"abstracts-retrieval-response": {"coredata": {"dc:description": "<p>Body</p>"}}}
    assert fa._parse_scopus_abstract(tagged) == "Body"
    assert fa._parse_scopus_abstract({}) is None
    assert fa._parse_scopus_abstract(
        {"abstracts-retrieval-response": {"coredata": {}}}
    ) is None


def test_scopus_fetch_404_is_clean_miss(monkeypatch):
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout, headers, **kw: DummyResponse({}, status_code=404))
    abstract, exhausted = fa._fetch_scopus_abstract("10.1/x", "KEY")
    assert abstract is None
    assert exhausted is False


def test_scopus_fetch_quota_exhausted_via_header(monkeypatch):
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        fa._SESSION, "get",
        lambda url, timeout, headers, **kw: DummyResponse(
            {}, status_code=429, headers={"X-RateLimit-Remaining": "0"}
        ),
    )
    abstract, exhausted = fa._fetch_scopus_abstract("10.1/x", "KEY")
    assert abstract is None
    assert exhausted is True


# ---------------------------------------------------------------------------
# Phase 4 integration — quota stop leaves later rows retryable
# ---------------------------------------------------------------------------

def test_phase4_stops_on_quota_and_leaves_rows_retryable(monkeypatch, tmp_path):
    import pandas as pd

    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    fa.ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(fa, "FOUND_INDEX_PATH", tmp_path / "found.txt")
    monkeypatch.setattr(fa, "CANDIDATES_PATH", tmp_path / "candidates.csv")
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa, "_parquet_path", lambda name: tmp_path / "missing.parquet")
    monkeypatch.setattr(fa, "_dc_refresh", lambda name: None)
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "KEY")
    monkeypatch.setattr(fa, "S2_API_KEY", "")

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

    def fake_get(url, timeout=None, headers=None, **kw):
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
    monkeypatch.setattr(fa, "FOUND_INDEX_PATH", tmp_path / "found.txt")
    monkeypatch.setattr(fa, "CANDIDATES_PATH", tmp_path / "candidates.csv")
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa, "_parquet_path", lambda name: tmp_path / "missing.parquet")
    monkeypatch.setattr(fa, "_dc_refresh", lambda name: None)
    monkeypatch.setattr(fa, "ELSEVIER_API_KEY", "KEY")
    monkeypatch.setattr(fa, "S2_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", "", ""],
        "doi_r": ["10.1/a", "10.1/b", "10.1/c"],
        "openalex_id_r": ["", "", ""],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    pf = tmp_path / "priority.txt"
    pf.write_text("10.1/c\n", encoding="utf-8")

    called_dois: list = []

    def fake_get(url, timeout=None, headers=None, **kw):
        if "europepmc" in url:
            return DummyResponse({"resultList": {"result": []}})   # Phase 2: all miss
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


# ---------------------------------------------------------------------------
# Session auth isolation — the OpenAlex Bearer key must not leak to CrossRef/S2/Scopus
# ---------------------------------------------------------------------------

def test_crossref_uses_shared_session_without_auth(monkeypatch):
    """CrossRef fetches must go through the no-auth shared session: it 401s on an
    unknown Bearer token, so the OpenAlex key has to ride on the request instead."""
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
    monkeypatch.setattr(fa, "FOUND_INDEX_PATH", tmp_path / "found.txt")
    monkeypatch.setattr(fa, "CANDIDATES_PATH", tmp_path / "candidates.csv")
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa, "_parquet_path", lambda name: tmp_path / "missing.parquet")
    monkeypatch.setattr(fa, "_dc_refresh", lambda name: None)


def _checkpoint(monkeypatch=None):
    return fa.CHECKPOINT_PATH.read_text(encoding="utf-8") if fa.CHECKPOINT_PATH.exists() else ""


@pytest.mark.parametrize("payload,status_code,expected", [
    # A 200 carrying an abstract: JATS markup is stripped off the way in.
    ({"message": {"abstract": "<jats:p>Real body</jats:p>"}}, 200, ("Real body", "ok")),
    # HTTP 200 with no abstract field, and a 404, are both DEFINITIVE misses.
    ({"message": {}}, 200, (None, "empty")),
    ({}, 404, (None, "empty")),
])
def test_crossref_status_mapping_and_mailto(monkeypatch, payload, status_code, expected):
    """CrossRef maps a response to (abstract, status), and every request carries the
    polite-pool ?mailto= param. (Retry/transient behaviour is pinned at the seam by
    test_request_with_retry_gives_up_as_transient_but_hands_back_4xx.)"""
    captured = {}

    def fake_get(url, timeout=None, **kwargs):
        captured["url"] = url
        return DummyResponse(payload, status_code=status_code)

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    assert fa._fetch_crossref_abstract("10.1/x") == expected
    assert "mailto=" in captured["url"]


def test_crossref_phase_checkpoints_empty_not_transient(monkeypatch, tmp_path, no_scopus_key):
    """The CrossRef phase checkpoints an 'empty' DOI but leaves a 'transient' one
    un-checkpointed so a later run retries it."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", ""],
        "doi_r": ["10.1/empty", "10.1/transient"],
        "openalex_id_r": ["https://openalex.org/W1", "https://openalex.org/W2"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    # OpenAlex and Europe PMC find nothing → both rows fall through to CrossRef.
    def fake_get(url, timeout=None, **kwargs):
        if "api.openalex.org" in url:
            return DummyResponse({"results": []})
        if "europepmc" in url:
            return DummyResponse(_epmc_payload())
        doi = url.split("/works/", 1)[1].split("?", 1)[0]
        if doi == "10.1/empty":
            return DummyResponse({"message": {}})          # definitive miss
        return DummyResponse({}, status_code=429)          # transient

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa.run(scopus_limit=0)

    done = _checkpoint()
    assert "doi:10.1/empty" in done          # definitive miss is checkpointed
    assert "doi:10.1/transient" not in done  # transient is NOT — it retries next run


def test_crossref_circuit_breaker_stops_phase(monkeypatch, tmp_path, no_scopus_key):
    """After N consecutive transient failures the phase breaks; DOIs after the
    break are never requested and never checkpointed."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "TRANSIENT_BREAKER_LIMIT", 3)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

    dois = [f"10.1/{c}" for c in "abcde"]
    df = pd.DataFrame({
        "abstract_r": [""] * 5,
        "doi_r": dois,
        "openalex_id_r": [f"https://openalex.org/W{i}" for i in range(5)],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    requested = set()

    def fake_get(url, timeout=None, **kwargs):
        if "api.openalex.org" in url:
            return DummyResponse({"results": []})
        if "europepmc" in url:
            return DummyResponse(_epmc_payload())
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


def test_openalex_whole_batch_failure_not_checkpointed(monkeypatch, tmp_path, no_scopus_key):
    """A whole-batch HTTP failure returns None and poisons no ids: none of the
    batch's ids are cached or checkpointed, so they all retry next run. This is the
    contract every batched phase shares (Europe PMC and S2 route through the same
    _run_batch_phase)."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", ""],
        "doi_r": ["", ""],           # no DOI → no CrossRef/S2/Scopus fallback
        "openalex_id_r": ["https://openalex.org/W1", "https://openalex.org/W2"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout=None, **kw: DummyResponse({}, status_code=500))
    fa.run(scopus_limit=0)

    assert "oa:" not in _checkpoint()   # batch failure checkpointed nothing
    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert list(out["abstract_r"]) == ["", ""]


def test_openalex_successful_batch_missing_id_is_checkpointed(monkeypatch, tmp_path,
                                                              no_scopus_key):
    """A successful batch where a specific id is absent from the response is a
    DEFINITIVE miss for that id — it must be checkpointed (unlike a batch failure).
    Shared _run_batch_phase contract, so it holds for Europe PMC and S2 too."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

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
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout=None, **kw: DummyResponse(payload))
    fa.run(scopus_limit=0)

    done = _checkpoint()
    assert "oa:https://openalex.org/W1" in done
    assert "oa:https://openalex.org/W2" in done   # absent-in-response = definitive miss
    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert out.loc[0, "abstract_r"] == "Found"
    assert out.loc[1, "abstract_r"] == ""


# ---------------------------------------------------------------------------
# Semantic Scholar batch endpoint (Phase 2) — mirrors the OpenAlex batch tests
# ---------------------------------------------------------------------------

def test_s2_batch_preserves_request_order_no_id_join_needed(monkeypatch):
    """Unlike OpenAlex, S2's batch response is a plain array in request order —
    zip(dois, response) is the whole join, verified against a real shape. A
    whole-batch HTTP failure returns None instead, so the caller checkpoints nothing
    in the batch (the same contract as _fetch_openalex_batch)."""
    captured = {}

    def fake_post(url, params=None, json=None, headers=None, timeout=None, **kw):
        captured["url"] = url
        captured["ids"] = json["ids"]
        captured["headers"] = headers
        return DummyResponse([{"abstract": "Found"}, None])

    monkeypatch.setattr(fa._SESSION, "post", fake_post)
    result = fa._fetch_s2_batch(["10.1/a", "10.1/b"], "KEY")

    assert result == {"10.1/a": "Found", "10.1/b": None}
    assert captured["ids"] == ["DOI:10.1/a", "DOI:10.1/b"]
    assert "semanticscholar.org/graph/v1/paper/batch" in captured["url"]
    assert captured["headers"]["x-api-key"] == "KEY"

    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa._SESSION, "post",
                        lambda *a, **k: DummyResponse({}, status_code=500))
    assert fa._fetch_s2_batch(["10.1/a"], "KEY") is None


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
    monkeypatch.setattr(fa, "S2_API_KEY", "KEY")
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

    def fake_oa_get(url, timeout=None, **kw):
        # Row 0 (W0) resolves via OpenAlex; all other OA lookups empty.
        if "W0" in url:
            return DummyResponse({"results": [
                {"id": "https://openalex.org/W0",
                 "abstract_inverted_index": {"OA": [0], "hit": [1]}},
            ]})
        return DummyResponse({"results": []})

    def fake_get(url, timeout=None, headers=None, **kwargs):
        # One session serves every host now (the OpenAlex key rides on the request,
        # not the session), so this router covers the OpenAlex phase too.
        if "api.openalex.org" in url:
            return fake_oa_get(url, timeout=timeout, **kwargs)
        # Europe PMC (Phase 2) now runs before every other doi-keyed phase, so it
        # sees all four doi rows first. All miss here, so each row still falls
        # through to the single source the test intends to exercise.
        if "europepmc" in url:
            return DummyResponse({"resultList": {"result": []}})
        if "crossref.org" in url:
            doi = url.split("/works/", 1)[1].split("?", 1)[0]
            if doi == "10.1/cr":
                return DummyResponse({"message": {"abstract": "<jats:p>CR hit</jats:p>"}})
            return DummyResponse({"message": {}})                       # CrossRef miss
        # Elsevier Scopus
        doi = url.split("/content/abstract/doi/", 1)[-1]
        if doi == "10.1/sc":
            return DummyResponse(
                {"abstracts-retrieval-response": {"coredata": {"dc:description": "SC hit"}}}
            )
        return DummyResponse({}, status_code=404)                       # Scopus miss

    def fake_post(url, params=None, json=None, headers=None, timeout=None, **kwargs):
        # S2 batch (Phase 2) runs BEFORE CrossRef/Scopus now — every doi-bearing row
        # passes through here first. Only "10.1/s2" hits; everything else must miss
        # so it falls through to CrossRef/Scopus/none as the test intends.
        assert "semanticscholar.org" in url
        requested = [i.split("DOI:", 1)[1] for i in json["ids"]]
        return DummyResponse([
            {"abstract": "S2 hit"} if d == "10.1/s2" else None
            for d in requested
        ])

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    monkeypatch.setattr(fa._SESSION, "post", fake_post)

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


def test_skip_openalex_makes_no_openalex_call_but_still_tries_crossref(monkeypatch, tmp_path,
                                                                        no_scopus_key):
    """--skip-openalex must not touch OpenAlex at all, and must not strand doi_r rows —
    Phase 3 (CrossRef; S2 is skipped here via a blank key) has to see them regardless
    of Phase 1 having run."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r":    [""],
        "doi_r":         ["10.1/only-doi"],
        "openalex_id_r": ["https://openalex.org/W1"],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    def fail_if_called(*a, **k):
        raise AssertionError("OpenAlex must not be called when --skip-openalex is set")
    monkeypatch.setattr(fa, "_fetch_openalex_batch", fail_if_called)
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda *a, **k: DummyResponse({"message": {}}))

    fa.run(scopus_limit=0, skip_openalex=True)   # must not raise

    assert f"oa:{'https://openalex.org/W1'}" not in _checkpoint()
    assert "doi:10.1/only-doi" in _checkpoint(), \
        "Phase 3 (CrossRef) must still process the row even though Phase 1 was skipped"


def test_candidates_parquet_is_never_read_whole(monkeypatch, tmp_path):
    """Guard against the same OOM anti-pattern on the Parquet fast path: a single
    pq.read_table(...).to_pandas() materialises the entire mirror at once (2.5 GB /
    2.58M rows in production — confirmed ArrowMemoryError). _build_worklist must use
    ParquetFile.iter_batches() instead, so pq.read_table must never be called."""
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq_path = tmp_path / "candidates.parquet"
    df = pd.DataFrame({
        "abstract_r":    ["", "has one already", ""],
        "doi_r":         ["10.1/a", "10.1/b", ""],
        "openalex_id_r": ["https://openalex.org/W1", "", ""],
    })
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), pq_path)

    monkeypatch.setattr(fa, "_parquet_path", lambda name: pq_path)

    def guarded_read_table(*a, **k):
        raise AssertionError("pq.read_table must not be called — the Parquet path "
                             "must stream via ParquetFile.iter_batches()")
    monkeypatch.setattr(pq, "read_table", guarded_read_table)

    worklist, total_missing, has_oa, has_doi, _ = fa._build_worklist(dry_run=False, limit=None)

    assert total_missing == 2               # rows with blank abstract_r (rows 0 and 2)
    assert has_oa == 1                      # only row 0 has openalex_id_r
    assert has_doi == 1                     # only row 0 has doi_r; row 2 has neither
    assert {w["doi_r"] for w in worklist} == {"10.1/a", ""}


def test_dry_run_writes_nothing_but_reports_counts(monkeypatch, tmp_path, caplog, no_scopus_key):
    """--dry-run makes no API calls, rewrites no file, but still counts missing
    rows by identifier type."""
    import logging
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

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
    monkeypatch.setattr(fa._SESSION, "get", boom)
    monkeypatch.setattr(fa._SESSION, "post", boom)

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


def test_limit_caps_processing(monkeypatch, tmp_path, no_scopus_key):
    """--limit N caps the worklist to the first N missing rows: only those rows'
    identifiers are fetched/checkpointed."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r":    ["", "", "", ""],
        "doi_r":         ["", "", "", ""],
        "openalex_id_r": [f"https://openalex.org/W{i}" for i in range(4)],
    })
    df.to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    requested = []

    def fake_oa_get(url, timeout=None, **kw):
        requested.append(url)
        return DummyResponse({"results": []})

    monkeypatch.setattr(fa._SESSION, "get", fake_oa_get)
    fa.run(limit=2, scopus_limit=0)

    done = _checkpoint()
    assert "oa:https://openalex.org/W0" in done
    assert "oa:https://openalex.org/W1" in done
    assert "oa:https://openalex.org/W2" not in done   # beyond the limit
    assert "oa:https://openalex.org/W3" not in done
    # Only the first two ids ever reached the OpenAlex batch call.
    joined = " ".join(requested)
    assert "W2" not in joined and "W3" not in joined


# ---------------------------------------------------------------------------
# Europe PMC (Phase 2) — the keyless source that covers the Elsevier/Springer gap
# ---------------------------------------------------------------------------

def _epmc_payload(*records):
    return {"resultList": {"result": list(records)}}


def test_epmc_batch_joins_by_doi_not_by_position(monkeypatch):
    """Europe PMC returns matches unordered and may omit a DOI entirely, so the
    join must be by the 'doi' field. A DOI absent from a successful response is a
    definitive miss, mapped to None."""
    payload = _epmc_payload(
        {"doi": "10.1/c", "abstractText": "third"},
        {"doi": "10.1/a", "abstractText": "first"},
    )
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, params=None, timeout=None: DummyResponse(payload))

    result = fa._fetch_epmc_batch(["10.1/a", "10.1/b", "10.1/c"])
    assert result == {"10.1/a": "first", "10.1/b": None, "10.1/c": "third"}

    # A persistent 5xx returns None instead, so the caller checkpoints nothing —
    # one transient failure must not poison EPMC_BATCH_SIZE DOIs as permanent misses.
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, params=None, timeout=None:
                        DummyResponse({}, status_code=503))
    assert fa._fetch_epmc_batch(["10.1/a", "10.1/b"]) is None


def test_epmc_batch_requests_core_view_and_quotes_dois(monkeypatch):
    """resultType=core is REQUIRED — the 'lite' view omits abstractText, so with it
    every DOI would silently look like a miss. Guard the query shape too."""
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return DummyResponse(_epmc_payload())

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa._fetch_epmc_batch(["10.1/a", "10.1/b"])

    assert "europepmc" in captured["url"]
    assert captured["params"]["resultType"] == "core"
    assert captured["params"]["query"] == 'DOI:"10.1/a" OR DOI:"10.1/b"'


def test_epmc_batch_duplicate_records_keep_first_abstract(monkeypatch):
    """A DOI can match both a preprint and its published record. The first record
    carrying an abstract wins; a later empty one must not overwrite it."""
    monkeypatch.setattr(
        fa._SESSION, "get",
        lambda url, params=None, timeout=None: DummyResponse(_epmc_payload(
            {"doi": "10.1/a", "abstractText": "the good one"},
            {"doi": "10.1/a", "abstractText": ""})))
    assert fa._fetch_epmc_batch(["10.1/a"])["10.1/a"] == "the good one"


def test_epmc_hit_skips_s2_and_crossref_entirely(monkeypatch, tmp_path, no_scopus_key):
    """Ordering check: Europe PMC runs first, so a DOI it resolves must never reach
    the S2 batch endpoint or CrossRef at all — not merely be unaffected by them."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "S2_API_KEY", "KEY")

    pd.DataFrame({
        "abstract_r":    [""],
        "doi_r":         ["10.1016/x"],
        "openalex_id_r": [""],
    }).to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    calls = {"epmc": 0, "crossref": 0, "s2_post": 0}

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        if "europepmc" in url:
            calls["epmc"] += 1
            return DummyResponse(_epmc_payload(
                {"doi": "10.1016/x", "abstractText": "EPMC hit"}))
        if "crossref.org" in url:
            calls["crossref"] += 1
        return DummyResponse({"message": {}})

    def fake_post(*a, **k):
        calls["s2_post"] += 1
        return DummyResponse([])

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    monkeypatch.setattr(fa._SESSION, "post", fake_post)
    fa.run(scopus_limit=0)

    assert calls["epmc"] == 1
    assert calls["s2_post"] == 0, "S2 must not be called for a DOI Europe PMC resolved"
    assert calls["crossref"] == 0, "CrossRef must not be called for a DOI Europe PMC resolved"

    out = pd.read_csv(fa.CANDIDATES_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    assert out.loc[0, "abstract_r"] == "EPMC hit"


def test_dataset_dois_are_excluded_from_every_phase(monkeypatch, tmp_path, no_scopus_key):
    """Dataverse/Zenodo DOIs register datasets, not articles — no abstract exists,
    so they must never reach a fetch phase. They still count as missing."""
    import pandas as pd
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

    pd.DataFrame({
        "abstract_r":    ["", "", ""],
        "doi_r":         ["10.7910/DVN/ABC", "10.5281/zenodo.123", "10.1016/real"],
        "openalex_id_r": ["", "", ""],
    }).to_csv(fa.CANDIDATES_PATH, index=False, encoding="utf-8-sig")

    worklist, total_missing, has_oa, has_doi, n_dataset = fa._build_worklist(
        dry_run=False, limit=None)

    assert total_missing == 3      # all three really are missing an abstract
    assert n_dataset == 2
    assert has_doi == 1
    assert [w["doi_r"] for w in worklist] == ["10.1016/real"]

    queried = []

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        queried.append((params or {}).get("query", url))
        return DummyResponse(_epmc_payload())

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa.run(scopus_limit=0)

    joined = " ".join(queried)
    assert "10.7910" not in joined and "10.5281" not in joined
    assert "10.1016/real" in joined


def test_enrich_abstracts_batches_epmc_first_and_skips_dataset_dois(monkeypatch, tmp_path):
    """The per-row path used by run_search's merge shares the waterfall's ordering:
    Europe PMC is batched first, a DOI it resolves never reaches CrossRef or S2, and
    dataset DOIs are never queried at all."""
    import pandas as pd

    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    fa.ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fa, "FOUND_INDEX_PATH", tmp_path / "found.txt")
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fa, "S2_API_KEY", "")

    df = pd.DataFrame({
        "abstract_r": ["", "", "", "keep me"],
        "doi_r":      ["10.1016/epmc", "10.1/cr", "10.7910/DVN/X", "10.1/untouched"],
    })

    queries, crossref_dois = [], []

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        if "europepmc" in url:
            queries.append(params["query"])
            return DummyResponse({"resultList": {"result": [
                {"doi": "10.1016/epmc", "abstractText": "EPMC hit"}]}})
        crossref_dois.append(url.split("/works/", 1)[1].split("?", 1)[0])
        return DummyResponse({"message": {"abstract": "CR hit"}})

    monkeypatch.setattr(fa._SESSION, "get", fake_get)

    out = fa.enrich_abstracts(df)

    assert out.loc[0, "abstract_r"] == "EPMC hit"
    assert out.loc[1, "abstract_r"] == "CR hit"
    assert out.loc[2, "abstract_r"] == ""          # dataset DOI: nothing to find
    assert out.loc[3, "abstract_r"] == "keep me"   # already had one, untouched

    # One batched Europe PMC call covering both article DOIs, and no dataset DOI.
    assert len(queries) == 1
    assert "10.1016/epmc" in queries[0] and "10.1/cr" in queries[0]
    assert "10.7910" not in queries[0]

    # CrossRef only saw the DOI Europe PMC missed.
    assert crossref_dois == ["10.1/cr"]


# ---------------------------------------------------------------------------
# _request_with_retry — the one retry/Retry-After block behind all five sources
# ---------------------------------------------------------------------------

def test_request_with_retry_gives_up_as_transient_but_hands_back_4xx(monkeypatch):
    """Transient (429/5xx/network/non-JSON-2xx) must end as (None, "transient") so the
    caller declines to checkpoint; a 4xx is the caller's to interpret, not a retry."""
    monkeypatch.setattr(fa.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def send_429():
        calls["n"] += 1
        return DummyResponse({}, status_code=429)

    resp, status = fa._request_with_retry("test", send_429)
    assert (resp, status) == (None, "transient")
    assert calls["n"] == 3

    not_found = DummyResponse({}, status_code=404)
    assert fa._request_with_retry("test", lambda: not_found) == (not_found, "ok")
