"""
Tests for shared.pdf_sources — the acquire_pdf tier waterfall.

All network access is mocked; no live API calls are made.
Run:  python -m pytest tests/test_pdf_sources.py -v
"""
from unittest.mock import patch

import shared.pdf_sources as ps


_ALL_TIERS = [
    "get_openalex_fulltext", "get_arxiv_pdf_url", "get_osf_pdf_url",
    "get_openalex_oa_url", "get_all_unpaywall_pdf_urls",
    "get_semanticscholar_pdf_url", "get_core_pdf_url", "get_europepmc_pdf_url",
    "scrape_pdf_from_landing_page", "get_serpapi_pdf_url",
    "extract_html_text_as_fulltext",
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
