"""
code_outcome.py — LLM outcome coding for Stage 3.

The outcome is decided by an LLM reading the ABSTRACT, and on escalation — when the
abstract call returns cannot_be_determined, or there is no abstract — by a second call
reading the paper's DISCUSSION AND CONCLUSION. That order is FLoRA's rule: "We rely on
what replication authors say in the abstract, or if not stated there, what is written
in the report (discussion and conclusion sections)."

The escalation text is selected by `pdf_parsing.outcome_text()`, not taken from the
front of the parse: an introduction routinely discusses OTHER studies' failures ("X
failed to replicate in prior work"), and the head of a paper truncated at
_FULLTEXT_CAP is mostly introduction and methods.

The keyword scan is the --no-llm fallback, not a pre-filter. Every outcome the
pipeline records with an LLM available comes from the LLM, which also applies the
is_genuine_attempt veto that a keyword match cannot. The one other consumer of the
keyword patterns is predict_outcome_keyword(), the --predicted-outcome sampling
filter.

Exhausting every provider yields outcome = "api_error", never a verdict.

Public API:
    extract_outcome(doi_r, abstract_r, fulltext, title_r) → dict
"""
import json
import re
import time
from typing import Optional

from shared.config import (
    GEMINI_HEAVY_MODEL, LLM_CACHE_DIR,
    OUTCOME_FULLTEXT_ESCALATION, log,
)
from shared import token_counter
from shared.cache import content_key, read_cache, write_cache
from shared.llm_client import call_llm, ladder_fingerprint
from shared.prompts import (
    build_outcome_abstract_prompt, build_outcome_fulltext_prompt,
    build_repro_abstract_prompt, build_repro_fulltext_prompt, prompt_versions,
)
from shared.schema import OUTCOME_CATEGORIES, outcome_categories_for
from shared.token_usage import TokenBudgetExhausted

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
                          n_context: int = 2) -> str:
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
# Explicit failure phrasings — each one names the replication as the thing that
# failed, so they are checked before success ("failed to replicate" would otherwise
# hit the bare-"replicated" success catch-all).

_FAILURE = re.compile(
    r"\b("
    r"failed to replicate|replication failed|could not replicate"
    r"|did not replicate|not replicated|no support for the original"
    r"|inconsistent with (?:the )?(?:original|prior)"
    r"|results did not (?:hold|replicate)|null result"
    r"|failed to reproduce|did not reproduce"
    r")\b",
    re.IGNORECASE,
)

# "No evidence" and "no significant difference" describe a statistical test, not a
# verdict on the replication — and in a SUCCESSFUL replication they routinely
# describe the comparison against the original ("no significant difference between
# our estimate and the original"). They are therefore checked last, after success
# and mixed, and only at medium confidence.
_FAILURE_WEAK = re.compile(
    r"\bno (?:evidence|significant (?:effect|difference))\b",
    re.IGNORECASE,
)

# The explicit success phrasings, without the bare-"replicated" catch-all. Used
# both as the success pattern's core and as the veto on a failure match: a
# sentence carrying one of these is reporting a successful replication whatever
# else is in it. The catch-all cannot serve as a veto, because "not replicated"
# contains "replicated".
_SUCCESS_EXPLICIT = re.compile(
    r"\b("
    r"successfully replicated|replication succeeded|results (?:were )?replicated"
    r"|confirmed the (?:original|findings?|results?|effect)"
    r"|supported the original"
    r"|consistent with (?:the )?(?:original|prior)"
    r"|replication was successful|effect was reproduced"
    r"|was (?:successfully )?replicated|replicated successfully"
    r")\b",
    re.IGNORECASE,
)

_SUCCESS = re.compile(
    _SUCCESS_EXPLICIT.pattern
    + r"|(?<!\w)replicated(?!\w)",   # bare "replicated" as low-priority catch-all
    re.IGNORECASE,
)

# Mixed requires the AUTHORS to frame their own evidence as partly supporting and
# partly not — matching the LLM rules in shared/prompts.py. Reduced effect size is
# deliberately not a trigger: a smaller but supported effect is a success, and the
# old "smaller effect"/"reduced magnitude" alternatives coded those as mixed while
# the LLM path called them success.
_MIXED = re.compile(
    r"\b("
    r"partially replicated|mixed results?|partial replication"
    r"|some but not all|some (?:but not all|support)"
    r"|nuanced|qualified support"
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


def _failure_match(text: str) -> "re.Match | None":
    """First explicit failure match whose own sentence does not also report success.

    "The effect did not replicate in Study 1, but was successfully replicated in
    Study 2" is not a failure; taking the first failure match regardless of what
    else the sentence says coded a good many successes as failures.
    """
    for m in _FAILURE.finditer(text):
        sentence = _expand_to_sentences(text, m.start(), m.end(), n_context=0)
        if not _SUCCESS_EXPLICIT.search(sentence):
            return m
    return None


def _keyword_scan(text: str, source: str) -> Optional[dict]:
    """Return a result dict if a keyword pattern matches, else None.

    Check order: explicit failure → mixed → success → weak failure → descriptive.
    Mixed is checked before success so that "partially replicated" resolves to
    mixed rather than triggering the broad bare-"replicated" success pattern, and
    the weak failure patterns are checked after success because they describe a
    statistical test that a successful replication reports too.
    """
    m = _failure_match(text)
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
    m = _FAILURE_WEAK.search(text)
    if m:
        return {"outcome": "failure",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "medium", "out_quote_source": source}
    m = _DESCRIPTIVE.search(text)
    if m:
        return {"outcome": "descriptive",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "medium", "out_quote_source": source}
    return None


def _call_outcome_llm(prompt: str, doi_r: str) -> tuple[Optional[dict], str]:
    """Call the outcome LLM with up to 3 retries and exponential backoff.

    call_llm reports provider failure by returning None rather than by raising, so
    the backoff is applied on the None path; keeping it in the except arm alone
    meant three outer retries — nine provider attempts — fired back to back with no
    delay at all, which is exactly how a rate-limited provider stays rate-limited.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            result, model_used, err = call_llm(prompt, gemini_model=GEMINI_HEAVY_MODEL,
                                                prefer_openai=True)
            if result:
                return result, model_used
        except TokenBudgetExhausted:
            raise   # retrying a call the budget refuses only delays the stop
        except Exception as e:
            err = str(e)
        wait_time = 2 ** attempt  # 1s, 2s, 4s
        if attempt < max_retries - 1:
            log.warning("[%s] outcome LLM failed (attempt %d/%d), retrying in %ds: %s",
                        doi_r, attempt + 1, max_retries, wait_time, err)
            time.sleep(wait_time)
        else:
            log.warning("[%s] outcome LLM failed after %d retries: %s", doi_r, max_retries, err)
    return None, ""


def _normalise(result: dict, prompt: str, model_used: str,
               record_type: str = "replication") -> dict:
    outcome = str(result.get("outcome", "cannot_be_determined")).lower()
    # Reproductions use the 3x3 computation/robustness grid, replications the
    # success/failure/... enum. Validating against the wrong one would silently
    # coerce every reproduction verdict to cannot_be_determined.
    if outcome not in outcome_categories_for(record_type):
        outcome = "cannot_be_determined"

    # is_genuine_attempt defaults to True when absent (e.g. a cached response written
    # before this field existed, or a test double that omits it) — absence must not
    # silently reclassify existing/mocked rows as false positives.
    if result.get("is_genuine_attempt", True) is False:
        outcome = "not_a_replication"

    return {
        "outcome":            outcome,
        "outcome_phrase":     str(result.get("outcome_phrase",    "") or ""),
        "outcome_confidence": str(result.get("confidence", "low") or "low"),
        "out_quote_source":   str(result.get("out_quote_source",  "") or ""),
        "outcome_reasoning":  str(result.get("outcome_reasoning", "") or ""),
        "llm_model":          model_used,
        "llm_prompt":         prompt,
        "llm_response":       json.dumps(result, ensure_ascii=False),
    }


def _llm_outcome(doi_r: str, title_r: str, abstract_r: str, fulltext: str,
                 original_title: str = "", original_authors: str = "",
                 original_year: str = "", record_type: str = "replication") -> dict:
    """LLM-based outcome extraction.

    The primary pass reads the abstract. If it returns cannot_be_determined (or the
    abstract is empty) and parsed fulltext is available, a second, fulltext-based
    call is made and its result is used.

    One cache entry, keyed on everything the answer depends on: the model, the
    versions of both prompts that could have produced it, the record type (a
    reproduction gets a different prompt and a different outcome vocabulary, so it
    must not read back a replication-coded entry), and every input sent — title,
    abstract, the original-study block and the fulltext the escalation would read.
    """
    abstract_snip = (abstract_r[:_ABSTRACT_CAP] + "…") if len(abstract_r) > _ABSTRACT_CAP else abstract_r
    text_snip     = (fulltext[:_FULLTEXT_CAP] + "…") if len(fulltext) > _FULLTEXT_CAP else fulltext

    original_block = ""
    if original_title:
        original_block = (
            f"This paper replicates: {original_authors} ({original_year}). {original_title}\n\n"
        )

    is_repro = str(record_type or "").strip().lower() == "reproduction"
    versions = (prompt_versions("build_repro_abstract_prompt", "build_repro_fulltext_prompt")
                if is_repro else
                prompt_versions("build_outcome_abstract_prompt", "build_outcome_fulltext_prompt"))
    key = content_key("outcome", doi_r, ladder_fingerprint(GEMINI_HEAVY_MODEL),
                      versions, record_type,
                      title_r, abstract_snip, original_block, text_snip)
    cached = read_cache(LLM_CACHE_DIR, key)
    if cached is not None:
        cached.setdefault("outcome_reasoning", "")
        return cached

    token_counter.set_stage("extract_outcome")

    # Exhausting every provider is an api_error, not a verdict: cannot_be_determined
    # is a judgement the model made about the paper, and recording one for the other
    # made a quota outage indistinguishable from a genuinely unclassifiable abstract.
    # Not cached — a re-run must be able to code the row.
    _api_error = {"outcome": "api_error", "outcome_phrase": "",
                  "outcome_confidence": "low", "out_quote_source": "",
                  "outcome_reasoning": "", "llm_model": ""}

    prompt = (build_repro_abstract_prompt(title_r, abstract_snip, original_block) if is_repro
              else build_outcome_abstract_prompt(title_r, abstract_snip, original_block))
    result, model_used = _call_outcome_llm(prompt, doi_r)
    if not result:
        log.warning("[%s] outcome LLM failed after all retries — marking api_error", doi_r)
        return _api_error

    output = _normalise(result, prompt, model_used, record_type)

    # Escalation: the abstract could not settle it → read the parsed fulltext.
    if (OUTCOME_FULLTEXT_ESCALATION
            and fulltext
            and (output["outcome"] == "cannot_be_determined" or not abstract_r)):
        esc_prompt = (build_repro_fulltext_prompt(title_r, abstract_snip, text_snip, original_block)
                      if is_repro else
                      build_outcome_fulltext_prompt(title_r, abstract_snip, text_snip, original_block))
        esc_result, esc_model = _call_outcome_llm(esc_prompt, doi_r)
        if not esc_result:
            # Caching the abstract's cannot_be_determined here would retire the
            # escalation for good; the fulltext call must stay retryable.
            log.warning("[%s] outcome fulltext escalation failed — returning the "
                        "abstract verdict uncached so a re-run retries it", doi_r)
            return output
        output = _normalise(esc_result, esc_prompt, esc_model, record_type)
        if not output["out_quote_source"]:
            output["out_quote_source"] = "fulltext"

    write_cache(LLM_CACHE_DIR, key, output)
    return output


def predict_outcome_keyword(title_r: str, abstract_r: str) -> str:
    """Fast keyword-only outcome prediction for pre-filtering before extraction.

    Runs the same regex patterns as the --no-llm path of extract_outcome but on
    title + abstract only — no LLM, no fulltext.  Used by --predicted-outcome to
    decide whether to process a row at all.

    The selection is lexical, so a sample drawn this way is a sample of papers whose
    abstracts USE the phrasing, not of papers with the outcome: treat it as a way to
    find rows to look at, never as an estimate of anything.

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
                    original_year: str = "",
                    record_type: str = "replication") -> dict:
    """Extract the outcome from available text.

    record_type selects the vocabulary: "reproduction" uses the 3x3
    computation/robustness grid, anything else the replication enum.

    Returns a dict with keys: outcome, outcome_phrase, outcome_confidence,
    out_quote_source, outcome_reasoning (empty string for keyword-matched rows).
    """
    _kw_fallback = {"outcome_reasoning": "", "llm_model": "keyword"}

    # The keyword patterns below are replication-specific ("failed to replicate",
    # "successfully replicated", ...). Running them on a reproduction would code it
    # in the wrong vocabulary, so reproductions go straight to the LLM.
    if str(record_type or "").strip().lower() == "reproduction":
        if no_llm:
            return {"outcome": "cannot_be_determined", "outcome_phrase": "",
                    "outcome_confidence": "low", "out_quote_source": "",
                    "outcome_reasoning": "", "llm_model": ""}
        return _llm_outcome(doi_r, title_r, abstract_r, fulltext,
                            original_title=original_title,
                            original_authors=original_authors,
                            original_year=original_year,
                            record_type="reproduction")

    # Keyword fast-path is the NO-LLM fallback only (#70). When the LLM is available
    # every "is this a genuine replication?" decision must be seen by it: _llm_outcome
    # judges is_genuine_attempt (vetoing obvious non-replications to not_a_replication)
    # as well as coding the outcome. A bare keyword hit like "failed to replicate" can
    # fire on background prose or an AI-generated abstract, so short-circuiting on it
    # would let obvious non-replications through as coded replications.
    if no_llm:
        # Title scan — only high-confidence hits (avoid "replication of X" false triggers).
        if title_r:
            hit = _keyword_scan(title_r, "title")
            if hit and hit["outcome_confidence"] == "high":
                return {**hit, **_kw_fallback}
        # Abstract scan — accept any hit.
        if abstract_r:
            hit = _keyword_scan(abstract_r, "abstract")
            if hit:
                return {**hit, **_kw_fallback}
        # Fulltext is deliberately NOT keyword-scanned: an introduction's background
        # prose about OTHER studies' outcomes misfires the patterns.
        return {"outcome": "cannot_be_determined", "outcome_phrase": "",
                "outcome_confidence": "low", "out_quote_source": "",
                "outcome_reasoning": "", "llm_model": "keyword"}

    # LLM pass (abstract-based, with fulltext escalation) — codes the outcome AND
    # applies the is_genuine_attempt veto.
    return _llm_outcome(doi_r, title_r, abstract_r, fulltext,
                        original_title=original_title,
                        original_authors=original_authors,
                        original_year=original_year,
                        record_type=record_type)
