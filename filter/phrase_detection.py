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

from shared.utils import sentence_spans

# Patterns are intentionally compiled WITHOUT a global counterpart — JS's /g
# flag carries lastIndex across calls (LESSONS.md #15 in the SciMeto repo).
# Python's ``re`` API is stateless, so this just means we use ``search``.
# Reproduction-genre phrases, shared by REPLICATION_PHRASES and REPRODUCTION_PHRASES.
# ``is_reproduction_only`` compares pattern objects by identity, and ``re.compile``
# caches on (pattern, flags), so listing the same source twice would still work —
# but naming them once makes the shared membership explicit.
_REPRODUCTION_ANCHORED: list[re.Pattern] = [
    re.compile(r"\breproductions? of ['\"“‘]", re.IGNORECASE),
    # "comput\w*" not "computational": the genre says "computationally reproducible"
    # at least as often as "computational reproduction".
    re.compile(r"\bcomput\w*\s+reproduc\w+\b", re.IGNORECASE),
    re.compile(r"\brobustness\s+(?:replicabilit\w+|reproducibilit\w+|replication)\b", re.IGNORECASE),
    re.compile(r"\breproduc\w+\s+(?:and|&)\s+(?:replicat|extend|extension)\w*\b", re.IGNORECASE),
    # Re-analysis of a prior study's data is a reproduction. Cost-free on every
    # labelled set and the largest single gain on the curated reproduction list.
    re.compile(r"\bre-?analys[ei]s of\b", re.IGNORECASE),
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


# Phrase-scoped negative contexts, keyed by pattern source. A phrase whose guard
# fires is skipped while every OTHER phrase in the same text can still match —
# unlike the row-level patterns in exclusion-patterns.yaml, which kill the row.
# That distinction matters: the measurement-reliability vocabulary below makes
# "reproducibility of" worthless but says nothing about "a direct replication of
# Smith (2010)" in the same abstract.
PHRASE_GUARDS: dict[str, re.Pattern] = {
    # "We replicated one SNP (rs133885) from 585 SNPs previously reported..." — a
    # GWAS discovery/replication-cohort design, not a replication of a study.
    # Removes 59 curated negatives at zero cost on either gold set. The same guard
    # on "replication stud(y|ies)" costs 28 human-gold papers to remove 18
    # negatives, so it is deliberately scoped to this one phrase.
    r"\bwe replicated\b": re.compile(
        r"\b(?:gwas|genome[-\s]wide|snps?|alleles?|genotyp\w+|haplotype|"
        r"linkage disequilibrium|minor allele frequency|loci|polymorphism\w*|"
        r"replication cohort|discovery cohort|exome)\b", re.IGNORECASE),
}
# NOT guarded: "re-analysis of". Its noise has no compact shape. Over ~1,280
# corpus rows the object after "of" is "data" (260), "studies" (50), "evidence"
# (40) and then a flat tail of 50+ domain nouns at ~10 each — climate is 2% of it,
# so a climate word list was fitting 4 rows of a 20-row sample, not the phenomenon.
# An object blacklist cannot work either: "data" is the commonest object and is
# ambiguous ("re-analysis of data from Smith (2010)" vs "...from the Framingham
# cohort"). Requiring a same-sentence author-year cite keeps only 5% and discards
# real work, including the Davey et al. re-analysis of the Kenya deworming trial.
# The discrimination is semantic, so it belongs to Stage 3's screen, not here.


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


def _sentence_around(text: str, pos: int) -> str:
    """The sentence containing *pos*, for evaluating a phrase guard.

    A guard must judge the phrase's own context. Searching the whole title+abstract
    let one stray token anywhere veto every occurrence: "We replicated Smith (2010).
    We also conducted a GWAS." lost the replication claim to the second sentence.
    """
    for start, end in sentence_spans(text):
        if start <= pos < end:
            return text[start:end]
    return text


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
        guard = PHRASE_GUARDS.get(regex.pattern)
        if guard is None:
            m = regex.search(text)
            if m:
                return m.group(0).lower(), m.start(), m.end()
            continue
        for m in regex.finditer(text):
            if not guard.search(_sentence_around(text, m.start())):
                return m.group(0).lower(), m.start(), m.end()
    return None


def is_reproduction_only(text: str) -> bool:
    """True if every matching phrase in ``text`` is a reproduction phrase.

    Used to decide between filter_status == ``replication`` vs ``reproduction``
    when the rule filter tags the row.
    """
    if not text:
        return False

    def _hits(regex: re.Pattern) -> bool:
        """A guarded-out phrase is not a hit here either, or a row could be called
        reproduction on the strength of a phrase the span finder already skipped.
        Scoped per occurrence, exactly as find_replication_phrase_span does."""
        guard = PHRASE_GUARDS.get(regex.pattern)
        if guard is None:
            return bool(regex.search(text))
        return any(not guard.search(_sentence_around(text, m.start()))
                   for m in regex.finditer(text))

    repro_hits = [r for r in REPRODUCTION_PHRASES if _hits(r)]
    if not repro_hits:
        return False
    other_hits = [
        r for r in REPLICATION_PHRASES
        if r not in REPRODUCTION_PHRASES and _hits(r)
    ]
    return not other_hits
