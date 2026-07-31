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


class TestOutcomeText:
    """FLoRA reads the outcome from the abstract and, failing that, from the
    discussion and conclusion — not from the front of the paper."""

    _PAPER = (
        "Title of the paper\n\nAbstract\nWe attempted a replication.\n\n"
        "1. Introduction\n" + ("Prior work failed to replicate the effect. " * 60) +
        "\n2. Methods\n" + ("We recruited participants. " * 60) +
        "\n3. Results\n" + ("The effect was estimated. " * 40) +
        "\n4. General Discussion\n" + ("We did not replicate the original effect. " * 30) +
        "\nReferences\nSmith, J. (2010). A paper.\n"
    )

    def test_returns_the_discussion_not_the_introduction(self):
        from shared.pdf_parsing import outcome_text
        text, provenance = outcome_text(self._PAPER)
        assert provenance == "discussion"
        assert text.startswith("4. General Discussion")
        assert "Prior work failed to replicate" not in text

    def test_stops_before_the_reference_list(self):
        from shared.pdf_parsing import outcome_text
        text, _ = outcome_text(self._PAPER)
        assert "Smith, J. (2010)" not in text

    def test_falls_back_to_the_tail_when_no_heading(self):
        from shared.pdf_parsing import outcome_text
        paper = ("Introduction\n" + ("background prose. " * 200)
                 + "CLOSING STATEMENT the effect did not replicate.\n"
                 + "References\nSmith, J. (2010). A paper.\n")
        text, provenance = outcome_text(paper)
        assert provenance == "tail"
        assert "CLOSING STATEMENT" in text
        assert "Smith, J. (2010)" not in text

    def test_structured_abstract_label_does_not_win(self):
        """'Discussion:' in a structured abstract sits at the front of the paper —
        taking it would hand back the introduction this function exists to avoid."""
        from shared.pdf_parsing import outcome_text
        paper = ("Discussion: we consider the implications.\n"
                 + ("body prose about methods. " * 300)
                 + "final paragraph with the verdict.\n")
        text, provenance = outcome_text(paper)
        assert provenance == "tail"
        assert "final paragraph with the verdict." in text

    def test_heading_with_no_content_is_skipped(self):
        from shared.pdf_parsing import outcome_text
        paper = (("body prose. " * 300) + "\nConclusion\n")
        text, provenance = outcome_text(paper)
        assert provenance == "tail"

    def test_respects_max_chars(self):
        from shared.pdf_parsing import outcome_text
        text, _ = outcome_text(self._PAPER, max_chars=100)
        assert len(text) <= 100

    def test_an_over_long_discussion_keeps_its_ending(self):
        """The verdict is in the closing paragraphs, so a head-only truncation
        drops exactly the sentence the escalation exists to read."""
        from shared.pdf_parsing import outcome_text
        paper = ("Introduction\n" + ("background prose. " * 800)
                 + "\n4. General Discussion\n"
                 + ("We consider the implications at length. " * 300)
                 + "In sum, the original effect did not replicate.\n"
                 + "References\nSmith, J. (2010). A paper.\n")
        text, provenance = outcome_text(paper, max_chars=2000)
        assert provenance == "discussion"
        assert len(text) <= 2000
        assert text.startswith("4. General Discussion")
        assert "In sum, the original effect did not replicate." in text
        assert "background prose" not in text

    def test_empty_input(self):
        from shared.pdf_parsing import outcome_text
        assert outcome_text("") == ("", "none")
        assert outcome_text("   ") == ("", "none")

    def test_in_text_mention_is_not_a_heading(self):
        from shared.pdf_parsing import outcome_text
        paper = ("Introduction\n" + ("prose. " * 100)
                 + "We return to this point in the Discussion of our findings below. "
                 + ("more prose. " * 200)
                 + "\nDiscussion\n" + ("the effect replicated. " * 30))
        text, provenance = outcome_text(paper)
        assert provenance == "discussion"
        assert text.startswith("Discussion")


class TestDirectRefsCacheIdentity:
    """The direct/image reference caches are keyed on filenames, and cache/pdf holds
    one file per DOI — so a replaced PDF, or a changed model, must not read back the
    previous answer."""

    _REFS = {"references": [{"authors": ["Smith"], "year": 2010, "title": "A paper"}]}

    def _run(self, tmp_path, pdf: Path):
        from shared import grobid
        with patch.object(grobid, "GROBID_CACHE_DIR", tmp_path), \
             patch("shared.llm_client.call_gemini_with_pdf",
                   return_value=self._REFS) as call:
            grobid._extract_refs_via_pdf_direct("10.1/x", pdf)
        return call

    def test_same_pdf_hits_the_cache(self, tmp_path):
        pdf = tmp_path / "10.1_x.pdf"
        pdf.write_bytes(b"%PDF-1.4 first version")
        assert self._run(tmp_path, pdf).call_count == 1
        assert self._run(tmp_path, pdf).call_count == 0

    def test_replaced_pdf_at_the_same_path_misses(self, tmp_path):
        pdf = tmp_path / "10.1_x.pdf"
        pdf.write_bytes(b"%PDF-1.4 first version")
        self._run(tmp_path, pdf)
        pdf.write_bytes(b"%PDF-1.4 a different paper entirely")
        assert self._run(tmp_path, pdf).call_count == 1

    def test_changed_model_misses(self, tmp_path, monkeypatch):
        from shared import grobid
        pdf = tmp_path / "10.1_x.pdf"
        pdf.write_bytes(b"%PDF-1.4 first version")
        self._run(tmp_path, pdf)
        monkeypatch.setattr(grobid, "GEMINI_MODEL", "gemini-next")
        assert self._run(tmp_path, pdf).call_count == 1

    def test_image_refs_cache_also_names_the_pdf_and_model(self):
        import inspect
        from shared import grobid
        src = inspect.getsource(grobid._extract_refs_via_pdf_images)
        assert "_pdf_fingerprint(pdf_path)" in src
        assert "GEMINI_MODEL" in src
