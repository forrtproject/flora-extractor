"""
grobid.py — PDF section extraction for original study identification.

Primary method  : pdfminer.six  (local, no external server required)
Fallback 1      : GROBID public server (https://kermitt2-grobid.hf.space)
                  — called when pdfminer extracts 0 references.
Fallback 2      : Gemini with full PDF bytes  (success_direct_llm)
Fallback 3      : Gemini with rendered page images  (success_image_llm)

Public API:
    parse_pdf_sections(pdf_path) → dict
        keys: abstract, intro, methods, references (list[dict])
    run_grobid(doi_r, pdf_path)  → dict
        keys: grobid_status, sections, n_refs_parsed

    # Legacy GROBID wrappers (kept for import compatibility):
    process_pdf_with_grobid(pdf_path) → str | None
    parse_tei_sections(tei_xml)       → dict
"""
import base64
import hashlib
import re
from pathlib import Path
from typing import Optional

import requests

from .config import PDF_PARSE_MODEL, GROBID_CACHE_DIR, GROBID_SERVER, log

# Seconds between GROBID calls. The public HuggingFace server is shared and slow to
# anger; a local Docker GROBID does not need it, but paying 3s on a local server is
# cheaper than getting the shared one to refuse everyone.
GROBID_RATE_SEC = 3.0
from .cache import write_json
from .rate_limit import throttle
from .prompts import PDF_IMAGE_REFERENCES_PROMPT, PDF_REFERENCES_PROMPT, prompt_version
from .utils import clean_citation_title, usable_title

# ── pdfminer import (installed lazily) ───────────────────────────────────────

def _extract_pdf_text(pdf_path: Path, max_pages: int = 40) -> str:
    """
    Extract raw text from *pdf_path* using pdfminer.six.
    Falls back to an empty string on any failure.
    """
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(pdf_path), maxpages=max_pages) or ""
    except Exception as e:
        log.warning("pdfminer failed for %s: %s", pdf_path.name, e)
        return ""


# ── Section splitter ──────────────────────────────────────────────────────────

# Section header keywords (case-insensitive, must appear near start of a line)
_SECTION_HEADERS = {
    "abstract"  : re.compile(r"(?i)^\s*abstract\s*$", re.MULTILINE),
    "intro"     : re.compile(r"(?i)^\s*(?:\d+[\.\s]+)?introduction\b", re.MULTILINE),
    "methods"   : re.compile(r"(?i)^\s*(?:\d+[\.\s]+)?(?:method|material|procedure|"
                              r"participant|design|experiment\s*1\b)", re.MULTILINE),
    # Match references header on its own line OR followed immediately by an author
    "references": re.compile(r"(?i)^\s*(?:references?|bibliography|works\s+cited)"
                              r"(?:\s*\n|\s*$|\s+[A-Z])", re.MULTILINE),
}


def _split_sections(text: str) -> dict:
    """
    Split PDF full-text into abstract / intro / methods / references blocks.
    Returns a dict with the same keys as parse_tei_sections.
    """
    out = {"abstract": "", "intro": "", "methods": "", "references_raw": ""}

    # Find the start position of each section header (take the LAST match for
    # methods/intro since papers sometimes have multiple headings like "1. Method")
    positions: dict[str, int] = {}
    for name, pat in _SECTION_HEADERS.items():
        matches = list(pat.finditer(text))
        if matches:
            # For references, take the LAST occurrence (avoid in-text "References show…")
            # For others, take the FIRST
            positions[name] = (matches[-1].start()
                               if name == "references" else matches[0].start())

    sorted_sections = sorted(positions.items(), key=lambda x: x[1])
    n = len(sorted_sections)

    for i, (name, start) in enumerate(sorted_sections):
        end = sorted_sections[i + 1][1] if i + 1 < n else len(text)
        block = text[start:end].strip()
        if name == "references":
            out["references_raw"] = block
        elif name == "abstract":
            out["abstract"] = block[:2000]
        elif name == "intro":
            out["intro"] = block[:3000]
        elif name == "methods":
            out["methods"] = block[:2000]

    # Fallback abstract: first 1500 chars of the document if no header found
    if not out["abstract"] and text:
        out["abstract"] = text[:1500]

    # Fallback references: if the header-based split found nothing, look for a
    # trailing block where ≥4 lines match the APA "Author, I. (YYYY)." pattern
    if not out["references_raw"]:
        _apa = re.compile(r"^[A-Z][a-z]+,\s+[A-Z].*\(\d{4}\)", re.MULTILINE)
        paragraphs = re.split(r"\n{2,}", text)
        # Walk paragraphs from the end; collect once we hit a dense APA block
        ref_parts: list[str] = []
        for para in reversed(paragraphs):
            hits = len(_apa.findall(para))
            if hits >= 1 or ref_parts:
                ref_parts.append(para)
            # Stop collecting once we've gone past the dense part
            if ref_parts and hits == 0 and len(ref_parts) > 3:
                break
        if len(ref_parts) >= 3:
            out["references_raw"] = "\n\n".join(reversed(ref_parts))

    return out


# ── Reference parser ──────────────────────────────────────────────────────────

# Matches a year in the range 1900-2099
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

def _parse_references_block(block: str) -> list[dict]:
    """
    Parse a raw reference block into a list of dicts:
        authors  list[str]
        year     int | None
        title    str
        raw_ref  str
    """
    if not block:
        return []

    # Split into individual reference entries
    # Strategy: split on lines that look like reference starters
    lines = block.split("\n")
    entries: list[str] = []
    current: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # New entry: starts with [N] or looks like "Surname, I. (YEAR)" — in any
        # script, so a Cyrillic or Greek reference list is split into entries too
        # rather than accumulating into whichever Latin entry preceded it.
        is_new = bool(re.match(r"^\[?\d+\][\.\s]", stripped)) or \
                 bool(re.match(r"^[^\W\d_]{2,},\s+[^\W\d_]", stripped, re.UNICODE) and
                      bool(_YEAR_RE.search(stripped[:100])))
        if is_new and current:
            entries.append(" ".join(current))
            current = [stripped]
        else:
            current.append(stripped)

    if current:
        entries.append(" ".join(current))

    # Parse each entry
    refs: list[dict] = []
    for entry in entries:
        entry = entry.strip()
        if len(entry) < 15:
            continue
        # Skip the section header itself
        if re.match(r"^references?\.?$", entry, re.IGNORECASE):
            continue

        ref: dict = {"authors": [], "year": None, "title": "", "raw_ref": entry}

        # Year: first 4-digit year in 1900-2099 range (within first 150 chars)
        m_year = _YEAR_RE.search(entry[:150])
        if m_year:
            ref["year"] = int(m_year.group(1))

        # Authors: text before "(" or before the year match
        year_start = m_year.start() if m_year else min(len(entry), 60)
        pre_year   = entry[:year_start]
        # Remove numbered prefix [1] or 1.
        pre_year   = re.sub(r"^\[?\d+\]?[\.\s]+", "", pre_year)
        # Remove trailing " (" or "(" left from "(YYYY)"
        pre_year   = re.sub(r"[\s\(]+$", "", pre_year).rstrip(",").strip()
        if pre_year:
            # Take first author only (before " & " or " and ")
            first_auth = re.split(r"\s+&\s+|\s+and\s+", pre_year)[0].strip()
            if first_auth:
                ref["authors"] = [first_auth]

        # Title: text after closing ")" of year group, stripped of leading punct
        post_year = entry[(m_year.end() if m_year else 0):]
        post_year = re.sub(r"^[\s\.\,\)]+", "", post_year)
        # Numeric (Vancouver) references put the year last, so there is no year match
        # to cut at and post_year is still the whole citation: "[2] L.J.T. Balter, et
        # al., Low-grade inflammation …". An entry marker followed by an author list
        # is demonstrably not the title, so it goes before the sentence split runs.
        cleaned = clean_citation_title(post_year)
        # First sentence ending at ". Capital" is the title — but the period after an
        # author's initial ("M. Moieni, M.R") is not a sentence end, so the token
        # before it must be at least two characters ("… of HIV. Journal of …" still
        # splits, in any script).
        title_m = re.match(r"(.{10,}?\w{2})[\.?!]\s+[A-Z]", cleaned, re.UNICODE)
        title = (title_m.group(1) if title_m else cleaned[:200]).strip()
        # Cleaning that leaves something unusable ("M.R") has taken the reference's
        # only description with it, so the longer parsed string is kept instead. A
        # reference is never dropped or blanked for an awkward title — it would vanish
        # from the @key namespace, invisible to the target prompt and counted in no
        # shortfall. usable_title() gates what downstream DOES with a string (its
        # confidence, whether it is searched on), never a record's existence.
        fallback = post_year[:200].strip()
        ref["title"] = (title if usable_title(title) or len(title) >= len(fallback)
                        else fallback)

        # Skip entries with no useful information
        if not ref["title"] and not ref["year"]:
            continue

        refs.append(ref)

    return refs


# ── Main public API ───────────────────────────────────────────────────────────

def parse_pdf_sections(pdf_path: Path) -> dict:
    """
    Extract and return the key sections from *pdf_path*.

    Uses pdfminer.six for local text extraction — no external server needed.
    Caches the result in GROBID_CACHE_DIR/<stem>.json.

    Returns a dict matching the shape used by lib/llm.py:
        abstract   str
        intro      str
        methods    str
        references list[dict]  — each: {authors, year, title, raw_ref}
    """
    import json

    empty = {"abstract": "", "intro": "", "methods": "", "references": []}

    if not pdf_path or not Path(pdf_path).exists():
        return empty

    pdf_path = Path(pdf_path)

    # JSON cache (faster than re-parsing)
    cache_file = GROBID_CACHE_DIR / f"{pdf_path.stem}.json"
    if cache_file.exists() and cache_file.stat().st_size > 50:
        try:
            with cache_file.open(encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass  # corrupt cache — re-extract

    text = _extract_pdf_text(pdf_path)
    if not text.strip():
        log.warning("pdfminer returned empty text for %s", pdf_path.name)
        return empty

    sections = _split_sections(text)
    refs_raw = sections.pop("references_raw", "")
    sections["references"] = _parse_references_block(refs_raw)

    # Cache it
    try:
        write_json(cache_file, sections, indent=2)
    except Exception as e:
        log.debug("Could not cache sections for %s: %s", pdf_path.name, e)

    return sections


_MAX_PDF_BYTES = 45 * 1024 * 1024   # 45 MB safety margin (Gemini limit: 50 MB)


def _pdf_fingerprint(pdf_path: Path) -> str:
    """Short content hash of a PDF, for cache filenames.

    The path is not an identity: a re-downloaded or corrected PDF lands at the
    same cache/pdf/<doi>.pdf and would otherwise replay the old file's references.
    """
    try:
        return hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:12]
    except Exception as e:
        log.debug("Could not fingerprint %s: %s", pdf_path, e)
        return "nohash"


class ReferenceExtractionUnavailable(RuntimeError):
    """The reference extractor never answered.

    Distinct from "this document has no reference list", which is a finding. A
    provider outage is not, and the two arrived as the same empty list: run_grobid
    reported `success` with zero references, run_extract cached the whole parse
    result to disk, and from then on the paper was one that cites nothing — on
    every later run, without another request ever being made.
    """


def _extract_refs_via_pdf_direct(doi_r: str, pdf_path: Path) -> list[dict]:
    """
    Send the full PDF directly to Gemini with MEDIA_RESOLUTION_LOW for reference
    extraction. This is more accurate than image rendering for native-text PDFs
    and uses fewer tokens (Gemini reads embedded text natively without image billing).

    Returns the references the model found — possibly none, which is an answer.
    Raises ReferenceExtractionUnavailable when the provider never answered.

    Falls back silently (returning []) when the PDF exceeds 45 MB or cannot be read:
    those are settled facts about this document, not failures to reach a model.
    """
    import json

    # Prompt version, model and PDF content are all in the filename: change any of
    # them and the previous answer's reference list stops being read back.
    cache_file = (GROBID_CACHE_DIR /
                  f"{pdf_path.stem}_direct_refs_{prompt_version('PDF_REFERENCES_PROMPT')}"
                  f"_{PDF_PARSE_MODEL}_{_pdf_fingerprint(pdf_path)}.json")
    if cache_file.exists():
        try:
            with cache_file.open(encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass

    pdf_size = pdf_path.stat().st_size
    if pdf_size > _MAX_PDF_BYTES:
        log.info("[%s] PDF too large for direct Gemini (%d MB) — skipping",
                 doi_r, pdf_size // (1024 * 1024))
        return []

    try:
        pdf_bytes = pdf_path.read_bytes()
    except Exception as e:
        log.warning("[%s] Could not read PDF: %s", doi_r, e)
        return []

    from .llm_client import call_gemini_with_pdf
    result, err = call_gemini_with_pdf(PDF_REFERENCES_PROMPT, pdf_bytes)

    if err:
        raise ReferenceExtractionUnavailable(f"direct-PDF Gemini: {err}")
    if not result or not isinstance(result.get("references"), list):
        log.info("[%s] Direct-PDF Gemini returned no references", doi_r)
        return []

    refs = []
    for ref in result["references"]:
        if not isinstance(ref, dict):
            continue
        authors = ref.get("authors", [])
        if isinstance(authors, str):
            authors = [authors]
        title = str(ref.get("title", "") or "")
        try:
            year = int(ref["year"]) if ref.get("year") else None
        except (TypeError, ValueError):
            year = None
        if not title and not year:
            continue
        refs.append({"authors": authors, "year": year, "title": title, "raw_ref": ""})

    log.info("[%s] Direct-PDF Gemini: extracted %d refs", doi_r, len(refs))

    if refs:
        try:
            write_json(cache_file, refs, indent=2)
        except Exception:
            pass

    return refs


def _extract_refs_via_pdf_images(doi_r: str, pdf_path: Path) -> list[dict]:
    """
    Render the last N pages of a PDF as grayscale PNG images and ask Gemini
    to extract the reference list. Used as a fallback when pdfminer finds text
    but extracts 0 references (e.g. two-column or non-standard layouts).

    Requires: pip install pymupdf
    Returns [] silently when PyMuPDF is not installed (a fact about this machine,
    settled without asking anyone). Raises ReferenceExtractionUnavailable when the
    provider never answered — see _extract_refs_via_pdf_direct.
    """
    import json

    cache_file = (GROBID_CACHE_DIR /
                  f"{pdf_path.stem}_img_refs_{prompt_version('PDF_IMAGE_REFERENCES_PROMPT')}"
                  f"_{PDF_PARSE_MODEL}_{_pdf_fingerprint(pdf_path)}.json")
    if cache_file.exists():
        try:
            with cache_file.open(encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass

    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.debug("PyMuPDF not installed — skipping image-based ref extraction")
        return []

    try:
        doc        = fitz.open(str(pdf_path))
        n_pages    = len(doc)
        # References are typically in the last ~20 % of pages; clamp 1–6
        n_ref_pages = min(6, max(1, round(n_pages * 0.20)))
        page_nums   = list(range(max(0, n_pages - n_ref_pages), n_pages))

        images = []
        for pnum in page_nums:
            page = doc[pnum]
            # 1.5× zoom, greyscale → smaller payload, still readable
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5),
                                   colorspace=fitz.csGRAY)
            images.append({
                "mime_type": "image/png",
                "data"     : base64.b64encode(pix.tobytes("png")).decode(),
            })
        doc.close()
    except Exception as e:
        log.warning("[%s] PyMuPDF render failed: %s", doi_r, e)
        return []

    if not images:
        return []

    # Lazy import to avoid circular dependency at module load time
    from .llm_client import call_gemini_with_images
    result, err = call_gemini_with_images(PDF_IMAGE_REFERENCES_PROMPT, images)

    if err:
        raise ReferenceExtractionUnavailable(f"image-based Gemini: {err}")
    if not result or not isinstance(result.get("references"), list):
        log.info("[%s] Image-based ref extraction returned nothing", doi_r)
        return []

    refs = []
    for ref in result["references"]:
        if not isinstance(ref, dict):
            continue
        authors = ref.get("authors", [])
        if isinstance(authors, str):
            authors = [authors]
        title = str(ref.get("title", "") or "")
        try:
            year = int(ref["year"]) if ref.get("year") else None
        except (TypeError, ValueError):
            year = None
        if not title and not year:
            continue
        refs.append({"authors": authors, "year": year, "title": title, "raw_ref": ""})

    log.info("[%s] Image LLM: extracted %d refs from %d page(s)",
             doi_r, len(refs), len(images))

    if refs:
        try:
            write_json(cache_file, refs, indent=2)
        except Exception:
            pass

    return refs


def _extract_refs_via_grobid(doi_r: str, pdf_path: Path) -> list:
    """Call the GROBID public server; return parsed reference list or []."""
    tei_xml = process_pdf_with_grobid(pdf_path, server=GROBID_SERVER)
    if not tei_xml:
        log.info("[%s] GROBID: no response", doi_r)
        return []
    sections = parse_tei_sections(tei_xml)
    refs = sections.get("references", [])
    log.info("[%s] GROBID: extracted %d refs", doi_r, len(refs))
    return refs


def run_grobid(doi_r: str, pdf_path: Optional[Path],
               no_llm: bool = False) -> dict:
    """
    Run the full extraction pipeline for one paper.

    Returns:
        grobid_status   "success" | "success_grobid" | "success_direct_llm" |
                        "success_image_llm" | "refs_unavailable" |
                        "pdfminer_failed" | "no_pdf"
        sections        dict (abstract, intro, methods, references)
        n_refs_parsed   int

    Fallback order when pdfminer finds 0 references:
        1. GROBID public server (https://kermitt2-grobid.hf.space)
        2. Gemini with full PDF bytes   (success_direct_llm)  — skipped when no_llm=True
        3. Gemini with rendered images  (success_image_llm)   — skipped when no_llm=True

    `refs_unavailable` is the status for a run that ended the fallback chain without
    an ANSWER about the references — the provider was unreachable. It is deliberately
    not `success`: the reference list is the evidence one whole rung of the resolution
    ladder reads, and reporting an outage as "zero references" both wastes that rung
    and gets frozen into run_extract's parse cache. parse_grobid() turns it into an
    error result so no reader can mistake it for a finding.
    """
    if not pdf_path:
        return {"grobid_status": "no_pdf", "sections": {}, "n_refs_parsed": 0}

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return {"grobid_status": "no_pdf", "sections": {}, "n_refs_parsed": 0}

    sections = parse_pdf_sections(pdf_path)
    if not sections.get("abstract") and not sections.get("references"):
        return {"grobid_status": "pdfminer_failed", "sections": {}, "n_refs_parsed": 0}

    n_refs  = len(sections.get("references", []))
    status  = "success"

    if n_refs == 0:
        grobid_refs = _extract_refs_via_grobid(doi_r, pdf_path)
        if grobid_refs:
            sections["references"] = grobid_refs
            n_refs  = len(grobid_refs)
            status  = "success_grobid"
            log.info("[%s] Used GROBID fallback: %d refs", doi_r, n_refs)
        elif not no_llm:
            # The unavailability of either LLM rung ends the chain as unanswered,
            # rather than falling through to a zero-reference "success". The image
            # rung is a fallback for a document the direct rung read and found no
            # references IN — not for a request that never arrived.
            try:
                direct_refs = _extract_refs_via_pdf_direct(doi_r, pdf_path)
                if direct_refs:
                    sections["references"] = direct_refs
                    n_refs  = len(direct_refs)
                    status  = "success_direct_llm"
                    log.info("[%s] Used direct-PDF-LLM fallback: %d refs", doi_r, n_refs)
                else:
                    img_refs = _extract_refs_via_pdf_images(doi_r, pdf_path)
                    if img_refs:
                        sections["references"] = img_refs
                        n_refs  = len(img_refs)
                        status  = "success_image_llm"
                        log.info("[%s] Used image-LLM fallback: %d refs", doi_r, n_refs)
            except ReferenceExtractionUnavailable as exc:
                log.warning("[%s] reference extraction unavailable (%s) — reporting "
                            "no answer rather than zero references", doi_r, exc)
                status = "refs_unavailable"

    return {
        "grobid_status" : status,
        "sections"      : sections,
        "n_refs_parsed" : n_refs,
    }


# ── Legacy GROBID wrappers (kept for import compatibility) ────────────────────

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def process_pdf_with_grobid(pdf_path: Path,
                              server: str = GROBID_SERVER) -> Optional[str]:
    """
    Legacy: send *pdf_path* to the GROBID REST API.
    Kept for compatibility; prefer parse_pdf_sections() for local extraction.
    Returns TEI-XML string or None.
    """
    if not pdf_path or not Path(pdf_path).exists():
        return None

    # XML cache
    xml_cache = GROBID_CACHE_DIR / f"{Path(pdf_path).stem}.xml"
    if xml_cache.exists() and xml_cache.stat().st_size > 0:
        return xml_cache.read_text(encoding="utf-8")

    throttle("grobid", GROBID_RATE_SEC)

    endpoint = f"{server}/api/processFulltextDocument"
    try:
        with Path(pdf_path).open("rb") as fh:
            r = requests.post(
                endpoint,
                files={"input": (Path(pdf_path).name, fh, "application/pdf")},
                data={"consolidateHeader": "1"},
                timeout=180,
            )
        if r.status_code != 200:
            log.warning("GROBID HTTP %s for %s", r.status_code, Path(pdf_path).name)
            return None
        # Verify it's XML, not an error HTML page
        if not r.text.strip().startswith("<"):
            log.warning("GROBID returned non-XML for %s", Path(pdf_path).name)
            return None
        xml_cache.write_text(r.text, encoding="utf-8")
        return r.text
    except Exception as e:
        log.warning("GROBID error for %s: %s", Path(pdf_path).name, e)
        return None


def _tei_localname(el) -> str:
    """Lowercased local name of an element (``""`` for comments and PIs).

    Matching on the lowercased local name is what lets one parser serve both TEI
    dialects we see: the local GROBID server's camelCase, namespaced TEI, and the
    copy OpenAlex stores, which was round-tripped through an HTML parser and so
    arrives HTML-lowercased (``listbibl``, ``biblstruct``, ``persname``) inside an
    ``<html><body>…`` wrapper.
    """
    tag = getattr(el, "tag", None)
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _tei_root(root):
    """The TEI element itself, unwrapping OpenAlex's ``<html><body><tei>`` shell.

    Descending to the TEI element first matters: the HTML wrapper contributes a
    ``<body>`` of its own, and a bare local-name search would take that for the
    TEI body and never fall back to ``<text>``.
    """
    if _tei_localname(root) == "tei":
        return root
    for el in root.iter():
        if _tei_localname(el) == "tei":
            return el
    return root


def _tei_find(node, name: str):
    """First descendant-or-self with local name *name*, else None."""
    for el in node.iter():
        if _tei_localname(el) == name:
            return el
    return None


def _tei_findall(node, name: str) -> list:
    """All descendants-or-self with local name *name*."""
    return [el for el in node.iter() if _tei_localname(el) == name]


# Elements whose content is not the paper's argument: the bibliography is parsed
# separately into `references`, and <back> also holds acknowledgements and annexes.
# Leaving them in raw_text is not merely noise — outcome_text() falls back to the
# tail of the text, so a bibliography at the end becomes the "closing pages" the
# outcome model is asked to read a verdict out of.
_TEI_SKIP_IN_TEXT = {"back", "listbibl"}

# Block-level elements get a line break around their content. outcome_text() and
# _split_sections() find headings at the START OF A LINE, so a globally
# whitespace-collapsed body has no headings at all — and the OpenAlex dialect,
# whose headings survive only as the text node opening a <div>, would lose every
# section boundary it has left.
_TEI_BLOCK_TAGS = {"div", "p", "head", "figure", "table", "row", "list", "item",
                   "note", "formula", "ab", "quote"}


def _tei_structured_text(el) -> str:
    """Element text with newline markers around block elements (unnormalised)."""
    if _tei_localname(el) in _TEI_SKIP_IN_TEXT or el.get("type") == "references":
        return ""
    parts: list[str] = [el.text or ""]
    for child in el:
        chunk = _tei_structured_text(child)
        if chunk:
            name = _tei_localname(child)
            if name in _TEI_BLOCK_TAGS:
                parts.append(f"\n{chunk}\n")
            elif name == "s":
                # GROBID wraps each sentence in <s>; without a separator the last
                # word of one sentence runs into the first word of the next.
                parts.append(f" {chunk}")
            else:
                parts.append(chunk)
        parts.append(child.tail or "")
    return "".join(parts)


def _tei_body_text(el) -> str:
    """Readable body text of *el*: line breaks kept, other whitespace normalised."""
    lines = [re.sub(r"[^\S\n]+", " ", ln).strip()
             for ln in _tei_structured_text(el).split("\n")]
    return "\n".join(ln for ln in lines if ln)


def parse_tei_sections(tei_xml: str) -> dict:
    """Parse GROBID TEI-XML into sections and references.

    One code path for both dialects (see ``_tei_localname``). The local GROBID
    server returns a ``<body>`` with ``<head>`` elements, so intro and methods can
    be picked out by heading. OpenAlex's HTML-mangled copy has neither body nor
    heads, so what is recoverable there is the abstract, the references, and
    ``raw_text`` (the ``<text>`` element, minus the bibliography) — intro and
    methods stay empty rather than being guessed at from a document with no
    headings. ``link_original.run_for_doi()`` is what carries that raw_text on to
    the model when the headings are missing.
    """
    out: dict = {"abstract": "", "intro": "", "methods": "", "references": [],
                 "raw_text": ""}
    if not tei_xml:
        return out
    try:
        from lxml import etree

        def _text_of(node) -> str:
            return re.sub(r"\s+", " ", "".join(node.itertext())).strip()

        root = _tei_root(etree.fromstring(tei_xml.encode("utf-8")))

        ab = _tei_find(root, "abstract")
        if ab is not None:
            out["abstract"] = _text_of(ab)

        # <body> when the parse kept it, otherwise the enclosing <text>.
        container = _tei_find(root, "body")
        if container is None:
            container = _tei_find(root, "text")
        if container is not None:
            out["raw_text"] = _tei_body_text(container)
            for div in _tei_findall(container, "div"):
                head = next((c for c in div if _tei_localname(c) == "head"), None)
                if head is None:
                    continue
                head_text = _text_of(head).lower()
                text = _text_of(div)
                if any(k in head_text for k in ("introduction", "intro", "background")):
                    if not out["intro"]:
                        out["intro"] = text
                elif any(k in head_text for k in ("method", "material", "procedure",
                                                    "participant", "design")):
                    if not out["methods"]:
                        out["methods"] = text

        # Only biblStructs inside a listBibl: the teiHeader carries one for the
        # paper itself, which is not a reference.
        for bibl_list in _tei_findall(root, "listbibl"):
            for bib in _tei_findall(bibl_list, "biblstruct"):
                ref: dict = {"authors": [], "year": None, "title": "", "raw_ref": ""}
                titles = _tei_findall(bib, "title")
                for title_el in titles:
                    if title_el.get("level", "") in ("a", "m"):
                        ref["title"] = _text_of(title_el)
                        break
                if not ref["title"] and titles:
                    ref["title"] = _text_of(titles[0])
                for author in _tei_findall(bib, "author"):
                    pers = _tei_find(author, "persname")
                    if pers is None:
                        continue
                    sn = _tei_find(pers, "surname")
                    fn = _tei_find(pers, "forename")
                    if sn is not None:
                        surname  = _text_of(sn)
                        forename = _text_of(fn) if fn is not None else ""
                        ref["authors"].append(
                            f"{surname}, {forename[0]}." if forename else surname
                        )
                for date_el in _tei_findall(bib, "date"):
                    if date_el.get("type") != "published":
                        continue
                    m = re.search(r"(\d{4})", date_el.get("when", ""))
                    if m:
                        ref["year"] = int(m.group(1))
                    break
                ref["raw_ref"] = _text_of(bib)
                out["references"].append(ref)
    except Exception as e:
        log.warning("TEI parse error: %s", e)
    return out
