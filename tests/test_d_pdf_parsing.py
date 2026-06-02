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


class TestParseMarkitdown:
    def test_returns_error_when_pdf_path_is_none(self):
        from shared.pdf_parsing import parse_markitdown
        r = parse_markitdown(None, doi_r="10.1234/test")
        assert r["error"] is not None
        assert r["source"] == "markitdown"

    def test_returns_error_when_doi_r_empty(self, tmp_path):
        from shared.pdf_parsing import parse_markitdown
        r = parse_markitdown(tmp_path / "fake.pdf", doi_r="")
        assert r["error"] is not None
        assert r["source"] == "markitdown"

    def test_returns_error_when_file_missing(self, tmp_path, monkeypatch):
        from shared import config as cfg
        monkeypatch.setattr(cfg, "MARKITDOWN_CACHE_DIR", tmp_path / "md")
        (tmp_path / "md").mkdir()
        from shared.pdf_parsing import parse_markitdown
        r = parse_markitdown(tmp_path / "missing.pdf", doi_r="10.1234/x")
        assert r["error"] is not None

    def test_uses_cached_md_if_present(self, tmp_path, monkeypatch):
        from shared import config as cfg
        monkeypatch.setattr(cfg, "MARKITDOWN_CACHE_DIR", tmp_path)
        from shared.utils import cache_key
        key = cache_key("10.1234/cached")
        (tmp_path / f"{key}.md").write_text(
            "# Abstract\nCached abstract text.\n# Introduction\nIntro here.",
            encoding="utf-8",
        )
        from shared.pdf_parsing import parse_markitdown
        r = parse_markitdown(tmp_path / "any.pdf", doi_r="10.1234/cached")
        assert r["error"] is None
        assert "Cached abstract text" in r["abstract"]
        assert "Intro here" in r["intro"]

    def test_markitdown_not_installed_returns_error(self, tmp_path, monkeypatch):
        import builtins
        from shared import config as cfg
        monkeypatch.setattr(cfg, "MARKITDOWN_CACHE_DIR", tmp_path / "md")
        (tmp_path / "md").mkdir()
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        real_import = builtins.__import__
        def _mock_import(name, *args, **kwargs):
            if name == "markitdown":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", _mock_import)
        from shared.pdf_parsing import parse_markitdown
        r = parse_markitdown(pdf, doi_r="10.1234/nolib")
        assert r["error"] is not None

    def test_markitdown_in_parse_methods(self):
        from shared.pdf_parsing import PARSE_METHODS
        assert "markitdown" in PARSE_METHODS

    def test_parse_all_includes_markitdown_key(self, tmp_path, monkeypatch):
        from shared import config as cfg
        monkeypatch.setattr(cfg, "MARKITDOWN_CACHE_DIR", tmp_path)
        from shared.pdf_parsing import parse_all
        with patch("shared.pdf_parsing.parse_markitdown",
                   return_value=_error_result("markitdown", "no pdf_path")):
            result = parse_all("10.1234/x", pdf_path=None, oa_xml=None)
        assert "markitdown" in result
