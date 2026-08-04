"""
disambiguation.py — Shared helpers for original-study candidate comparison.

Public API:
    jaccard_similarity(a, b) → float
    is_umbrella_paper(title) → bool

Umbrella/multi-lab papers (ManyLabs, PSA, registered replication reports, etc.)
are flagged because they are never the specific original study being replicated,
so a match against one must not be auto-accepted.

The two resolution routines this module once held (same-author/year title
overlap and GROBID-reference matching) live in extract/link_original.py, which
reimplemented them against the live candidate shape.
"""
import re

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


def is_umbrella_paper(title: str) -> bool:
    """Return True if the title looks like an umbrella/framework project paper."""
    return bool(_UMBRELLA_PATTERNS.search(title or ""))


# Keep private alias so existing internal calls still work
_is_umbrella_paper = is_umbrella_paper


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens of ≥ 3 characters."""
    return {t.lower() for t in re.findall(r"\b\w{3,}\b", text)}


def jaccard_similarity(a: str, b: str) -> float:
    """Jaccard similarity between word-token sets of *a* and *b*."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
