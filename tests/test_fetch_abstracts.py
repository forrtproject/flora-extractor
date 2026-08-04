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
    """Redirect the module's cache, checkpoint and found-index into tmp_path."""
    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    fa.ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(fa, "FOUND_INDEX_PATH", tmp_path / "found.txt")
    monkeypatch.setattr(fa.time, "sleep", lambda *_: None)


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


def test_crossref_phase_checkpoints_empty_not_transient(monkeypatch, tmp_path):
    """The item-phase runner checkpoints an 'empty' DOI but leaves a 'transient' one
    un-checkpointed so a later run retries it."""
    _setup_run(monkeypatch, tmp_path)

    def fake_get(url, timeout=None, **kwargs):
        doi = url.split("/works/", 1)[1].split("?", 1)[0]
        if doi == "10.1/empty":
            return DummyResponse({"message": {}})          # definitive miss
        return DummyResponse({}, status_code=429)          # transient

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa._run_item_phase("CrossRef", "doi", ["10.1/empty", "10.1/transient"], 0,
                       fa._fetch_crossref_abstract, set(), 1000)

    done = _checkpoint()
    assert "doi:10.1/empty" in done          # definitive miss is checkpointed
    assert "doi:10.1/transient" not in done  # transient is NOT — it retries next run


def test_crossref_circuit_breaker_stops_phase(monkeypatch, tmp_path):
    """After N consecutive transient failures the phase breaks; DOIs after the
    break are never requested and never checkpointed."""
    _setup_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fa, "TRANSIENT_BREAKER_LIMIT", 3)

    requested = set()

    def fake_get(url, timeout=None, **kwargs):
        requested.add(url.split("/works/", 1)[1].split("?", 1)[0])
        return DummyResponse({}, status_code=429)   # everything transient

    monkeypatch.setattr(fa._SESSION, "get", fake_get)
    fa._run_item_phase("CrossRef", "doi", [f"10.1/{c}" for c in "abcde"], 0,
                       fa._fetch_crossref_abstract, set(), 1000)

    # Breaker trips at 3 consecutive transient rows → later DOIs never touched.
    assert "10.1/a" in requested
    assert "10.1/d" not in requested
    assert "10.1/e" not in requested
    assert "doi:" not in _checkpoint()   # nothing checkpointed at all


def test_openalex_whole_batch_failure_not_checkpointed(monkeypatch, tmp_path):
    """A whole-batch HTTP failure returns None and poisons no ids: none of the
    batch's ids are cached or checkpointed, so they all retry next run. This is the
    contract every batched phase shares (Europe PMC and S2 route through the same
    _run_batch_phase)."""
    _setup_run(monkeypatch, tmp_path)
    ids = ["https://openalex.org/W1", "https://openalex.org/W2"]

    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout=None, **kw: DummyResponse({}, status_code=500))
    found = fa._run_batch_phase("OpenAlex", "oa", ids, 25, 0,
                                fa._fetch_openalex_batch, set())

    assert found == 0
    assert "oa:" not in _checkpoint()   # batch failure checkpointed nothing


def test_openalex_successful_batch_missing_id_is_checkpointed(monkeypatch, tmp_path):
    """A successful batch where a specific id is absent from the response is a
    DEFINITIVE miss for that id — it must be checkpointed (unlike a batch failure).
    Shared _run_batch_phase contract, so it holds for Europe PMC and S2 too."""
    _setup_run(monkeypatch, tmp_path)
    ids = ["https://openalex.org/W1", "https://openalex.org/W2"]

    # Successful response contains only W1; W2 is simply absent.
    payload = {"results": [
        {"id": "https://openalex.org/W1", "abstract_inverted_index": {"Found": [0]}},
    ]}
    monkeypatch.setattr(fa._SESSION, "get",
                        lambda url, timeout=None, **kw: DummyResponse(payload))
    found = fa._run_batch_phase("OpenAlex", "oa", ids, 25, 0,
                                fa._fetch_openalex_batch, set())

    done = _checkpoint()
    assert found == 1
    assert "oa:https://openalex.org/W1" in done
    assert "oa:https://openalex.org/W2" in done   # absent-in-response = definitive miss
    assert fa._read_abstract_cache("oa:https://openalex.org/W1") == "Found"
    assert fa._read_abstract_cache("oa:https://openalex.org/W2") == "__none__"


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
