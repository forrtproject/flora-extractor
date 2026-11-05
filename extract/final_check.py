"""The final AI check: a blind recode of every changed work before it can ship.

Stage 3 writes verdicts and `extract/export.py` renders them, but validators should
only receive rows a second, independent reading has agreed with. This is that reading.
`python -m extract.final_check --release <id>` renders what the export would ship,
finds the works whose checked fields differ from the file committed at HEAD, codes each
one blind and writes `data/final_check.csv`. The export reads that file and nothing
else from this module, so it stays a pure render.

    .venv/bin/python -m extract.final_check --release <id>                # the worklist
    .venv/bin/python -m extract.final_check --release <id> --run          # code it
    .venv/bin/python -m extract.final_check --release <id> --run --model gpt-6.1-sol
    .venv/bin/python -m extract.final_check --release <id> --run --limit 5 \\
        --mode validation --out data/extracted-test.csv                   # the sandbox

**The review.** A work is `released` when its originals pair off one-to-one and every
pair agrees on type and outcome with a confident blind coder (`compare_work()`).
Anything else is `review`: whoever runs the pipeline reads the two codings side by
side in the CSV and fills `decision` with `ship`, `hold` or `reject`. A decision is
bound to the work's fingerprint (`work_fingerprint()` in `extract/export.py`), so a
later change to the work voids it and the work is checked again.

**Blind.** The coder sees the title, abstract, parsed document and reference list, and
never the pipeline's answer. It runs headless on a subscription (`claude -p` or
`codex exec`), with no tools and no user configuration. A coding during which any tool
ran is refused, and a failed call raises and is not cached. Neither route falls back
to the other. A work whose document is not in this machine's parse cache is skipped
rather than coded from its abstract alone: run this where the parse cache lives.

The codebook is the pipeline's own: the target rules are cut out of `_TARGET_TASK`
and the outcome rules are `_OUTCOME_RULES` / `_REPRO_AXIS_RULES` /
`_MULTI_TARGET_SCOPE`, imported from `shared/prompts.py`. Only the wrapper differs:
there is no keyed candidate list, so the original is named as authors, year, title and
DOI, and the record type is asked first instead of being assumed.

The evaluation that set the release rule is `analysis/final_ai_check_2026-10/`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from extract.export import (DEFAULT_OUT, FINAL_CHECK_NAME, _write_final_check,
                            admitted_work_ids, changed_works, previous_shipped,
                            read_final_check, render, work_fingerprint)
from filter.engine.claims import ClaimsClient, ClaimsNotConfigured
from filter.engine.export import SPEC_DIR, StaleBundleError
from filter.engine.store import StoreUnavailable
from shared.cache import cache_exists, read_json
from shared.config import (CACHE_DIR, FINAL_CHECK_EFFORT, FINAL_CHECK_MODEL,
                           FINAL_CHECK_MODELS, OA_CACHE_DIR)
from shared.disambiguation import jaccard_similarity, token_coverage
from shared.pdf_parsing import read_parse_cache, score_parse_result
from shared.prompts import (EVIDENCE_POLICY, _MULTI_TARGET_SCOPE, _OUTCOME_RULES,
                            _REPRO_AXIS_RULES, _TARGET_TASK)
from shared.row_key import primary_key
from shared.schema import OUTCOME_LABELS, set_aside_dir
from shared.utils import cache_key, clean_doi

ROOT = Path(__file__).resolve().parent.parent
CACHE = CACHE_DIR / "final_check"
# Opus reads 1M tokens; 150k characters (~40k tokens) holds a whole article with its
# reference list, which is where the original's title and DOI are read from.
FULLTEXT_CHARS = 150_000
# The most of that cap a reference list may take when the document is over it.
REFERENCE_CHARS = 50_000


def _between(text: str, start: str, end: str) -> str:
    """The pipeline's own wording between two headings; fails loudly if they move."""
    i, j = text.index(start), text.index(end)
    return text[i:j].strip()


# What counts as a target and how to count them, verbatim from the pipeline prompt.
# The rest of _TARGET_TASK is about @keys in candidate lists this check never shows.
_TARGET_RULES = (_between(_TARGET_TASK, "What counts as a target:", "How to count:")
                 + "\n\n" + _between(_TARGET_TASK, "How to count:", "Two separate judgments"))

# The sibling checklist, verbatim. The pipeline wraps it in its @key / match_certain
# vocabulary, which this check does not use, so only the checklist itself is cut out.
_SIBLINGS = re.sub(r"\s+", " ", _between(_TARGET_TASK, "rule out:", "When the evidence only"))

_TASK = """You are checking a record for a database of replication and reproduction studies.
Read the paper below and code it from scratch.

STEP 1 — RECORD TYPE

"record_type": "replication" if the paper collected new data or used a different sample
to re-test a previously published finding; "reproduction" if it re-analysed the
original study's own data (or code); "not_a_replication" if it re-tests no specific
earlier published finding at all (an original study, a review, a meta-analysis, a
method paper, a commentary); "unclear" if the text does not let you tell. A paper that
reports "replicating" its own new study within the same paper is not a replication of
a PUBLISHED finding.

"study_status": "completed", or "prospective" when the record IS a plan (a
preregistration, protocol, analysis plan or Stage 1 registered report) that reports no
result. A finished study whose result you cannot see is still "completed".

STEP 2 — THE ORIGINAL STUDY OR STUDIES (skip when not_a_replication)

{target_rules}

For each original, find its entry in the paper's reference list and copy the citation
from there: "authors" as listed (surname first), "year", and the full "title". Where the
paper cites by number, follow the number to the reference. Give the "doi" ONLY if it is
printed in the supplied text — never from memory. Leave a field "" only when the text
truly does not give it; a year printed beside an in-text citation is still the year.

Which entry is the original: where the paper cites it, follow the citation. Where it
only DESCRIBES the original ("earlier research found that X", "our previous study"),
name the one entry in the reference list whose title or content states that finding,
when exactly one entry fits; leave the citation fields "" only when two entries fit
equally well or none does. Before naming any entry, {siblings}

"identified_by": "citation" when the paper cites the entry you named, "description" when
you named it because it is the one entry that fits the paper's description of the
finding, "" when you named none.

STEP 3 — OUTCOME, per original

For a replication, code "outcome" in this vocabulary:
{replication_values}

{outcome_rules}

For a reproduction, code the two axes instead ("outcome" is then ""):

{repro_rules}

{multi_scope}

"outcome_confident": true or false — whether you would stake the verdict on the
evidence as written. It is about the OUTCOME only: how the original was identified is
recorded in "identified_by", and must not lower this.

QUOTES

For each verdict, copy the verbatim passage that proves it: 1-4 complete consecutive
sentences, word for word from the text below, the shortest passage that makes the
verdict self-contained. Name the section it came from: "title", "abstract",
"introduction", "methods", "results", "discussion" or "fulltext" (in the document but
section unclear). Use "" when no passage supports the verdict.

Respond with the JSON object the schema asks for."""

TASK = _TASK.format(
    target_rules=_TARGET_RULES,
    siblings=_SIBLINGS,
    replication_values=", ".join(f'"{v}"' for v in OUTCOME_LABELS.values()),
    outcome_rules=_OUTCOME_RULES,
    repro_rules=_REPRO_AXIS_RULES,
    multi_scope=_MULTI_TARGET_SCOPE,
)

_S = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "record_type": {"enum": ["replication", "reproduction", "not_a_replication",
                                 "unclear"]},
        "record_type_quote": _S,
        "study_status": {"enum": ["completed", "prospective"]},
        "targets": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "authors": _S, "year": _S, "title": _S, "doi": _S,
                "identified_by": {"enum": ["citation", "description", ""]},
                "study_numbers": _S, "replication_study_numbers": _S,
                "evidence_quote": _S,
                "outcome": {"enum": [*OUTCOME_LABELS.values(), ""]},
                "outcome_quote": _S, "outcome_quote_section": _S,
                "outcome_computation": {"enum": [
                    "computationally reproducible", "computational issues",
                    "technical failure", "not checked", "cannot_be_determined", ""]},
                "outcome_computational_quote": _S,
                "outcome_robustness": {"enum": [
                    "robust", "robustness challenges", "not checked",
                    "cannot_be_determined", ""]},
                "outcome_robustness_quote": _S,
                "outcome_confident": {"type": "boolean"},
                "outcome_reasoning": _S,
            },
            "required": ["authors", "year", "title", "doi", "identified_by",
                         "evidence_quote", "outcome",
                         "outcome_quote", "outcome_quote_section", "outcome_confident"],
        }},
        "reasoning": _S,
    },
    "required": ["record_type", "study_status", "targets", "reasoning"],
}

PROMPT_VERSION = hashlib.sha256(
    (TASK + json.dumps(SCHEMA, sort_keys=True)).encode()).hexdigest()[:12]


# ── Evidence ─────────────────────────────────────────────────────────────────

def document_text(row: dict) -> tuple[str, str, dict[str, str]]:
    """(text sent, parse method, every parse's raw text) from the pipeline's parse cache.

    Keyed the way `extract/run_extract.py:_cache_id` keys it: the DOI, else the row's
    next-strongest identity. The text sent is the raw text of the best-scoring method
    that has any — the same choice the ladder makes. The other parses are kept for
    locating a pipeline quote taken from a different parser's text.
    """
    for cid in (clean_doi(row.get("doi_r", "")), primary_key({**row, "doi_r": ""})):
        if not cid:
            continue
        res = read_parse_cache(cid)
        if not res:
            continue
        ranked = sorted((r for r in res.values() if isinstance(r, dict)),
                        key=score_parse_result, reverse=True)
        texts = {str(r.get("source") or i): str(r.get("raw_text") or "")
                 for i, r in enumerate(ranked) if str(r.get("raw_text") or "").strip()}
        if not texts:
            continue
        method = next(iter(texts))
        text = texts[method]
        # The XML parses keep the reference list out of raw_text, and the list is where
        # an original's title and year are read from. The best parse's list, as printed.
        refs = next((r.get("references") for r in ranked if r.get("references")), [])
        if refs and not _REF_HEADING.search(text[-len(text) // 3:]):
            text += "\n\nREFERENCES (as parsed)\n" + "\n".join(
                str(r.get("raw_ref") or f"{'; '.join(r.get('authors') or [])} "
                    f"({r.get('year') or ''}). {r.get('title') or ''}") for r in refs)
        return text, method, texts
    return "", "", {}


_REF_HEADING = re.compile(r"^\s*(references|bibliography|literature cited)\s*$",
                         re.I | re.M)


def metadata_references(row: dict) -> list[str]:
    """The work's reference list from OpenAlex (OpenCitations as fallback), one line
    per work, read from the pipeline's cache only.

    These are the lists the ladder's reference-list rung reads for a row with no
    document (`extract/link_original.py`). They are an input, not the pipeline's
    answer, so giving them to the blind coder keeps the recode blind. A list that is
    not cached is simply absent: this check makes no network call.
    """
    oa = row.get("oa_work_id_r") or str(row.get("openalex_id_r") or "").rsplit("/", 1)[-1]
    paths = [OA_CACHE_DIR / f"refs_{oa}.json"] if oa else []
    if clean_doi(row.get("doi_r", "")):
        paths.append(OA_CACHE_DIR / f"ocrefs_{cache_key(clean_doi(row['doi_r']))}.json")
    works = next((read_json(p) for p in paths if cache_exists(p) and read_json(p)), [])
    lines = []
    for w in works:
        names = [a.get("author", {}).get("display_name", "") for a in w.get("authorships") or []]
        authors = ", ".join(names[:3]) + (" et al." if len(names) > 3 else "")
        doi = clean_doi(w.get("doi") or "")
        lines.append(f"{authors or 'unknown'} ({w.get('publication_year') or '?'}). "
                     f"{w.get('title') or ''}{f'. doi:{doi}' if doi else ''}")
    return lines


def _cap_document(text: str) -> str:
    """The document within FULLTEXT_CHARS, cutting the body and not the reference list.

    The list sits at the end, and it is where an original's title and year are read
    from. A document within the cap passes through unchanged.
    """
    if len(text) <= FULLTEXT_CHARS:
        return text
    start = text.rfind("REFERENCES (as parsed)")
    if start < 0:
        tail = len(text) - len(text) // 3
        start = max((m.start() for m in _REF_HEADING.finditer(text, tail)), default=-1)
    if start < 0:
        return text[:FULLTEXT_CHARS]
    refs = text[start:start + REFERENCE_CHARS]
    return text[:FULLTEXT_CHARS - len(refs) - 2] + "\n\n" + refs


def build_prompt(row: dict, fulltext: str) -> str:
    parts = [EVIDENCE_POLICY + TASK, "PAPER", f"TITLE: {row.get('title_r', '')}"]
    if row.get("abstract_r"):
        parts.append(f"ABSTRACT: {row['abstract_r']}")
    if fulltext:
        parts.append("FULL TEXT, as parsed from the document (sections in order, "
                     "reference list at the end):\n" + _cap_document(fulltext))
    # Without a document, or with one whose parse found no reference list, the
    # original can only be named from the work's metadata reference list.
    if not _REF_HEADING.search(fulltext) and "REFERENCES (as parsed)" not in fulltext:
        refs = metadata_references(row)
        if refs:
            parts.append("REFERENCE LIST of this paper (from OpenAlex, not from the "
                         "document; unordered):\n" + "\n".join(refs))
    return "\n\n".join(parts)


# ── The call ─────────────────────────────────────────────────────────────────

def recode(prompt: str, model: str = FINAL_CHECK_MODEL, cached_only: bool = False) -> dict:
    """The blind coding for *prompt*, cached on prompt version + model@effort + prompt.

    With *cached_only*, a miss raises instead of calling, so a re-score cannot buy a
    new coding by accident. A failed or tool-using call raises and is not cached.
    """
    if model not in FINAL_CHECK_MODELS:
        raise ValueError(f"unknown model: {model} (expected one of {FINAL_CHECK_MODELS})")
    key = hashlib.sha256(
        f"{PROMPT_VERSION}|{model}@{FINAL_CHECK_EFFORT}|{prompt}".encode()).hexdigest()[:24]
    path = CACHE / f"recode_{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    if cached_only:
        raise RuntimeError(f"no cached coding at {path}")
    t0 = time.time()
    call = _call_claude if model.startswith("claude-") else _call_codex
    record = {**call(prompt, model), "seconds": round(time.time() - t0, 1),
              "prompt_version": PROMPT_VERSION, "model": model,
              "effort": FINAL_CHECK_EFFORT}
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=1))
    os.replace(tmp, path)
    return record


def _call_claude(prompt: str, model: str) -> dict:
    """`claude -p` with no tools, no MCP servers and no settings: the text is all it has."""
    with tempfile.TemporaryDirectory(prefix="flora-final-check-") as cwd:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--effort", FINAL_CHECK_EFFORT,
             "--tools", "", "--strict-mcp-config", "--setting-sources", "",
             "--no-session-persistence", "--output-format", "json",
             "--json-schema", json.dumps(SCHEMA)],
            input=prompt, capture_output=True, text=True, timeout=900, cwd=cwd)
    out = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
    coding = out.get("structured_output")
    if proc.returncode or out.get("is_error") or not isinstance(coding, dict):
        raise RuntimeError(f"claude -p failed ({proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[:500]}")
    usage = out.get("modelUsage", {}).get(model, {})
    return {"coding": coding,
            "input_tokens": usage.get("inputTokens", 0)
                            + usage.get("cacheCreationInputTokens", 0)
                            + usage.get("cacheReadInputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "list_price_usd": out.get("total_cost_usd")}


def _strict(schema: dict) -> dict:
    """*schema* in the form OpenAI's structured output demands: every property required
    and no others. Local to this call, because `SCHEMA` is part of `PROMPT_VERSION`."""
    out = dict(schema)
    if "properties" in out:
        out["properties"] = {k: _strict(v) for k, v in out["properties"].items()}
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    if "items" in out:
        out["items"] = _strict(out["items"])
    return out


# What a run with no tools emits. Anything else means the coder acted on something
# beyond the prompt, so the coding is not blind and is refused.
_CODEX_PASSIVE_ITEMS = {"agent_message", "reasoning"}


def _call_codex(prompt: str, model: str) -> dict:
    """`codex exec` in an empty directory, under an empty CODEX_HOME holding only the login.

    `--ignore-user-config` alone still loads the global AGENTS.md (measured 2026-11-05:
    the coder quoted its paths), which would put this machine's instructions in front
    of the coding. The shell and web search are off; the event log is the check.
    """
    source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    if not source.exists():
        raise RuntimeError(f"codex exec needs a login at {source}")
    with tempfile.TemporaryDirectory(prefix="flora-final-check-") as tmp:
        home, cwd = Path(tmp) / "home", Path(tmp) / "work"
        home.mkdir()
        cwd.mkdir()
        (home / "auth.json").symlink_to(source)
        schema = Path(tmp) / "schema.json"
        schema.write_text(json.dumps(_strict(SCHEMA)))
        proc = subprocess.run(
            ["codex", "exec", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
             "--sandbox", "read-only", "--cd", str(cwd), "--model", model,
             "-c", f"model_reasoning_effort={FINAL_CHECK_EFFORT}",
             "-c", 'web_search="disabled"', "--disable", "shell_tool",
             "--output-schema", str(schema), "--json", "-"],
            input=prompt, capture_output=True, text=True, timeout=900, cwd=cwd,
            env={**os.environ, "CODEX_HOME": str(home)})
    events = [json.loads(line) for line in proc.stdout.splitlines()
              if line.startswith("{")]
    items = [e["item"] for e in events if e.get("type") == "item.completed"]
    acted = sorted({str(i.get("type")) for i in items} - _CODEX_PASSIVE_ITEMS)
    if acted:
        raise RuntimeError(f"codex exec used tools ({', '.join(acted)}): not a blind coding")
    messages = [i.get("text", "") for i in items if i.get("type") == "agent_message"]
    try:
        coding = json.loads(messages[-1])
    except (IndexError, ValueError):
        coding = None
    if proc.returncode or not isinstance(coding, dict):
        raise RuntimeError(f"codex exec failed ({proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[-500:]}")
    usage = next((e.get("usage", {}) for e in reversed(events)
                  if e.get("type") == "turn.completed"), {})
    return {"coding": coding, "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0), "list_price_usd": None}


# ── Comparison ───────────────────────────────────────────────────────────────

def _surname(authors: str) -> str:
    """First author's surname from 'Auerbach, A. J.; Hassett' or 'Smith and Jones'."""
    first = re.split(r";| and | & |,? et al", authors or "", maxsplit=1)[0].strip()
    first = first.split(",")[0].strip() if "," in first else (first.split() or [""])[-1]
    return re.sub(r"[^a-z]", "", first.lower())


def _year(y: str) -> str:
    m = re.search(r"(1[89]|20)\d\d", str(y or ""))
    return m.group(0) if m else ""


def original_grade(row: dict, target: dict) -> tuple[str, float]:
    """How well one blind target matches the row's original, best grade first.

    Title similarity is the larger of the Jaccard index and the share of the shorter
    title's words found in the longer one, so a title cited with its subtitle on one
    side and without it on the other still matches.

    doi            cleaned DOIs equal
    title          title similarity >= 0.6 and the year or the first author agrees
    author_year    first-author surname and year agree, and the blind coder gave no
                   title or one at least 0.3 similar; two unlike titles by the same
                   author in the same year are the sibling case, and do not agree
    title_only     title similarity >= 0.6, but year and author both disagree
    partial        title similarity 0.3-0.6 with the year or the author agreeing
    different      none of the above
    The first three count as agreement.
    """
    d_row, d_t = clean_doi(row.get("doi_o", "")), clean_doi(target.get("doi", ""))
    t_row, t_blind = row.get("title_o", ""), target.get("title", "")
    shorter, longer = sorted((t_row, t_blind), key=lambda t: len(_tokens(t)))
    jac = max(jaccard_similarity(t_row, t_blind), token_coverage(shorter, longer))
    sur = _surname(row.get("authors_o", ""))
    same_author = bool(sur) and sur == _surname(target.get("authors", ""))
    same_year = bool(_year(row.get("year_o"))) and _year(row.get("year_o")) == _year(target.get("year"))
    if d_row and d_row == d_t:
        return "doi", jac
    if jac >= 0.6 and (same_year or same_author):
        return "title", jac
    if same_author and same_year and (jac >= 0.3 or not t_blind):
        return "author_year", jac
    if jac >= 0.6:
        return "title_only", jac
    if jac >= 0.3 and (same_year or same_author):
        return "partial", jac
    return "different", jac


ORIGINAL_AGREES = {"doi", "title", "author_year"}
_GRADE_RANK = ["doi", "title", "author_year", "title_only", "partial", "different"]

# Validators split on this boundary more than any other (88% inter-rater agreement on
# outcome, most splits failed-vs-mixed), so it is reported as its own grade.
SOFT_PAIRS = {frozenset({"failed", "mixed"})}


def outcome_grade(row: dict, target: dict) -> str:
    """exact | soft (failed vs mixed) | different, on the row's own vocabulary."""
    if row.get("type") == "reproduction":
        pairs = [(row.get("outcome_computation", ""), target.get("outcome_computation", "")),
                 (row.get("outcome_robustness", ""), target.get("outcome_robustness", ""))]
        return "exact" if all(a == b for a, b in pairs) else "different"
    a, b = row.get("outcome", ""), target.get("outcome", "")
    if a == b:
        return "exact"
    return "soft" if frozenset({a, b}) in SOFT_PAIRS else "different"


# ── Quote heuristic ──────────────────────────────────────────────────────────

_TOKEN = re.compile(r"[a-z]+|\d+(?:\.\d+)?")
_NUMBER = re.compile(r"(?<![\w.])[<>=]?\s*-?\d*\.\d+|\b\d{2,}\b")
SHINGLE = 4
# How far a quote's 4-grams may drift apart and still count as one passage: enough for
# a page header or a footnote marker the parse left inside a sentence.
PASSAGE_SLACK = 20


def _tokens(text: str) -> list[str]:
    text = (text or "").lower().replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = re.sub(r"-\s*\n\s*", "", text)  # PDF hyphenation across lines
    return _TOKEN.findall(text)


def _shingles(tokens: list[str]) -> list[tuple]:
    return [tuple(tokens[i:i + SHINGLE]) for i in range(max(len(tokens) - SHINGLE + 1, 0))]


def _numbers(text: str) -> set[str]:
    """Distinctive numbers: decimals (effect sizes, p-values) and integers >= 10 (N),
    minus four-digit years, which every citation carries."""
    out = set()
    for m in _NUMBER.findall(text or ""):
        n = re.sub(r"[<>=\s]", "", m).lstrip("0") or "0"
        if not re.fullmatch(r"(19|20)\d\d", n):
            out.add(n)
    return out


class SourceIndex:
    """Token positions of one source text, for locating quotes in it."""

    def __init__(self, text: str):
        self.tokens = _tokens(text)
        self.at: dict[tuple, list[int]] = {}
        for i, s in enumerate(_shingles(self.tokens)):
            self.at.setdefault(s, []).append(i)

    def locate(self, quote: str) -> tuple[float, tuple[int, int] | None]:
        """(share of the quote's 4-grams found in one passage, that passage's span).

        The passage is the window of PASSAGE_SLACK implied start positions holding the
        most distinct 4-grams. Counting only those stops common phrases scattered
        through a long paper from passing a fabricated quote. A quote under four words
        has no 4-gram, so it counts as found when its words occur in sequence.
        """
        words = _tokens(quote)
        sh = _shingles(words)
        if not sh:
            hit = next((i for i in range(len(self.tokens) - len(words) + 1)
                        if words and self.tokens[i:i + len(words)] == words), None)
            return (1.0, (hit, hit + len(words))) if hit is not None else (0.0, None)
        # Each found 4-gram implies a start position for the quote.
        hits = sorted((p - k, k) for k, s in enumerate(sh) for p in self.at.get(s, []))
        if not hits:
            return 0.0, None
        most, anchor, lo = 0, hits[0][0], 0
        for hi in range(len(hits)):
            while hits[hi][0] - hits[lo][0] > PASSAGE_SLACK:
                lo += 1
            found = len({k for _, k in hits[lo:hi + 1]})
            if found > most:
                most, anchor = found, hits[lo][0]
        return most / len(sh), (anchor, anchor + len(words))


def _parts(quote: str) -> list[str]:
    return [p.strip() for p in (quote or "").split(" | ") if p.strip()]


_STOP = set("""a an the and or but of in on at to for from by with as is are was were be been
being this that these those it its we our us they their them he she his her not no nor
than then there here which who whom what when where while however whether such also
could would should can may might will shall do does did done have has had into onto
over under about above across after before between both each either more most other
some any all only same so very just thus therefore one two three""".split())


def _stems(text: str) -> set[str]:
    """Content words cut to five letters: a crude stem that lets 'moderated' meet
    'moderating' and 'framing' meet 'frames', without a stemmer dependency."""
    return {t[:5] for t in _tokens(text) if t.isalpha() and len(t) > 2 and t not in _STOP}


def quote_check(q_pipe: str, q_blind: str, sources: list[SourceIndex]) -> dict:
    """Do the pipeline's and the blind coder's quotes point at the same evidence?

    Signals, each simple enough to read off the output:
      present_*   share of the quote's 4-grams found in one passage of the source
                  (title + abstract, or any parse of the document). Each " | " part
                  is scored on its best source and the quote takes its WORST part, so
                  one real part cannot cover for an invented one. Under 0.5 the quote
                  is not in the text: flagged `fabricated_*`.
      overlap     token containment |A∩B| / min(|A|,|B|) over all words.
      content     the same over content-word stems (stopwords dropped).
      distance    tokens between the two passages, located in a source both are in
                  (0 = overlapping); blank when no one source holds both.
      numbers     distinctive numbers both quotes share (decimals, N >= 10).
    Verdict, first that applies:
      no_quote    either quote empty
      same        overlap >= 0.6, or the passages overlap
      adjacent    passages within 60 tokens (about two sentences)
      same_claim  content >= 0.4: a different passage making the same statement —
                  typically the abstract restating the discussion
      numbers     >= 2 shared distinctive numbers
      different   otherwise
    """
    res: dict = {"present_pipe": None, "present_blind": None, "overlap": None,
                 "content": None, "distance": None, "shared_numbers": ""}
    located = {name: [[s.locate(p) for s in sources] for p in _parts(q)]
               for name, q in (("pipe", q_pipe), ("blind", q_blind))}
    for name, hits in located.items():
        if hits:
            res[f"present_{name}"] = round(min(max(h[0] for h in part) for part in hits), 2)
    if not located["pipe"] or not located["blind"]:
        res["verdict"] = "no_quote"
        return res

    a, b = set(_tokens(q_pipe)), set(_tokens(q_blind))
    res["overlap"] = round(len(a & b) / max(min(len(a), len(b)), 1), 2)
    a, b = _stems(q_pipe), _stems(q_blind)
    res["content"] = round(len(a & b) / max(min(len(a), len(b)), 1), 2)
    shared = _numbers(q_pipe) & _numbers(q_blind)
    res["shared_numbers"] = " ".join(sorted(shared))
    gaps = [max(0, max(sp[0], sb[0]) - min(sp[1], sb[1]))
            for pp in located["pipe"] for pb in located["blind"]
            for (fp, sp), (fb, sb) in zip(pp, pb)
            if sp and sb and fp >= 0.5 and fb >= 0.5]
    res["distance"] = min(gaps) if gaps else None
    if res["overlap"] >= 0.6 or res["distance"] == 0:
        res["verdict"] = "same"
    elif res["distance"] is not None and res["distance"] <= 60:
        res["verdict"] = "adjacent"
    elif res["content"] >= 0.4:
        res["verdict"] = "same_claim"
    elif len(shared) >= 2:
        res["verdict"] = "numbers"
    else:
        res["verdict"] = "different"
    flags = [n for n in ("pipe", "blind") if res[f"present_{n}"] < 0.5]
    res["verdict"] += "".join(f"+fabricated_{n}" for n in flags)
    return res


def pair_originals(work_rows: list[dict], targets: list[dict]) -> list[tuple]:
    """One-to-one pairs of (row index, target index, grade, title Jaccard).

    The FLoRA unit is the replication-original PAIR, so each shipped row may claim at
    most one blind target and each target at most one row. Pairs are taken greedily,
    strongest grade first (then the higher title Jaccard), and only from the grades
    that count as agreement; a row left over has no counterpart on the blind side.
    """
    cands = sorted(((i, j, *original_grade(r, t)) for i, r in enumerate(work_rows)
                    for j, t in enumerate(targets)),
                   key=lambda c: (_GRADE_RANK.index(c[2]), -c[3]))
    pairs, rows_used, targets_used = [], set(), set()
    for i, j, grade, jac in cands:
        if grade in ORIGINAL_AGREES and i not in rows_used and j not in targets_used:
            pairs.append((i, j, grade, jac))
            rows_used.add(i)
            targets_used.add(j)
    return pairs


def _quotes(row: dict, target: dict) -> tuple[str, str]:
    if row.get("type") == "reproduction":
        keys = ("outcome_computational_quote", "outcome_robustness_quote")
        return (" | ".join(filter(None, (row.get(k, "") for k in keys))),
                " | ".join(filter(None, (target.get(k, "") for k in keys))))
    return row.get("outcome_phrase", ""), target.get("outcome_quote", "")


def compare_work(work_rows: list[dict], coding: dict,
                 sources: list[SourceIndex]) -> list[dict]:
    """Agreement for every shipped row of one work, keyed by pair_id.

    Each row is compared with the blind target it is paired with
    (`pair_originals`). A row is released when its pair agrees on type and outcome,
    the blind coder was confident of that outcome (an outcome it would not stake was,
    in the pilot, the kind humans corrected), and the whole work pairs off. The rule
    holds every row of a work whose two sides do not pair off completely, whichever
    side has the extra original: a different count of originals is the clearest sign
    that one side misread the paper, and it is where outcomes get scoped to the wrong
    set of originals.
    """
    targets = coding.get("targets") or []
    pairs = {i: (j, grade, jac) for i, j, grade, jac in pair_originals(work_rows, targets)}
    extra_rows = len(work_rows) - len(pairs)
    extra_targets = len(targets) - len(pairs)
    # A plan reports nothing to validate, so a blind "prospective" disagrees with a
    # shipped replication whatever the type says.
    status_ok = coding.get("study_status") != "prospective"
    work = {"blind_type": coding.get("record_type"),
            "blind_status": coding.get("study_status"),
            "work_n_shipped": len(work_rows), "work_n_blind": len(targets),
            "work_extra_shipped": extra_rows, "work_extra_blind": extra_targets,
            "work_pairs_off": extra_rows == 0 and extra_targets == 0}
    out = []
    for i, row in enumerate(work_rows):
        res = {"pair_id": row.get("pair_id"), **work,
               "type_agrees": status_ok and coding.get("record_type") == row.get("type")}
        if i not in pairs:
            best = max((original_grade(row, t) for t in targets),
                       key=lambda g: (-_GRADE_RANK.index(g[0]), g[1]),
                       default=("no_target", 0.0))
            res.update(original_grade=f"unpaired:{best[0]}", original_agrees=False,
                       outcome_grade="unpaired", outcome_agrees=False,
                       outcome_agrees_soft=False, title_jaccard=best[1],
                       blind_original="", blind_outcome="", blind_confident=None,
                       blind_identified_by="", blind_quote="",
                       quote_present_pipe=None, quote_present_blind=None,
                       quote_overlap=None, quote_content=None, quote_distance=None,
                       quote_shared_numbers="", quote_verdict="unpaired", released=False)
            out.append(res)
            continue
        j, grade, jac = pairs[i]
        t = targets[j]
        og = outcome_grade(row, t)
        q_pipe, q_blind = _quotes(row, t)
        res.update(original_grade=grade, title_jaccard=round(jac, 2), original_agrees=True,
                   blind_original=f"{t.get('authors', '')} ({t.get('year', '')}) "
                                  f"{t.get('title', '')[:80]} {t.get('doi', '')}".strip(),
                   blind_outcome=(t.get("outcome") or
                                  f"{t.get('outcome_computation', '')}, "
                                  f"{t.get('outcome_robustness', '')}"),
                   blind_confident=t.get("outcome_confident"),
                   blind_identified_by=t.get("identified_by"),
                   outcome_grade=og, outcome_agrees=og == "exact",
                   outcome_agrees_soft=og in ("exact", "soft"), blind_quote=q_blind)
        res.update({f"quote_{k}": v for k, v in quote_check(q_pipe, q_blind, sources).items()})
        res["released"] = bool(res["type_agrees"] and res["outcome_agrees"]
                               and t.get("outcome_confident") is True
                               and work["work_pairs_off"])
        out.append(res)
    return out


# ── The pipeline step ────────────────────────────────────────────────────────

def _pipeline_text(rows: list[dict]) -> str:
    """What the pipeline shipped, one block per original, for the reviewer."""
    blocks = []
    for r in rows:
        verdict = r.get("outcome") or (f"{r.get('outcome_computation', '')}, "
                                       f"{r.get('outcome_robustness', '')}")
        quote = " | ".join(filter(None, _quotes(r, {})[:1]))
        blocks.append(f"{r.get('authors_o', '')} ({r.get('year_o', '')}) "
                      f"{r.get('title_o', '')} {r.get('doi_o', '')}\n"
                      f"{r.get('type', '')}: {verdict}\n{quote}".strip())
    return "\n\n".join(blocks)


def _blind_text(coding: dict) -> str:
    """What the blind coder found, in the same shape as `_pipeline_text`."""
    blocks = []
    for t in coding.get("targets") or []:
        verdict = t.get("outcome") or (f"{t.get('outcome_computation', '')}, "
                                       f"{t.get('outcome_robustness', '')}")
        quote = t.get("outcome_quote") or " | ".join(filter(None, (
            t.get("outcome_computational_quote"), t.get("outcome_robustness_quote"))))
        sure = "confident" if t.get("outcome_confident") is True else "not confident"
        blocks.append(f"{t.get('authors', '')} ({t.get('year', '')}) {t.get('title', '')} "
                      f"{t.get('doi', '')}\n{verdict} ({sure})\n{quote}".strip())
    return "\n\n".join(blocks) or f"{coding.get('record_type')}: no original named"


def _reasons(rows: list[dict], results: list[dict]) -> str:
    """Why a work is held, in the terms the reviewer will check."""
    first = results[0]
    reasons = []
    if first["blind_status"] == "prospective":
        reasons.append("blind: prospective")
    if not all(r["type_agrees"] for r in results):
        reasons.append(f"type: {rows[0].get('type', '')} vs {first['blind_type']}")
    if not first["work_pairs_off"]:
        reasons.append(f"originals: {first['work_n_shipped']} vs {first['work_n_blind']}")
    for row, res in zip(rows, results):
        if res["original_agrees"] and not res["outcome_agrees"]:
            reasons.append(f"outcome {row.get('doi_o', '')}: {row.get('outcome', '')} "
                           f"vs {res['blind_outcome']}")
        elif res["original_agrees"] and res["blind_confident"] is not True:
            reasons.append(f"not confident {row.get('doi_o', '')}")
    return "; ".join(reasons)


def check_work(label: str, rows: list[dict], model: str,
               now: datetime | None = None) -> dict | None:
    """One work's entry in the check file, or None when its document is not on this
    machine. A row that acquired a document must be coded from it: the abstract alone
    would be a weaker reading that looks the same in the file."""
    row = rows[0]
    text, _, texts = document_text(row)
    if not text and str(row.get("parse_method") or "").strip():
        return None
    record = recode(build_prompt(row, text), model=model)
    coding = record["coding"]
    sources = [SourceIndex(f"{row.get('title_r', '')}\n{row.get('abstract_r', '')}")]
    sources += [SourceIndex(t) for t in texts.values()]
    results = compare_work(rows, coding, sources)
    released = all(r["released"] for r in results)
    fabricated = sorted({r["quote_verdict"] for r in results
                         if "fabricated" in str(r["quote_verdict"])})
    return {"work": label, "doi_r": row.get("doi_r", ""), "title_r": row.get("title_r", ""),
            "fingerprint": work_fingerprint(rows),
            "status": "released" if released else "review",
            "reason": "" if released else _reasons(rows, results),
            "quotes": "; ".join(fabricated),
            "pipeline": _pipeline_text(rows), "blind": _blind_text(coding),
            "blind_reasoning": str(coding.get("reasoning") or ""),
            "blind_coding": json.dumps(coding, ensure_ascii=False, sort_keys=True),
            "model": record["model"], "effort": record["effort"],
            "prompt_version": record["prompt_version"],
            "checked_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "decision": "", "note": ""}


def worklist(changed: dict[str, list[dict]], entries: dict[str, dict]) -> dict[str, list[dict]]:
    """The changed works no entry answers: a new fingerprint or an edited prompt."""
    def answered(label: str, rows: list[dict]) -> bool:
        entry = entries.get(label) or {}
        return (entry.get("fingerprint") == work_fingerprint(rows)
                and entry.get("prompt_version") == PROMPT_VERSION)
    return {label: rows for label, rows in changed.items() if not answered(label, rows)}


# Three failures in a row is a subscription limit or a logged-out CLI, not three papers.
MAX_CONSECUTIVE_FAILURES = 3
_SAVE_EVERY = 25


def run_check(todo: dict[str, list[dict]], entries: dict[str, dict], path: Path,
              model: str, workers: int) -> dict:
    """Code *todo*, saving as it goes. A work that fails stays unanswered, so the export
    holds it and the next run retries it."""
    counts = {"released": 0, "review": 0, "skipped_no_document": 0, "failed": 0}
    consecutive = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_work, label, rows, model): label
                   for label, rows in todo.items()}
        for future in as_completed(futures):
            label = futures[future]
            try:
                entry = future.result()
            except Exception as exc:  # noqa: BLE001 — one work must not cost the others
                counts["failed"] += 1
                consecutive += 1
                print(f"  {label}: {exc}")
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    print(f"  stopping after {consecutive} failures in a row")
                    for pending in futures:
                        pending.cancel()
                    break
                continue
            consecutive = 0
            if entry is None:
                counts["skipped_no_document"] += 1
                continue
            entries[label] = entry
            counts[entry["status"]] += 1
            done += 1
            if done % _SAVE_EVERY == 0:
                _write_final_check(path, entries)
                print(f"  {done:,} of {len(todo):,} coded")
    _write_final_check(path, entries)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m extract.final_check",
        description="Code every work the next export would change, blind, and write the "
                    "review file the export gates on. Without --run, prints the worklist.")
    parser.add_argument("--release", default="", help="The routing release, as for extract.export.")
    parser.add_argument("--spec-dir", type=Path, default=SPEC_DIR,
                        help="The rule book the release is checked against, as for extract.export.")
    parser.add_argument("--run", action="store_true", help="Make the headless calls.")
    parser.add_argument("--model", choices=FINAL_CHECK_MODELS, default=FINAL_CHECK_MODEL)
    parser.add_argument("--limit", type=int, default=None, help="Code at most this many works.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--mode", choices=("live", "validation"), default="live")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="The CSV the export will write; the check file sits beside it.")
    args = parser.parse_args(argv)

    try:
        release_id, admitted, gates = admitted_work_ids(args.release or None,
                                                        spec_dir=args.spec_dir)
        client = ClaimsClient()
    except (StoreUnavailable, ClaimsNotConfigured, StaleBundleError, ValueError) as exc:
        raise SystemExit(str(exc))
    # A sandbox render has no validation queue behind it, so nothing counts as shipped.
    production = args.mode == "live" and args.out.resolve() == DEFAULT_OUT.resolve()
    previous, baseline = previous_shipped(args.out, ["HEAD"]) if production else ({}, "none")
    report = render(client, mode=args.mode, admitted=admitted, gates=gates,
                    shipped_before=frozenset())
    path = set_aside_dir(args.out) / FINAL_CHECK_NAME
    entries = read_final_check(path)
    changed = changed_works(report["main"], previous)
    todo = worklist(changed, entries)
    print(f"release {release_id[:12]}  baseline {baseline}  mode {args.mode}")
    print(f"  {len(changed):,} work(s) differ from the baseline; "
          f"{len(todo):,} need a blind coding; prompt {PROMPT_VERSION}")
    if args.limit is not None:
        todo = dict(list(todo.items())[:args.limit])
    if not args.run:
        print(f"  dry run: --run would code {len(todo):,} with {args.model} "
              f"(about {len(todo) * 14 / max(args.workers, 1) / 60:,.0f} min at the "
              f"pilot's 14 s per call and {args.workers} workers)")
        return 0
    counts = run_check(todo, entries, path, args.model, args.workers)
    print(f"  {counts}  → {path}")
    if counts["failed"]:
        print("  failed works stay held; re-running retries them from the cache onwards")
    print("  next: fill `decision` (ship | hold | reject) on the `review` rows, then "
          "run extract.export")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
