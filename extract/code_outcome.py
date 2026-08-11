"""
code_outcome.py — the STANDALONE outcome coder for Stage 3.

Most rows are coded where their target is chosen: `resolve_targets_and_outcomes()`
asks both questions of one reading, at every LLM rung of the resolution ladder. This
module codes the rows that call never saw — a deterministic rule resolved the link, so
no model ever read the paper to find it, and the original reaching this call is an
assertion an earlier stage made. It is given as evidence to CHECK, with the link
evidence that produced it, and the model answers `target_check` on it.

The call is a single one over whatever text the row has: the abstract, and — when a
document was acquired — the paper's introduction and its DISCUSSION AND CONCLUSION,
each named. That order is FLoRA's rule: "We rely on what replication authors say in the
abstract, or if not stated there, what is written in the report (discussion and
conclusion sections)." The closing text is selected by `pdf_parsing.outcome_text()`,
not taken from the front of the parse: an introduction routinely discusses OTHER
studies' failures ("X failed to replicate in prior work").

There is no full-text escalation here any more. It could not fire: escalating needed a
parsed document, and a row resolved from the abstract never acquired one. Reading on
for an unsettled verdict is the ladder's job now — see OUTCOME_DESCENT in
extract/link_original.py.

The keyword scan is the --no-llm fallback, not a pre-filter. Every outcome the
pipeline records with an LLM available comes from the LLM, which also applies the
record_type_check veto that a keyword match cannot. The one other consumer of the
keyword patterns is predict_outcome_keyword(), the --predicted-outcome sampling
filter.

A reproduction is coded on two independent axes (computation and robustness), each
with its own quote and quote source; the shared `outcome` column carries the two
verdicts joined, so one column reads the same way for both record types.

OUTCOME_MODEL codes every row, on both passes. A call it cannot answer yields
outcome = "api_error", never a verdict and never another model's verdict.

Public API:
    extract_outcome(doi_r, abstract_r, fulltext, title_r) → dict
"""
import json
import re
import time
from typing import Optional

from shared.config import LLM_CACHE_DIR, OUTCOME_EFFORT, OUTCOME_MODEL, log
from shared import token_counter
from shared.cache import content_key, read_cache_migrating, write_cache
from shared.llm_client import cache_model_id, call_model
from shared.prompts import (
    build_outcome_prompt, build_repro_outcome_prompt, prompt_version,
)
from shared.schema import (EMPTY_OUTCOME_AXES, canonical_outcome,
                           normalise_outcome_block)
from shared.token_usage import TokenBudgetExhausted

# Truncation caps (chars) for the passages this call sends.
_ABSTRACT_CAP = 3000
_FULLTEXT_CAP = 8000

# The two versions `build_outcome_prompt` hashed to either side of the FLoRA
# outcome-label rename — the declared equivalence read at the cache key below.
_OUTCOME_PRE_RENAME_VERSION = "88efc83ad293"
_OUTCOME_RENAMED_VERSION    = "e9ef589b44d1"
_INTRO_CAP    = 2000

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

# The empty axis block a replication row carries, and the block a "neither" verdict
# clears a reproduction row down to.
_EMPTY_AXES = EMPTY_OUTCOME_AXES


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
        return {"outcome": "failed",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "high", "out_quote_source": source}
    m = _MIXED.search(text)
    if m:
        return {"outcome": "mixed",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "medium", "out_quote_source": source}
    m = _SUCCESS.search(text)
    if m:
        return {"outcome": "successful",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "high", "out_quote_source": source}
    m = _FAILURE_WEAK.search(text)
    if m:
        return {"outcome": "failed",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "medium", "out_quote_source": source}
    m = _DESCRIPTIVE.search(text)
    if m:
        return {"outcome": "descriptive only",
                "outcome_phrase": _expand_to_sentences(text, m.start(), m.end()),
                "outcome_confidence": "medium", "out_quote_source": source}
    return None


def _call_outcome_llm(prompt: str, doi_r: str) -> tuple[Optional[dict], str]:
    """Call OUTCOME_MODEL with up to 3 retries and exponential backoff.

    call_model reports provider failure by returning None rather than by raising, so
    the backoff is applied on the None path; keeping it in the except arm alone
    meant three outer retries — nine provider attempts — fired back to back with no
    delay at all, which is exactly how a rate-limited provider stays rate-limited.

    One model codes every outcome. When it is down these retries are the whole of the
    recovery: the row ends at outcome=api_error and a later run codes it, rather than
    entering the corpus graded by a model no evaluation covered.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # OUTCOME_EFFORT is stated here rather than inherited from whichever
            # constant happens to name the same model id — and it pins medium, which
            # is what the pre-refactor code actually sent and therefore what coded
            # every outcome on disk.
            result, _provider, err = call_model(prompt, OUTCOME_MODEL,
                                                reasoning_effort=OUTCOME_EFFORT)
            if result:
                return result, OUTCOME_MODEL
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
               record_type: str = "replication", has_text: bool = False) -> dict:
    """The LLM response as the row shape, plus the plumbing the row records.

    The verdict itself is read by shared.schema.normalise_outcome_block, which the
    combined target+outcome call uses too — so a verdict means the same thing whichever
    question asked for it. *has_text* says whether the model was given any of the
    paper's own body: the two check fields are only asked then, and their absence
    otherwise is not a verdict.
    """
    return {
        **normalise_outcome_block(result, record_type, has_text),
        "llm_model":    model_used,
        "llm_prompt":   prompt,
        "llm_response": json.dumps(result, ensure_ascii=False),
    }


def _outcome_result(doi_r: str, title_r: str, abstract_r: str, fulltext: str,
                    original_title: str = "", original_authors: str = "",
                    original_year: str = "", record_type: str = "replication",
                    recoded: bool = False, intro_text: str = "",
                    original_evidence: str = "",
                    fulltext_provenance: str = "") -> tuple[dict, bool]:
    """LLM-based outcome extraction, with whether the result may be cached.

    One result is NOT cacheable: api_error after provider exhaustion. It is transient,
    and a cached one is a definitive miss the pipeline never retries. A recode inherits
    the cacheability of the call it delegated to — the outer call cannot tell a
    delegated api_error from a delegated verdict by looking at the dict, and writing
    one under the original vocabulary's key checkpointed an outage as an answer.

    One call, over every passage the row has. It answers record_type_check and
    target_check whenever it was given any of the paper's own text: "neither" and
    "no_original" veto the row, and naming the other vocabulary re-codes it once under
    the other prompt — one hop, never a loop, so a call made to fix a mis-typed row is
    not itself re-typed.

    One cache entry, keyed on everything the answer depends on: the model, the prompt
    version, the record type (a reproduction gets a different prompt and a different
    outcome vocabulary, so it must not read back a replication-coded entry), and every
    input sent — title, abstract, the original-study block with the evidence that
    produced it, and both passages of the paper's own text with the provenance the
    closing one is labelled by.
    """
    abstract_snip = (abstract_r[:_ABSTRACT_CAP] + "…") if len(abstract_r) > _ABSTRACT_CAP else abstract_r
    text_snip     = (fulltext[:_FULLTEXT_CAP] + "…") if len(fulltext) > _FULLTEXT_CAP else fulltext
    intro_snip    = (intro_text or "")[:_INTRO_CAP]

    is_repro = str(record_type or "").strip().lower() == "reproduction"
    build = build_repro_outcome_prompt if is_repro else build_outcome_prompt
    version = prompt_version(
        "build_repro_outcome_prompt" if is_repro else "build_outcome_prompt")
    parts = (record_type, title_r, abstract_snip,
             original_authors, original_year, original_title,
             original_evidence, intro_snip, fulltext_provenance, text_snip)
    model_id = cache_model_id(OUTCOME_MODEL, OUTCOME_EFFORT)
    key = content_key("outcome", doi_r, model_id, version, *parts)
    # The FLoRA-label rename (OUTCOME_LABELS in shared/schema.py) changed how six
    # categories are spelled and nothing else, so the answers filed under the previous
    # version of this prompt are the answers this call would get today. Unlike the
    # combined target+outcome key, this one does not hash the rendered prompt, so the
    # frozen version is the whole equivalence. Pinned to the version the rename
    # produced: a later edit to the prompt matches neither literal and pays.
    legacy_keys = ([content_key("outcome", doi_r, model_id,
                                _OUTCOME_PRE_RENAME_VERSION, *parts)]
                   if version == _OUTCOME_RENAMED_VERSION else [])
    cached = read_cache_migrating(LLM_CACHE_DIR, key, legacy_keys,
                                  {"prompt_version": version,
                                   "migration": "flora_outcome_labels"})
    if cached is not None:
        cached.setdefault("outcome_reasoning", "")
        cached.setdefault("target_check", "")
        # A hit is a stored ROW, already normalised when it was written, so nothing
        # re-runs the vocabulary check on it: an entry bought before the FLoRA rename
        # says `failure` where the pipeline now says `failed`. Translated at the door.
        cached["outcome"] = canonical_outcome(cached.get("outcome"))
        return cached, True

    token_counter.set_stage("extract_outcome")

    # A failed call is an api_error, not a verdict: cannot_be_determined
    # is a judgement the model made about the paper, and recording one for the other
    # made a quota outage indistinguishable from a genuinely unclassifiable abstract.
    # Not cached — a re-run must be able to code the row.
    _api_error = {"outcome": "api_error", "outcome_phrase": "",
                  "outcome_confidence": "low", "out_quote_source": "",
                  "outcome_reasoning": "", "llm_model": "", **_EMPTY_AXES}

    prompt = build(title_r, abstract_snip, original_authors, original_year,
                   original_title, text_snip=text_snip, intro_snip=intro_snip,
                   original_evidence=original_evidence,
                   text_provenance=fulltext_provenance)
    result, model_used = _call_outcome_llm(prompt, doi_r)
    if not result:
        log.warning("[%s] outcome LLM failed after all retries — marking api_error", doi_r)
        return _api_error, False

    has_text = bool(text_snip or intro_snip)
    output = _normalise(result, prompt, model_used, record_type, has_text=has_text)
    cacheable = True

    other = "reproduction" if not is_repro else "replication"
    if has_text and not recoded and output["record_type_check"] == other:
        log.info("[%s] the text says this is a %s, not a %s — re-coding once",
                 doi_r, other, record_type)
        output, cacheable = _outcome_result(
            doi_r, title_r, abstract_r, fulltext,
            original_title=original_title, original_authors=original_authors,
            original_year=original_year, record_type=other, recoded=True,
            intro_text=intro_text, original_evidence=original_evidence,
            fulltext_provenance=fulltext_provenance)
        output = {**output, "record_type": other}

    if cacheable:
        write_cache(LLM_CACHE_DIR, key, output)
    return output, cacheable


def _llm_outcome(doi_r: str, title_r: str, abstract_r: str, fulltext: str,
                 original_title: str = "", original_authors: str = "",
                 original_year: str = "", record_type: str = "replication",
                 intro_text: str = "", original_evidence: str = "",
                 fulltext_provenance: str = "") -> dict:
    """The outcome row for *doi_r* — see _outcome_result, whose cacheability flag the
    pipeline has no use for."""
    output, _ = _outcome_result(doi_r, title_r, abstract_r, fulltext,
                                original_title=original_title,
                                original_authors=original_authors,
                                original_year=original_year,
                                record_type=record_type,
                                intro_text=intro_text,
                                original_evidence=original_evidence,
                                fulltext_provenance=fulltext_provenance)
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
                    record_type: str = "replication",
                    intro_text: str = "",
                    original_evidence: str = "",
                    fulltext_provenance: str = "") -> dict:
    """Extract the outcome from available text.

    *fulltext* is the paper's closing sections, *intro_text* its opening ones, and
    *fulltext_provenance* the label the closing block is introduced by
    (shared.prompts.PROVENANCE_LABEL). *original_evidence* is what linked this paper to
    the named original — the model is asked to check the link against the text, and
    answers target_check.

    record_type selects the vocabulary: "reproduction" uses the computation/robustness
    axes, anything else the replication enum. It can also be corrected here — when the
    full-text pass reports the other vocabulary the row is re-coded once and the
    returned dict carries a "record_type" key for the caller to write to `type`.

    Returns a dict with keys: outcome, outcome_phrase, outcome_confidence,
    out_quote_source, outcome_reasoning (empty string for keyword-matched rows),
    record_type_check, target_check and the six reproduction axis fields (empty on a
    replication row).
    """
    _kw_fallback = {"outcome_reasoning": "", "llm_model": "keyword", **_EMPTY_AXES}

    # The keyword patterns below are replication-specific ("failed to replicate",
    # "successfully replicated", ...). Running them on a reproduction would code it
    # in the wrong vocabulary, so reproductions go straight to the LLM.
    if str(record_type or "").strip().lower() == "reproduction":
        if no_llm:
            return {"outcome": "cannot_be_determined", "outcome_phrase": "",
                    "outcome_confidence": "low", "out_quote_source": "",
                    "outcome_reasoning": "", "llm_model": "", **_EMPTY_AXES}
        return _llm_outcome(doi_r, title_r, abstract_r, fulltext,
                            original_title=original_title,
                            original_authors=original_authors,
                            original_year=original_year,
                            record_type="reproduction",
                            intro_text=intro_text,
                            original_evidence=original_evidence,
                            fulltext_provenance=fulltext_provenance)

    # Keyword fast-path is the NO-LLM fallback only (#70). When the LLM is available
    # every "is this a genuine replication?" decision must be seen by it: on the
    # full-text pass _llm_outcome judges record_type_check (vetoing papers that do not
    # check the named original to not_a_replication) as well as coding the outcome. A
    # bare keyword hit like "failed to replicate" can fire on background prose or an
    # AI-generated abstract, so short-circuiting on it would let obvious
    # non-replications through as coded replications.
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
                "outcome_reasoning": "", "llm_model": "keyword", **_EMPTY_AXES}

    # LLM pass (abstract-based, with fulltext escalation) — codes the outcome AND
    # applies the record_type_check veto.
    return _llm_outcome(doi_r, title_r, abstract_r, fulltext,
                        original_title=original_title,
                        original_authors=original_authors,
                        original_year=original_year,
                        record_type=record_type,
                        intro_text=intro_text,
                        original_evidence=original_evidence,
                        fulltext_provenance=fulltext_provenance)
