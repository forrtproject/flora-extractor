"""The optional cheap pre-screen in front of Stage 3's validated front-door screen.

Two very small models are asked, in sequence, whether a paper could possibly be
re-testing an earlier study. They can only DISCARD, and only when both say no — every
other outcome (one keep, an unreadable reply, an API failure, a bypass) falls through to
the validated screen unchanged. So this tier can lose papers but can never add them,
which is why it is off by default and why everything below is built to fail open.

The whole tier lives in one module, calling into llm_client for transport only, so it
can be removed in one piece without touching the validated screen's semantics.

A pre-screen discard is terminal: the row never reaches the validated Stage 3 screen and
never reaches a human. Small models are cheap enough to gate the corpus but not reliable
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
from .config import (CURATED_SOURCES, LLM_CACHE_DIR, PRESCREEN_MIN_ABSTRACT_CHARS,
                     PRESCREEN_VOTER1_MODEL, PRESCREEN_VOTER2_MODEL)
from .llm_client import cache_model_id, call_openai, call_openrouter
from .prompts import build_prescreen_prompt, prompt_version
from . import token_counter

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
    """(provider, model) in call order. A model id containing "/" routes to OpenRouter,
    anything else to OpenAI direct — the same rule screen_voters() uses.

    Configuring the same model twice would silently collapse the AND gate: the second
    vote hits the first one's cache key, replays its "no", and one answer becomes a
    terminal discard that the row records as two voters agreeing. Refused outright —
    two calls to one model are not two-model agreement.
    """
    if PRESCREEN_VOTER1_MODEL == PRESCREEN_VOTER2_MODEL:
        raise RuntimeError(
            "PRESCREEN_VOTER1_MODEL and PRESCREEN_VOTER2_MODEL are both "
            f"{PRESCREEN_VOTER1_MODEL!r}. The pre-screen may only discard on two "
            "independent voters agreeing; set them to different models."
        )
    return [("openrouter" if "/" in m else "openai", m)
            for m in (PRESCREEN_VOTER1_MODEL, PRESCREEN_VOTER2_MODEL)]


def _vote(prompt: str, provider: str, model: str, doi_r: str, title: str) -> "str | None":
    """One voter's answer: "yes", "no", or None when there is no usable answer.

    Cached per voter rather than per gate, so a re-run reuses the vote that succeeded
    and retries only the one that failed. A non-answer is never cached: an unparseable
    reply and a 503 are both "ask again", not "this paper is out of scope".
    """
    key = content_key("prescreen_vote", doi_r or title,
                      prompt_version("build_prescreen_prompt"),
                      cache_model_id(model), provider, prompt)
    try:
        cached = read_cache(LLM_CACHE_DIR, key)
        if isinstance(cached, dict) and cached.get("verdict") in {"yes", "no"}:
            return cached["verdict"]

        result, error = (call_openrouter(prompt, model=model) if provider == "openrouter"
                         else call_openai(prompt, model=model))
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


def prescreen(doi_r: str, title: str, abstract: str, source: str = "") -> dict:
    """Ask the cheap voters whether to discard this row before the validated screen.

    Returns a dict with `verdict` ("discard" or "proceed"), the `bypass` reason if one
    fired, and `evidence` naming every voter and what it said — a discard is terminal,
    so the row has to record who ended it.

    Voter 2 is only asked when voter 1 said "no": once the row can no longer be
    discarded, a second opinion cannot change the outcome and is not worth paying for.
    """
    out = {"verdict": "proceed", "bypass": "", "evidence": "",
           "models": "+".join(m for _, m in prescreen_voters())}

    bypass = prescreen_bypass(title, abstract, source)
    if bypass:
        out["bypass"] = bypass
        out["evidence"] = f"prescreen bypassed: {bypass}"
        return out

    token_counter.set_stage("extract_prescreen")
    prompt = build_prescreen_prompt(title, abstract)
    said: list[str] = []
    for provider, model in prescreen_voters():
        verdict = _vote(prompt, provider, model, doi_r, title)
        said.append(f"{model}={verdict or 'no-answer'}")
        if verdict != "no":
            out["evidence"] = f"prescreen proceed: {'; '.join(said)}"
            return out

    out["verdict"] = "discard"
    out["evidence"] = f"prescreen discard: {'; '.join(said)}"
    return out
