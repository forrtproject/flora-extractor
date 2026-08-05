"""The cheap discard-only tier's vocabulary: its bypasses, its voters, its one vote.

Two very small models are asked, in sequence, whether a paper could possibly be
re-testing an earlier study. They can only DISCARD, and only when both say no — every
other outcome (one keep, an unreadable reply, an API failure, a bypass) leaves the row
on its way to the validated screen. So this tier can lose papers but can never add
them, which is why everything below is built to fail open.

**Where the tier runs.** Stage 2, over the `screen_cheap` pile
(`run_screen_cheap()` / `_cheap_judge()` in `filter/engine/tiers.py`): which rows get
it is a routing decision, claimed and budget-gated like every other spend. This
module is not a tier runner — it holds what the tier ASKS, so the question, the
bypasses and the parsing live in one place and the claim/verdict plumbing lives in
the other. `prescreen_vote()` is the seam between them.

Stage 3 never calls any of this. It used to run the tier itself, in front of its own
screen; that half is deleted, because a global on/off would have re-applied the cheap
gate to rows the rule book deliberately routed to the expensive tier.

A pre-screen discard is terminal: the row never reaches the validated screen and never
reaches a human. Small models are cheap enough to gate the corpus but not reliable
enough to be trusted alone — issue #130 measured both cheap voters discarding a paper
whose abstract opens "The purpose of this ... systematic replication study was to...".

So the phrases that state the design outright are matched deterministically before any
model is asked. A hit means the row skips the pre-screen entirely and goes to the
validated screen. A false positive here costs one screen call (~$0.0018); a false
negative in the pre-screen costs a replication study, silently and permanently.
"""
import logging
import re

from .cache import content_key, read_cache, write_cache
from .config import (
    CURATED_SOURCES, LLM_CACHE_DIR, PRESCREEN_MODEL_1, PRESCREEN_MODEL_2,
)
from .llm_client import cache_model_id, call_model, provider_for
from .prompts import prompt_version

# Below this many characters of abstract there is not enough text for a 3B-class model
# to be trusted with a terminal verdict, so the row bypasses the tier.
PRESCREEN_MIN_ABSTRACT_CHARS = 200

log = logging.getLogger(__name__)

# Phrases that, on their own, mean a human would keep the paper for a closer look.
# Written to be over- rather than under-inclusive: the cost of a needless match is the
# price of one validated-screen call.
_SIGNAL_PATTERNS = (
    # the paper naming its own design
    r"replicat(?:ion|ions)\s+(?:stud(?:y|ies)|attempts?|experiments?|effort)",
    r"(?:direct|close|exact|conceptual|systematic|independent|internal|external"
    r"|pre-?registered|registered|high-?powered|large-?scale|multi-?(?:lab|site|centre|center))"
    r"[\s-]+replicat(?:ion|ions)",
    r"registered\s+replication\s+report",
    r"\breplication\s+of\s+(?:a|an|the|their|his|her|our|two|three|\w+'s|study|studies)",
    r"\breplicat(?:ion|ions)\s+and\s+extensions?",
    r"\bre-?test(?:s|ed|ing)?\s+(?:the|an|a)\s+(?:original|earlier|previous|prior|published)",

    # the authors saying what they did
    r"\bwe\s+(?:\w+\s+){0,3}replicat(?:e|ed)\b",
    r"\b(?:aim(?:s|ed)?|attempt(?:s|ed)?|sought|seeks?|set\s+out|tried|tries|purpose[^.]{0,40})"
    r"\s+to\s+replicate\b",
    r"\b(?:failed|failure|unable|succeeded|success(?:ful)?)\s+to\s+replicate\b",
    r"\b(?:successfully|closely|fully|partially|not)\s+replicated\b",
    r"\breplicated\s+(?:the|their|these|this|an|a)\s+"
    r"(?:original|earlier|previous|prior|published|finding|result|effect|stud)",

    # reproduction / re-analysis vocabulary
    r"\breproduc(?:e|ed|ing|tion)\s+(?:the\s+)?"
    r"(?:results?|findings?|analys[ei]s|estimates?|tables?|figures?)\s+(?:of|from|reported)",
    r"\bcomputational\s+reproduc(?:tion|ibility)",
    r"\breproducibility\s+(?:project|check|assessment|audit)",
    r"\bre-?analys(?:is|ed|es|ing|e)\s+(?:of\s+)?(?:the\s+)?"
    r"(?:original|published|previously|earlier|prior)",

    # the large coordinated efforts, which name themselves
    r"\bmany\s?labs\b",
    r"\breproducibility\s+project\b",

    # Everything below was derived from the vocabulary of 7,505 FLoRA papers rather than
    # from the four misses that motivated the block above, and measured on a held-out
    # half those patterns were never shown: recall 80% -> 94%, for ~$7 of extra screen
    # calls per corpus pass. Evidence: analysis/prescreen_eval/OVERRIDE_EVAL.md.
    r"\b(?:stud(?:y|ies)|paper|article|experiments?|analys[ei]s|work|note|report|research"
    r"|investigation|manuscript)\s+(?:\w+\s+){0,2}replicat(?:e|es|ed|ing)\b",
    r"\b(?:results?|findings?|effects?|data)\s+(?:\w+\s+){0,1}replicat(?:e|es|ed)\b",
    r"\breplicat(?:e|es|ed|ing|ion|ions)\s+and\s+(?:extend|expand|generali[sz]|elaborat|explor)\w*"
    r"|\b(?:extend|expand)\w*\s+and\s+replicat\w+",
    r"\breplicat(?:e|es|ed|ing)\s+(?:the\s+|their\s+|these\s+|a\s+|an\s+)?"
    r"(?:previous|prior|earlier|original|published|key|main|core|central)\b",
    r"\breplicat(?:e|es|ed|ing)\s+(?:the\s+|their\s+|these\s+|our\s+|its\s+|his\s+|her\s+|a\s+|an\s+)?"
    r"(?:findings?|results?|effects?|analys[ei]s|procedures?|estimates?|experiments?|stud(?:y|ies))\b",
    r"\breplicating\b",
    r"\b(?:narrow|wide|long|partial|approximate|full|successful|unsuccessful|failed"
    r"|methodological|experimental|attempted|scientific|near|quasi|constructive"
    r"|cross-?cultural|cross-?national|online|field|lab(?:oratory)?)[\s-]+replicat(?:ion|ions)\b",
    r"\b(?:aim|goal|purpose|objective)[^.]{0,40}\breplicat",
    r"\b(?:failure|failures|attempt|attempts|attempting|efforts?)\s+to\s+replicat",
    r"\b(?:did|does|do|was|were|could|can|would|is|are)\s+not\s+(?:be\s+)?replicat(?:e|ed)\b",
    r"\breplication\s+(?:analys[ei]s|sample|cohort|data\s?sets?|set|stage|phase|series"
    r"|trial|attempt|effort|package|material)",
    r"\b(?:identification|discovery|fine-?mapping|detection|association|validation"
    r"|extension|confirmation)\s+and\s+replication\b",
    r"\breproduc(?:e|es|ed|ing|tion)\s+(?:the|their|these|his|her|its|our|previously|original)\s+"
    r"(?:published\s+)?(?:results?|findings?|analys[ei]s|estimates?|numbers?|tables?|figures?"
    r"|work|stud(?:y|ies)|experiments?)\b",
    r"\breplicated\s+in\s+(?:a|an|the|two|three|our|independent|separate)\b",
    r"\breplications?\s+of\s+[^.;:]{0,60}?(?:\(?(?:19|20)\d{2}\)?|et\s+al)",
    r"\breplications?\s+of\s+(?:the\s+|a\s+|an\s+|their\s+|this\s+|that\s+|these\s+|our\s+)?"
    r"(?:previous|prior|earlier|original|published|previously|older|existing|classic|seminal|key)",
)

_SIGNAL_RE = re.compile("|".join(f"(?:{p})" for p in _SIGNAL_PATTERNS), re.IGNORECASE)


def hard_signal(title: str, abstract: str) -> str:
    """Return the replication phrase found in the text, or "" when none fires."""
    match = _SIGNAL_RE.search(f"{title or ''}\n{abstract or ''}")
    return match.group(0).strip() if match else ""


def prescreen_bypass(title: str, abstract: str, source: str = "") -> str:
    """Why this row must not be pre-screened at all, or "" when it may be.

    Three cases, all of them "a small model should not get the last word here":

      hard_signal  — the text states the design outright.
      short_text   — under PRESCREEN_MIN_ABSTRACT_CHARS there is not enough to judge,
                     and the rows with no abstract are the pre-screen's worst stratum.
      curated:X    — a human already put the paper on a replication list, and the
                     engine routes such a row to a screen pile rather than letting
                     a keyword rule discard it. Undoing that here with a 3B model
                     would defeat the point of the curated lists.

    Stage 2's own high-confidence `replication` verdict is deliberately NOT a bypass:
    measured over data/filtered.csv, 98% of rows that reach Stage 3 carry it, including
    every screen-confirmed negative, so bypassing on it would disable the tier entirely.
    """
    if (source or "").strip().lower() in CURATED_SOURCES:
        return f"curated:{source.strip().lower()}"
    if len((abstract or "").strip()) < PRESCREEN_MIN_ABSTRACT_CHARS:
        return "short_text"
    signal = hard_signal(title, abstract)
    return f"hard_signal:{signal}" if signal else ""


def prescreen_voters() -> list[tuple[str, str]]:
    """(provider, model) in call order. The provider follows the model id, through
    provider_for() — the same rule screen_voters() uses.

    Configuring the same model twice would silently collapse the AND gate: the second
    vote hits the first one's cache key, replays its "no", and one answer becomes a
    terminal discard that the row records as two voters agreeing. Refused outright —
    two calls to one model are not two-model agreement.
    """
    if PRESCREEN_MODEL_1 == PRESCREEN_MODEL_2:
        raise RuntimeError(
            "PRESCREEN_MODEL_1 and PRESCREEN_MODEL_2 are both "
            f"{PRESCREEN_MODEL_1!r}. The pre-screen may only discard on two "
            "independent voters agreeing; set them to different models."
        )
    return [(provider_for(m), m) for m in (PRESCREEN_MODEL_1, PRESCREEN_MODEL_2)]


def prescreen_vote(prompt: str, provider: str, model: str, doi_r: str,
                   title: str) -> "str | None":
    """One voter's answer: "yes", "no", or None when there is no usable answer.

    The public seam between this module and the tier runner: `_cheap_judge()` in
    `filter/engine/tiers.py` asks the voters through here, so the prompt, the cache
    key, the parsing and the rule that an unrecognised label is a NON-answer rather
    than a "no" are defined once, on the side that owns the question.

    Cached per voter rather than per gate, so a re-run reuses the vote that succeeded
    and retries only the one that failed. A non-answer is never cached: an unparseable
    reply and a 503 are both "ask again", not "this paper is out of scope".
    """
    key = content_key("prescreen_vote", doi_r or title,
                      prompt_version("build_prescreen_prompt"),
                      # No reasoning: the tier's whole point is a one-field answer
                      # from a tiny model, and the empty effort is passed to both the
                      # key and the call so neither can be given one by accident.
                      cache_model_id(model, ""), provider, prompt)
    try:
        cached = read_cache(LLM_CACHE_DIR, key)
        if isinstance(cached, dict) and cached.get("verdict") in {"yes", "no"}:
            return cached["verdict"]

        result, _provider, error = call_model(prompt, model, reasoning_effort="")
        if not isinstance(result, dict):
            log.debug("prescreen %s/%s no usable answer: %s", provider, model, error)
            return None
        verdict = str(result.get("maybe_replication", "")).strip().lower()
        if verdict not in {"yes", "no"}:
            # An unrecognised label must never collapse to "no" — the terminal side.
            log.debug("prescreen %s/%s unusable label %r", provider, model, verdict[:40])
            return None
        write_cache(LLM_CACHE_DIR, key, {"verdict": verdict})
        return verdict
    except Exception as e:  # noqa: BLE001 - API/cache boundary
        # The tier is optional and every non-answer proceeds, so a corrupt cache entry
        # or a transport that returns something unexpected must cost this row its
        # saving, never the run and never the paper.
        log.debug("prescreen %s/%s failed: %s", provider, model, e)
        return None
