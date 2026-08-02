"""Tests for OpenAlex GROBID XML acquisition."""
import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from shared.grobid import parse_tei_sections
from shared.pdf_sources import get_openalex_fulltext

# The camelCase, namespaced TEI a local GROBID server returns: a real <body> with
# <head> elements, so intro/methods are recoverable by heading.
GROBID_TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>A replication study</title></titleStmt>
      <sourceDesc><biblStruct><title level="a">Not a reference</title></biblStruct></sourceDesc>
    </fileDesc>
  </teiHeader>
  <text>
    <front>
      <abstract><div><p>We replicated the effect.</p></div></abstract>
    </front>
    <body>
      <div><head>Introduction</head><p>In this study we replicate Smith.</p></div>
      <div><head>Methods</head><p>Participants were students.</p></div>
    </body>
    <back>
      <div type="references"><listBibl>
        <biblStruct>
          <analytic>
            <title level="a">Ego depletion revisited</title>
            <author><persName><forename>John</forename><surname>Smith</surname></persName></author>
          </analytic>
          <monogr><imprint><date type="published" when="2009-05">2009</date></imprint></monogr>
        </biblStruct>
      </listBibl></div>
    </back>
  </text>
</TEI>"""

# What OpenAlex actually stores: the same TEI round-tripped through an HTML parser —
# wrapped in <html><body>, every tag lowercased, the nested TEI <body>/<head>
# elements hoisted away. Captured from content.openalex.org/works/W2982430379.
OPENALEX_TEI = """<?xml version="1.0" encoding="UTF-8"?><html><body>\
<tei xml:space="preserve" xmlns="http://www.tei-c.org/ns/1.0">
  <teiheader>
    <filedesc>
      <titlestmt><title level="a">A replication study</title></titlestmt>
      <sourcedesc><biblstruct><title level="a">Not a reference</title></biblstruct></sourcedesc>
    </filedesc>
  </teiheader>
  <text xml:lang="en">
    <abstract><div><p>We replicated the effect.</p></div></abstract>
    <p>In this study we replicate Smith. Participants were students.</p>
    <div type="references"><listbibl>
      <biblstruct>
        <analytic>
          <title level="a">Ego depletion revisited</title>
          <author><persname><forename>John</forename><surname>Smith</surname></persname></author>
        </analytic>
        <monogr><imprint><date type="published" when="2009-05">2009</date></imprint></monogr>
      </biblstruct>
    </listbibl></div>
  </text>
</tei></body></html>"""


@pytest.fixture(autouse=True)
def _openalex_key():
    """The content endpoint is key-gated; give every test a key unless it says otherwise."""
    with patch("shared.pdf_sources.OPENALEX_API_KEYS", ["test-key"]):
        yield


def _xml_response(body: str, gzipped: bool = False) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    if gzipped:
        resp.content = gzip.compress(body.encode("utf-8"))
        resp.headers = {"Content-Type": "application/gzip",
                        "Content-Disposition": 'attachment; filename="w.grobid.xml.gz"'}
        # What requests hands back when it does not decompress: mojibake.
        resp.text = resp.content.decode("latin-1")
    else:
        resp.content = body.encode("utf-8")
        resp.headers = {"Content-Type": "application/xml"}
        resp.text = body
    return resp


def _meta_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "has_content": {"grobid_xml": True},
        "content_urls": {"grobid_xml": "https://content.openalex.org/works/W12345.grobid-xml"},
    }
    return resp


def _cache_file(oa_id: str) -> Path:
    from shared.config import OA_XML_CACHE_DIR
    from shared.utils import cache_key
    return OA_XML_CACHE_DIR / f"oa_xml_{cache_key(oa_id)}.json"


class TestTeiDialects:
    """One parser, two dialects — see shared.grobid._tei_localname."""

    def test_camelcase_grobid_tei(self):
        out = parse_tei_sections(GROBID_TEI)
        assert out["abstract"] == "We replicated the effect."
        assert out["intro"].startswith("Introduction")
        assert "Participants" in out["methods"]
        assert [r["title"] for r in out["references"]] == ["Ego depletion revisited"]
        assert out["references"][0]["authors"] == ["Smith, J."]
        assert out["references"][0]["year"] == 2009
        assert "Participants were students" in out["raw_text"]

    def test_lowercased_html_wrapped_openalex_tei(self):
        out = parse_tei_sections(OPENALEX_TEI)
        assert out["abstract"] == "We replicated the effect."
        # No <head> elements survived the HTML round-trip, so these stay empty
        # rather than being guessed at; the text goes to raw_text instead.
        assert out["intro"] == ""
        assert out["methods"] == ""
        assert "In this study we replicate Smith" in out["raw_text"]
        assert [r["title"] for r in out["references"]] == ["Ego depletion revisited"]
        assert out["references"][0]["authors"] == ["Smith, J."]
        assert out["references"][0]["year"] == 2009


class TestGetOpenAlexFulltext:
    def test_returns_none_when_no_openalex_id(self):
        result = get_openalex_fulltext("")
        assert result is None

    def test_skips_the_tier_without_an_openalex_key(self):
        """The download is metered ($0.01/work) — no key means no request at all."""
        with patch("shared.pdf_sources.OPENALEX_API_KEYS", []), \
             patch("shared.pdf_sources.requests.get") as mock_get:
            result = get_openalex_fulltext("W00001")
        assert result is None
        mock_get.assert_not_called()

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
        cache_file = _cache_file("W12345")
        cache_file.unlink(missing_ok=True)

        try:
            with patch("shared.pdf_sources.requests.get",
                       side_effect=[_meta_response(), _xml_response(GROBID_TEI)]):
                result = get_openalex_fulltext("W12345")
        finally:
            cache_file.unlink(missing_ok=True)

        assert result is not None
        assert result["source"] == "openalex_xml"
        assert result["sections"]["references"]

    def test_gunzips_the_gzip_file_the_endpoint_serves(self):
        """The response is a gzip FILE with no Content-Encoding, so requests does
        not decompress it — .text alone is mojibake and parses to an empty shell."""
        cache_file = _cache_file("W12346")
        cache_file.unlink(missing_ok=True)

        try:
            with patch("shared.pdf_sources.requests.get",
                       side_effect=[_meta_response(),
                                    _xml_response(OPENALEX_TEI, gzipped=True)]):
                result = get_openalex_fulltext("W12346")
            assert result is not None
            sections = result["sections"]
            assert [r["title"] for r in sections["references"]] == ["Ego depletion revisited"]
            assert "In this study we replicate Smith" in sections["raw_text"]
            # And it was cached, so the metered endpoint is paid for once.
            assert json.loads(cache_file.read_text(encoding="utf-8"))["sections"]["references"]
        finally:
            cache_file.unlink(missing_ok=True)

    def test_refetch_replaces_a_shell_cache_entry(self):
        """A content-free shell on disk is ignored, re-fetched, and overwritten."""
        cache_file = _cache_file("W12347")
        shell = {"source": "openalex_xml", "xml_url": "u",
                 "sections": {"abstract": "", "intro": "", "methods": "", "references": []}}
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(shell), encoding="utf-8")

        try:
            with patch("shared.pdf_sources.requests.get",
                       side_effect=[_meta_response(),
                                    _xml_response(OPENALEX_TEI, gzipped=True)]):
                result = get_openalex_fulltext("W12347")
            assert result["sections"]["references"]
            on_disk = json.loads(cache_file.read_text(encoding="utf-8"))
            assert on_disk["sections"]["references"]
        finally:
            cache_file.unlink(missing_ok=True)

    def test_uses_cache_on_second_call(self):
        """Cached result is returned without hitting the network."""
        cache_file = _cache_file("W99999")
        cached = {"source": "openalex_xml", "sections": {"intro": "cached intro"}, "xml_url": ""}
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cached), encoding="utf-8")

        try:
            with patch("shared.pdf_sources.requests.get") as mock_get:
                result = get_openalex_fulltext("W99999")

            mock_get.assert_not_called()
            assert result["sections"]["intro"] == "cached intro"
        finally:
            cache_file.unlink(missing_ok=True)
