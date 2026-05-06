"""
pdf_parsing.py — Uniform interface for five PDF/text parsing methods.

Each parse_* function returns the same shape dict so callers can compare
outputs side-by-side without branching on the method:

    {
        "source":     str,            # method name
        "title":      str,
        "abstract":   str,
        "intro":      str,
        "references": list[dict],     # [{authors, year, title, ...}, ...]
        "raw_text":   str,
        "error":      str | None,     # None = success
    }

Public API:
    parse_openalex_xml(oa_xml_data) -> dict
    parse_pdfminer(pdf_path)        -> dict
    parse_grobid(doi_r, pdf_path)   -> dict
    parse_docpluck(pdf_path)        -> dict
    parse_docling(pdf_path)         -> dict
    parse_all(doi_r, pdf_path, oa_xml=None) -> dict[str, dict]
    PARSE_METHODS                   -> list[str]  (keys of parse_all output)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .grobid import run_grobid
from .config import log

PARSE_METHODS: list[str] = ["openalex_xml", "pdfminer", "grobid", "docpluck", "docling"]

_EMPTY: dict[str, Any] = {
    "source": "", "title": "", "abstract": "", "intro": "",
    "references": [], "raw_text": "", "error": None,
}


def _error_result(source: str, error: str) -> dict:
    r = dict(_EMPTY)
    r["source"] = source
    r["error"]  = error
    return r


def _uniform_shape(source: str, partial: dict) -> dict:
    r = dict(_EMPTY)
    r.update(partial)
    r["source"] = source
    if r["error"] is None and "error" not in partial:
        r["error"] = None
    return r


# ── Method 1: OpenAlex GROBID XML ────────────────────────────────────────────

def parse_openalex_xml(oa_xml_data: dict | None) -> dict:
    """Parse a cached OpenAlex GROBID XML result dict."""
    if not oa_xml_data:
        return _error_result("openalex_xml", "no openalex_xml data")
    sections = oa_xml_data.get("sections", {})
    return _uniform_shape("openalex_xml", {
        "abstract":   sections.get("abstract", ""),
        "intro":      sections.get("intro", ""),
        "references": sections.get("references", []),
        "raw_text":   sections.get("raw_text", ""),
    })


# ── Method 2: pdfminer ───────────────────────────────────────────────────────

def parse_pdfminer(pdf_path) -> dict:
    """Extract text sections from a PDF using pdfminer.six."""
    if pdf_path is None:
        return _error_result("pdfminer", "no pdf_path")
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return _error_result("pdfminer", f"file not found: {pdf_path}")
    try:
        from pdfminer.high_level import extract_text
        raw_text = extract_text(str(pdf_path), maxpages=40) or ""
    except ImportError:
        return _error_result("pdfminer", "pdfminer not installed")
    except Exception as exc:
        return _error_result("pdfminer", str(exc))

    from .grobid import _split_sections, _parse_references_block
    sections = _split_sections(raw_text)
    refs_raw  = sections.pop("references_raw", "")
    references = _parse_references_block(refs_raw)
    return _uniform_shape("pdfminer", {
        "abstract":   sections.get("abstract", ""),
        "intro":      sections.get("intro", ""),
        "references": references,
        "raw_text":   raw_text[:5000],
    })


# ── Method 3: GROBID ─────────────────────────────────────────────────────────

def parse_grobid(doi_r: str, pdf_path, no_llm: bool = False) -> dict:
    """Run the GROBID pipeline (pdfminer + fallbacks) for one PDF."""
    if pdf_path is None:
        return _error_result("grobid", "no pdf_path")
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return _error_result("grobid", f"file not found: {pdf_path}")
    result = run_grobid(doi_r, pdf_path, no_llm=no_llm)
    if result.get("grobid_status") in ("no_pdf", "pdfminer_failed"):
        return _error_result("grobid", result["grobid_status"])
    sections = result.get("sections", {})
    return _uniform_shape("grobid", {
        "abstract":   sections.get("abstract", ""),
        "intro":      sections.get("intro", ""),
        "references": sections.get("references", []),
    })


# ── Method 4: Docpluck ───────────────────────────────────────────────────────

def parse_docpluck(pdf_path) -> dict:
    """Extract text using the docpluck library (pip install docpluck)."""
    if pdf_path is None:
        return _error_result("docpluck", "no pdf_path")
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return _error_result("docpluck", f"file not found: {pdf_path}")
    try:
        import docpluck  # type: ignore
    except ImportError:
        return _error_result("docpluck", "docpluck not installed")
    try:
        doc = docpluck.parse(str(pdf_path))
        raw_text = getattr(doc, "text", "") or ""
        abstract = getattr(doc, "abstract", "") or ""
        return _uniform_shape("docpluck", {
            "abstract": abstract,
            "raw_text": raw_text[:5000],
        })
    except Exception as exc:
        return _error_result("docpluck", str(exc))


# ── Method 5: Docling ────────────────────────────────────────────────────────

def parse_docling(pdf_path) -> dict:
    """Extract text using the docling library (pip install docling)."""
    if pdf_path is None:
        return _error_result("docling", "no pdf_path")
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return _error_result("docling", f"file not found: {pdf_path}")
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except (ImportError, TypeError):
        return _error_result("docling", "docling not installed")
    try:
        converter = DocumentConverter()
        result    = converter.convert(str(pdf_path))
        doc       = result.document
        raw_text  = doc.export_to_markdown() if hasattr(doc, "export_to_markdown") else ""
        return _uniform_shape("docling", {"raw_text": raw_text[:5000]})
    except Exception as exc:
        return _error_result("docling", str(exc))


# ── Orchestrator ─────────────────────────────────────────────────────────────

def parse_all(doi_r: str, pdf_path, oa_xml: dict | None = None,
             no_llm: bool = False) -> dict[str, dict]:
    """Run all parsing methods and return a dict keyed by method name."""
    return {
        "openalex_xml": parse_openalex_xml(oa_xml),
        "pdfminer":     parse_pdfminer(pdf_path),
        "grobid":       parse_grobid(doi_r, pdf_path, no_llm=no_llm),
        "docpluck":     parse_docpluck(pdf_path),
        "docling":      parse_docling(pdf_path),
    }
