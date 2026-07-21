"""
code_outcome.py — Keyword + LLM outcome extraction for Stage 3.

The outcome of a replication is decided from the ABSTRACT by default: a keyword
scan of title + abstract, then an abstract-based LLM call for anything the scan
cannot classify. Parsed fulltext is held in reserve — an introduction routinely
discusses OTHER studies' failures ("X failed to replicate in prior work"), so
scanning it at high confidence misfires. Fulltext is used only as an escalation:
when the abstract LLM returns cannot_be_determined (or there is no abstract), a
second LLM call reads the parsed text.

Public API:
    extract_outcome(doi_r, abstract_r, fulltext, title_r) → dict
"""
import json
import re
import time
from typing import Optional

from shared.config import (
    GEMINI_HEAVY_MODEL, LLM_CACHE_DIR, LLM_CACHE_READ, LLM_RATE_SEC,
    OUTCOME_FULLTEXT_ESCALATION, log,
)
from shared import token_counter
from shared.cache import read_dual_cache, write_dual_cache
from shared.llm_client import call_llm
from shared.schema import OUTCOME_CATEGORIES
from shared.utils import cache_key

# Bump when the prompt or model wiring changes so the content-keyed cache misses
# stale entries. read_dual_cache in "latest" mode keys on this; "accumulate" mode
# still prefers the legacy DOI-keyed entry.
PROMPT_VERSION = "2026-07-16-abstract-escalation"

# Truncation caps (chars) for the abstract-based and fulltext-escalation prompts.
_ABSTRACT_CAP = 3000
_FULLTEXT_CAP = 8000

# ── Sentence splitter helpers ─────────────────────────────────────────────────

_ABBREV_RE = re.compile(
    r"\b(?:et al|e\.g|i\.e|vs|Dr|Mr|Mrs|Ms|Prof|Fig|No|Vol|pp|cf)\."
    r"|(?<!\w)\b[A-Z]\.",
    re.IGNORECASE,
)


def _expand_to_sentences(text: str, match_start: int, match_end: int,
                          n_context: int = 1) -> str:
    """Return the sentence containing the match plus n_context sentences on each side."""
    if not text:
        return ""
    placeholder = "\x00"
    masked = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", placeholder), text)
    raw_sentences = re.split(r"(?<=[.!?])\s+", masked.strip())
    sentences = [s.replace(placeholder, ".") for s in raw_sentences if s.strip()]
    if not sentences:
        return text.strip()
    target_idx = len(sentences) - 1
    cumulative = 0
    for i, sent in enumerate(sentences):
        pos = text.find(sent.strip(), cumulative)
        if pos == -1:
            pos = cumulative
        end_pos = pos + len(sent)
        if pos <= match_start < end_pos:
            target_idx = i
            break
        cumulative = end_pos
    lo = max(0, target_idx - n_context)
    hi = min(len(sentences) - 1, target_idx + n_context)
    return " ".join(sentences[lo : hi + 1]).strip()


# ── Keyword patterns (Pass 1) ─────────────────────────────────────────────────
# Failure is checked before success to avoid "failed to replicate" hitting success.

_FAILURE = re.compile(
    r"\b("
    r"failed to replicate|replication failed|could not replicate"
    r"|did not replicate|not replicated|no support for the original"
    r"|inconsistent with (?:the )?(?:original|prior)"
    r"|results did not (?:hold|replicate)|null result"
    r"|no evidence|no significant (?:effect|difference)"
    r"|failed to reproduce|did not reproduce"
    r")\b",
    re.IGNORECASE,
)

_SUCCESS = re.compile(
    r"\b("
    r"successfully replicated|replication succeeded|results (?:were )?replicated"
    r"|confirmed the (?:original|findings?|results?|effect)"
    r"|supported the original"
    r"|consistent with (?:the )?(?:original|prior)"
    r"|replication was successful|effect was reproduced"
    r"|was (?:successfully )?replicated|replicated successfully"
    r")\b"
    r"|(?<!\w)replicated(?!\w)",   # bare "replicated" as low-priority catch-all
    re.IGNORECASE,
)

_MIXED = re.compile(
    r"\b("
    r"partially replicated|mixed results?|partial replication"
    r"|some but not all|some (?:but not all|support)"
    r"|nuanced|qualified support"
    r"|smaller (?:effect|than original)|reduced (?:effect|magnitude)"
    r")\b",
    re.IGNORECASE,
)

_DESCRIPTIVE = re.compile(
    r"\b("
    r"adapted (?:the|this) (?:method|procedure|paradigm)"
    r"|in a (?:different|new) (?:context|sample|culture|population)"
    r"|not intended to test|not a direct test"
    r")\b",
    re.IGNORECASE,
)

# Single source of truth (schema.OUTCOME_CATEGORIES) — includes not_a_replication,
# which _llm_outcome emits when is_genuine_attempt=false.
_VALID_OUTCOMES = OUTCOME_CATEGORIES


def _keyword_scan(text: str, source: str) -> Optional[dict]:
    """Return a result dict if a keyword pattern matches, else None.

    Check order: failure → mixed → success → descriptive.
    Mixed is checked before success so that "partially replicated" resolves
    to mixed rather than triggering the broad bare-"replicated" success pattern.
    """
    m = _FAILURE.search(text)
    if m:
        return {"outcome": "failure",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "high", "out_quote_source": source}
    m = _MIXED.search(text)
    if m:
        return {"outcome": "mixed",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "medium", "out_quote_source": source}
    m = _SUCCESS.search(text)
    if m:
        return {"outcome": "success",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "high", "out_quote_source": source}
    m = _DESCRIPTIVE.search(text)
    if m:
        return {"outcome": "descriptive",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "medium", "out_quote_source": source}
    return None


_OUTCOME_RULES = (
    "Outcome classification rules:\n"
    "- success: authors explicitly state the original finding was confirmed, replicated, or supported\n"
    "- failure: authors explicitly state the original finding was NOT found, contradicted, or failed to replicate\n"
    "- mixed: authors state that SOME but not all aspects of the original finding were confirmed\n"
    "- descriptive: authors adapted or extended methods in a different context/population WITHOUT directly testing the original claim\n"
    "- cannot_be_determined: the text lacks sufficient detail to classify the outcome (not when authors say it's unclear, but when WE cannot tell)\n\n"
    "Few-shot examples:\n"
    "1. DESCRIPTIVE (methods reused, original claim not tested): 'This conceptual replication extends the theory but does not directly test the original hypothesis.'\n"
    "2. CANNOT_BE_DETERMINED (insufficient detail): 'We conducted a replication study in a different population.' (no mention of success or failure)\n"
    "3. MIXED (partial success): 'We replicated the main effect but not the interaction.'\n"
    "4. SUCCESS (confirmation): 'Our findings confirm Smith et al. (2015)'\n\n"
    "CRITICAL: Only output 'cannot_be_determined' when the text genuinely lacks detail.\n\n"
    "Before classifying the outcome, first judge: does this text describe a genuine "
    "attempt to replicate OR reproduce the specific original study named above (or "
    "discussed in the abstract)? Both replications (new data/sample testing whether "
    "the finding holds) and reproductions (re-analysis of the same original data) "
    "count as genuine attempts — this judgment does not distinguish between them, "
    "that classification happens elsewhere in the pipeline. Answer false only when "
    "the text does not engage with verifying that specific original at all — e.g. "
    "'replicate'/'reproduce' is used in an unrelated biological or technical sense "
    "(DNA replication, code reproduction), or metaphorically/colloquially (e.g. "
    "'a replication of prior interests and positions'), or the text is simply "
    "unrelated to the named original study.\n\n"
)


def _abstract_prompt(title_r: str, abstract_snip: str, original_block: str) -> str:
    return (
        "You are a research methodology expert. Classify the replication outcome based on what the paper's abstract states.\n\n"
        + original_block
        + f"TITLE: {title_r}\n"
        f"ABSTRACT: {abstract_snip or '(not available)'}\n\n"
        + _OUTCOME_RULES +
        "Respond with ONLY this JSON:\n"
        '{"is_genuine_attempt": <true|false>, '
        '"outcome": "<success|failure|mixed|descriptive|cannot_be_determined>", '
        '"outcome_phrase": "<verbatim quote of 2-3 sentences from the abstract that specifically describes what replicated and what did not>", '
        '"outcome_confidence": "<high|medium|low>", '
        '"out_quote_source": "<abstract|title>", '
        '"outcome_reasoning": "<one sentence explaining the classification choice>"}'
    )


def _fulltext_prompt(title_r: str, abstract_snip: str, text_snip: str,
                     original_block: str) -> str:
    return (
        "You are a research methodology expert. The abstract alone could not settle "
        "the replication outcome. Classify it using the paper's full text.\n\n"
        + original_block
        + f"TITLE: {title_r}\n"
        f"ABSTRACT: {abstract_snip or '(not available)'}\n"
        f"PARSED FULLTEXT: {text_snip or '(not available)'}\n\n"
        + _OUTCOME_RULES +
        "Judge the outcome of THIS paper's own replication, not outcomes it reports "
        "for other studies in its background or literature review.\n\n"
        "Respond with ONLY this JSON:\n"
        '{"is_genuine_attempt": <true|false>, '
        '"outcome": "<success|failure|mixed|descriptive|cannot_be_determined>", '
        '"outcome_phrase": "<verbatim quote of 2-3 sentences from the paper that specifically describes what replicated and what did not>", '
        '"outcome_confidence": "<high|medium|low>", '
        '"out_quote_source": "<abstract|title|fulltext>", '
        '"outcome_reasoning": "<one sentence explaining the classification choice>"}'
    )


def _call_outcome_llm(prompt: str, doi_r: str) -> tuple[Optional[dict], str]:
    """Call the outcome LLM with up to 3 retries and exponential backoff."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            result, model_used, _ = call_llm(prompt, gemini_model=GEMINI_HEAVY_MODEL,
                                              prefer_openai=True)
            if result:
                time.sleep(LLM_RATE_SEC)
                return result, model_used
        except Exception as e:
            wait_time = 2 ** attempt  # 1s, 2s, 4s
            if attempt < max_retries - 1:
                log.warning("[%s] outcome LLM failed (attempt %d/%d), retrying in %ds: %s",
                            doi_r, attempt + 1, max_retries, wait_time, str(e))
                time.sleep(wait_time)
            else:
                log.warning("[%s] outcome LLM failed after %d retries: %s", doi_r, max_retries, str(e))
    return None, ""


def _normalise(result: dict, prompt: str, model_used: str) -> dict:
    outcome = str(result.get("outcome", "cannot_be_determined")).lower()
    if outcome not in _VALID_OUTCOMES:
        outcome = "cannot_be_determined"

    # is_genuine_attempt defaults to True when absent (e.g. a cached response written
    # before this field existed, or a test double that omits it) — absence must not
    # silently reclassify existing/mocked rows as false positives.
    if result.get("is_genuine_attempt", True) is False:
        outcome = "not_a_replication"

    return {
        "outcome":            outcome,
        "outcome_phrase":     str(result.get("outcome_phrase",    "") or ""),
        "outcome_confidence": str(result.get("outcome_confidence", "low") or "low"),
        "out_quote_source":   str(result.get("out_quote_source",  "") or ""),
        "outcome_reasoning":  str(result.get("outcome_reasoning", "") or ""),
        "llm_model":          model_used,
        "llm_prompt":         prompt,
        "llm_response":       json.dumps(result, ensure_ascii=False),
    }


def _llm_outcome(doi_r: str, title_r: str, abstract_r: str, fulltext: str,
                 original_title: str = "", original_authors: str = "",
                 original_year: str = "") -> dict:
    """LLM-based outcome extraction.

    The primary pass reads the abstract. If it returns cannot_be_determined (or the
    abstract is empty) and parsed fulltext is available, a second, fulltext-based
    call is made and its result is used. Results are dual-cached (see
    shared/cache.py): under the legacy DOI key and a content key folding in the
    model, prompt version and abstract.
    """
    legacy_key  = f"outcome_{cache_key(doi_r)}"
    content_key = f"outcome_{cache_key(doi_r + '|' + GEMINI_HEAVY_MODEL + '|' + PROMPT_VERSION + '|' + abstract_r)}"
    cached = read_dual_cache(LLM_CACHE_DIR, legacy_key, content_key, mode=LLM_CACHE_READ)
    if cached is not None:
        cached.setdefault("outcome_reasoning", "")
        return cached

    abstract_snip = (abstract_r[:_ABSTRACT_CAP] + "…") if len(abstract_r) > _ABSTRACT_CAP else abstract_r

    original_block = ""
    if original_title:
        original_block = (
            f"This paper replicates: {original_authors} ({original_year}). {original_title}\n\n"
        )

    token_counter.set_stage("extract_outcome")

    _fallback = {"outcome": "cannot_be_determined", "outcome_phrase": "",
                 "outcome_confidence": "low", "out_quote_source": "",
                 "outcome_reasoning": "", "llm_model": ""}

    prompt = _abstract_prompt(title_r, abstract_snip, original_block)
    result, model_used = _call_outcome_llm(prompt, doi_r)
    if not result:
        log.warning("[%s] outcome LLM failed after all retries — marking cannot_be_determined", doi_r)
        return _fallback

    output = _normalise(result, prompt, model_used)

    # Escalation: the abstract could not settle it → read the parsed fulltext.
    if (OUTCOME_FULLTEXT_ESCALATION
            and fulltext
            and (output["outcome"] == "cannot_be_determined" or not abstract_r)):
        text_snip = (fulltext[:_FULLTEXT_CAP] + "…") if len(fulltext) > _FULLTEXT_CAP else fulltext
        esc_prompt = _fulltext_prompt(title_r, abstract_snip, text_snip, original_block)
        esc_result, esc_model = _call_outcome_llm(esc_prompt, doi_r)
        if esc_result:
            output = _normalise(esc_result, esc_prompt, esc_model)
            if not output["out_quote_source"]:
                output["out_quote_source"] = "fulltext"

    write_dual_cache(LLM_CACHE_DIR, legacy_key, content_key, output)
    return output


def predict_outcome_keyword(title_r: str, abstract_r: str) -> str:
    """Fast keyword-only outcome prediction for pre-filtering before extraction.

    Runs the same regex patterns as Pass 1 of extract_outcome but on title +
    abstract only — no LLM, no fulltext.  Used by --predicted-outcome to decide
    whether to process a row at all.

    Returns one of: failure | success | mixed | descriptive | cannot_be_determined
    """
    if title_r:
        hit = _keyword_scan(title_r, "title")
        if hit and hit["outcome_confidence"] == "high":
            return hit["outcome"]
    if abstract_r:
        hit = _keyword_scan(abstract_r, "abstract")
        if hit:
            return hit["outcome"]
    return "cannot_be_determined"


def extract_outcome(doi_r: str,
                    abstract_r: str,
                    fulltext: str = "",
                    title_r: str = "",
                    no_llm: bool = False,
                    original_title: str = "",
                    original_authors: str = "",
                    original_year: str = "") -> dict:
    """Extract replication outcome from available text.

    Returns a dict with keys: outcome, outcome_phrase, outcome_confidence,
    out_quote_source, outcome_reasoning (empty string for keyword-matched rows).
    """
    _kw_fallback = {"outcome_reasoning": ""}

    # Title scan — only act on high-confidence hits (avoid false triggers like "replication of X")
    if title_r:
        hit = _keyword_scan(title_r, "title")
        if hit and hit["outcome_confidence"] == "high":
            return {**hit, **_kw_fallback}

    # Abstract scan — accept any hit
    if abstract_r:
        hit = _keyword_scan(abstract_r, "abstract")
        if hit:
            return {**hit, **_kw_fallback}

    # Fulltext is deliberately NOT keyword-scanned here: an introduction's
    # background prose about OTHER studies' outcomes ("X failed to replicate in
    # prior work") misfires the patterns. Fulltext is used only via the LLM
    # escalation inside _llm_outcome.

    if no_llm:
        return {"outcome": "cannot_be_determined", "outcome_phrase": "",
                "outcome_confidence": "low", "out_quote_source": "",
                "outcome_reasoning": ""}

    # LLM pass (abstract-based, with fulltext escalation) for anything unresolved.
    return _llm_outcome(doi_r, title_r, abstract_r, fulltext,
                        original_title=original_title,
                        original_authors=original_authors,
                        original_year=original_year)
