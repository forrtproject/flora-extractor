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
    "get_openalex_oa_url", "get_all_unpaywall_pdf_urls",
    "get_semanticscholar_pdf_url", "get_core_pdf_url", "get_europepmc_pdf_url",
    "scrape_pdf_from_landing_page", "get_serpapi_pdf_url",
]


def _mock_all_tiers(**overrides):
    """Patch every tier lookup to a no-hit default, then apply *overrides*.

    Returns the list of active context managers' mocks keyed by name, so a test
    can assert on call counts without hand-patching eleven functions each time.
    """
    patchers = {}
    for name in _ALL_TIERS:
        default = [] if name == "get_all_unpaywall_pdf_urls" else None
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
    for name in ("get_openalex_oa_url", "get_all_unpaywall_pdf_urls",
                 "get_semanticscholar_pdf_url", "get_core_pdf_url",
                 "get_europepmc_pdf_url", "get_serpapi_pdf_url",
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
    mocks["get_openalex_oa_url"].assert_called_once()
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

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=0):
        yield self._body


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
    for name in ("get_arxiv_pdf_url", "get_osf_pdf_url", "get_openalex_oa_url",
                 "get_all_unpaywall_pdf_urls"):
        started[name].assert_not_called()


def test_a_saved_pdf_without_provenance_still_runs_the_waterfall():
    """Nothing recorded the tier before this record existed; those PDFs keep the old
    behaviour — the waterfall runs, its first cache hit writes the record."""
    doi = "10.1016/j.example.2020.01.010"
    _save_pdf(doi)
    patchers = _mock_all_tiers(get_openalex_oa_url="https://x/z.pdf")
    started = {name: p.start() for name, p in patchers.items()}
    try:
        out = ps.acquire_pdf(doi, "A Title")
    finally:
        for p in patchers.values():
            p.stop()

    started["get_openalex_oa_url"].assert_called_once()
    assert out["pdf_ok"] is True
    assert out["pdf_source"] == "openalex_oa"
    assert ps._read_provenance(ps.clean_doi(doi)) == {"source": "openalex_oa",
                                                      "url": "https://x/z.pdf"}


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
    assert started["get_openalex_oa_url"].called
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
