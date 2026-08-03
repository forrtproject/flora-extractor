"""
Replication-phrase detection — port of SciMeto's
``apps/worker/src/services/replication/phraseDetection.ts``.

Two regex sets:
    REPLICATION_PHRASES                    — strong replication signals
    NON_SCHOLARLY_REPLICATION_CONTEXTS     — DNA / code / fork etc., loaded from
                                             the seven exclusion specs in the
                                             filter/spec/ bundle, so one edit
                                             moves both this rule and the engine.

If a non-scholarly context fires, the row is treated as not-a-replication even
when a replication phrase also appears.

Intentionally NO ``re.compile`` flag for ``re.M`` or ``re.S`` — the TS source
uses default flags too. ``re.IGNORECASE`` is set on every exclusion pattern —
spec regexes are case-insensitive by default, so the pyarrow and ``re`` backends
cannot diverge on case.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from filter.engine.spec import load_specs
from shared.openalex_client import extract_author_year_patterns
from shared.utils import sentence_spans

# The loose stem alternation. Stage 1's vectorized Stage A gate runs THIS source
# string through pyarrow over the raw inverted-index JSON (where word order does
# not exist, so only single-token tests are sound), and ``keyword_verdict`` runs
# it over the title alone. One definition, so the two cannot drift apart.
# Non-English stems reach works the English ones cannot: 111 of 112 Korean 재검증
# works and 10 of 11 반복검증 are invisible to the English stems. They carry no
# matching phrases, so a hit can only ever be `ambiguous` — which is the intent,
# since the LLM screen reads these languages and the rule filter does not.
REPLICATION_STEM_PATTERN = (
    r"(?i)replicat|replicab|reproduc|reanalys|re-analys|reanalyz|re-analyz"
    r"|replikat|réplicat|replicaci|replicaç|replicazion|reproduç|reproduzi"
    r"|追試|반복검증|재검증")
_STEM_RE = re.compile(REPLICATION_STEM_PATTERN)

# Patterns are intentionally compiled WITHOUT a global counterpart — JS's /g
# flag carries lastIndex across calls (LESSONS.md #15 in the SciMeto repo).
# Python's ``re`` API is stateless, so this just means we use ``search``.
# Reproduction-genre phrases, shared by REPLICATION_PHRASES and REPRODUCTION_PHRASES.
# ``is_reproduction_only`` compares pattern objects by identity, and ``re.compile``
# caches on (pattern, flags), so listing the same source twice would still work —
# but naming them once makes the shared membership explicit.
_REPRODUCTION_ANCHORED: list[re.Pattern] = [
    re.compile(r"\breproductions? of ['\"“‘]", re.IGNORECASE),
    # The quote-required form above is kept as measured (4 hits / 4.2M rows, 0
    # exclusive) but misses the computational-reproduction genre. A results-noun
    # object reaches it at 14 exclusive hits / 4.2M. A BARE "reproductions? of"
    # is deliberately NOT used: 807 exclusive hits, dominated by biological and
    # *social* reproduction ("The Reproduction of Mothering") and epidemiological
    # R₀ — 0 of 15 sampled were our sense (#137).
    re.compile(r"\breproductions? of\s+(?:the\s+)?"
               r"(?:(?:original|published|reported|previous|prior|main|key|core)\s+)?"
               r"(?:results?|findings?|analys[ei]s|estimates?|figures?|tables?|"
               r"stud(?:y|ies)|experiments?|data)\b", re.IGNORECASE),
    # "comput\w*" not "computational": the genre says "computationally reproducible"
    # at least as often as "computational reproduction".
    re.compile(r"\bcomput\w*\s+reproduc\w+\b", re.IGNORECASE),
    re.compile(r"\brobustness\s+(?:replicabilit\w+|reproducibilit\w+|replication)\b", re.IGNORECASE),
    re.compile(r"\breproduc\w+\s+(?:and|&)\s+(?:replicat|extend|extension)\w*\b", re.IGNORECASE),
    # Re-analysis of a prior study's data is a reproduction. Cost-free on every
    # labelled set and the largest single gain on the curated reproduction list.
    # Broadened from "re-?analys[ei]s of" to cover the verb forms and the American
    # spelling; a strict superset of the original, which stays admitted.
    re.compile(r"\bre-?analy[sz](?:is|es|ed|ing|e)\s+of\b", re.IGNORECASE),
    # Bare American spelling. "reanalysis" reaches Stage A on the "reanalys" stem
    # and Stage B via the pattern above; "reanalyzed"/"reanalyzing" reached NEITHER
    # (#137). No "of" is required because the token itself is already specific —
    # unlike "reproduction", it has no biological or social homograph.
    re.compile(r"\bre-?analyz\w+\b", re.IGNORECASE),
    # --- reproduce-side verbs (#137 item 3) ---
    # Never anchored on a bare "reproduce": that is biology. Each of these needs a
    # results-noun object, a first-person subject, or a failure/attempt framing.
    # The optional ``\w+ly`` slot is the adverb insertion of #137 item 4
    # ("reproduce the results" / "reproduce exactly the reported estimates").
    re.compile(r"\breproduc(?:e|es|ed|ing)\s+(?:\w+ly\s+)?(?:the\s+)?"
               r"(?:original|published|reported|previous|prior|main|key|core|"
               r"their|these|authors'?)?\s*"
               r"(?:results?|findings?|estimates?|figures?|tables?|analys[ei]s)\b",
               re.IGNORECASE),
    # "try" is spelled out rather than stemmed: ``try\w*`` would miss "tried"/"tries"
    # and ``tri\w*`` would swallow "trial", so the four inflections are listed the way
    # ``attempt\w*`` covers its own on the replicate side. "trying to reproduce" was
    # the hole.
    re.compile(r"\b(?:fail\w*|unable|inabilit\w+|attempt\w*|sought|seek\w*|aim\w*|"
               r"tr(?:y|ies|ied|ying))\s+to\s+reproduce\b", re.IGNORECASE),
    re.compile(r"\bwe\s+(?:\w+ly\s+)?reproduced?\b", re.IGNORECASE),
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
    # over 15,440 corpus rows), while these anchored forms cost nothing and score
    # better on the curated reproduction list.
    *_REPRODUCTION_ANCHORED,
    re.compile(r"\breplicat(?:e|es|ed|ing)\s+(?:the\s+)?"
               r"(?:previous|prior|original|earlier|main|key|core|published)?\s*"
               r"(?:findings?|results?|effects?|analys[ei]s|stud(?:y|ies))\b",
               re.IGNORECASE),
    # --- negative and passive replicate forms (#137 item 2) ---
    # The list already had "failed to replicate" and "did not replicate" and nothing
    # else: a failed replication is exactly what FLoRA wants, and English expresses
    # it half a dozen other ways. One alternation for the auxiliary+negation shapes
    # ("does not replicate", "was not replicated", "could not be replicated",
    # "cannot be replicated"), one for the noun/adjective framings.
    # The perfect ("has not BEEN replicated") and "never" were the two holes: the
    # auxiliary+negation shape is the same, but "been" is not "be" and "never" is not
    # "not" — 803 rows of candidates.csv say it one of those two ways.
    re.compile(r"\b(?:(?:d(?:oes|o|id)|w(?:as|ere)|is|are|ha[sd]|have|could|would|"
               r"should|can|may|might)\s+(?:not|never)|cannot|can['’]t)\s+"
               r"(?:(?:be|been)\s+)?replicat\w+\b", re.IGNORECASE),
    re.compile(r"\b(?:unable|inabilit\w+|failure)\s+to\s+replicate\b", re.IGNORECASE),
    # --- adverb insertion (#137 item 4) ---
    # "we successfully/partially/closely/directly replicated". Kept as a separate
    # pattern rather than widening "\bwe replicated\b" in place, so that phrase's
    # PHRASE_GUARDS key (and its measured behaviour) is untouched; the new pattern
    # carries the same GWAS guard below.
    re.compile(r"\bwe\s+\w+ly\s+replicate[sd]?\b", re.IGNORECASE),
]

# Subset that should be classified as ``reproduction`` rather than ``replication``
# when the only matching phrases come from this list. The set is intentionally
# narrow — see RULEBOOK §Filter.
REPRODUCTION_PHRASES: list[re.Pattern] = list(_REPRODUCTION_ANCHORED)


# "We replicated one SNP (rs133885) from 585 SNPs previously reported..." — a
# GWAS discovery/replication-cohort design, not a replication of a study.
# Removes 59 curated negatives at zero cost on either gold set. The same guard
# on "replication stud(y|ies)" costs 28 human-gold papers to remove 18
# negatives, so it is deliberately scoped to the first-person phrases.
_GWAS_GUARD = re.compile(
    r"\b(?:gwas|genome[-\s]wide|snps?|alleles?|genotyp\w+|haplotype|"
    r"linkage disequilibrium|minor allele frequency|loci|polymorphism\w*|"
    r"replication cohort|discovery cohort|exome)\b", re.IGNORECASE)

# Phrase-scoped negative contexts, keyed by pattern source. A phrase whose guard
# fires is skipped while every OTHER phrase in the same text can still match —
# unlike the row-level patterns in exclusion-patterns.yaml, which kill the row.
# That distinction matters: the measurement-reliability vocabulary makes
# "reproducibility of" worthless but says nothing about "a direct replication of
# Smith (2010)" in the same abstract.
PHRASE_GUARDS: dict[str, re.Pattern] = {
    r"\bwe replicated\b": _GWAS_GUARD,
    # "We successfully replicated one SNP ..." is the same design with an adverb.
    r"\bwe\s+\w+ly\s+replicate[sd]?\b": _GWAS_GUARD,
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


# The seven exclusion patterns, in the order the former exclusion-patterns.yaml
# listed them. Order is load-bearing: ``is_non_scholarly_context`` reports the
# FIRST pattern that fires, and that id reaches filter_evidence. The engine's own
# ordering is by precedence and is a different question, so it is not reused here.
_EXCLUSION_SPEC_IDS = ("biological", "technical-object", "technical-verb",
                       "structural", "biological-of", "editorial-artifact",
                       "data-availability")


def _load_exclusion_regexes() -> list[tuple[str, re.Pattern]]:
    """The exclusion patterns as (legacy id, compiled) pairs, from the spec bundle.

    Two of the seven need a lookaround that RE2 forbids, so their specs carry a
    decomposed RE2 match for the vectorized engine AND the exact original under
    ``pyre_regex``. This loader takes the original wherever it exists, which is
    what keeps ``keyword_verdict`` byte-identical to the YAML era while the
    engine evaluates the (wider, and therefore shadow) decomposition.
    """
    specs = {s.id: s for s in load_specs(Path(__file__).parent / "spec")}
    out: list[tuple[str, re.Pattern]] = []
    for spec_id in _EXCLUSION_SPEC_IDS:
        match = specs[spec_id].match
        pattern = match.pyre_regex or match.text_regex
        out.append((spec_id.upper().replace("-", "_"),
                    re.compile(pattern, re.IGNORECASE)))
    return out


# Compiled once at import. The bundle is small and immutable across a run;
# reloading on every call would be wasteful.
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


# ---------------------------------------------------------------------------
# The shared keyword decision
# ---------------------------------------------------------------------------


def _author_year_cites(text: str, year: Optional[int]) -> list:
    """Author-year citations in *text*. strict_bare drops single_bare false matches
    (months, "Study 2019", date ranges) that would otherwise auto-accept a row."""
    return (extract_author_year_patterns(text, max_year=year, strict_bare=True)
            if year else extract_author_year_patterns(text, strict_bare=True))


@dataclass(frozen=True)
class KeywordVerdict:
    """What the keyword rule says about one paper.

    ``text`` is the title+abstract blob the verdict was computed on — callers reuse
    it (for sentence spans, citations) instead of joining the two fields themselves,
    so nobody can evaluate the rule against a differently-assembled string.
    """

    outcome: str            # "positive" | "ambiguous" | "negative"
    reason: str             # evidence, ready for filter_evidence
    text: str
    phrase: str = ""
    phrase_start: int = -1
    is_reproduction: bool = False
    exclusion: str = ""


def keyword_verdict(title: str, abstract: str,
                    year: Optional[int] = None) -> KeywordVerdict:
    """THE keyword decision, shared by Stage 1's snapshot gate and Stage 2's filter.

    Title and abstract are taken SEPARATELY because the rule treats them
    differently: a title is a handful of topical words, so a bare replication stem
    in one is a real signal, while the same stem anywhere in a 1,500-character
    abstract is noise. That distinction used to live only inside Stage 1's
    admission and was invisible to Stage 2; it is now part of the shared rule.

    Three outcomes:

      positive   a precise ``REPLICATION_PHRASES`` match that survived the
                 exclusion patterns and the phrase guards.
      ambiguous  worth a look, not settled — either a replication stem in the
                 TITLE with no precise phrase anywhere, or a phrase that an
                 exclusion pattern killed while a specific author-year cite is
                 present (the #44 targeted readmission).
      negative   nothing, or an exclusion with nothing to rescue.

    An exclusion is checked FIRST and dominates the title-stem tier: "Origins of
    DNA replication" is a biological title, and admitting every such title on its
    stem is exactly the noise the exclusion patterns exist to remove.
    """
    text = f"{title or ''}\n{abstract or ''}".strip()

    excl = is_non_scholarly_context(text)
    if excl:
        # The exclusion gate suppresses phrase detection, so re-check ignoring it:
        # exclusions misfire on in-scope computational reproductions ("replicated
        # the analysis code of Smith (2019)"). A phrase AND a specific author-year
        # cite together are enough to hand the row on rather than reject it.
        rescue = find_replication_phrase_span(text, ignore_exclusions=True)
        if rescue is not None and _author_year_cites(text, year):
            return KeywordVerdict(
                "ambiguous", f"exclusion:{excl}; phrase+cite present — LLM review",
                text, phrase=rescue[0], phrase_start=rescue[1], exclusion=excl)
        return KeywordVerdict("negative", f"exclusion:{excl}", text, exclusion=excl)

    match = find_replication_phrase_span(text)
    if match is not None:
        phrase, start, _end = match
        return KeywordVerdict("positive", f"phrase:{phrase!s}", text, phrase=phrase,
                              phrase_start=start,
                              is_reproduction=is_reproduction_only(text))

    stem = _STEM_RE.search(title or "")
    if stem:
        return KeywordVerdict("ambiguous",
                              f"title stem:{stem.group(0).lower()}; no phrase", text)

    return KeywordVerdict("negative", "no replication phrase detected", text)
