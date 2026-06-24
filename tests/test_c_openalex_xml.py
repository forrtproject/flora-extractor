"""Tests for OpenAlex GROBID XML acquisition."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from shared.pdf_sources import get_openalex_fulltext


class TestGetOpenAlexFulltext:
    def test_returns_none_when_no_openalex_id(self):
        result = get_openalex_fulltext("")
        assert result is None

    def test_returns_none_when_grobid_xml_false(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "has_content": {"pdf": True, "grobid_xml": False}
        }
        with patch("shared.pdf_sources.requests.get", return_value=mock_resp):
            result = get_openalex_fulltext("W00002")
        assert result is None

    def test_returns_none_when_has_content_missing(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        with patch("shared.pdf_sources.requests.get", return_value=mock_resp):
            result = get_openalex_fulltext("W00003")
        assert result is None

    def test_returns_sections_when_grobid_xml_true(self):
        """When grobid_xml=true, download XML and parse sections."""
        from shared.config import OA_XML_CACHE_DIR
        from shared.utils import cache_key

        test_id   = "W12345"
        cache_key_ = cache_key(f"W{test_id}" if not test_id.startswith("W") else test_id)
        cache_file = OA_XML_CACHE_DIR / f"oa_xml_{cache_key_}.json"
        cache_file.unlink(missing_ok=True)

        meta_resp = MagicMock()
        meta_resp.status_code = 200
        meta_resp.json.return_value = {
            "has_content": {"grobid_xml": True},
            "content_urls": {"grobid_xml": "https://content.openalex.org/works/W12345.grobid-xml"},
        }
        xml_resp = MagicMock()
        xml_resp.status_code = 200
        xml_resp.text = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>A replication study</title></titleStmt>
    </fileDesc>
  </teiHeader>
  <text>
    <front>
      <div type="abstract"><p>We replicated the effect.</p></div>
    </front>
    <body>
      <div><head>Introduction</head><p>In this study we replicate...</p></div>
    </body>
  </text>
</TEI>"""
        try:
            with patch("shared.pdf_sources.requests.get", side_effect=[meta_resp, xml_resp]):
                result = get_openalex_fulltext(test_id)
        finally:
            cache_file.unlink(missing_ok=True)

        assert result is not None
        assert result["source"] == "openalex_xml"
        assert "sections" in result

    def test_uses_cache_on_second_call(self):
        """Cached result is returned without hitting the network."""
        from shared.config import OA_XML_CACHE_DIR
        from shared.utils import cache_key

        oa_id = "W99999"
        key = cache_key(oa_id)
        cache_file = OA_XML_CACHE_DIR / f"oa_xml_{key}.json"
        cached = {"source": "openalex_xml", "sections": {"intro": "cached intro"}, "xml_url": ""}
        cache_file.write_text(json.dumps(cached), encoding="utf-8")

        try:
            with patch("shared.pdf_sources.requests.get") as mock_get:
                result = get_openalex_fulltext(oa_id)

            mock_get.assert_not_called()
            assert result["sections"]["intro"] == "cached intro"
        finally:
            if cache_file.exists():
                cache_file.unlink()
