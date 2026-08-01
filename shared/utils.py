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


_NON_ARTICLE_DOI_RE = re.compile(r"/reviews/|/decisions/", re.IGNORECASE)


def non_article_doi(doi: str) -> str:
    """Reason string if *doi* is a non-article object (not a study), else "".

    figshare (10.6084) DOIs are data records / figures / posters; a "/reviews/" or
    "/decisions/" segment marks a peer-review or editorial object
    (e.g. 10.7287/peerj.10325v0.1/reviews/2). Neither is the replication itself, so
    both are Stage-2 false positives (issue #17).
    """
    doi = clean_doi(doi)
    if not doi:
        return ""
    if doi.startswith("10.6084/"):
        return "figshare_data_record"
    if _NON_ARTICLE_DOI_RE.search(doi):
        return "peer_review_object"
    return ""


_NON_ARTICLE_TYPES = {
    "dataset": "dataset_record",
    "database": "dataset_record",
    "software": "software_record",
    "peer-review": "peer_review_object",
    "supplementary-materials": "supplementary_material",
    "component": "supplementary_material",
    "paratext": "paratext_record",
    "libguides": "library_guide",
    "grant": "grant_record",
    "standard": "standard_document",
}


def non_article_type(work_type: str) -> str:
    """Reason string if the OpenAlex/CrossRef work *type* is a non-study object, else "".

    Companion to non_article_doi(): that one pattern-matches DOI strings, this one uses
    the work type the registry reports. A hand-check of 50 provisionally-linked rows
    found 23 were not studies — Dataverse/Zenodo/Mendeley deposits and eLife
    "Author response" objects — none of which the DOI patterns catch, but all of which
    carry a giveaway type (dataset, peer-review, ...).

    EXCLUDE-ONLY, never require. Only types affirmatively known not to be studies are
    rejected; an empty, missing or unrecognised type returns "". Do NOT rewrite this as
    an allow-list of acceptable types: `type` coverage is incomplete and inconsistent
    across CrossRef, OpenAlex and content negotiation, so requiring a known-good type
    would silently drop real replications.

    "other" is deliberately NOT excluded — it is the registries' catch-all for anything
    unclassified, including ordinary articles. "data-paper" and "software-paper" are
    papers *about* data/software and are likewise kept, which is why matching is exact
    rather than by substring.
    """
    t = str(work_type or "").strip().lower().replace("_", "-")
    return _NON_ARTICLE_TYPES.get(t, "")


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
