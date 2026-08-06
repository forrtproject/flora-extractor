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
