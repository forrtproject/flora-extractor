"""
matching.py — Matching logic for linking candidates to all_replications.

Strategies (priority order):
  1. Exact DOI match (highest confidence)
  2. Exact URL match (high confidence)
  3. Fuzzy title + year + first author (medium confidence)

Each match function returns (method_name, confidence) or None if no match.
"""

from typing import Optional, Tuple
from shared.utils import clean_doi


def normalize_url(url: str) -> str:
    """Normalize URL for comparison."""
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    # Remove trailing slash
    url = url.rstrip("/")
    # Normalize scheme
    url = url.replace("http://", "https://")
    return url.lower()


def match_by_doi(
    row_candidate: dict, row_reference: dict
) -> Optional[Tuple[str, float]]:
    """
    Match by exact DOI.

    Args:
        row_candidate: dict with 'doi_r' key
        row_reference: dict with 'doi_o' or 'doi_r' key (depending on context)

    Returns:
        ("doi", 1.0) if match, None otherwise
    """
    doi_c = row_candidate.get("doi_r", "")
    # Try doi_o first (for all_replications on original side), then doi_r
    doi_r = row_reference.get("doi_o", "") or row_reference.get("doi_r", "")

    if not doi_c or not doi_r:
        return None

    if clean_doi(str(doi_c)) == clean_doi(str(doi_r)):
        return ("doi", 1.0)

    return None


def match_by_url(
    row_candidate: dict, row_reference: dict
) -> Optional[Tuple[str, float]]:
    """
    Match by exact URL.

    Args:
        row_candidate: dict with 'url_r' key
        row_reference: dict with 'url_o' or 'url_r' key

    Returns:
        ("url", 1.0) if match, None otherwise
    """
    url_c = row_candidate.get("url_r", "")
    # Try url_o first, then url_r
    url_r = row_reference.get("url_o", "") or row_reference.get("url_r", "")

    if not url_c or not url_r:
        return None

    if normalize_url(str(url_c)) == normalize_url(str(url_r)):
        return ("url", 1.0)

    return None


def match_by_fuzzy_title(
    row_candidate: dict,
    row_reference: dict,
    title_threshold: float = 0.70,
) -> Optional[Tuple[str, float]]:
    """
    Match by fuzzy title similarity + exact year + first author.

    Args:
        row_candidate: dict with 'title_r', 'year_r', 'authors_r'
        row_reference: dict with 'title_o'/'study_o' or 'title_r'/'study_r', 'year_o'/'year_r', 'authors_o'/'authors_r'
        title_threshold: Jaccard similarity threshold (0-1)

    Returns:
        ("fuzzy_title", confidence) if match, None otherwise
    """
    # Year must match exactly
    year_c = row_candidate.get("year_r")
    year_r = row_reference.get("year_o") or row_reference.get("year_r")
    if year_c != year_r:
        return None

    # Extract first author last name
    authors_c = str(row_candidate.get("authors_r", ""))
    authors_r = str(
        row_reference.get("authors_o") or row_reference.get("authors_r", "")
    )

    if not authors_c or not authors_r:
        return None

    first_author_c = authors_c.split(",")[0].strip().lower()
    first_author_r = authors_r.split(",")[0].strip().lower()

    if first_author_c != first_author_r:
        return None

    # Compute title similarity (Jaccard)
    # Try title_o/study_o first, then title_r/study_r
    title_r = (
        row_reference.get("title_o")
        or row_reference.get("study_o")
        or row_reference.get("title_r")
        or row_reference.get("study_r", "")
    )
    title_c = str(row_candidate.get("title_r", "")).lower()
    title_r = str(title_r).lower()

    if not title_c or not title_r:
        return None

    # Jaccard: intersection / union
    words_c = set(title_c.split())
    words_r = set(title_r.split())

    if not words_c or not words_r:
        return None

    intersection = len(words_c & words_r)
    union = len(words_c | words_r)
    jaccard = intersection / union if union > 0 else 0.0

    if jaccard >= title_threshold:
        return ("fuzzy_title", jaccard)

    return None


def find_best_match(
    row_candidate: dict, row_reference: dict
) -> Optional[Tuple[str, float]]:
    """
    Find best match using priority order: DOI → URL → fuzzy title.

    Returns:
        (method, confidence) for best match, or None if no match found
    """
    # Try DOI (highest priority)
    result = match_by_doi(row_candidate, row_reference)
    if result:
        return result

    # Try URL
    result = match_by_url(row_candidate, row_reference)
    if result:
        return result

    # Try fuzzy title
    result = match_by_fuzzy_title(row_candidate, row_reference)
    if result:
        return result

    return None
