"""
disambiguation.py — Same-author / same-year original study disambiguation.

Public API:
    jaccard_similarity(a, b) → float
    resolve_same_author_year(doi_r, study_r, abstract_r, candidates) → dict
"""
import json
import re
from typing import Optional

# Keywords that indicate a paper is an umbrella/framework project, not an
# original study being directly replicated.
_UMBRELLA_PATTERNS = re.compile(
    r"\b("
    r"EEGManyLabs|ManyLabs|Many\s+Labs"
    r"|Psychological\s+Science\s+Accelerator"
    r"|StudySwap"
    r"|registered\s+replication\s+report"
    r"|multi.?lab\s+replication"
    r"|collaborative\s+replication"
    r")\b",
    re.IGNORECASE,
)


def _is_umbrella_paper(title: str) -> bool:
    """Return True if the title looks like an umbrella/framework project paper."""
    return bool(_UMBRELLA_PATTERNS.search(title or ""))


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens of ≥ 3 characters."""
    return {t.lower() for t in re.findall(r"\b\w{3,}\b", text)}


def jaccard_similarity(a: str, b: str) -> float:
    """Jaccard similarity between word-token sets of *a* and *b*."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def resolve_same_author_year(doi_r: str,
                              study_r: str,
                              abstract_r: str,
                              candidates: list[dict]) -> dict:
    """
    Attempt to resolve ambiguity without needing full-text PDF.

    Strategy:
      1. Single candidate → resolved immediately.
      2. All candidates share same first-author surname AND year →
         pick the one whose title has highest Jaccard overlap with
         the replication title + abstract (requires 2× margin over second).
      3. Otherwise → route to full-text (PDF + GROBID + LLM).

    Returns a dict:
        resolved           bool
        resolution_method  str
        resolved_doi_o     str
        resolved_title_o   str
        resolved_year_o    int | None
        resolution_score   float
        all_candidates_json str  (JSON array)
    """
    base: dict = {
        "resolved"            : False,
        "resolution_method"   : "none",
        "resolved_doi_o"      : "",
        "resolved_title_o"    : "",
        "resolved_year_o"     : None,
        "resolution_score"    : 0.0,
        "all_candidates_json" : json.dumps(candidates, ensure_ascii=False),
    }

    if not candidates:
        base["resolution_method"] = "no_candidates_found"
        return base

    # ── Single candidate: resolved with full confidence ───────────────────────
    # Skip auto-resolution for umbrella/framework papers — they are never the
    # specific original study being replicated and must be confirmed by LLM.
    if len(candidates) == 1:
        c = candidates[0]
        if _is_umbrella_paper(c.get("title", "")):
            base["resolution_method"] = "needs_fulltext"
            return base
        return {
            **base,
            "resolved"         : True,
            "resolution_method": "single_candidate_after_requery",
            "resolved_doi_o"   : c["doi"],
            "resolved_title_o" : c["title"],
            "resolved_year_o"  : c["year"],
            "resolution_score" : 1.0,
        }

    # ── Multiple candidates: check if they share surname + year ──────────────
    surnames = {
        (c["first_author"].lower().split()[-1] if c["first_author"] else "")
        for c in candidates
    }
    years = {c["year"] for c in candidates}

    context = (abstract_r or "") + " " + (study_r or "")

    if len(surnames) == 1 and len(years) == 1:
        scored = sorted(
            candidates,
            key=lambda c: jaccard_similarity(c["title"], context),
            reverse=True,
        )
        best        = scored[0]
        best_score  = jaccard_similarity(best["title"], context)
        second_score = (
            jaccard_similarity(scored[1]["title"], context)
            if len(scored) > 1 else 0.0
        )

        # Accept only when clearly better than the runner-up
        if best_score > 0.05 and best_score >= second_score * 1.5:
            return {
                **base,
                "resolved"         : True,
                "resolution_method": "same_author_year_title_overlap",
                "resolved_doi_o"   : best["doi"],
                "resolved_title_o" : best["title"],
                "resolved_year_o"  : best["year"],
                "resolution_score" : round(best_score, 4),
            }

    # ── Cannot resolve without full-text ─────────────────────────────────────
    base["resolution_method"] = "needs_fulltext"
    return base


def resolve_by_grobid_refs(doi_r:      str,
                            candidates: list[dict],
                            sections:   dict) -> dict:
    """
    Match GROBID reference list against pre-identified candidate originals.

    For each candidate, find the best-matching GROBID reference by Jaccard
    title similarity.  A match is accepted when:
      • similarity ≥ 0.45  (title overlap)
      • year within ±1 of candidate year  (when both are known)
      • OR author surname matches AND similarity ≥ 0.30

    Returns the same shape as resolve_same_author_year.
    """
    base: dict = {
        "resolved"          : False,
        "resolution_method" : "grobid_ref_no_match",
        "resolved_doi_o"    : "",
        "resolved_title_o"  : "",
        "resolved_year_o"   : None,
        "resolved_author_o" : "",
        "resolution_score"  : 0.0,
    }

    refs = sections.get("references", [])
    if not refs or not candidates:
        return base

    best_cand  = None
    best_score = 0.0
    best_ref   = None

    for cand in candidates:
        cand_title  = cand.get("title",        "") or ""
        cand_year   = cand.get("year")
        cand_auth   = (cand.get("first_author", "") or "").lower().split()[-1] \
                      if cand.get("first_author") else ""

        for ref in refs:
            ref_title   = ref.get("title", "") or ""
            ref_year    = ref.get("year")
            ref_authors = ref.get("authors", [])
            ref_first   = (ref_authors[0].split(",")[0].lower()
                           if ref_authors else "")

            sim = jaccard_similarity(cand_title, ref_title)

            # Year gate: must be within ±1 when both present
            if cand_year and ref_year:
                if abs(int(cand_year) - int(ref_year)) > 1:
                    continue

            # Author bonus: lower threshold when first-author surname matches
            author_match = bool(cand_auth and ref_first and
                                (cand_auth in ref_first or ref_first in cand_auth))
            threshold = 0.30 if author_match else 0.45

            if sim >= threshold and sim > best_score:
                best_score = sim
                best_cand  = cand
                best_ref   = ref

    if best_cand:
        return {
            **base,
            "resolved"          : True,
            "resolution_method" : "grobid_ref_match",
            "resolved_doi_o"    : best_cand.get("doi",          ""),
            "resolved_title_o"  : best_cand.get("title",        ""),
            "resolved_year_o"   : best_cand.get("year"),
            "resolved_author_o" : best_cand.get("first_author", ""),
            "resolution_score"  : round(best_score, 4),
        }

    return base
