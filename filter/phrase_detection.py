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
REPLICATION_PHRASES: list[re.Pattern] = [
    re.compile(r"\breplication of\b", re.IGNORECASE),
    re.compile(r"\bwe replicated\b", re.IGNORECASE),
    re.compile(r"\bwe replicate\b", re.IGNORECASE),
    re.compile(r"\breplicating the findings\b", re.IGNORECASE),
    re.compile(r"\bdirect replication\b", re.IGNORECASE),
    re.compile(r"\bconceptual replication\b", re.IGNORECASE),
    re.compile(r"\bpreregistered replication\b", re.IGNORECASE),
    re.compile(r"\bregistered replication\b", re.IGNORECASE),
    re.compile(r"\bfailed to replicate\b", re.IGNORECASE),
    re.compile(r"\bdid not replicate\b", re.IGNORECASE),
    re.compile(r"\bcould not reproduce\b", re.IGNORECASE),
    re.compile(r"\bsuccessfully replicated\b", re.IGNORECASE),
    re.compile(r"\breproducibility of\b", re.IGNORECASE),
    re.compile(r"\breplication and extensions?\b", re.IGNORECASE),
    re.compile(r"\bregistered report of\b", re.IGNORECASE),
    re.compile(r"\b(?:close|high[-\s]powered|pre[-\s]?registered|large[-\s]scale)\s+replication\b", re.IGNORECASE),
    re.compile(r"\breplication (?:and|&) extension\b", re.IGNORECASE),
    re.compile(r"\breproduce[ds]?\s+(?:the\s+)?(?:original\s+)?(?:findings?|effects?|results?)\b", re.IGNORECASE),
]

# Subset that should be classified as ``reproduction`` rather than ``replication``
# when the only matching phrases come from this list. The set is intentionally
# narrow — see RULEBOOK §Filter.
REPRODUCTION_PHRASES: list[re.Pattern] = [
    re.compile(r"\bcould not reproduce\b", re.IGNORECASE),
    re.compile(r"\breproducibility of\b", re.IGNORECASE),
    re.compile(r"\breproduce[ds]?\s+(?:the\s+)?(?:original\s+)?(?:findings?|effects?|results?)\b", re.IGNORECASE),
]


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


# Compiled once at import. The YAML file is small (~4 patterns) and immutable
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


def has_replication_phrase(text: str) -> bool:
    """True iff the text contains a replication phrase AND no exclusion fires."""
    if not text:
        return False
    if is_non_scholarly_context(text):
        return False
    return any(regex.search(text) for regex in REPLICATION_PHRASES)


def find_replication_phrase(text: str) -> Optional[str]:
    """Return the lowercase first matching replication phrase, or None."""
    if not text:
        return None
    if is_non_scholarly_context(text):
        return None
    for regex in REPLICATION_PHRASES:
        m = regex.search(text)
        if m:
            return m.group(0).lower()
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
