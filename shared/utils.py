"""
utils.py — Common helpers shared across all pipeline stages.

Public API:
    clean_doi(doi) → str
    cache_key(text) → str
    pdf_serve_url(doi_r, result) → str
"""
import hashlib
import re
from pathlib import Path

from filelock import FileLock


def csv_lock(path, timeout: float = -1) -> FileLock:
    """Cross-process lock guarding a shared CSV against read-modify-write vs append races.

    Both the streaming extractor (append) and promote_test (full read-rewrite) target
    data/extracted.csv concurrently; without a shared lock the rewrite clobbers rows the
    extractor appended between its read and write. timeout=-1 blocks until acquired.
    """
    return FileLock(f"{path}.lock", timeout=timeout)


def clean_doi(doi: str) -> str:
    """
    Strip URL prefix from a DOI string and normalise to lowercase.

    Examples:
        "https://doi.org/10.1037/abc123" → "10.1037/abc123"
        "http://dx.doi.org/10.1037/abc123" → "10.1037/abc123"
        "doi:10.1037/abc123"               → "10.1037/abc123"
        "10.1037/abc123/"                  → "10.1037/abc123"
        "10.1037/abc123"                   → "10.1037/abc123"
    """
    if not doi:
        return ""
    doi = str(doi).strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:", "", doi, flags=re.IGNORECASE)
    return doi.strip().lower().rstrip("/")


def bare_work_id(value: str) -> str:
    """
    Normalise an OpenAlex work identifier to its bare form.

    Stage 1 stores openalex_id_r as the full URL, while the validation DB and the
    OpenAlex "work ID" users look up are the bare W-number. Anything that is not a
    W-id (an author/source/institution id, a stray DOI, junk) returns "".

    Examples:
        "https://openalex.org/W2884670852" → "W2884670852"
        "W2884670852"                      → "W2884670852"
        "w2884670852"                      → "W2884670852"
        "https://openalex.org/A5023888391" → ""
    """
    if not value:
        return ""
    tail = str(value).strip().rstrip("/").rsplit("/", 1)[-1]
    return tail.upper() if re.fullmatch(r"[Ww]\d+", tail) else ""


def cache_key(text: str) -> str:
    """
    Return a stable, filesystem-safe cache key for *text*.

    Uses MD5 (not cryptographic — just for deduplication).
    """
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()


def pdf_serve_url(doi_r: str, result: dict) -> str:
    """URL path to serve the cached PDF for *doi_r*, or "" if none is cached."""
    from shared.config import PDF_CACHE_DIR

    if result.get("pdf_path"):
        return f"/pdf/{Path(result['pdf_path']).name}"
    expected = PDF_CACHE_DIR / f"{cache_key(doi_r)}.pdf"
    return f"/pdf/{expected.name}" if expected.exists() else ""


_ABBREV_RE = re.compile(
    r"\b(?:et al|e\.g|i\.e|vs|Dr|Mr|Mrs|Ms|Prof|Fig|No|Vol|pp|cf)\."
    r"|(?<!\w)\b[A-Z]\.",
    re.IGNORECASE,
)


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """
    Return (start, end) character offsets into *text* for each sentence.

    Splits on whitespace following sentence-ending punctuation, while protecting
    common abbreviations (et al., e.g., Dr., single-letter initials like "J.") from
    being treated as sentence boundaries. Offsets index into the original *text*
    unchanged (the abbreviation mask is applied to a same-length working copy only),
    so callers can directly compare citation/phrase match offsets against these spans.
    """
    if not text:
        return []
    masked = _ABBREV_RE.sub(lambda m: "\x00" * len(m.group(0)), text)
    spans: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"(?<=[.!?])\s+", masked):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return spans
