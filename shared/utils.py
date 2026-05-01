"""
utils.py — Common helpers shared across all pipeline stages.

Public API:
    clean_doi(doi) → str
    cache_key(text) → str
"""
import hashlib
import re


def clean_doi(doi: str) -> str:
    """
    Strip URL prefix from a DOI string and normalise to lowercase.

    Examples:
        "https://doi.org/10.1037/abc123" → "10.1037/abc123"
        "http://dx.doi.org/10.1037/abc123" → "10.1037/abc123"
        "doi:10.1037/abc123"               → "10.1037/abc123"
        "10.1037/abc123"                   → "10.1037/abc123"
    """
    if not doi:
        return ""
    doi = str(doi).strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:", "", doi, flags=re.IGNORECASE)
    return doi.strip().lower()


def cache_key(text: str) -> str:
    """
    Return a stable, filesystem-safe cache key for *text*.

    Uses MD5 (not cryptographic — just for deduplication).
    """
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()
