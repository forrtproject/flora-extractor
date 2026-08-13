"""
pdf_parsing.py — Uniform interface for the PDF/text parsing methods.

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
    parse_docx(docx_path)            -> dict
    parse_all(doi_r, pdf_path, oa_xml=None) -> dict[str, dict]
    PARSE_METHODS                    -> list[str]  (keys parse_all can return)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import json

from .grobid import TEXT_EXTRACTION_VERSION, run_grobid
from .config import PARSE_CACHE_DIR, log
from .utils import cache_key

# "docx" is not one of the PDF methods — it is the only method a Word file gets, and
# the PDF methods are the only ones a PDF gets. See parse_all.
# The KEYS parse_all returns. The `openalex_xml` slot carries whichever structured
# source supplied the document, and the result's own `source` field names it — so a
# row's parse_method can read `epmc_xml` while the key here stays `openalex_xml`.
PARSE_METHODS: list[str] = ["openalex_xml", "pdfminer", "grobid", "docpluck", "opendataloader", "markitdown", "docx"]  # docling excluded (heavy deps)

# Stored raw_text is what the outcome coder later splits for the discussion block,
# which sits at the END of the text — a cap that bites cuts it off first. 200k chars
# covers all but book-length documents while keeping parse cache entries bounded.
_RAW_TEXT_CAP = 200_000

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
    """Parse a structured-source result dict — sections, not a file.

    Every source in `_STRUCTURED_SOURCES` (shared/pdf_sources.py) arrives through
    this one channel, so the `source` field of the result — which becomes the row's
    `parse_method` — is taken from the document rather than stamped: Europe PMC JATS
    is `epmc_xml`, and calling it `openalex_xml` would contradict the `pdf_source`
    written beside it. A document that names no source is OpenAlex's, which is the
    only one that predates the field.

    The dict KEY `parse_all` files this under stays `openalex_xml` whatever the
    source is (PARSE_METHODS), so cached parse results keep one shape.
    """
    if not oa_xml_data:
        return _error_result("openalex_xml", "no openalex_xml data")
    sections = oa_xml_data.get("sections", {})
    return _uniform_shape(str(oa_xml_data.get("source") or "openalex_xml"), {
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
        from .grobid import pdf_text_head_tail
        raw_text = pdf_text_head_tail(pdf_path)
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
        "raw_text":   raw_text[:_RAW_TEXT_CAP],
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
    # refs_unavailable belongs with the failures, not with the successes: the method
    # has no answer about this document's references, and a shape carrying [] would
    # be read — and cached — as the answer "none".
    if result.get("grobid_status") in ("no_pdf", "pdfminer_failed", "refs_unavailable"):
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
            "raw_text": raw_text[:_RAW_TEXT_CAP],
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
        "raw_text":   raw_text[:_RAW_TEXT_CAP],
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
        "raw_text":   raw_text[:_RAW_TEXT_CAP],
    })


# ── Method 8: Word (.docx) ───────────────────────────────────────────────────
# A preprint server hands back whatever the author uploaded, and for OSF that is a
# Word file about as often as a PDF: of twelve campaign DOIs probed on 2026-08-08,
# five answered with a PDF and seven with a .docx. Every one of the seven used to be
# thrown away for its format alone.
#
# A .docx is a ZIP of XML, so the standard library reaches the text — which is also
# the only option here: neither python-docx nor markitdown is installed in this
# checkout, and a parser that needs an install is a parser that silently returns
# nothing on the machine that skipped it.

_DOCX_BODY = "word/document.xml"
_W_NS      = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(source: "Path | str | bytes") -> str:
    """The paragraph text of a Word document, or "" when *source* is not one.

    Takes bytes so the acquisition tier can test a download before it saves it, or a
    path so this module can read one that was saved.

    Paragraphs are joined by a blank line rather than run together, because everything
    downstream — the section splitter, the reference-block parser, outcome_text() —
    reads line structure, and a single wall of text hides every heading in the file.

    Never raises: a ZIP that is not a Word document, or a body that will not parse, is
    "", and the caller decides what that means. The two callers mean different things
    by it (not a document / not parseable), so neither judgement belongs here.
    """
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            if _DOCX_BODY not in bundle.namelist():
                return ""     # some other ZIP — an epub, a spreadsheet, a data archive
            root = ET.fromstring(bundle.read(_DOCX_BODY))
    except Exception as exc:
        log.debug("Word document unreadable: %s", exc)
        return ""

    paragraphs: list[str] = []
    for para in root.iter(f"{_W_NS}p"):
        # A paragraph's text is split across runs at every formatting change, so the
        # runs are joined without a separator and the paragraph is the unit.
        line = "".join(node.text or "" for node in para.iter(f"{_W_NS}t")).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def parse_docx(docx_path) -> dict:
    """Extract text sections from a Word document, in the shape every method returns."""
    if docx_path is None:
        return _error_result("docx", "no docx_path")
    docx_path = Path(docx_path)
    if not docx_path.exists():
        return _error_result("docx", f"file not found: {docx_path}")

    raw_text = docx_text(docx_path)
    if not raw_text:
        return _error_result("docx", "no word/document.xml text")

    from .grobid import _split_sections, _parse_references_block
    sections   = _split_sections(raw_text)
    refs_raw   = sections.pop("references_raw", "")
    references = _parse_references_block(refs_raw)
    return _uniform_shape("docx", {
        "abstract":   sections.get("abstract", ""),
        "intro":      sections.get("intro", ""),
        "references": references,
        "raw_text":   raw_text[:_RAW_TEXT_CAP],
    })


# ── Outcome-bearing text ──────────────────────────────────────────────────────
# FLoRA codes the outcome from the abstract and, when the abstract does not state
# it, from "what is written in the report (discussion and conclusion sections)"
# (FLoRA FAQ, "What is the outcome based on?"). The escalation used to send the
# first 8,000 characters of the parsed text, which for a typical article ends
# somewhere in the methods: it read the introduction — the one section that
# routinely reports OTHER studies' replication failures — and truncated away the
# sections the rule names.

import re as _re

# A heading line, not a mention in running prose: anchored to the start of a line
# and allowed only a short tail (a subtitle, a page number), so "in the Discussion
# we return to…" cannot match.
_DISCUSSION_HEADER = _re.compile(
    r"(?im)^[^\S\n]{0,8}(?:\d+[.\s]{1,3})?(?:general\s+|overall\s+)?"
    r"(?:discussion|conclusions?|concluding\s+remarks|"
    r"(?:summary|discussion)\s+and\s+(?:discussion|conclusions?|implications))"
    r"[^\S\n]*[:.]?[^\n]{0,40}$"
)

# The last section heading before the discussion can start is the references one,
# and that boundary is already solved: the same pattern grobid._split_sections
# uses, taking the last match so an in-text "References show…" cannot win.
def _references_start(text: str) -> int:
    from .grobid import _SECTION_HEADERS
    matches = list(_SECTION_HEADERS["references"].finditer(text))
    return matches[-1].start() if matches else len(text)


# Below this fraction of the body a "Discussion" heading is almost always a
# structured abstract's own label ("Discussion: we find…") rather than the
# section, and taking it would hand the model the front of the paper again —
# exactly the failure this function exists to remove.
_MIN_DISCUSSION_POSITION = 0.4

# A heading with almost nothing under it is a table of contents entry or a
# running header, not the section.
_MIN_DISCUSSION_CHARS = 300

# Marks the gap in a discussion kept head-and-tail, so the model does not read the
# two halves as consecutive text.
_ELLIPSIS = "\n…\n"


def outcome_text(raw_text: str, max_chars: int = 8000) -> tuple[str, str]:
    """The part of a paper that states its own replication verdict.

    Returns (text, provenance) where provenance is one of:
      "discussion" — sliced from the last discussion/conclusion heading to the
                     references, which is what the FLoRA rule asks for;
      "tail"       — no usable heading, so the text immediately before the
                     references. Still the right end of the paper;
      "none"       — nothing to send.

    The caller passes the provenance to the model, so a quote is never attributed
    to a section the model was not shown.
    """
    if not raw_text or not raw_text.strip():
        return "", "none"

    body = raw_text[:_references_start(raw_text)]
    if not body.strip():
        body = raw_text

    for m in reversed(list(_DISCUSSION_HEADER.finditer(body))):
        if m.start() < len(body) * _MIN_DISCUSSION_POSITION:
            break
        block = body[m.start():].strip()
        if len(block) >= _MIN_DISCUSSION_CHARS:
            if len(block) <= max_chars:
                return block, "discussion"
            # A head-only truncation drops the closing paragraphs, which is where
            # the authors state their verdict — keep both ends of the discussion.
            budget = max_chars - len(_ELLIPSIS)
            head = int(budget * 0.6)
            return (f"{block[:head].strip()}{_ELLIPSIS}{block[head - budget:].strip()}",
                    "discussion")

    return body[-max_chars:].strip(), "tail"


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


# Method errors that mean "no answer about this document", not "this document has no
# such content": something prevented the method from answering — a provider outage, a
# safety block, a request that never arrived — and the next attempt may well answer.
# Any method reporting one, whatever produced it, taints the whole parse for caching
# purposes: the other methods' text is fine, but freezing the dict to disk freezes the
# non-answer with it, and every later run reads the outage back as a finding.
TRANSIENT_PARSE_ERRORS = frozenset({"refs_unavailable"})


def parse_result_has_transient_failure(results: "dict[str, dict] | None") -> bool:
    """True when any method in a parse_all() output failed without an answer.

    Keyed on the error VALUE rather than on which method produced it, so a new caller
    (or a new failure mode of an existing one) is covered by reporting one of these
    errors and nothing else has to know about it.
    """
    if not isinstance(results, dict):
        return False
    return any(str((r or {}).get("error", "") or "") in TRANSIENT_PARSE_ERRORS
               for r in results.values() if isinstance(r, dict))


def parse_result_is_empty(results: "dict[str, dict] | None") -> bool:
    """True when no method in a parse_all() output carries any usable text.

    A cached parse with score 0 everywhere — no references, no sections, no raw text —
    is what gets written when the row never acquired a document. It is
    indistinguishable from never having parsed the paper, so readers must treat it as
    a cache miss; the alternative is that one PDF-less run permanently pins a paper to
    abstract-only outcome coding (audit B4).
    """
    if not isinstance(results, dict) or not results:
        return True
    return max((score_parse_result(r) for r in results.values()
                if isinstance(r, dict)), default=0) <= 0


def parse_cache_path(cache_id: str, cache_dir: "Path | None" = None) -> "Path":
    """Where the parse_all() results for *cache_id* live.

    The filename carries `TEXT_EXTRACTION_VERSION` as well as the identity, because
    the ladder reads a cached parse INSTEAD of parsing: without it, a change to what
    the parsers extract never reaches a document already on disk, and the campaign
    re-codes outcomes over text the previous logic produced.

    *cache_id* is the row's identity, not necessarily its DOI: 30% of the
    2026-08-06 handoff carries no DOI, and keying those on `doi_r` filed every one
    of them under `cache_key("") = d41d8cd9…` — a single bucket in which the first
    blank-DOI row's full text was served to every later one. It raises rather than
    returning that shared path, so a caller that loses the identity fails loudly
    instead of silently reading another paper's document.
    """
    if not str(cache_id or "").strip():
        raise ValueError(
            "parse cache needs a row identity (doi_r, or url:/oa:/title: from "
            "primary_key) — an empty one collides across every identity-less row")
    return ((cache_dir or PARSE_CACHE_DIR) /
            f"parse_{cache_key(cache_id)}_v{TEXT_EXTRACTION_VERSION}.json")


def read_parse_cache(cache_id: str, cache_dir: "Path | None" = None) -> "dict | None":
    """Return the cached parse_all() results for *cache_id*, or None on a miss.

    *cache_dir* defaults to PARSE_CACHE_DIR; each caller passes its own module-level
    copy so the directory stays patchable where the caller lives.

    Lives here rather than in either caller because both the ladder
    (`extract/link_original.py`, Stage 6) and `extract/run_extract.py` read the same
    files, and run_extract imports link_original — a reader in either module would
    have had to be duplicated.

    An all-empty cache counts as a miss: it is what a PDF-less run wrote, and reading
    it back would pin the paper to abstract-only coding forever (audit B4).

    So does a cache carrying a transient failure (a method that never got an answer,
    e.g. reference extraction while the provider was down). Such a file should no
    longer be written at all, but the ones written before that was true are on disk,
    and reading them back keeps the outage as this paper's permanent answer about its
    own references. Treating them as a miss re-parses the document — which is what
    heals them, since the re-parse is only cached once every method has answered.
    """
    cache_file = parse_cache_path(cache_id, cache_dir)
    if not cache_file.exists():
        return None
    try:
        with cache_file.open(encoding="utf-8") as fh:
            results = json.load(fh)
    except Exception:
        return None
    if parse_result_is_empty(results) or parse_result_has_transient_failure(results):
        return None
    return results


# ── Orchestrator ─────────────────────────────────────────────────────────────

def parse_all(doi_r: str, pdf_path, oa_xml: dict | None = None,
             no_llm: bool = False) -> dict[str, dict]:
    """Run every method that can read this document, keyed by method name.

    A Word file gets parse_docx and nothing else. The six PDF methods have no answer
    about one — pdfminer and GROBID reject the bytes, the rest are not installed — so
    running them would fill the dict with errors, and an error-shaped result scores -1
    rather than losing to the real parse on merit. The OpenAlex XML is a separate
    document from a separate source, so it is asked either way.
    """
    if pdf_path is not None and Path(pdf_path).suffix.lower() == ".docx":
        return {
            "openalex_xml": parse_openalex_xml(oa_xml),
            "docx":         parse_docx(pdf_path),
        }
    return {
        "openalex_xml":   parse_openalex_xml(oa_xml),
        "pdfminer":       parse_pdfminer(pdf_path),
        "grobid":         parse_grobid(doi_r, pdf_path, no_llm=no_llm),
        "docpluck":       parse_docpluck(pdf_path),
        # "docling":      parse_docling(pdf_path),  # disabled — heavy deps
        "opendataloader": parse_opendataloader(pdf_path),
        "markitdown":     parse_markitdown(pdf_path, doi_r=doi_r),
    }
