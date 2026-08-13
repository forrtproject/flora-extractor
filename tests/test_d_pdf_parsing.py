"""Tests for shared/pdf_parsing.py — uniform parsing result shape."""
import builtins
from pathlib import Path
from unittest.mock import patch
import pytest

from shared.pdf_parsing import (
    parse_openalex_xml, parse_pdfminer, parse_grobid,
    parse_docpluck, parse_docling, parse_opendataloader,
    parse_markitdown,
)

_SHAPE_KEYS = ("source", "title", "abstract", "intro", "references", "raw_text", "error")


def _fake_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    return pdf


def _docling_absent(tmp_path):
    with patch.dict("sys.modules", {"docling": None, "docling.document_converter": None}):
        return parse_docling(_fake_pdf(tmp_path))


def _markitdown_absent(tmp_path):
    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "markitdown":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", _mock_import):
        return parse_markitdown(_fake_pdf(tmp_path), doi_r="10.1234/nolib")


class TestBadInputErrorShape:
    """Bad input — no path, a path that does not exist, or a missing library — must
    come back as an error-shaped dict from every parser, never as a raised exception:
    parse_all runs all six and the caller scores whatever it gets."""

    @pytest.mark.parametrize("call", [
        pytest.param(lambda p: parse_openalex_xml(None), id="openalex_xml_none"),
        pytest.param(lambda p: parse_pdfminer(None), id="pdfminer_none"),
        pytest.param(lambda p: parse_pdfminer(p / "nonexistent.pdf"), id="pdfminer_missing"),
        pytest.param(lambda p: parse_grobid("10.1234/test", None), id="grobid_none"),
        pytest.param(lambda p: parse_docpluck(p / "nonexistent.pdf"), id="docpluck_missing"),
        pytest.param(lambda p: parse_opendataloader(None), id="opendataloader_none"),
        pytest.param(_docling_absent, id="docling_not_installed"),
        pytest.param(lambda p: parse_markitdown(None, doi_r="10.1234/test"),
                     id="markitdown_none"),
        pytest.param(lambda p: parse_markitdown(p / "fake.pdf", doi_r=""),
                     id="markitdown_no_doi"),
        pytest.param(lambda p: parse_markitdown(p / "missing.pdf", doi_r="10.1234/x"),
                     id="markitdown_missing_file"),
        pytest.param(_markitdown_absent, id="markitdown_not_installed"),
    ])
    def test_returns_uniform_error(self, call, tmp_path, monkeypatch):
        from shared import config as cfg
        md_dir = tmp_path / "md"
        md_dir.mkdir()
        monkeypatch.setattr(cfg, "MARKITDOWN_CACHE_DIR", md_dir)
        r = call(tmp_path)
        assert r["error"] is not None
        for key in _SHAPE_KEYS:
            assert key in r, f"missing key: {key}"


class TestParseOpenAlexXml:
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


class TestParseGrobid:
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


class TestParseMarkitdown:
    def test_uses_cached_md_if_present(self, tmp_path, monkeypatch):
        from shared import config as cfg
        monkeypatch.setattr(cfg, "MARKITDOWN_CACHE_DIR", tmp_path)
        from shared.utils import cache_key
        key = cache_key("10.1234/cached")
        (tmp_path / f"{key}.md").write_text(
            "# Abstract\nCached abstract text.\n# Introduction\nIntro here.",
            encoding="utf-8",
        )
        r = parse_markitdown(tmp_path / "any.pdf", doi_r="10.1234/cached")
        assert r["error"] is None
        assert "Cached abstract text" in r["abstract"]
        assert "Intro here" in r["intro"]


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
                   return_value=(self._REFS, "")) as call:
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
        monkeypatch.setattr(grobid, "PDF_PARSE_MODEL", "gemini-next")
        assert self._run(tmp_path, pdf).call_count == 1


class TestNumericReferenceTitles:
    """REGRESSION (doi_r 10.1016/j.physbeh.2021.113324, PR #122 acceptance run): a
    numeric-style reference list has no "(year)" to cut the title at, so every parsed
    title was the raw citation line — "[2] L.J.T. Balter, et al., Low-grade
    inflammation decrea…" — and one was a bare fragment, "[3] M. Moieni, M.R".
    Both reached title_o."""

    _BLOCK = (
        "References\n"
        "[2] L.J.T. Balter, et al., Low-grade inflammation decreases emotion "
        "recognition - Evidence from the vaccination model of inflammation, "
        "Brain Behav. Immun. 73 (2018) 216-221.\n"
        "[3] M. Moieni, M.R\n"
        "Smith, J. (2009). Attitudes toward HIV. Journal of Social Psychology, 12, 3-9.\n"
        "Thaler, R. (2008). Nudge. Yale University Press.\n"
        "Иванов, И. (2015). Влияние воспаления на распознавание эмоций. "
        "Вопросы психологии, 3, 12-20.\n"
    )

    def _refs(self) -> list[dict]:
        from shared.grobid import _parse_references_block
        return _parse_references_block(self._BLOCK)

    def test_entry_marker_and_author_list_are_not_the_title(self):
        title = self._refs()[0]["title"]
        assert title.startswith("Low-grade inflammation decreases emotion recognition")
        assert "[2]" not in title and "Balter" not in title

    def test_every_reference_keeps_a_title_and_reaches_the_key_namespace(self):
        """No reference is ever dropped or blanked for an awkward title: one that
        vanishes from the @key namespace is invisible to the target prompt and is
        counted in no shortfall. That includes the truncated fragment, the short
        title ("Nudge") and the Cyrillic one."""
        from shared.target_keys import assign_target_keys

        refs = self._refs()
        assert len(refs) == 5, [r["title"] for r in refs]
        assert all(r["title"] for r in refs)
        assert any("Moieni" in r["title"] for r in refs), "the fragment keeps its string"
        assert any(r["title"].startswith("Nudge") for r in refs)
        assert any(r["title"].startswith("Влияние") for r in refs)

        entries, _ = assign_target_keys([], refs)
        assert len(entries) == len(refs), "the prompt must offer every reference"

    def test_an_abbreviation_no_longer_swallows_the_journal(self):
        """REGRESSION: requiring a lowercase character before the sentence period cut
        no title at "… of HIV." and let the journal name into it."""
        assert "Attitudes toward HIV" in [r["title"] for r in self._refs()]


class TestReferenceExtractionOutageIsNotZeroReferences:
    """`call_gemini_with_pdf` used to return None for both "the model found no
    references" and "the model was never reached". The second then became
    `grobid_status: success` with n_refs 0 — a finding — which run_extract's parse
    cache then froze onto the paper for good."""

    def test_provider_failure_raises_rather_than_returning_no_refs(self, tmp_path):
        from shared import grobid
        pdf = _fake_pdf(tmp_path)
        with patch.object(grobid, "GROBID_CACHE_DIR", tmp_path), \
             patch("shared.llm_client.call_gemini_with_pdf",
                   return_value=(None, "quota exhausted on key 1 (429)")):
            with pytest.raises(grobid.ReferenceExtractionUnavailable):
                grobid._extract_refs_via_pdf_direct("10.1/x", pdf)
        # …and nothing was written, so a later run asks again.
        assert list(tmp_path.glob("*_direct_refs_*.json")) == []

    def test_a_model_that_answered_with_nothing_is_still_an_answer(self, tmp_path):
        from shared import grobid
        pdf = _fake_pdf(tmp_path)
        with patch.object(grobid, "GROBID_CACHE_DIR", tmp_path), \
             patch("shared.llm_client.call_gemini_with_pdf",
                   return_value=({"references": []}, "")):
            assert grobid._extract_refs_via_pdf_direct("10.1/x", pdf) == []

    def test_run_grobid_reports_refs_unavailable_and_parse_grobid_errors(self, tmp_path):
        from shared import grobid
        pdf = _fake_pdf(tmp_path)
        sections = {"abstract": "An abstract.", "intro": "", "methods": "",
                    "references": []}
        with patch.object(grobid, "parse_pdf_sections", return_value=sections), \
             patch.object(grobid, "_extract_refs_via_grobid", return_value=[]), \
             patch.object(grobid, "_extract_refs_via_pdf_direct",
                          side_effect=grobid.ReferenceExtractionUnavailable("down")), \
             patch.object(grobid, "_extract_refs_via_pdf_images") as images:
            out = grobid.run_grobid("10.1/x", pdf)
            # The image rung is a fallback for a document the direct rung READ, not
            # for a request that never arrived.
            images.assert_not_called()
        assert out["grobid_status"] == "refs_unavailable"

        with patch("shared.pdf_parsing.run_grobid", return_value=out):
            result = parse_grobid("10.1/x", pdf)
        assert result["error"] == "refs_unavailable"
        assert result["references"] == []


def test_a_transient_failure_is_recognised_by_its_error_not_its_method():
    """What makes a parse uncacheable is the error VALUE, so any method reporting one
    — a new caller, or a new failure mode of an existing one — is covered without
    anything else knowing about it."""
    from shared.pdf_parsing import parse_result_has_transient_failure as transient

    ok = {"source": "pdfminer", "raw_text": "text", "references": [], "error": None}
    settled = {"source": "grobid", "raw_text": "", "references": [], "error": "no_pdf"}
    outage = {"source": "markitdown", "raw_text": "", "references": [],
              "error": "refs_unavailable"}

    assert not transient({"pdfminer": ok, "grobid": settled})
    assert transient({"pdfminer": ok, "markitdown": outage})


# ── Word documents ────────────────────────────────────────────────────────────

def _docx(tmp_path: Path, body: str) -> Path:
    """A minimal Word file on disk: a ZIP whose word/document.xml holds *body*."""
    import zipfile
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paras = "".join(f'<w:p><w:r><w:t>{line}</w:t></w:r></w:p>'
                    for line in body.splitlines() if line)
    path = tmp_path / "paper.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>'
                   f'{paras}</w:body></w:document>')
    return path


class TestWordDocuments:
    """A .docx is parsed by parse_docx and named by it, so a reader can tell a Word
    extraction from a PDF one."""

    def test_a_word_file_is_parsed_to_sections(self, tmp_path):
        from shared.pdf_parsing import parse_docx
        body = ("Abstract\nWe replicated Smith (2009).\n"
                "Introduction\nThe original reported a large effect.\n"
                "References\nSmith, J. (2009). A paper. Journal, 1, 1-10.")
        out = parse_docx(_docx(tmp_path, body))
        assert out["error"] is None
        assert out["source"] == "docx"
        assert "We replicated Smith" in out["raw_text"]
        assert "large effect" in out["intro"]

    def test_a_zip_that_is_not_a_word_file_errors(self, tmp_path):
        import zipfile
        from shared.pdf_parsing import parse_docx
        path = tmp_path / "data.docx"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("data/values.csv", "a,b\n1,2\n")
        assert parse_docx(path)["error"] == "no word/document.xml text"

    def test_parse_all_sends_a_word_file_to_parse_docx_alone(self, tmp_path):
        """The six PDF methods have no answer about a Word file, and an error-shaped
        result scores -1 rather than losing to the real parse on merit."""
        from shared.pdf_parsing import best_parse_result, parse_all
        path = _docx(tmp_path, "\n".join(f"Sentence {i}." for i in range(40)))
        results = parse_all("10.31219/osf.io/x", path)
        assert set(results) == {"openalex_xml", "docx"}
        assert best_parse_result(results)["source"] == "docx"

    def test_a_pdf_still_goes_to_the_pdf_methods(self, tmp_path):
        from shared.pdf_parsing import parse_all
        results = parse_all("10.1/x", _fake_pdf(tmp_path))
        assert "docx" not in results
        assert "pdfminer" in results


class TestReferenceHeadingForms:
    """The references heading may be numbered ("7. References") or carry a colon
    ("References:"), and the first entry after it may start with an author, a "[1]"
    marker or a "1." number — all forms observed in the direct-PDF fallback corpus,
    where each cost a paid Gemini call for a list the text already held."""

    BODY = "Introduction\nWe replicated Smith.\n"

    @pytest.mark.parametrize("heading, first_entry", [
        ("References", "Smith, J. (2010). A paper. Journal, 1, 1-2."),
        ("7.  References", "Smith, J. (2010). A paper. Journal, 1, 1-2."),
        ("6. REFERENCES", "[1] Smith, J. (2010). A paper. Journal, 1, 1-2."),
        ("References:", "1. Smith J, Jones B (2010) A paper. Journal 1:1-2."),
    ])
    def test_heading_form_yields_a_reference_block(self, heading, first_entry):
        from shared.grobid import _split_sections
        text = f"{self.BODY}\n{heading}\n{first_entry}\n"
        assert "Smith" in _split_sections(text)["references_raw"]
