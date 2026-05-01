"""
grobid.py — PDF section extraction for original study identification.

Primary method : pdfminer.six  (local, no external server required)
Fallback method: GROBID public server (https://kermitt2-grobid.hf.space)
                 — used only when pdfminer fails and GROBID is reachable.

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
import re
import textwrap
import time
from pathlib import Path
from typing import Optional

import requests

from .config import GROBID_CACHE_DIR, GROBID_RATE_SEC, GROBID_SERVER, log

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
        # New entry: starts with [N] or looks like "Surname, I. (YEAR)"
        is_new = bool(re.match(r"^\[?\d+\][\.\s]", stripped)) or \
                 bool(re.match(r"^[A-Z][a-z]+,\s+[A-Z]", stripped) and
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
        # First sentence ending at ". Capital" is the title
        title_m = re.match(r"(.{10,}?)[\.?!]\s+[A-Z]", post_year)
        ref["title"] = (title_m.group(1) if title_m else post_year[:200]).strip()

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
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(sections, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        log.debug("Could not cache sections for %s: %s", pdf_path.name, e)

    return sections


_MAX_PDF_BYTES = 45 * 1024 * 1024   # 45 MB safety margin (Gemini limit: 50 MB)


def _extract_refs_via_pdf_direct(doi_r: str, pdf_path: Path) -> list[dict]:
    """
    Send the full PDF directly to Gemini with MEDIA_RESOLUTION_LOW for reference
    extraction. This is more accurate than image rendering for native-text PDFs
    and uses fewer tokens (Gemini reads embedded text natively without image billing).

    Falls back silently if the PDF exceeds 45 MB or if no API keys are configured.
    """
    import json

    cache_file = GROBID_CACHE_DIR / f"{pdf_path.stem}_direct_refs.json"
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

    prompt = textwrap.dedent("""
        The attached PDF is an academic paper. Extract every entry from its
        References / Bibliography section.

        For each reference return:
        - "authors": list of author strings, e.g. ["Smith, J.", "Jones, A."]
        - "year": publication year as an integer, or null if not found
        - "title": full title of the referenced work (empty string if unreadable)

        Include only entries where you can determine at least a year OR a title.
        Return ONLY this JSON — no prose outside the braces:
        {
          "references": [
            {"authors": ["Surname, I."], "year": 2020, "title": "Paper title"},
            ...
          ]
        }
    """).strip()

    from .llm import call_gemini_with_pdf
    result = call_gemini_with_pdf(prompt, pdf_bytes)

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
            with cache_file.open("w", encoding="utf-8") as fh:
                json.dump(refs, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return refs


def _extract_refs_via_pdf_images(doi_r: str, pdf_path: Path) -> list[dict]:
    """
    Render the last N pages of a PDF as grayscale PNG images and ask Gemini
    to extract the reference list. Used as a fallback when pdfminer finds text
    but extracts 0 references (e.g. two-column or non-standard layouts).

    Requires: pip install pymupdf
    Returns [] silently when PyMuPDF is not installed.
    """
    import json

    cache_file = GROBID_CACHE_DIR / f"{pdf_path.stem}_img_refs.json"
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

    prompt = textwrap.dedent("""
        The attached images show page(s) from an academic paper — likely the
        References / Bibliography section.

        Extract EVERY reference entry you can clearly read.

        For each reference return:
        - "authors": list of author strings, e.g. ["Smith, J.", "Jones, A."]
        - "year": publication year as an integer, or null if not visible
        - "title": full title of the referenced work (empty string if unreadable)

        Include only entries where you can read at least a year OR a title.
        Return ONLY this JSON — no prose outside the braces:
        {
          "references": [
            {"authors": ["Surname, I."], "year": 2020, "title": "Paper title"},
            ...
          ]
        }
    """).strip()

    # Lazy import to avoid circular dependency at module load time
    from .llm import call_gemini_with_images
    result = call_gemini_with_images(prompt, images)

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
            with cache_file.open("w", encoding="utf-8") as fh:
                json.dump(refs, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return refs


def run_grobid(doi_r: str, pdf_path: Optional[Path]) -> dict:
    """
    Run the full extraction pipeline for one paper.

    Returns:
        grobid_status   "success" | "success_image_llm" | "pdfminer_failed" | "no_pdf"
        sections        dict (abstract, intro, methods, references)
        n_refs_parsed   int
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

    # If pdfminer parsed text but found 0 references, try Gemini with the full
    # PDF first (native-text PDFs: efficient + accurate), then fall back to
    # image rendering for scanned / non-standard layouts.
    if n_refs == 0:
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

    return {
        "grobid_status" : status,
        "sections"      : sections,
        "n_refs_parsed" : n_refs,
    }


# ── Legacy GROBID wrappers (kept for import compatibility) ────────────────────

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}
_grobid_last = 0.0


def process_pdf_with_grobid(pdf_path: Path,
                              server: str = GROBID_SERVER) -> Optional[str]:
    """
    Legacy: send *pdf_path* to the GROBID REST API.
    Kept for compatibility; prefer parse_pdf_sections() for local extraction.
    Returns TEI-XML string or None.
    """
    global _grobid_last

    if not pdf_path or not Path(pdf_path).exists():
        return None

    # XML cache
    xml_cache = GROBID_CACHE_DIR / f"{Path(pdf_path).stem}.xml"
    if xml_cache.exists() and xml_cache.stat().st_size > 0:
        return xml_cache.read_text(encoding="utf-8")

    wait = GROBID_RATE_SEC - (time.time() - _grobid_last)
    if wait > 0:
        time.sleep(wait)
    _grobid_last = time.time()

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


def parse_tei_sections(tei_xml: str) -> dict:
    """Legacy: parse GROBID TEI-XML. Kept for import compatibility."""
    out = {"abstract": "", "intro": "", "methods": "", "references": []}
    if not tei_xml:
        return out
    try:
        from lxml import etree

        def _text_of(node) -> str:
            return re.sub(r"\s+", " ", "".join(node.itertext())).strip()

        root = etree.fromstring(tei_xml.encode("utf-8"))

        ab = root.find(".//tei:abstract", TEI_NS)
        if ab is not None:
            out["abstract"] = _text_of(ab)

        body = root.find(".//tei:body", TEI_NS)
        if body is not None:
            for div in body.findall(".//tei:div", TEI_NS):
                head = div.find("tei:head", TEI_NS)
                head_text = _text_of(head).lower() if head is not None else ""
                text = _text_of(div)
                if any(k in head_text for k in ("introduction", "intro", "background")):
                    if not out["intro"]:
                        out["intro"] = text
                elif any(k in head_text for k in ("method", "material", "procedure",
                                                    "participant", "design")):
                    if not out["methods"]:
                        out["methods"] = text

        for bib in root.findall(".//tei:listBibl//tei:biblStruct", TEI_NS):
            ref: dict = {"authors": [], "year": None, "title": "", "raw_ref": ""}
            for title_el in bib.findall(".//tei:title", TEI_NS):
                if title_el.get("level", "") in ("a", "m"):
                    ref["title"] = _text_of(title_el)
                    break
            if not ref["title"]:
                t = bib.find(".//tei:title", TEI_NS)
                if t is not None:
                    ref["title"] = _text_of(t)
            for author in bib.findall(".//tei:author", TEI_NS):
                sn = author.find("tei:persName/tei:surname",  TEI_NS)
                fn = author.find("tei:persName/tei:forename", TEI_NS)
                if sn is not None:
                    surname  = _text_of(sn)
                    forename = _text_of(fn) if fn is not None else ""
                    ref["authors"].append(
                        f"{surname}, {forename[0]}." if forename else surname
                    )
            date_el = bib.find(".//tei:date[@type='published']", TEI_NS)
            if date_el is not None:
                m = re.search(r"(\d{4})", date_el.get("when", ""))
                if m:
                    ref["year"] = int(m.group(1))
            ref["raw_ref"] = _text_of(bib)
            out["references"].append(ref)
    except Exception as e:
        log.warning("TEI parse error: %s", e)
    return out
