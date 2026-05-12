"""
Candidate normalizer — converts RawCandidate → NormalizedCandidate, normalizes
the DOI, and prepares for ranker scoring.
"""

import re

from .types import MatchedKeyword, NormalizedCandidate, RawCandidate

_DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
_DOI_PREFIX_TXT_RE = re.compile(r"^doi:\s*", re.IGNORECASE)


def normalize_doi(doi: str) -> str:
    """Lowercase, no leading https://doi.org/ or doi:, no trailing slash."""
    if not doi:
        return ""
    d = doi.strip().lower()
    d = _DOI_PREFIX_RE.sub("", d)
    d = _DOI_PREFIX_TXT_RE.sub("", d)
    if d.endswith("/"):
        d = d[:-1]
    return d


def _clean(s: str | None) -> str | None:
    return s.strip() if isinstance(s, str) else s


def normalize_candidate(raw: RawCandidate) -> NormalizedCandidate:
    """Convert a RawCandidate; ``search_score`` stays 0 until the ranker runs."""
    return NormalizedCandidate(
        source=raw.source,
        source_record_id=raw.source_record_id,
        doi=normalize_doi(raw.doi),
        title=_clean(raw.title),
        abstract=_clean(raw.abstract),
        year=raw.year,
        authors=raw.authors,
        journal=_clean(raw.journal),
        url=raw.url,
        language=raw.language,
        matched_keywords=[raw.matched_keyword],
        search_score=0.0,
    )


def merge_candidates(
    a: NormalizedCandidate,
    b: NormalizedCandidate,
) -> NormalizedCandidate:
    """Merge two candidates with the same DOI.

    Keeps the first non-null metadata from either; concatenates matched_keywords
    with dedup; takes the higher search_score. The first-seen source is kept on
    the merged row (other sources are still available via the run-level
    sources_matched set used by the ranker).
    """
    seen: set[str] = set()
    merged: list[MatchedKeyword] = []
    for m in [*a.matched_keywords, *b.matched_keywords]:
        key = f"{m.id}|{m.field}|{m.permutation}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(m)

    return NormalizedCandidate(
        source=a.source,
        source_record_id=a.source_record_id or b.source_record_id,
        doi=a.doi,
        title=a.title or b.title,
        abstract=a.abstract or b.abstract,
        year=a.year if a.year is not None else b.year,
        authors=a.authors or b.authors,
        journal=a.journal or b.journal,
        url=a.url or b.url,
        language=a.language or b.language,
        matched_keywords=merged,
        search_score=max(a.search_score, b.search_score),
    )
