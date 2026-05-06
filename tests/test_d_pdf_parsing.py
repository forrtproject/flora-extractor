"""Tests for shared/pdf_parsing.py — uniform parsing result shape."""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from shared.pdf_parsing import (
    _error_result, _uniform_shape,
    parse_openalex_xml, parse_pdfminer, parse_grobid,
    parse_docpluck, parse_docling,
    parse_all, PARSE_METHODS,
)


class TestUniformShape:
    def test_error_result_has_all_keys(self):
        r = _error_result("pdfminer", "failed")
        for key in ("source", "title", "abstract", "intro", "references", "raw_text", "error"):
            assert key in r, f"missing key: {key}"
        assert r["source"] == "pdfminer"
        assert r["error"] == "failed"

    def test_uniform_shape_fills_missing_keys(self):
        r = _uniform_shape("grobid", {"intro": "hello"})
        assert r["source"] == "grobid"
        assert r["intro"] == "hello"
        assert r["abstract"] == ""
        assert r["references"] == []
        assert r["error"] is None


class TestParseOpenAlexXml:
    def test_returns_error_when_input_none(self):
        r = parse_openalex_xml(None)
        assert r["error"] is not None

    def test_returns_sections_from_cached_dict(self):
        cached = {
            "source": "openalex_xml",
            "sections": {
                "abstract": "We replicated the effect.",
                "intro":    "In this study...",
                "references": [{"authors": ["Smith"], "year": 2005, "title": "A study"}],
            }
        }
        r = parse_openalex_xml(cached)
        assert r["source"] == "openalex_xml"
        assert r["abstract"] == "We replicated the effect."
        assert len(r["references"]) == 1
        assert r["error"] is None


class TestParsePdfminer:
    def test_returns_error_when_path_none(self):
        r = parse_pdfminer(None)
        assert r["error"] is not None

    def test_returns_error_when_file_not_found(self, tmp_path):
        r = parse_pdfminer(tmp_path / "nonexistent.pdf")
        assert r["error"] is not None


class TestParseGrobid:
    def test_returns_error_when_path_none(self):
        r = parse_grobid("10.1234/test", None)
        assert r["error"] is not None

    def test_calls_run_grobid_and_maps_sections(self, tmp_path):
        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4")
        mock_grobid_result = {
            "grobid_status": "success",
            "n_refs_parsed": 2,
            "sections": {
                "abstract": "We replicated.",
                "intro":    "This is the intro.",
                "methods":  "We used the same method.",
                "references": [
                    {"authors": ["Jones"], "year": 2010, "title": "Original study"},
                ],
            }
        }
        with patch("shared.pdf_parsing.run_grobid", return_value=mock_grobid_result):
            r = parse_grobid("10.1234/test", fake_pdf)
        assert r["source"] == "grobid"
        assert r["abstract"] == "We replicated."
        assert len(r["references"]) == 1
        assert r["error"] is None


class TestParseDocling:
    def test_returns_error_when_docling_not_installed(self, tmp_path):
        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4")
        with patch.dict("sys.modules", {"docling": None, "docling.document_converter": None}):
            r = parse_docling(fake_pdf)
        assert r["error"] is not None


class TestParseAll:
    def test_returns_dict_with_all_method_keys(self, tmp_path):
        fake_pdf = tmp_path / "test.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4")

        oa_xml_data = {"source": "openalex_xml", "sections": {"abstract": "hello"}}

        with patch("shared.pdf_parsing.parse_pdfminer", return_value=_error_result("pdfminer", "skip")), \
             patch("shared.pdf_parsing.parse_grobid",   return_value=_error_result("grobid",   "skip")), \
             patch("shared.pdf_parsing.parse_docling",  return_value=_error_result("docling",  "not installed")), \
             patch("shared.pdf_parsing.parse_docpluck", return_value=_error_result("docpluck", "not installed")):
            results = parse_all("10.1234/t", fake_pdf, oa_xml=oa_xml_data)

        for method in PARSE_METHODS:
            assert method in results
