"""
Tests for shared.pdf_sources — the acquire_pdf tier waterfall.

All network access is mocked; no live API calls are made.
Run:  python -m pytest tests/test_pdf_sources.py -v
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import shared.pdf_sources as ps


@pytest.fixture(autouse=True)
def _pdf_cache_in_tmp(tmp_path, monkeypatch):
    """Keep the per-DOI retry log out of the real cache.

    acquire_pdf now records which tiers came back empty, under PDF_CACHE_DIR. Writing
    those into cache/pdfs would both pollute a working checkout and make the suite
    order-dependent: the second test to use a DOI would find every tier held back.
    """
    monkeypatch.setattr(ps, "PDF_CACHE_DIR", tmp_path)


def _retry_log(doi: str) -> dict:
    from shared.utils import cache_key
    path = ps.PDF_CACHE_DIR / f"retry_{cache_key(ps.clean_doi(doi))}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_retry_log(doi: str, entries: dict) -> None:
    from shared.utils import cache_key
    path = ps.PDF_CACHE_DIR / f"retry_{cache_key(ps.clean_doi(doi))}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


_ALL_TIERS = [
    "get_openalex_fulltext", "get_arxiv_pdf_url", "get_osf_pdf_url",
    "get_openalex_locations", "get_datacite_urls", "get_all_unpaywall_pdf_urls",
    "get_semanticscholar_pdf_url", "get_core_pdf_url", "get_europepmc_pmcid",
    "get_europepmc_fulltext",
    "scrape_pdf_from_landing_page", "get_serpapi_pdf_url",
    "list_osf_files", "crossref_reviewed_doi", "crossref_title_matches",
    "crossref_title",
]

# The tier lookups that answer with a list; everything else answers with a scalar or
# None. A default of None where a list is expected would raise rather than miss.
_LIST_TIERS = {"get_all_unpaywall_pdf_urls", "get_openalex_locations",
               "get_datacite_urls", "scrape_pdf_from_landing_page",
               "list_osf_files", "crossref_title_matches"}


def _mock_all_tiers(**overrides):
    """Patch every tier lookup to a no-hit default, then apply *overrides*.

    Returns the list of active context managers' mocks keyed by name, so a test
    can assert on call counts without hand-patching every tier lookup each time.
    """
    patchers = {}
    for name in _ALL_TIERS:
        default = [] if name in _LIST_TIERS else (
            "" if name in ("crossref_reviewed_doi", "crossref_title") else None)
        patchers[name] = patch.object(ps, name, return_value=overrides.get(name, default))
    return patchers


def test_unpaywall_not_queried_when_an_earlier_tier_already_won():
    """Every tier but Unpaywall was guarded by `if not dl["success"]`. A DOI already
    served by arXiv/OSF/OpenAlex still cost an Unpaywall round-trip on every run."""
    with patch.object(ps, "get_openalex_fulltext", return_value=None), \
         patch.object(ps, "get_arxiv_pdf_url", return_value="https://arxiv.org/pdf/2301.1"), \
         patch.object(ps, "get_all_unpaywall_pdf_urls", return_value=[]) as uw, \
         patch.object(ps, "download_pdf",
                      return_value={"success": True, "path": "/tmp/x.pdf",
                                    "source": "download", "reason": ""}):
        out = ps.acquire_pdf("10.48550/arXiv.2301.00001", "A Title")

    assert out["pdf_ok"] is True
    assert out["pdf_source"] == "arxiv"
    uw.assert_not_called()


def test_unpaywall_still_queried_when_earlier_tiers_miss():
    """The guard must not disable the tier — a DOI the cheap tiers miss still
    reaches Unpaywall."""
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "get_pdf_via_playwright",
                          return_value={"success": False, "path": None,
                                        "source": "", "reason": "no_pdf"}), \
             patch.object(ps, "download_pdf",
                          return_value={"success": False, "path": None,
                                        "source": "", "reason": "not_a_pdf"}):
            out = ps.acquire_pdf("10.1016/j.example.2020.01.001", "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    started["get_all_unpaywall_pdf_urls"].assert_called_once()
    assert out["pdf_ok"] is False
    assert out["pdf_source"] == "none"


# ── The negative-acquisition cache ────────────────────────────────────────────

_NO_PDF = {"success": False, "path": None, "source": "", "reason": "not_a_pdf"}
_NO_PLAYWRIGHT = {"success": False, "path": None, "source": "",
                  "reason": "playwright_no_pdf_found"}


def _run_all_tiers_missing(doi: str, **tier_returns):
    """Run acquire_pdf with every tier mocked to a miss, returning (out, mocks)."""
    patchers = _mock_all_tiers(**tier_returns)
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "get_pdf_via_playwright",
                          return_value=_NO_PLAYWRIGHT) as pw, \
             patch.object(ps, "download_pdf", return_value=_NO_PDF), \
             patch.object(ps, "SERPAPI_KEYS", ["k"]):
            out = ps.acquire_pdf(doi, "A Title")
    finally:
        for p in patchers.values():
            p.stop()
    started["get_pdf_via_playwright"] = pw
    return out, started


def test_a_tier_that_failed_inside_the_ttl_is_not_re_probed():
    """1,048 of the 1,061 target_pending rows have no PDF and every run re-paid the
    whole waterfall. A tier that came back empty is held for PDF_RETRY_AFTER_DAYS."""
    doi = "10.1016/j.example.2020.01.002"
    out, mocks = _run_all_tiers_missing(doi)
    assert out["pdf_ok"] is False
    recorded = _retry_log(doi)
    assert {"openalex_oa", "unpaywall_pdf", "semanticscholar", "core",
            "europepmc", "landing", "serpapi", "playwright"} <= set(recorded)

    out2, mocks2 = _run_all_tiers_missing(doi)
    assert out2["pdf_ok"] is False
    for name in ("get_openalex_locations", "get_all_unpaywall_pdf_urls",
                 "get_semanticscholar_pdf_url", "get_core_pdf_url",
                 "get_europepmc_pmcid", "get_serpapi_pdf_url",
                 "get_pdf_via_playwright"):
        mocks2[name].assert_not_called()


def test_a_tier_is_re_probed_once_the_ttl_lapses():
    """The record is a retry delay, not a verdict: a paper deposited last week is
    found next run."""
    doi = "10.1016/j.example.2020.01.003"
    stale = _ago(ps.PDF_RETRY_AFTER_DAYS + 1)
    _write_retry_log(doi, {tier: stale for tier in
                           ("openalex_oa", "unpaywall_pdf", "semanticscholar", "core",
                            "europepmc", "landing", "serpapi", "playwright")})

    _, mocks = _run_all_tiers_missing(doi)
    mocks["get_openalex_locations"].assert_called_once()
    mocks["get_semanticscholar_pdf_url"].assert_called_once()
    mocks["get_pdf_via_playwright"].assert_called_once()


def test_a_tier_skipped_for_a_missing_key_is_not_recorded_as_failed():
    """SerpAPI without a key and Playwright without the package were never asked —
    a key or a pip install next week must take effect immediately."""
    doi = "10.1016/j.example.2020.01.004"
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "get_pdf_via_playwright",
                          return_value={"success": False, "path": None, "source": "",
                                        "reason": "playwright_not_installed"}), \
             patch.object(ps, "download_pdf", return_value=_NO_PDF), \
             patch.object(ps, "SERPAPI_KEYS", []):
            ps.acquire_pdf(doi, "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    recorded = _retry_log(doi)
    assert "serpapi" not in recorded
    assert "playwright" not in recorded
    started["get_serpapi_pdf_url"].assert_not_called()
    assert "core" in recorded          # the tiers that were actually asked ARE recorded


def test_a_successful_acquisition_clears_the_record():
    doi = "10.1016/j.example.2020.01.005"
    _write_retry_log(doi, {"core": _ago(1)})
    with patch.object(ps, "get_openalex_fulltext", return_value=None), \
         patch.object(ps, "get_arxiv_pdf_url", return_value="https://arxiv.org/pdf/2301.1"), \
         patch.object(ps, "download_pdf",
                      return_value={"success": True, "path": "/tmp/x.pdf",
                                    "source": "download", "reason": ""}):
        out = ps.acquire_pdf(doi, "A Title")
    assert out["pdf_ok"] is True
    assert _retry_log(doi) == {}


def test_an_unreadable_record_probes_everything():
    doi = "10.1016/j.example.2020.01.006"
    from shared.utils import cache_key
    (ps.PDF_CACHE_DIR / f"retry_{cache_key(doi)}.json").write_text("{not json",
                                                                  encoding="utf-8")
    _, mocks = _run_all_tiers_missing(doi)
    mocks["get_core_pdf_url"].assert_called_once()


# ── The per-URL record of a dead URL ──────────────────────────────────────────

class _Resp:
    def __init__(self, status: int, body: bytes = b""):
        self.status_code = status
        self._body = body
        self.content = body
        self.text = body.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=0):
        yield self._body

    def json(self):
        return json.loads(self._body.decode("utf-8"))


def _url_record(url: str) -> dict:
    path = ps._url_failure_path(url)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.mark.parametrize("status", [404, 410])
def test_a_url_that_answered_gone_is_not_re_fetched(status):
    """download_pdf cached only successes, so a permanently dead URL was re-fetched
    once per tier retry window."""
    url = f"https://example.org/dead-{status}.pdf"
    with patch.object(ps.requests, "get", return_value=_Resp(status)) as get:
        first = ps.download_pdf(url, doi=f"10.1/gone{status}")
    assert first["reason"] == f"http_{status}"
    assert list(_url_record(url)) == [f"http_{status}"]

    with patch.object(ps.requests, "get") as get2:
        second = ps.download_pdf(url, doi=f"10.1/gone{status}")
    assert second["reason"] == "url_gone"
    get2.assert_not_called()


@pytest.mark.parametrize("failure", [
    _Resp(503), _Resp(429), _Resp(403),
])
def test_a_transient_or_refused_response_is_not_recorded(failure):
    """A server that failed to answer, or refused to serve a document that exists, is
    not evidence of absence — recording it would checkpoint it as a definitive miss."""
    url = f"https://example.org/soft-{failure.status_code}.pdf"
    with patch.object(ps.requests, "get", return_value=failure):
        ps.download_pdf(url, doi="10.1/soft")
    assert _url_record(url) == {}

    with patch.object(ps.requests, "get", return_value=failure) as get2:
        ps.download_pdf(url, doi="10.1/soft")
    get2.assert_called_once()


def test_a_connection_error_is_not_recorded():
    url = "https://example.org/timeout.pdf"
    with patch.object(ps.requests, "get", side_effect=OSError("timed out")):
        out = ps.download_pdf(url, doi="10.1/timeout")
    assert out["success"] is False
    assert _url_record(url) == {}


def test_a_dead_url_is_re_fetched_once_the_window_lapses():
    """The record is a retry delay on the same window as the per-tier one."""
    url = "https://example.org/lapsed.pdf"
    ps._write_retry_log(ps._url_failure_path(url),
                        {"http_404": _ago(ps.PDF_RETRY_AFTER_DAYS + 1)})
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, b"%PDF-1.4" + b"x" * 10_000)) as get:
        out = ps.download_pdf(url, doi="10.1/lapsed")
    get.assert_called_once()
    assert out["success"] is True
    assert _url_record(url) == {}          # the URL serves a document after all


# ── The PDF already on disk ───────────────────────────────────────────────────

def _save_pdf(doi: str) -> None:
    ps.pdf_cache_path(ps.clean_doi(doi)).write_bytes(b"%PDF-1.4" + b"x" * 10_000)


def test_a_pdf_already_on_disk_returns_before_any_tier_runs():
    """The short-circuit used to happen inside the winning tier's download_pdf, so it
    cost every URL lookup above that tier. The saved provenance is replayed."""
    doi = "10.1016/j.example.2020.01.009"
    _save_pdf(doi)
    ps._write_provenance(ps.clean_doi(doi), "unpaywall_pdf", "https://x/y.pdf")

    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "download_pdf") as dl:
            out = ps.acquire_pdf(doi, "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_ok"] is True
    assert out["pdf_source"] == "unpaywall_pdf"
    assert out["pdf_url"] == "https://x/y.pdf"
    assert out["pdf_url_tried"] == ["https://x/y.pdf"]
    assert out["pdf_path"] == str(ps.pdf_cache_path(ps.clean_doi(doi)))
    dl.assert_not_called()
    for name in ("get_arxiv_pdf_url", "get_osf_pdf_url", "get_openalex_locations",
                 "get_all_unpaywall_pdf_urls"):
        started[name].assert_not_called()


def test_a_saved_pdf_without_provenance_still_runs_the_waterfall():
    """Nothing recorded the tier before this record existed; those PDFs keep the old
    behaviour — the waterfall runs, its first cache hit writes the record."""
    doi = "10.1016/j.example.2020.01.010"
    _save_pdf(doi)
    patchers = _mock_all_tiers(get_openalex_locations=[
        {"url": "https://x/z.pdf", "type": "pdf", "host": "", "license": ""}])
    started = {name: p.start() for name, p in patchers.items()}
    try:
        out = ps.acquire_pdf(doi, "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    started["get_openalex_locations"].assert_called_once()
    assert out["pdf_ok"] is True
    assert out["pdf_source"] == "openalex_oa"
    # No name: only the OSF file tier knows one, and this document came from OpenAlex.
    assert ps._read_provenance(ps.clean_doi(doi)) == {"source": "openalex_oa",
                                                      "url": "https://x/z.pdf",
                                                      "name": ""}


# ── Tier 0 short-circuit ──────────────────────────────────────────────────────

def test_openalex_xml_with_content_skips_the_download_tiers():
    """A GROBID-XML result with content IS the document — link_original parses it the
    same way it parses a PDF, so the ten download tiers underneath it buy nothing."""
    xml = {"source": "openalex_xml", "xml_url": "u",
           "sections": {"abstract": "We replicated it.", "intro": "", "methods": "",
                        "references": [{"title": "r"}]}}
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    started["get_openalex_fulltext"].return_value = xml
    try:
        with patch.object(ps, "get_pdf_via_playwright", return_value=_NO_PLAYWRIGHT) as pw, \
             patch.object(ps, "download_pdf", return_value=_NO_PDF) as dl:
            out = ps.acquire_pdf("10.1016/j.example.2020.01.007", "A Title",
                                 openalex_id="W1")
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_source"] == "openalex_xml"
    assert out["openalex_xml"] == xml
    assert out["pdf_path"] is None and out["pdf_ok"] is False
    assert out["pdf_url_tried"] == []
    dl.assert_not_called()
    pw.assert_not_called()
    for name in ("get_arxiv_pdf_url", "get_all_unpaywall_pdf_urls",
                 "get_semanticscholar_pdf_url", "get_serpapi_pdf_url"):
        started[name].assert_not_called()


def test_an_xml_success_clears_the_retry_record_too():
    """An XML with content is a document: if its cache is later lost, the download
    tiers must be probeable immediately, not held for the rest of the window."""
    doi = "10.1016/j.example.2020.01.008"
    _write_retry_log(doi, {"core": _ago(1), "playwright": _ago(1)})
    xml = {"source": "openalex_xml", "xml_url": "u",
           "sections": {"abstract": "We replicated it.", "intro": "", "methods": "",
                        "references": [{"title": "r"}]}}
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    started["get_openalex_fulltext"].return_value = xml
    try:
        out = ps.acquire_pdf(doi, "A Title", openalex_id="W1")
    finally:
        for p in patchers.values():
            p.stop()
    assert out["pdf_source"] == "openalex_xml"
    assert _retry_log(doi) == {}


# ── Word documents ────────────────────────────────────────────────────────────
# OSF serves whatever the author uploaded: of twelve campaign preprint DOIs probed on
# 2026-08-08, five answered with a PDF and seven with a Word file. The %PDF test
# discarded all seven as "no document".

def _docx_bytes(body: str) -> bytes:
    """A minimal Word file — a ZIP whose word/document.xml holds *body* as paragraphs.

    Built in-test rather than kept as a fixture so the suite carries no binary and no
    Word library; the real files differ only in how much else is in the ZIP.
    """
    import io
    import zipfile
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paras = "".join(f'<w:p><w:r><w:t>{line}</w:t></w:r></w:p>'
                    for line in body.splitlines() if line)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>'
                   f'{paras}</w:body></w:document>')
    return buf.getvalue()


def _pad(content: bytes) -> bytes:
    """Past _MIN_PDF_BYTES, which every writing tier enforces on any format."""
    return content + b"\x00" * max(0, 6_000 - len(content))


def test_a_word_download_is_saved_as_a_document():
    url  = "https://osf.io/download/wordy/"
    body = "\n".join(f"Paragraph {i} of a replication report." for i in range(60))
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, _pad(_docx_bytes(body)))):
        out = ps.download_pdf(url, doi="10.31219/osf.io/wordy")

    assert out["success"] is True
    # Saved under its own suffix: the parsers dispatch on it, and pdfminer cannot
    # read a file that only looks like a PDF because of where it was written.
    assert out["path"].suffix == ".docx"
    assert ps.cached_pdf("10.31219/osf.io/wordy") == out["path"]


def test_a_pdf_download_is_unchanged():
    url = "https://example.org/paper.pdf"
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, b"%PDF-1.4" + b"x" * 10_000)):
        out = ps.download_pdf(url, doi="10.1/still-a-pdf")
    assert out["success"] is True
    assert out["path"].suffix == ".pdf"


@pytest.mark.parametrize("content, reason", [
    pytest.param(_docx_bytes("A Title\nAn Author"), "docx_no_content", id="trivial_docx"),
    pytest.param(b"PK\x03\x04not-a-word-file", "not_a_pdf", id="some_other_zip"),
])
def test_a_word_file_with_no_paper_in_it_is_refused(content, reason):
    """The content check that separates a document from a record of one applies to a
    Word file too — and the ZIP magic alone is any ZIP, an epub or a data archive
    included."""
    url = f"https://osf.io/download/{reason}/"
    with patch.object(ps.requests, "get", return_value=_Resp(200, _pad(content))):
        out = ps.download_pdf(url, doi=f"10.31219/osf.io/{reason}")
    assert out["success"] is False
    assert out["reason"] == reason
    assert ps.cached_pdf(f"10.31219/osf.io/{reason}") is None


@pytest.mark.parametrize("doi, expected", [
    ("10.31234/osf.io/8cqpk",    "https://osf.io/download/8cqpk/"),
    ("10.31219/osf.io/zr7a5",    "https://osf.io/download/zr7a5/"),
    # The version suffix stays: the bare guid serves the LATEST version, which for
    # d3x9p is a different file from the one the DOI names.
    ("10.31234/osf.io/d3x9p_v1", "https://osf.io/download/d3x9p_v1/"),
    # The registration registrant — osf.io/download answers HTTP 500 for those, and
    # Tier 0b reads them through the API instead.
    ("10.17605/osf.io/tp32p",    None),
    ("10.1016/j.example.2020.1", None),
])
def test_osf_preprint_download_urls(doi, expected):
    assert ps.get_osf_pdf_url(doi) == expected


# ── The row's own URL ─────────────────────────────────────────────────────────
# Every other tier derives its URL from the DOI through an external index. For a row
# whose registrant is not in Crossref the whole chain has nothing to say, while the
# row itself carries a direct link — and that link was never fetched.

def test_the_rows_own_url_is_tried_before_any_index():
    doi = "10.15456/j1.2025082.1931045150"
    url = "https://www.econstor.eu/bitstream/10419/318610/1/JCRE-2025-2.pdf"
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps.requests, "get",
                          return_value=_Resp(200, b"%PDF-1.4" + b"x" * 10_000)):
            out = ps.acquire_pdf(doi, "A Title", url_r=url)
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_ok"] is True
    assert out["pdf_url"] == url
    # Its own name, because it is a distinct acquisition route: not the Unpaywall
    # landing scrape, and not the HTML tier at the bottom.
    assert out["pdf_source"] == "row_url"
    for name, mock in started.items():
        assert not mock.called, f"{name} was consulted although the row's URL served"


def test_a_row_url_that_is_a_web_page_falls_through():
    """An HTML answer is a tier failure, not a document and not a verdict on the URL:
    the DOI-keyed tiers still run, and Tier 11 can still argue for the page."""
    url = "https://repo.example.org/item/1"
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps.requests, "get",
                          return_value=_Resp(200, b"<html>a record page</html>")), \
             patch.object(ps, "get_pdf_via_playwright", return_value=_NO_PLAYWRIGHT), \
             patch.object(ps, "get_html_document", return_value=None) as html:
            out = ps.acquire_pdf("10.1016/j.example.2020.01.011", "A Title",
                                 url_r=url)
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_ok"] is False
    assert out["pdf_source"] == "none"
    assert started["get_openalex_locations"].called
    html.assert_called_once_with(url)


def test_a_row_with_no_doi_still_gets_its_url_tried():
    """223 of the 568 no-document rows carry a URL and no DOI — the rows with nothing
    else to go on are exactly the ones this tier is for."""
    url = "https://osf.io/download/nodoi/"
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps.requests, "get",
                          return_value=_Resp(200, b"%PDF-1.4" + b"x" * 10_000)):
            out = ps.acquire_pdf("", "A Title", url_r=url)
    finally:
        for p in patchers.values():
            p.stop()
    assert (out["pdf_ok"], out["pdf_source"]) == (True, "row_url")


def test_a_repository_that_answers_the_browser_identity_with_a_page_is_asked_again():
    """econstor.eu serves a 4,806-byte interstitial to the spoofed Chrome identity and
    774 KB of PDF to the named crawler. One identity is not a superset of the other."""
    url = "https://www.econstor.eu/bitstream/10419/318610/1/JCRE-2025-2.pdf"
    answers = [_Resp(200, b"<!doctype html><p>checking your browser</p>"),
               _Resp(200, b"%PDF-1.4" + b"x" * 10_000)]
    with patch.object(ps.requests, "get", side_effect=answers) as get:
        out = ps.download_pdf(url, doi="10.15456/j1.2025082.1931045150")

    assert out["success"] is True
    assert get.call_count == 2
    first, second = (call.kwargs["headers"]["User-Agent"] for call in get.call_args_list)
    assert "Chrome" in first
    assert "FLoRA" in second


def test_a_url_with_no_document_under_either_identity_is_asked_no_more():
    url = "https://repo.example.org/record/1"
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, b"<html>a record page</html>")) as get:
        out = ps.download_pdf(url, doi="10.1/page-only")
    assert (out["success"], out["reason"]) == (False, "not_a_pdf")
    assert get.call_count == 2


# ── The document is the paper that was asked for ──────────────────────────────
#
# Observed 2026-08-08: two fetches of one PLOS printable URL returned two different
# papers. Nothing below acquisition can notice — a wrong document parses and codes as
# the row's evidence — so the check is here, on the bytes, before they are saved.

_TITLE = "Do infants prefer prosocial others? A direct replication of Hamlin & Wynn (2011)"


def test_a_document_whose_title_matches_is_saved():
    url = "https://journals.plos.org/plosone/article/file?id=10.1/x&type=printable"
    page = ("Do infants prefer prosocial others? A direct replication of Hamlin and "
            "Wynn (2011)\n\nAbstract\n" + "text " * 200)
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, b"%PDF-1.4" + b"x" * 10_000)), \
         patch.object(ps, "_front_matter_text", return_value=page):
        out = ps.download_pdf(url, doi="10.1/prosocial", title=_TITLE)

    assert out["success"] is True
    assert out["title_check"] == "match"
    assert ps.cached_pdf("10.1/prosocial") == out["path"]


def test_the_wrong_paper_at_the_right_url_is_refused():
    """The bug's shape: the URL is right, the server hands back another paper."""
    url = "https://journals.plos.org/plosone/article/file?id=10.1/x&type=printable"
    windfall = ("Revisiting the psychology of windfall gains: Replication and "
                "extensions of Arkes et al. (1994)\n\n" + "money " * 200)
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, b"%PDF-1.4" + b"x" * 10_000)), \
         patch.object(ps, "_front_matter_text", return_value=windfall):
        out = ps.download_pdf(url, doi="10.1/prosocial", title=_TITLE)

    assert (out["success"], out["reason"]) == (False, "wrong_document")
    assert ps.cached_pdf("10.1/prosocial") is None
    # Not recorded against the URL: the observed mis-serve was transient, and the same
    # URL served the right paper on another fetch.
    assert not ps._url_is_gone(url)


def test_a_document_with_no_title_in_it_is_kept():
    """A scanned page image, a slide deck, a data deposit: absence of a title is not
    evidence of mismatch, and the measured sample holds one PDF with 17 characters of
    text in the whole file."""
    url = "https://example.org/scanned.pdf"
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, b"%PDF-1.4" + b"x" * 10_000)), \
         patch.object(ps, "_front_matter_text", return_value="\x0c\x0c"):
        out = ps.download_pdf(url, doi="10.1/scanned", title=_TITLE)

    assert out["success"] is True
    assert out["title_check"] == "no_text"
    assert ps.cached_pdf("10.1/scanned") == out["path"]


def test_a_mis_served_document_already_on_disk_is_discarded():
    """Refusing it is not enough: every tier asks download_pdf, which reads this same
    cache entry before fetching, so a stored mismatch left in place would close the
    row to every tier for ever."""
    doi = "10.1/poisoned"
    ps.document_cache_path(doi, ".pdf").write_bytes(b"%PDF-1.4" + b"x" * 10_000)
    ps._write_provenance(doi, "openalex_oa", "https://example.org/wrong.pdf")
    good = ("Do infants prefer prosocial others? A direct replication of Hamlin and "
            "Wynn (2011)\n\n" + "text " * 200)

    with patch.object(ps, "_front_matter_text",
                      side_effect=["Some entirely different paper. " * 40, good]), \
         patch.object(ps.requests, "get",
                      return_value=_Resp(200, b"%PDF-1.4" + b"y" * 10_000)):
        out = ps.download_pdf("https://example.org/right.pdf", doi=doi, title=_TITLE)

    assert out["success"] is True
    assert out["title_check"] == "match"
    assert out["path"].read_bytes().endswith(b"y" * 10)


def test_the_title_check_scores_are_the_measured_ones():
    """The 61-document measurement: a right document with its title on the page scored
    ≥ 0.90, the wrong-paper pairs a median of 0.25, and the observed mis-serve 0.067."""
    body = "Attentional priority for temporary goals: a replication and extension. " \
           + "body " * 200
    assert ps._title_check(b"", ".pdf", "")[0] == "no_text"
    with patch.object(ps, "_front_matter_text", return_value=body):
        assert ps._title_check(b"", ".pdf",
                               "Attentional Priority for Temporary Goals")[0] == "match"
        # Escaped entities come off OpenAlex doubly encoded; an "amp" token no PDF
        # can carry would count against every title that has one.
        verdict, coverage = ps._title_check(
            b"", ".pdf", "Attentional Priority &amp;amp; Temporary Goals")
        assert (verdict, coverage) == ("match", 1.0)


# ── Every copy, and every registrant ──────────────────────────────────────────

@pytest.fixture
def _oa_cache_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "OA_CACHE_DIR", tmp_path)
    return tmp_path


_OA_WORK = {
    "open_access": {"oa_url": "https://publisher.example/article"},
    "best_oa_location": {"pdf_url": "https://publisher.example/article.pdf",
                         "landing_page_url": "https://publisher.example/article",
                         "source": {"host_organization_name": "Wiley"}},
    "locations": [
        {"pdf_url": None, "landing_page_url": "https://pure.uva.nl/record/1",
         "is_oa": False, "source": None},
        {"pdf_url": "https://repo.example/full.pdf",
         "landing_page_url": "https://repo.example/record", "is_oa": True,
         "source": {"host_organization_name": "Porto"}},
    ],
}


def test_openalex_offers_every_location_not_just_the_oa_url(_oa_cache_in_tmp):
    """The repository mirrors live in locations[], which was never requested — and
    that decides rows whose publisher copy is walled while a university copy is free.
    """
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, json.dumps(_OA_WORK).encode())) as get:
        cands = ps.get_openalex_locations("10.1/many-copies")

    assert [c["url"] for c in cands] == [
        "https://publisher.example/article",      # oa_url, the one URL tried before
        "https://publisher.example/article.pdf",
        "https://repo.example/full.pdf",
        "https://pure.uva.nl/record/1",
        "https://repo.example/record",
    ]
    # Files before pages, and no is_oa filter: both measured hits were on locations
    # OpenAlex marks is_oa false.
    assert [c["type"] for c in cands[:3]] == ["pdf", "pdf", "pdf"]
    # A single-entity lookup by DOI is free; it must not become a search query.
    assert "search" not in get.call_args.kwargs["params"]


def test_a_datacite_doi_gets_candidates_from_its_own_registrant(_oa_cache_in_tmp):
    """Unpaywall is Crossref-only, so every tier above asks an index that has never
    heard of 10.23668. DataCite is where that DOI is registered."""
    body = json.dumps({"data": {"attributes": {
        "url": "https://www.psycharchives.org/handle/20.500.12034/4416",
        "contentUrl": "https://www.psycharchives.org/bitstream/paper.pdf",
        "publisher": "PsychArchives"}}}).encode()
    with patch.object(ps.requests, "get", return_value=_Resp(200, body)):
        cands = ps.get_datacite_urls("10.23668/psycharchives.4988")

    assert [(c["url"], c["type"]) for c in cands] == [
        ("https://www.psycharchives.org/bitstream/paper.pdf", "pdf"),
        ("https://www.psycharchives.org/handle/20.500.12034/4416", "landing"),
    ]


def test_unpaywall_404_is_an_answer_and_a_5xx_is_not(_oa_cache_in_tmp):
    """A DataCite DOI 404s at Unpaywall — a permanent fact about that API's coverage,
    cacheable. A 503 is a minute of trouble, and the caller turns a recorded tier
    failure into a fourteen-day suppression, so it must not be reported as absence."""
    with patch.object(ps.requests, "get", return_value=_Resp(404, b"<!doctype html>")):
        assert ps.get_all_unpaywall_pdf_urls("10.23668/psycharchives.4988") == []
    # Cached: the second call asks nobody.
    with patch.object(ps.requests, "get", side_effect=AssertionError("re-asked")):
        assert ps.get_all_unpaywall_pdf_urls("10.23668/psycharchives.4988") == []

    with patch.object(ps.requests, "get", return_value=_Resp(503, b"")):
        with pytest.raises(ps.DocumentSourceUnavailable):
            ps.get_all_unpaywall_pdf_urls("10.1/flaky")


def test_an_unpaywall_outage_does_not_hold_the_tier_for_a_fortnight(_oa_cache_in_tmp):
    patchers = _mock_all_tiers()
    patchers["get_all_unpaywall_pdf_urls"] = patch.object(
        ps, "get_all_unpaywall_pdf_urls",
        side_effect=ps.DocumentSourceUnavailable("Unpaywall HTTP 503"))
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "download_pdf", return_value=_NO_PDF), \
             patch.object(ps, "scrape_pdf_from_landing_page", return_value=[]), \
             patch.object(ps, "get_pdf_via_playwright", return_value=_NO_PDF), \
             patch.object(ps, "get_html_document", return_value=None):
            ps.acquire_pdf("10.1/outage", title="A paper")
    finally:
        for p in patchers.values():
            p.stop()

    log = _retry_log("10.1/outage")
    assert "unpaywall_pdf" not in log
    # Nor the landing tier it feeds: its candidates come from the same call, so
    # recording it would suppress for a fortnight a tier that saw nothing.
    assert "landing" not in log


def test_a_stale_mis_served_document_is_not_parsed(tmp_path):
    """A row that resolves above the acquisition rung never calls acquire_pdf, and
    finds its document by DOI — so the check has to live on that lookup too."""
    doi = "10.1/stale"
    path = ps.document_cache_path(doi, ".pdf", cache_dir=tmp_path)
    path.write_bytes(b"%PDF-1.4" + b"x" * 10_000)

    with patch.object(ps, "_front_matter_text",
                      return_value="Some entirely different paper. " * 40):
        found = ps.verified_cached_document(doi, _TITLE, cache_dir=tmp_path)

    assert found is None
    assert not path.exists()


# ── OSF file storage ──────────────────────────────────────────────────────────

_OSF_FILE_PAGE = {
    "data": [
        {"attributes": {"kind": "file", "name": "Data Wrangling Log (2417959).docx",
                        "size": 40_000},
         "links": {"download": "https://osf.io/download/wrangle/"}},
        {"attributes": {"kind": "folder", "name": "Archive of OSF Storage"},
         "relationships": {"files": {"links": {"related": {
             "href": "https://api.osf.io/v2/files/archive/"}}}}},
    ],
    "links": {"next": None},
}

_OSF_FOLDER_PAGE = {
    "data": [
        {"attributes": {"kind": "file", "name": "final test questions.pdf",
                        "size": 90_000},
         "links": {"download": "https://osf.io/download/questions/"}},
        {"attributes": {"kind": "file", "name": "Breakthrough_final.pdf",
                        "size": 300_000},
         "links": {"download": "https://osf.io/download/manuscript/"}},
    ],
    "links": {"next": None},
}


def _osf_listing_response(url, **kwargs):
    if "/nodes/" in url:
        return _Resp(404, b"{}")
    if "/registrations/" in url:
        return _Resp(200, json.dumps(_OSF_FILE_PAGE).encode())
    return _Resp(200, json.dumps(_OSF_FOLDER_PAGE).encode())


def test_osf_files_recurses_the_archive_folder_and_ranks_the_manuscript_first(
        _oa_cache_in_tmp):
    """A registration wraps its files in an "Archive of OSF Storage" folder, and the
    manuscript sits beside a wrangling log and a questionnaire."""
    with patch.object(ps.requests, "get", side_effect=_osf_listing_response):
        files = ps.list_osf_files("abc12")

    assert [f["name"] for f in files] == ["Data Wrangling Log (2417959).docx",
                                          "final test questions.pdf",
                                          "Breakthrough_final.pdf"]

    ranked = ps.rank_osf_files(files, title="A Breakthrough In Something")
    # The log and the questionnaire hit an exclusion and carry no positive signal, so
    # they are not offered at all — the title check downstream would pass a supplement
    # to the right paper.
    assert [f["name"] for f in ranked] == ["Breakthrough_final.pdf"]


@pytest.mark.parametrize("names, first", [
    (["Correspondence_with_Authors.docx", "Replication_report.docx"],
     "Replication_report.docx"),
    (["supplementary_materials.pdf",
      "PCIRR-S1-RNR-Arkes-etal-1994-RR-main-manuscript.docx"],
     "PCIRR-S1-RNR-Arkes-etal-1994-RR-main-manuscript.docx"),
    (["Weinstein1980-replication-qualtrics_survey.docx",
      "Xiao, Zeng, & Feldman-2021-CRSP-revisiting-decoy-effect-final-preprint.pdf"],
     "Xiao, Zeng, & Feldman-2021-CRSP-revisiting-decoy-effect-final-preprint.pdf"),
])
def test_the_ranking_picks_the_real_manuscripts(names, first):
    files = [{"name": n, "size": 100_000, "download": f"https://osf.io/download/{i}/"}
             for i, n in enumerate(names)]
    assert ps.rank_osf_files(files)[0]["name"] == first


def test_a_supplement_named_after_the_paper_is_not_the_paper():
    """The title bonus ranks candidates; it never rescues one from the exclusions.
    A supplement carries the paper's whole title and is still the supplement."""
    files = [{"name": "Supplementary materials for A Breakthrough In Something.pdf",
              "size": 900_000, "download": "https://osf.io/download/supp/"},
             {"name": "manuscript.docx", "size": 200_000,
              "download": "https://osf.io/download/ms/"}]
    ranked = ps.rank_osf_files(files, title="A Breakthrough In Something")
    assert [f["name"] for f in ranked] == ["manuscript.docx"]


@pytest.mark.parametrize("plan_name", [
    "Final report - study plan.pdf", "Final analytic report.pdf",
    "Main paper proposal.pdf",
])
def test_a_plan_document_ranks_behind_the_manuscript(plan_name):
    """A plan is the study before it ran (issue #196: 10.17605/osf.io/zya9n shipped an
    outcome coded off an analytic-plan DOCX). It is demoted, never dropped."""
    files = [{"name": plan_name, "size": 900_000,
              "download": "https://osf.io/download/plan/"},
             {"name": "manuscript.pdf", "size": 100_000,
              "download": "https://osf.io/download/ms/"}]
    ranked = [f["name"] for f in ps.rank_osf_files(files)]
    assert ranked == ["manuscript.pdf", plan_name]
    # The only file a project deposited is still the best statement of the target.
    assert ps.rank_osf_files(files[:1])[0]["name"] == plan_name


def test_the_plan_demotion_reads_whole_words():
    """"explanation" is not a plan, and a demotion on a substring would rank the
    manuscript below a supplement."""
    files = [{"name": "An explanation of the effect.pdf", "size": 100_000,
              "download": "https://osf.io/download/expl/"}]
    assert not ps._OSF_NAME_PREREG.search(files[0]["name"])


def test_a_partial_osf_listing_is_used_but_never_cached(_oa_cache_in_tmp):
    """A folder that failed transiently is part of the tree we did not see; caching
    the listing without it would hide the manuscript in it until the entry expired."""
    def _flaky(url, **kwargs):
        if "/nodes/" in url:
            return _Resp(404, b"{}")
        if "/registrations/" in url:
            return _Resp(200, json.dumps(_OSF_FILE_PAGE).encode())
        return _Resp(503, b"")

    with patch.object(ps.requests, "get", side_effect=_flaky):
        files = ps.list_osf_files("abc12")

    assert [f["name"] for f in files] == ["Data Wrangling Log (2417959).docx"]
    assert list(_oa_cache_in_tmp.glob("osffiles_*.json")) == []


def test_a_stale_osf_listing_is_re_fetched(_oa_cache_in_tmp):
    """OSF storage is mutable by design — the manuscript is uploaded after the
    registration — so the listing expires with the tier's own retry delay."""
    with patch.object(ps.requests, "get", side_effect=_osf_listing_response):
        assert len(ps.list_osf_files("abc12")) == 3
    cf = next(iter(_oa_cache_in_tmp.glob("osffiles_*.json")))
    cf.write_text(json.dumps({"files": [],
                              "fetched_at": _ago(ps.PDF_RETRY_AFTER_DAYS + 1)}),
                  encoding="utf-8")

    with patch.object(ps.requests, "get", side_effect=_osf_listing_response) as get:
        assert len(ps.list_osf_files("abc12")) == 3
    assert get.called


def test_osf_files_falls_through_to_the_registration_form_when_no_file_qualifies():
    """The form is the fallback, not the competitor: a project whose storage holds
    only supplements still gets read through its registration."""
    registration = {"sections": {"abstract": "x" * 2_000, "raw_text": ""}}
    patchers = _mock_all_tiers(list_osf_files=[
        {"name": "supplementary_materials.pdf", "size": 90_000,
         "download": "https://osf.io/download/supp/"}])
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "get_osf_registration",
                          return_value=registration) as reg, \
             patch.object(ps, "download_pdf", return_value=_NO_PDF) as dl:
            out = ps.acquire_pdf("10.17605/osf.io/abc12", "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_source"] == "osf_registration"
    assert out["openalex_xml"] is registration
    reg.assert_called_once()
    dl.assert_not_called()      # the one candidate was ranked out, not downloaded


def test_a_manuscript_in_osf_storage_beats_the_registration_form():
    patchers = _mock_all_tiers(list_osf_files=[
        {"name": "Replication_report.docx", "size": 300_000,
         "download": "https://osf.io/download/ms/"}])
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "get_osf_registration") as reg, \
             patch.object(ps, "download_pdf",
                          return_value={"success": True, "path": "/tmp/ms.docx",
                                        "source": "download", "reason": ""}):
            out = ps.acquire_pdf("10.17605/osf.io/abc12", "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_source"] == "osf_files"
    assert out["pdf_url"] == "https://osf.io/download/ms/"
    # WHICH of the project's files: the download URL is a guid, so the listing's own
    # name is the only thing that says what was read.
    assert out["pdf_name"] == "Replication_report.docx"
    reg.assert_not_called()


@pytest.mark.parametrize("url,expected", [
    ("https://arxiv.org/pdf/2301.12345v1.pdf", "2301.12345v1.pdf"),
    ("https://osf.io/download/abc123/", ""),          # a guid names no file
    ("https://example.org/a%20paper.pdf?x=1", "a paper.pdf"),
    ("", ""),
])
def test_the_url_names_the_file_only_when_it_ends_in_one(url, expected):
    """The fallback for every tier OSF storage is not: a direct download's last path
    segment IS the file name, a guid-shaped one is not, and blank beats a guess."""
    assert ps._url_file_name(url) == expected


def test_an_osf_listing_outage_is_not_recorded_as_a_failure(_oa_cache_in_tmp):
    patchers = _mock_all_tiers()
    patchers["list_osf_files"] = patch.object(
        ps, "list_osf_files",
        side_effect=ps.DocumentSourceUnavailable("OSF files HTTP 503"))
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "get_osf_registration", return_value=None), \
             patch.object(ps, "download_pdf", return_value=_NO_PDF), \
             patch.object(ps, "get_pdf_via_playwright", return_value=_NO_PLAYWRIGHT), \
             patch.object(ps, "get_html_document", return_value=None):
            ps.acquire_pdf("10.17605/osf.io/abc12", "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    assert "osf_files" not in _retry_log("10.17605/osf.io/abc12")


# ── The reviewed paper, and the paper behind a title ──────────────────────────

def test_a_review_doi_acquires_the_paper_it_reviews():
    """A PCI recommendation has no full text of its own; the preprint it reviewed does.
    """
    patchers = _mock_all_tiers(crossref_reviewed_doi="10.31234/osf.io/abc12")
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "document_urls_for_doi",
                          return_value=(["https://osf.io/download/abc12/"], False)) as urls, \
             patch.object(ps, "download_pdf",
                          return_value={"success": True, "path": "/tmp/p.pdf",
                                        "source": "download", "reason": ""}):
            out = ps.acquire_pdf("10.24072/pci.rr.100123", "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_source"] == "related_doi"
    assert out["pdf_url"] == "https://osf.io/download/abc12/"
    urls.assert_called_once_with("10.31234/osf.io/abc12")


_CR_SEARCH = {"message": {"items": [
    {"DOI": "10.1/original",
     "title": ["Do infants prefer prosocial others?"]},
]}}


def test_crossref_search_refuses_a_title_that_only_matches_one_way(_oa_cache_in_tmp):
    """The wrong-paper shape: the ORIGINAL's title is a subset of the replication's,
    so a one-directional coverage check would follow it and code the wrong paper."""
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, json.dumps(_CR_SEARCH).encode())):
        assert ps.crossref_title_matches(_TITLE) == []


@pytest.mark.parametrize("row_title, hit_title", [
    # Measured 0.71/1.00 — clears a 0.60 gate in both directions, fails the 0.80 one.
    ("A direct replication of Ego depletion and moral judgment",
     "Ego depletion and moral judgment"),
    # Measured 0.88/1.00 — clears the coverage gate both ways, so only the
    # replication vocabulary separates the replication from the paper it replicates.
    ("Revisiting the effect of ego depletion on moral judgment in adults",
     "The effect of ego depletion on moral judgment in adults"),
])
def test_crossref_search_refuses_the_original_under_the_replications_title(
        row_title, hit_title, _oa_cache_in_tmp):
    hit = {"message": {"items": [{"DOI": "10.1/original", "title": [hit_title]}]}}
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, json.dumps(hit).encode())):
        assert ps.crossref_title_matches(row_title) == []


def test_a_crossref_search_404_is_an_outage_and_a_record_404_is_an_answer(
        _oa_cache_in_tmp):
    """The search endpoint answers "nothing found" with 200 and no items, so a 404
    there is the service misbehaving — and caching it would disable the only
    acquisition route a DOI-less row has, for that title, for ever."""
    with patch.object(ps.requests, "get", return_value=_Resp(404, b"{}")):
        with pytest.raises(ps.DocumentSourceUnavailable):
            ps.crossref_title_matches(_TITLE)
    assert list(_oa_cache_in_tmp.glob("crsearch_*.json")) == []

    with patch.object(ps.requests, "get", return_value=_Resp(404, b"{}")):
        assert ps.crossref_reviewed_doi("10.24072/pci.rr.missing") == ""
    assert list(_oa_cache_in_tmp.glob("crmeta_*.json")) != []


def test_crossref_search_accepts_the_same_paper_under_another_doi(_oa_cache_in_tmp):
    same = {"message": {"items": [{"DOI": "10.1/mirror", "title": [_TITLE]}]}}
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, json.dumps(same).encode())):
        assert ps.crossref_title_matches(_TITLE) == ["10.1/mirror"]


def test_a_short_title_never_reaches_the_crossref_search(_oa_cache_in_tmp):
    with patch.object(ps.requests, "get") as get:
        assert ps.crossref_title_matches("Study 2") == []
    get.assert_not_called()


def test_a_row_with_no_doi_reaches_the_crossref_search():
    patchers = _mock_all_tiers(crossref_title_matches=["10.1/mirror"])
    started = {name: p.start() for name, p in patchers.items()}
    try:
        def _download(url, doi="", min_bytes=0, title="", referer=""):
            if url.endswith(".pdf"):
                return {"success": True, "path": "/tmp/p.pdf", "source": "download",
                        "reason": ""}
            return dict(_NO_PDF)     # the row's own URL is a record page

        with patch.object(ps, "document_urls_for_doi",
                          return_value=(["https://repo.example/full.pdf"], False)), \
             patch.object(ps, "download_pdf", side_effect=_download):
            out = ps.acquire_pdf("", _TITLE, url_r="https://repo.example/record")
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_source"] == "crossref_search"


# ── The landing-page scraper ──────────────────────────────────────────────────

_LANDING_HTML = b"""
<html><body>
  <a href="/bitstream/1/blocked.pdf">Publisher copy</a>
  <a href="/bitstream/2/accepted.pdf">Accepted manuscript</a>
</body></html>
"""


def test_the_scraper_returns_every_candidate_not_only_the_first():
    resp = _Resp(200, b"")
    resp.text = _LANDING_HTML.decode()
    with patch.object(ps.requests, "get", return_value=resp):
        found = ps.scrape_pdf_from_landing_page("https://repo.example/record/1")
    assert found == ["https://repo.example/bitstream/1/blocked.pdf",
                     "https://repo.example/bitstream/2/accepted.pdf"]


def test_the_landing_tier_sends_a_referer_and_tries_the_second_candidate():
    """The first href is as often the one that 403s as the one that serves, and a
    repository that serves its own page's links refuses a request from nowhere."""
    calls: list[dict] = []

    def _download(url, doi="", min_bytes=0, title="", referer=""):
        calls.append({"url": url, "referer": referer})
        if url.endswith("blocked.pdf"):
            return dict(_NO_PDF)
        return {"success": True, "path": "/tmp/p.pdf", "source": "download",
                "reason": ""}

    patchers = _mock_all_tiers(get_all_unpaywall_pdf_urls=[
        {"url": "https://repo.example/record/1", "type": "landing",
         "host": "Repo", "license": ""}])
    patchers["scrape_pdf_from_landing_page"] = patch.object(
        ps, "scrape_pdf_from_landing_page",
        return_value=["https://repo.example/bitstream/1/blocked.pdf",
                      "https://repo.example/bitstream/2/accepted.pdf"])
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "download_pdf", side_effect=_download):
            out = ps.acquire_pdf("10.1/landing", "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_ok"] is True
    assert out["pdf_source"] == "landing_Repo"
    assert calls[-1]["url"] == "https://repo.example/bitstream/2/accepted.pdf"
    assert all(c["referer"] == "https://repo.example/record/1" for c in calls[-2:])


def test_a_document_on_disk_keeps_the_tier_that_really_supplied_it():
    """The OSF tiers sit below the on-disk replay for the reason the replay exists: a
    cache hit inside osf_files would relabel a serpapi document as osf_files and report
    a URL nothing was ever fetched from."""
    doi = "10.17605/osf.io/abc12"
    path = ps.document_cache_path(doi, ".pdf")
    path.write_bytes(b"%PDF-1.4" + b"x" * 10_000)
    ps._write_provenance(doi, "serpapi", "https://scholar.example/paper.pdf")

    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    try:
        with patch.object(ps, "get_osf_registration") as reg, \
             patch.object(ps, "_title_check", return_value=("match", 0.95)):
            out = ps.acquire_pdf(doi, _TITLE)
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_source"] == "serpapi"
    assert out["pdf_url"] == "https://scholar.example/paper.pdf"
    started["list_osf_files"].assert_not_called()
    reg.assert_not_called()


# ── Europe PMC ────────────────────────────────────────────────────────────────
# The tier asks for the JATS full text first and falls back to the article page's
# rendered PDF. `backend/ptpmcrender.fcgi`, which the URL used to be built on, breaks
# the HTTP/2 stream; `europepmc.org/articles/<PMCID>?pdf=render` answers 200 with a
# PDF (both probed 2026-08-13).

_JATS = """<article>
  <front><article-meta><abstract><p>We replicated Smith (2009).</p></abstract>
  </article-meta></front>
  <body><sec><title>Introduction</title><p>The original reported a large effect.</p>
  </sec><sec><title>Discussion</title><p>The effect did not replicate.</p></sec></body>
  <back><ref-list><ref><element-citation>
    <person-group><name><surname>Smith</surname><given-names>J</given-names></name>
    </person-group><article-title>Attitudes toward HIV</article-title>
    <year>2009</year></element-citation></ref></ref-list></back>
</article>"""

# What the endpoint serves for an article it holds a RECORD of and no body: front
# matter, abstract included. It is truthy and it is not a document.
_JATS_RECORD_ONLY = ("<article><front><article-meta><title-group><article-title>"
                     "A paper</article-title></title-group>"
                     "<abstract><p>We replicated Smith (2009).</p></abstract>"
                     "</article-meta></front></article>")


@pytest.fixture()
def _epmc_cache_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "OA_XML_CACHE_DIR", tmp_path)
    return tmp_path


def test_the_europepmc_pdf_url_is_the_article_render_route():
    assert ps.europepmc_pdf_url("PMC123") == \
        "https://europepmc.org/articles/PMC123?pdf=render"


def test_epmc_jats_with_a_body_is_a_document(_epmc_cache_in_tmp):
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, _JATS.encode())):
        doc = ps.get_europepmc_fulltext("PMC123")

    assert doc["source"] == "epmc_xml"
    assert "did not replicate" in doc["sections"]["raw_text"]
    assert doc["sections"]["intro"].startswith("Introduction")
    assert doc["sections"]["references"][0]["title"] == "Attitudes toward HIV"
    assert ps.epmc_xml_has_content(doc) is True
    # Cached, so the second read costs no request.
    with patch.object(ps.requests, "get", side_effect=AssertionError) as get:
        assert ps.get_europepmc_fulltext("PMC123") == doc
        get.assert_not_called()


def test_a_record_shaped_epmc_xml_is_no_document_and_is_not_cached(_epmc_cache_in_tmp):
    """Front matter and an abstract are what a RECORD carries. Reading one as full
    text is what every structured source's content check exists to stop."""
    with patch.object(ps.requests, "get",
                      return_value=_Resp(200, _JATS_RECORD_ONLY.encode())):
        assert ps.get_europepmc_fulltext("PMC404") is None
    assert list(_epmc_cache_in_tmp.glob("epmc_xml_*.json")) == []


def test_an_epmc_5xx_is_an_outage_and_a_404_is_an_answer(_epmc_cache_in_tmp):
    with patch.object(ps.requests, "get", return_value=_Resp(503)):
        with pytest.raises(ps.DocumentSourceUnavailable):
            ps.get_europepmc_fulltext("PMC500")
    with patch.object(ps.requests, "get", return_value=_Resp(404)):
        assert ps.get_europepmc_fulltext("PMC404") is None


def test_the_epmc_xml_ends_the_waterfall_and_clears_the_retry_record():
    doi = "10.1016/j.example.2020.01.009"
    _write_retry_log(doi, {"core": _ago(1)})
    doc = {"source": "epmc_xml", "xml_url": "u",
           "sections": {"abstract": "We replicated it.", "intro": "", "methods": "",
                        "references": [{"title": "r"}]}}
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    started["get_europepmc_pmcid"].return_value = "PMC123"
    started["get_europepmc_fulltext"].return_value = doc
    try:
        with patch.object(ps, "download_pdf", return_value=_NO_PDF) as dl:
            out = ps.acquire_pdf(doi, _TITLE)
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_source"] == "epmc_xml"
    assert out["openalex_xml"] == doc
    assert _retry_log(doi) == {}
    dl.assert_not_called()


def test_no_epmc_xml_falls_back_to_the_rendered_pdf():
    doi = "10.1016/j.example.2020.01.010"
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    started["get_europepmc_pmcid"].return_value = "PMC123"
    started["get_europepmc_fulltext"].return_value = None
    tried = []

    def _download(url, **kwargs):
        tried.append(url)
        return {"success": True, "path": "/tmp/x.pdf", "source": "download",
                "reason": ""}

    try:
        with patch.object(ps, "download_pdf", side_effect=_download), \
             patch.object(ps, "_write_provenance"):
            out = ps.acquire_pdf(doi, _TITLE)
    finally:
        for p in patchers.values():
            p.stop()

    assert out["pdf_source"] == "europepmc"
    assert tried[-1] == "https://europepmc.org/articles/PMC123?pdf=render"


# ── Europe PMC: an outage is not a fourteen-day suppression ───────────────────

def test_the_epmc_search_raises_on_trouble_and_answers_on_an_empty_result(tmp_path,
                                                                          monkeypatch):
    """The search endpoint answers 200 with an empty result list for a DOI it does
    not hold. Anything else — a timeout, a 429, a gateway page — is no answer, and a
    None there would suppress the tier for fourteen days on a paper Europe PMC has."""
    monkeypatch.setattr(ps, "OA_CACHE_DIR", tmp_path)
    empty = json.dumps({"resultList": {"result": []}}).encode()

    with patch.object(ps.requests, "get", return_value=_Resp(200, empty)):
        assert ps.get_europepmc_pmcid("10.1/empty") is None

    with patch.object(ps.requests, "get", return_value=_Resp(429)):
        with pytest.raises(ps.DocumentSourceUnavailable):
            ps.get_europepmc_pmcid("10.1/busy")
    with patch.object(ps.requests, "get", side_effect=TimeoutError("timed out")):
        with pytest.raises(ps.DocumentSourceUnavailable):
            ps.get_europepmc_pmcid("10.1/slow")
    # Only the answer is filed; a run that reaches the same DOI tomorrow re-asks.
    assert len(list(tmp_path.glob("epmc_*.json"))) == 1


def _acquire_with_epmc(doi: str, *, pmcid, fulltext, download_reason: str):
    """acquire_pdf with every other tier missing; `pmcid`/`fulltext` may be
    exceptions to raise. Returns the retry log written for *doi*."""
    patchers = _mock_all_tiers()
    started = {name: p.start() for name, p in patchers.items()}
    for name, value in (("get_europepmc_pmcid", pmcid),
                        ("get_europepmc_fulltext", fulltext)):
        if isinstance(value, Exception):
            started[name].side_effect = value
        else:
            started[name].return_value = value
    try:
        with patch.object(ps, "get_pdf_via_playwright", return_value=_NO_PLAYWRIGHT), \
             patch.object(ps, "download_pdf",
                          return_value={"success": False, "path": None,
                                        "source": "", "reason": download_reason}):
            ps.acquire_pdf(doi, _TITLE)
    finally:
        for p in patchers.values():
            p.stop()
    return _retry_log(doi)


def test_an_epmc_search_outage_records_no_retry_stamp():
    log = _acquire_with_epmc("10.1016/j.example.2020.01.020",
                             pmcid=ps.DocumentSourceUnavailable("timeout"),
                             fulltext=None, download_reason="not_a_pdf")
    assert "europepmc" not in log


def test_an_empty_epmc_search_result_records_the_stamp():
    log = _acquire_with_epmc("10.1016/j.example.2020.01.021",
                             pmcid=None, fulltext=None,
                             download_reason="not_a_pdf")
    assert "europepmc" in log


def test_an_epmc_pdf_that_only_failed_to_download_records_no_stamp():
    log = _acquire_with_epmc("10.1016/j.example.2020.01.022",
                             pmcid="PMC123", fulltext=None,
                             download_reason="download_error: 503")
    assert "europepmc" not in log


def test_an_epmc_xml_outage_leaves_half_the_tier_unasked_so_no_stamp():
    log = _acquire_with_epmc("10.1016/j.example.2020.01.023",
                             pmcid="PMC123",
                             fulltext=ps.DocumentSourceUnavailable("503"),
                             download_reason="not_a_pdf")
    assert "europepmc" not in log


def test_both_epmc_routes_answering_nothing_records_the_stamp():
    log = _acquire_with_epmc("10.1016/j.example.2020.01.024",
                             pmcid="PMC123", fulltext=None,
                             download_reason="not_a_pdf")
    assert "europepmc" in log
