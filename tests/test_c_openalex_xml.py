"""Tests for OpenAlex GROBID XML acquisition and the two TEI dialects."""
import gzip
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from shared.grobid import parse_tei_sections
from shared.pdf_parsing import outcome_text
from shared.pdf_sources import get_openalex_fulltext

# The camelCase, namespaced TEI a local GROBID server returns: a real <body> with
# <head> elements, so intro/methods are recoverable by heading, and a <back> holding
# the bibliography. The teiHeader's own biblStruct is the paper, not a reference.
GROBID_TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title level="a" type="main">A replication study</title></titleStmt>
      <sourceDesc><biblStruct><analytic><title level="a">A replication study</title>
        <author><persName><forename>Ada</forename><surname>Lovelace</surname></persName></author>
      </analytic></biblStruct></sourceDesc>
    </fileDesc>
    <profileDesc>
      <abstract><div><p>We replicated the effect.</p></div></abstract>
    </profileDesc>
  </teiHeader>
  <text xml:lang="en">
    <body>
      <div><head>Introduction</head><p>In this study we replicate Smith.</p></div>
      <div><head>Methods</head><p>Participants were students.</p></div>
      <div><head>Discussion</head><p>The effect did not replicate.</p></div>
    </body>
    <back>
      <div type="references"><listBibl>
        <biblStruct>
          <analytic>
            <title level="a">Ego depletion revisited</title>
            <author><persName><forename>John</forename><surname>Smith</surname></persName></author>
          </analytic>
          <monogr><title level="j">Journal of Psychology</title>
            <imprint><date type="published" when="2009-05">2009</date></imprint></monogr>
        </biblStruct>
        <biblStruct>
          <analytic>
            <title level="a">A second cited work</title>
            <author><persName><surname>Jones</surname></persName></author>
          </analytic>
          <monogr><imprint><date type="published" when="2011">2011</date></imprint></monogr>
        </biblStruct>
      </listBibl></div>
    </back>
  </text>
</TEI>"""

# The complete parse of GROBID_TEI, pinned field by field: this is the local-GROBID
# behaviour the lowercase-local-name rewrite must not have moved. The first
# level="a"/"m" title wins (the journal title at level="j" is not the reference's).
GROBID_TEI_PARSE = {
    "abstract": "We replicated the effect.",
    "intro":    "IntroductionIn this study we replicate Smith.",
    "methods":  "MethodsParticipants were students.",
    "references": [
        {"authors": ["Smith, J."], "year": 2009, "title": "Ego depletion revisited",
         "raw_ref": "Ego depletion revisited JohnSmith Journal of Psychology 2009"},
        {"authors": ["Jones"], "year": 2011, "title": "A second cited work",
         "raw_ref": "A second cited work Jones 2011"},
    ],
}

# What OpenAlex actually stores: the same TEI round-tripped through an HTML parser —
# wrapped in <html><body>, every tag lowercased, the nested TEI <body>/<head>
# elements gone (a section heading survives only as the text node that opens its
# <div>), sentences wrapped in <s>. Shape captured from
# content.openalex.org/works/W2982430379.
_FILLER = ("Whole blood was collected from every subject at presentation and the "
           "assay was run in duplicate on the same instrument. " * 6)
OPENALEX_TEI = f"""<?xml version="1.0" encoding="UTF-8"?><html><body>\
<tei xml:space="preserve" xmlns="http://www.tei-c.org/ns/1.0">
<teiheader xml:lang="en">
<filedesc>
<titlestmt><title level="a" type="main">A replication study</title></titlestmt>
<sourcedesc><biblstruct><analytic><title level="a">A replication study</title>
</analytic></biblstruct></sourcedesc>
</filedesc>
<profiledesc><abstract><div><p>We replicated the effect.</p></div></abstract></profiledesc>
</teiheader>
<text xml:lang="en">
<div>INTRODUCTION<p><s>In this study we replicate Smith.</s><s>{_FILLER}</s></p></div>
<div>METHODS<p><s>Participants were students.</s><s>{_FILLER}</s></p></div>
<div>DISCUSSION<p><s>The effect did not replicate in our sample.</s><s>{_FILLER}</s></p></div>
<back>
<div type="acknowledgement"><div><p>We thank the funders.</p></div></div>
<div type="references"><listbibl>
<biblstruct>
<analytic><title level="a">Ego depletion revisited</title>
<author><persname><forename>John</forename><surname>Smith</surname></persname></author>
</analytic>
<monogr><title level="j">Journal of Psychology</title>
<imprint><date type="published" when="2009-05">2009</date></imprint></monogr>
</biblstruct>
</listbibl></div>
</back>
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
    resp.headers = {}
    resp.json.return_value = {
        "has_content": {"grobid_xml": True},
        "content_urls": {"grobid_xml": "https://content.openalex.org/works/W12345.grobid-xml"},
    }
    return resp


def _budget_refusal() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {}
    resp.json.return_value = {"message": "Insufficient budget",
                              "dailyRemainingUsd": 0, "prepaidRemainingUsd": 0}
    return resp


def _cache_file(oa_id: str) -> Path:
    from shared.config import OA_XML_CACHE_DIR
    from shared.utils import cache_key
    return OA_XML_CACHE_DIR / f"oa_xml_{cache_key(oa_id)}.json"


class TestTeiDialects:
    """One parser, two dialects — see shared.grobid._tei_localname."""

    def test_camelcase_grobid_tei_parses_exactly_as_before(self):
        out = parse_tei_sections(GROBID_TEI)
        assert {k: v for k, v in out.items() if k != "raw_text"} == GROBID_TEI_PARSE
        # raw_text is the one addition: the body, and only the body.
        assert "Participants were students" in out["raw_text"]
        assert "Ego depletion revisited" not in out["raw_text"]

    def test_lowercased_html_wrapped_openalex_tei(self):
        out = parse_tei_sections(OPENALEX_TEI)
        assert out["abstract"] == "We replicated the effect."
        # No <head> elements survived the HTML round-trip, so these stay empty
        # rather than being guessed at; the text goes to raw_text instead.
        assert out["intro"] == ""
        assert out["methods"] == ""
        assert "In this study we replicate Smith." in out["raw_text"]
        assert [r["title"] for r in out["references"]] == ["Ego depletion revisited"]
        assert out["references"][0]["authors"] == ["Smith, J."]
        assert out["references"][0]["year"] == 2009

    def test_raw_text_keeps_headings_on_their_own_line_and_drops_the_back_matter(self):
        """outcome_text() finds Discussion/References as LINE headings. Flattened to
        one line it finds neither, and its fallback hands the model the last pages —
        which, with the bibliography still in the text, is the bibliography."""
        out = parse_tei_sections(OPENALEX_TEI)
        raw = out["raw_text"]
        assert "\nDISCUSSION\n" in f"\n{raw}\n"
        assert "Ego depletion revisited" not in raw   # <listbibl> excluded
        assert "We thank the funders" not in raw      # rest of <back> too

        text, provenance = outcome_text(raw, max_chars=8000)
        assert provenance == "discussion"
        assert "The effect did not replicate in our sample." in text
        assert "Ego depletion revisited" not in text
        assert "INTRODUCTION" not in text

    def test_sentences_are_not_run_together(self):
        """GROBID wraps each sentence in <s>; itertext() alone welds them into
        "…replicate Smith.Whole blood…"."""
        raw = parse_tei_sections(OPENALEX_TEI)["raw_text"]
        assert "Smith.Whole" not in raw
        assert "Smith. Whole" in raw


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
        mock_resp.headers = {}
        mock_resp.json.return_value = {
            "has_content": {"pdf": True, "grobid_xml": False}
        }
        with patch("shared.pdf_sources.requests.get", return_value=mock_resp):
            result = get_openalex_fulltext("W00002")
        assert result is None

    def test_returns_none_when_has_content_missing(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {}
        with patch("shared.pdf_sources.requests.get", return_value=mock_resp):
            result = get_openalex_fulltext("W00003")
        assert result is None

    def test_a_drained_key_rotates_instead_of_giving_up(self):
        """OPENALEX_API_KEYS is a rotation list and this tier is metered per
        download, so it drains a key faster than the free endpoints do."""
        cache_file = _cache_file("W12348")
        cache_file.unlink(missing_ok=True)
        responses = [_meta_response(), _budget_refusal(),
                     _xml_response(OPENALEX_TEI, gzipped=True)]

        try:
            with patch("shared.pdf_sources.rotate_key", return_value=True) as rotate, \
                 patch("shared.pdf_sources.requests.get", side_effect=responses):
                result = get_openalex_fulltext("W12348")
            assert rotate.call_count == 1
            assert result is not None and result["sections"]["references"]
        finally:
            cache_file.unlink(missing_ok=True)

    def test_a_refusal_with_no_key_left_returns_none(self):
        with patch("shared.pdf_sources.rotate_key", return_value=False), \
             patch("shared.pdf_sources.requests.get",
                   side_effect=[_meta_response(), _budget_refusal()]):
            assert get_openalex_fulltext("W12349") is None

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
            assert "In this study we replicate Smith." in sections["raw_text"]
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


# The XML OpenAlex serves for a work it parsed to nothing: valid TEI, no body text,
# no bibliography. openalex_xml_has_content() calls it no document.
EMPTY_TEI = ("""<?xml version="1.0" encoding="UTF-8"?>"""
             """<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body></body></text></TEI>""")


class TestContentFreeRetryDelay:
    """A content-free answer is still never cached AS a success — but re-asking on
    every run re-paid the metered download (100x a filter query) on every run. The
    last content-free fetch is timestamped, and the tier stays quiet until it lapses."""

    @pytest.fixture(autouse=True)
    def _xml_cache_in_tmp(self, tmp_path, monkeypatch):
        import shared.pdf_sources as ps
        monkeypatch.setattr(ps, "OA_XML_CACHE_DIR", tmp_path)

    def _retry_path(self, oa_id: str) -> Path:
        import shared.pdf_sources as ps
        from shared.utils import cache_key
        return ps.OA_XML_CACHE_DIR / f"retry_{cache_key(oa_id)}.json"

    def test_a_content_free_fetch_is_recorded_and_not_repeated(self):
        with patch("shared.pdf_sources.requests.get",
                   side_effect=[_meta_response(), _xml_response(EMPTY_TEI)]) as first:
            assert get_openalex_fulltext("W55501") is None
        assert first.call_count == 2
        assert "content_free" in json.loads(
            self._retry_path("W55501").read_text(encoding="utf-8"))

        # Nothing was cached as a success, and the second run costs no request at all.
        with patch("shared.pdf_sources.requests.get") as again:
            assert get_openalex_fulltext("W55501") is None
        again.assert_not_called()

    def test_the_delay_lapses_and_the_work_is_re_fetched(self):
        """Content that appeared meanwhile is still picked up — the record is a delay,
        not a verdict."""
        import shared.pdf_sources as ps
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc)
                 - timedelta(days=ps.OA_XML_RETRY_AFTER_DAYS + 1)).isoformat()
        self._retry_path("W55502").write_text(json.dumps({"content_free": stale}),
                                              encoding="utf-8")

        with patch("shared.pdf_sources.requests.get",
                   side_effect=[_meta_response(), _xml_response(GROBID_TEI)]):
            result = get_openalex_fulltext("W55502")
        assert result["sections"]["references"]
        # Content arrived: the delay that held the re-fetch back is gone.
        assert not self._retry_path("W55502").exists()
