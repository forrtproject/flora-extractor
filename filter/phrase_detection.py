"""
Replication-phrase detection — port of SciMeto's
``apps/worker/src/services/replication/phraseDetection.ts``.

Two regex sets:
    REPLICATION_PHRASES                    — strong replication signals
    NON_SCHOLARLY_REPLICATION_CONTEXTS     — DNA / code / fork etc., loaded from
                                             filter/spec/exclusion-patterns.yaml
                                             so the data stays portable across
                                             SciMeto and flora-extractor.

If a non-scholarly context fires, the row is treated as not-a-replication even
when a replication phrase also appears.

Intentionally NO ``re.compile`` flag for ``re.M`` or ``re.S`` — the TS source
uses default flags too. ``re.IGNORECASE`` is set per-pattern via the YAML flags
list.
"""

import re
from pathlib import Path
from typing import Optional

import yaml

# Patterns are intentionally compiled WITHOUT a global counterpart — JS's /g
# flag carries lastIndex across calls (LESSONS.md #15 in the SciMeto repo).
# Python's ``re`` API is stateless, so this just means we use ``search``.
# Reproduction-genre phrases, shared by REPLICATION_PHRASES and REPRODUCTION_PHRASES.
# ``is_reproduction_only`` compares pattern objects by identity, and ``re.compile``
# caches on (pattern, flags), so listing the same source twice would still work —
# but naming them once makes the shared membership explicit.
_REPRODUCTION_ANCHORED: list[re.Pattern] = [
    re.compile(r"\breproductions? of ['\"“‘]", re.IGNORECASE),
    re.compile(r"\bcomputational reproduc\w+\b", re.IGNORECASE),
    re.compile(r"\brobustness\s+(?:replicabilit\w+|reproducibilit\w+|replication)\b", re.IGNORECASE),
    re.compile(r"\breproduc\w+\s+(?:and|&)\s+(?:replicat|extend|extension)\w*\b", re.IGNORECASE),
]

# Every "<qualifier> replication" pattern carries an explicit ``s?``: ``\b`` after
# ``replication`` fails on the ``s`` of "replications", so the singular-only forms
# silently missed every plural. On the human-curated FLoRA set that cost 332 hits.
REPLICATION_PHRASES: list[re.Pattern] = [
    # --- original phrases ---
    re.compile(r"\breplications? of\b", re.IGNORECASE),
    re.compile(r"\bwe replicated\b", re.IGNORECASE),
    re.compile(r"\bwe replicate\b", re.IGNORECASE),
    re.compile(r"\bdirect replications?\b", re.IGNORECASE),
    re.compile(r"\bconceptual replications?\b", re.IGNORECASE),
    re.compile(r"\bregistered replications?\b", re.IGNORECASE),
    re.compile(r"\bfailed to replicate\b", re.IGNORECASE),
    re.compile(r"\bdid not replicate\b", re.IGNORECASE),
    re.compile(r"\bregistered report of\b", re.IGNORECASE),
    re.compile(r"\b(?:close|high[-\s]powered|pre[-\s]?registered|large[-\s]scale)\s+replications?\b", re.IGNORECASE),
    # --- ported from old R pipeline's explicit_replication_claims ---
    # recovered ~15,862 candidates previously marked false_positive (phrase_coverage_analysis.py)
    re.compile(r"\battempt\w*\s+to\s+replicate\b", re.IGNORECASE),
    re.compile(r"\baim\w*\s+to\s+replicate\b", re.IGNORECASE),
    re.compile(r"\bset\s+out\s+to\s+replicate\b", re.IGNORECASE),
    re.compile(r"\bsuccess\w*\s+replicat\w*\b", re.IGNORECASE),
    re.compile(r"\bwe\s+(?:conducted|performed|carried\s+out)\s+a\s+replication\b", re.IGNORECASE),
    re.compile(r"\b(?:many-?labs?|multi-?site)\s+replications?\b", re.IGNORECASE),
    re.compile(r"\breplicat\w*\s+(?:and|&)\s+exten\w*\b", re.IGNORECASE),
    re.compile(r"\breplication\s+stud(?:y|ies)\b", re.IGNORECASE),
    re.compile(r"\bstudy\s+replicate[sd]\b", re.IGNORECASE),
    re.compile(r"\bour\s+replications?\b", re.IGNORECASE),
    re.compile(r"\bexact\s+replications?\b", re.IGNORECASE),
    re.compile(r"\breplication\s+attempts?\b", re.IGNORECASE),
    re.compile(r"\bcross-?(?:cultural|national|lab(?:oratory)?)\s+replications?\b", re.IGNORECASE),
    # --- reproduction vocabulary the list never covered ---
    # Anchored on purpose. A bare "reproduction of" matches animal breeding and
    # "social reproduction" far more often than a reproduction study (8% precision
    # over 15,440 corpus rows), while these four cost nothing and score better on
    # the curated reproduction list.
    *_REPRODUCTION_ANCHORED,
    re.compile(r"\breplicat(?:e|es|ed|ing)\s+(?:the\s+)?"
               r"(?:previous|prior|original|earlier|main|key|core|published)?\s*"
               r"(?:findings?|results?|effects?|analys[ei]s|stud(?:y|ies))\b",
               re.IGNORECASE),
]

# Subset that should be classified as ``reproduction`` rather than ``replication``
# when the only matching phrases come from this list. The set is intentionally
# narrow — see RULEBOOK §Filter.
REPRODUCTION_PHRASES: list[re.Pattern] = list(_REPRODUCTION_ANCHORED)


def _load_exclusion_regexes() -> list[tuple[str, re.Pattern]]:
    spec_path = Path(__file__).parent / "spec" / "exclusion-patterns.yaml"
    with spec_path.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    out: list[tuple[str, re.Pattern]] = []
    for p in doc.get("patterns", []):
        flags = 0
        for flag in p.get("flags", []):
            if flag.lower() == "i":
                flags |= re.IGNORECASE
        out.append((p["id"], re.compile(p["regex"], flags)))
    return out


# Compiled once at import. The YAML file is small (~6 patterns) and immutable
# across a run; reloading on every call would be wasteful.
NON_SCHOLARLY_REPLICATION_CONTEXTS: list[tuple[str, re.Pattern]] = _load_exclusion_regexes()


def is_non_scholarly_context(text: str) -> Optional[str]:
    """Return the matched exclusion pattern id, or None if no exclusion fires."""
    if not text:
        return None
    for pid, regex in NON_SCHOLARLY_REPLICATION_CONTEXTS:
        if regex.search(text):
            return pid
    return None


def find_replication_phrase_span(text: str,
                                 ignore_exclusions: bool = False) -> Optional[tuple[str, int, int]]:
    """Return (lowercase phrase, start, end) for the first matching replication phrase, or None.

    ignore_exclusions=True skips the non-scholarly-context gate — used by the Stage-2
    targeted-readmission rule (#44), which needs to know a phrase is present *even when*
    an exclusion pattern fired, to rescue in-scope computational reproductions.
    """
    if not text:
        return None
    if not ignore_exclusions and is_non_scholarly_context(text):
        return None
    for regex in REPLICATION_PHRASES:
        m = regex.search(text)
        if m:
            return m.group(0).lower(), m.start(), m.end()
    return None


def is_reproduction_only(text: str) -> bool:
    """True if every matching phrase in ``text`` is a reproduction phrase.

    Used to decide between filter_status == ``replication`` vs ``reproduction``
    when the rule filter tags the row.
    """
    if not text:
        return False
    repro_hits = [r for r in REPRODUCTION_PHRASES if r.search(text)]
    if not repro_hits:
        return False
    other_hits = [
        r for r in REPLICATION_PHRASES
        if r not in REPRODUCTION_PHRASES and r.search(text)
    ]
    return not other_hits
