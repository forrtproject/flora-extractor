"""
pdf_parsing.py — Uniform interface for six PDF/text parsing methods.

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
    parse_openalex_xml(oa_xml_data)  -> dict
    parse_pdfminer(pdf_path)         -> dict
    parse_grobid(doi_r, pdf_path)    -> dict
    parse_docpluck(pdf_path)         -> dict
    parse_docling(pdf_path)          -> dict
    parse_opendataloader(pdf_path)   -> dict
    parse_all(doi_r, pdf_path, oa_xml=None) -> dict[str, dict]
    PARSE_METHODS                    -> list[str]  (keys of parse_all output)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .grobid import run_grobid
from .config import log

PARSE_METHODS: list[str] = ["openalex_xml", "pdfminer", "grobid", "docpluck", "opendataloader", "markitdown"]  # docling excluded (heavy deps)

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
        "raw_text":   raw_text[:50000],
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
            "raw_text": raw_text[:50000],
        })
    except Exception as exc:
        return _error_result("docpluck", str(exc))


# ── Method 5: Docling (disabled — heavy deps trigger Flask reloader on import) ─
#
# def parse_docling(pdf_path) -> dict:
#     """Extract text using the docling library (pip install docling)."""
#     if pdf_path is None:
#         return _error_result("docling", "no pdf_path")
#     pdf_path = Path(pdf_path)
#     if not pdf_path.exists():
#         return _error_result("docling", f"file not found: {pdf_path}")
#     try:
#         from docling.document_converter import DocumentConverter  # type: ignore
#     except (ImportError, TypeError):
#         return _error_result("docling", "docling not installed")
#     try:
#         converter = DocumentConverter()
#         result    = converter.convert(str(pdf_path))
#         doc       = result.document
#         raw_text  = doc.export_to_markdown() if hasattr(doc, "export_to_markdown") else ""
#         return _uniform_shape("docling", {"raw_text": raw_text[:5000]})
#     except Exception as exc:
#         return _error_result("docling", str(exc))


def parse_docling(pdf_path) -> dict:
    """Stub — docling is disabled (heavy deps). Install docling and uncomment the implementation."""
    return _error_result("docling", "disabled — install docling to enable")


# ── Method 6: OpenDataLoader ─────────────────────────────────────────────────

def parse_opendataloader(pdf_path) -> dict:
    """Extract text using opendataloader_pdf (pip install -U opendataloader-pdf).

    Requires Java 11+ on the system PATH. Each call spawns a JVM process,
    so it is slower than the other parsers but can handle complex layouts.
    """
    if pdf_path is None:
        return _error_result("opendataloader", "no pdf_path")
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return _error_result("opendataloader", f"file not found: {pdf_path}")
    try:
        import opendataloader_pdf  # type: ignore
    except ImportError:
        return _error_result("opendataloader", "opendataloader_pdf not installed (pip install -U opendataloader-pdf)")
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            opendataloader_pdf.convert(
                input_path=str(pdf_path),
                output_dir=tmpdir,
                format="markdown",
                quiet=True,
            )
            out_file = Path(tmpdir) / (pdf_path.stem + ".md")
            if not out_file.exists():
                md_files = list(Path(tmpdir).glob("*.md"))
                if not md_files:
                    return _error_result("opendataloader", "convert() produced no output file")
                out_file = md_files[0]
            raw_text = out_file.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return _error_result("opendataloader", str(exc))

    from .grobid import _split_sections, _parse_references_block
    sections  = _split_sections(raw_text)
    refs_raw  = sections.pop("references_raw", "")
    references = _parse_references_block(refs_raw)
    return _uniform_shape("opendataloader", {
        "abstract":   sections.get("abstract", ""),
        "intro":      sections.get("intro", ""),
        "references": references,
        "raw_text":   raw_text[:50000],
    })


# ── Method 7: MarkItDown ─────────────────────────────────────────────────────

def _md_section(text: str, heading: str) -> str:
    """
    Extract a named section from markdown text using a line-by-line scanner.

    Handles all common academic PDF heading styles produced by MarkItDown:
      • Markdown headers:   # Abstract  /  ## Introduction
      • Bold headings:      **Abstract**  /  __Introduction__
      • ALL-CAPS lines:     ABSTRACT  /  INTRODUCTION
      • Numbered sections:  1. Introduction  /  1 Introduction
      • Plain with colon:   Abstract:
    """
    import re

    h_lower = heading.lower().strip()

    # Patterns that indicate any section heading — used to stop collecting
    _md_hdr   = re.compile(r"^#{1,4}\s+\S", re.IGNORECASE)
    _bold_hdr = re.compile(r"^(?:\*{2}|__).+?(?:\*{2}|__)\s*$")
    _caps_hdr = re.compile(r"^[A-Z][A-Z\s\-]{2,60}$")
    _num_hdr  = re.compile(r"^\d+\.?\s+[A-Z]")

    def _heading_text(line: str) -> str | None:
        """Return normalised heading text if line is a heading, else None."""
        s = line.strip()
        if not s:
            return None
        if _md_hdr.match(s):
            return re.sub(r"^#{1,4}\s+", "", s).lower().strip()
        if _bold_hdr.match(s):
            return re.sub(r"^\*{2}|^__|__$|\*{2}$", "", s).lower().strip()
        if _caps_hdr.match(s) and not s[0].isdigit():
            return s.lower().strip()
        if _num_hdr.match(s):
            return re.sub(r"^\d+\.?\s+", "", s).lower().strip()
        return None

    def _matches_heading(norm: str) -> bool:
        """True if the normalised heading text refers to the section we want."""
        return norm == h_lower or norm.startswith(h_lower)

    lines      = text.splitlines()
    start_idx  = None

    for i, line in enumerate(lines):
        norm = _heading_text(line)
        if norm is not None and _matches_heading(norm):
            start_idx = i + 1
            break

    if start_idx is None:
        return ""

    content: list[str] = []
    for line in lines[start_idx:]:
        norm = _heading_text(line)
        if norm is not None and not _matches_heading(norm):
            break
        content.append(line)

    return "\n".join(content).strip()[:3000]


def _md_references(text: str) -> list[dict]:
    """
    Parse a basic reference list from MarkItDown text.
    Returns a list of {"raw": str} dicts — enough for the scoring function to
    count references, even without full structured metadata.
    """
    import re
    refs_text = _md_section(text, "references") or _md_section(text, "bibliography")
    if not refs_text:
        return []
    # Split on numbered entries (1. / [1] / •) or blank-line-separated blocks
    items = re.split(r"\n(?=\[\d+\]|\d+\.\s|\d+\s+[A-Z]|•\s)", refs_text)
    result = []
    for item in items:
        item = item.strip()
        # Strip leading markers
        item = re.sub(r"^(?:\[\d+\]|\d+\.?)\s*", "", item).strip()
        if len(item) > 15:          # skip very short noise entries
            result.append({"raw": item[:300]})
        if len(result) >= 60:
            break
    return result


def parse_markitdown(pdf_path, doi_r: str) -> dict:
    """Convert PDF to Markdown using Microsoft MarkItDown. Caches .md to MARKITDOWN_CACHE_DIR.

    doi_r is required for a stable cache key — PDF paths change, DOIs don't.
    """
    from .config import MARKITDOWN_CACHE_DIR
    from .utils import cache_key as _ck

    if pdf_path is None:
        return _error_result("markitdown", "no pdf_path")
    if not doi_r:
        return _error_result("markitdown", "doi_r required for cache key")

    pdf_path = Path(pdf_path)
    key      = _ck(doi_r)
    md_path  = MARKITDOWN_CACHE_DIR / f"{key}.md"

    if md_path.exists():
        raw_text = md_path.read_text(encoding="utf-8", errors="replace")
    else:
        if not pdf_path.exists():
            return _error_result("markitdown", f"file not found: {pdf_path}")
        try:
            from markitdown import MarkItDown  # type: ignore
        except ImportError:
            return _error_result(
                "markitdown",
                "markitdown not installed — run: pip install markitdown",
            )
        try:
            result   = MarkItDown().convert(str(pdf_path))
            raw_text = result.text_content or ""
        except Exception as exc:
            return _error_result("markitdown", str(exc))
        try:
            md_path.write_text(raw_text, encoding="utf-8")
        except Exception:
            pass  # cache write failure is non-fatal

    abstract = _md_section(raw_text, "abstract")
    intro    = _md_section(raw_text, "introduction")
    refs     = _md_references(raw_text)

    # Fallback: if abstract is empty, skip the header block (first ~15 lines of
    # journal metadata) and take the next substantial paragraph.
    if not abstract:
        lines = raw_text.splitlines()
        skip  = min(15, len(lines))
        body  = "\n".join(lines[skip:]).strip()
        # Take text up to the first heading
        import re
        first_hdg = re.search(
            r"^(?:#{1,4}\s|\*{2}[A-Z]|[A-Z]{3,}$|\d+\.\s)",
            body, re.MULTILINE,
        )
        abstract = (body[: first_hdg.start()].strip() if first_hdg else body[:800])

    return _uniform_shape("markitdown", {
        "abstract":   abstract,
        "intro":      intro,
        "references": refs,
        "raw_text":   raw_text[:50000],
    })


# ── Parse scoring ─────────────────────────────────────────────────────────────

def score_parse_result(r: dict) -> int:
    """
    Score a single parse-method result. Higher = better text for LLM consumption.
    Returns -1 for any result with an error (unusable).

    Scoring rationale:
      - References (×300): the strongest signal of structured extraction — a method
        that found references almost certainly extracted coherent body text too.
      - Intro (×2): longer intro = more context for the LLM; weighted above abstract
        because many papers hide the abstract in the header block.
      - Abstract (×1): valuable but sometimes extracted as metadata noise.
      - Raw text (÷5, capped at 1000): tie-breaker for methods with no structured
        sections (e.g. pdfminer on a poorly structured PDF).
    """
    if r.get("error"):
        return -1
    refs    = len(r.get("references") or [])
    abs_len = len(r.get("abstract")   or "")
    int_len = len(r.get("intro")      or "")
    raw_len = len(r.get("raw_text")   or "")
    return refs * 300 + abs_len + int_len * 2 + min(raw_len // 5, 1000)


def best_parse_result(results: "dict[str, dict]") -> "dict | None":
    """
    Return the highest-scoring parse result from a parse_all() output dict.
    Returns None if all methods errored.
    """
    best_r, best_s = None, -1
    for r in results.values():
        s = score_parse_result(r)
        if s > best_s:
            best_s, best_r = s, r
    return best_r if best_s >= 0 else None


def best_parse_method_name(results: "dict[str, dict]") -> str:
    """Return the key of the highest-scoring parse method, or '' if all errored."""
    best_k, best_s = "", -1
    for k, r in results.items():
        s = score_parse_result(r)
        if s > best_s:
            best_s, best_k = s, k
    return best_k if best_s >= 0 else ""


# ── Orchestrator ─────────────────────────────────────────────────────────────

def parse_all(doi_r: str, pdf_path, oa_xml: dict | None = None,
             no_llm: bool = False) -> dict[str, dict]:
    """Run all parsing methods and return a dict keyed by method name."""
    return {
        "openalex_xml":   parse_openalex_xml(oa_xml),
        "pdfminer":       parse_pdfminer(pdf_path),
        "grobid":         parse_grobid(doi_r, pdf_path, no_llm=no_llm),
        "docpluck":       parse_docpluck(pdf_path),
        # "docling":      parse_docling(pdf_path),  # disabled — heavy deps
        "opendataloader": parse_opendataloader(pdf_path),
        "markitdown":     parse_markitdown(pdf_path, doi_r=doi_r),
    }
