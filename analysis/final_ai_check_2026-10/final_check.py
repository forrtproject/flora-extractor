"""Final AI check: a blind Claude recode of a Stage 3 row, compared with the row.

Claude Opus 5.5 (`claude -p`, on the subscription) reads the record — title, abstract
and, where the pipeline acquired one, the parsed full text — WITHOUT the pipeline's
answer, and codes type, original(s) and outcome. `compare()` then grades agreement
on each field. A row that agrees on all three may be released as a preliminary AI
code; anything else goes to a human first.

The codebook is the pipeline's own: the target rules are cut out of `_TARGET_TASK`
and the outcome rules are `_OUTCOME_RULES` / `_REPRO_AXIS_RULES` /
`_MULTI_TARGET_SCOPE`, imported from `shared/prompts.py`. Only the wrapper differs:
there is no keyed candidate list (the recode is blind), so the original is named as
authors, year, title and DOI, and the record type is asked first instead of being
assumed.

Read-only towards the pipeline: reads the row CSV and the parse cache, writes only
under this directory (`cache/`). Run with the main checkout's cache:

    FLORA_CACHE_DIR=/home/lukas/flora-extractor/cache \
      /home/lukas/flora-extractor/.venv/bin/python analysis/final_ai_check_2026-10/pilot.py
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from shared.cache import cache_exists, read_json  # noqa: E402
from shared.config import OA_CACHE_DIR  # noqa: E402
from shared.disambiguation import jaccard_similarity, token_coverage  # noqa: E402
from shared.pdf_parsing import read_parse_cache, score_parse_result  # noqa: E402
from shared.prompts import (  # noqa: E402
    EVIDENCE_POLICY, _MULTI_TARGET_SCOPE, _OUTCOME_RULES, _REPRO_AXIS_RULES,
    _TARGET_TASK)
from shared.row_key import primary_key  # noqa: E402
from shared.schema import OUTCOME_LABELS  # noqa: E402
from shared.utils import cache_key, clean_doi  # noqa: E402

MODEL = "claude-opus-5-5"
EFFORT = "high"
CACHE = HERE / "cache"
# Opus reads 1M tokens; 150k characters (~40k tokens) holds a whole article with its
# reference list, which is where the original's title and DOI are read from.
FULLTEXT_CHARS = 150_000


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


def build_prompt(row: dict, fulltext: str) -> str:
    parts = [EVIDENCE_POLICY + TASK, "PAPER", f"TITLE: {row.get('title_r', '')}"]
    if row.get("abstract_r"):
        parts.append(f"ABSTRACT: {row['abstract_r']}")
    if fulltext:
        parts.append("FULL TEXT, as parsed from the document (sections in order, "
                     "reference list at the end):\n" + fulltext[:FULLTEXT_CHARS])
    # Without a document, or with one whose parse found no reference list, the
    # original can only be named from the work's metadata reference list.
    if not _REF_HEADING.search(fulltext) and "REFERENCES (as parsed)" not in fulltext:
        refs = metadata_references(row)
        if refs:
            parts.append("REFERENCE LIST of this paper (from OpenAlex, not from the "
                         "document; unordered):\n" + "\n".join(refs))
    return "\n\n".join(parts)


# ── The call ─────────────────────────────────────────────────────────────────

def recode(prompt: str) -> dict:
    """The blind coding for *prompt*, cached on prompt version + model + prompt.

    `claude -p` with no tools, no MCP servers and no settings: the text is all it
    has. A failed call raises and is not cached.
    """
    key = hashlib.sha256(
        f"{PROMPT_VERSION}|{MODEL}@{EFFORT}|{prompt}".encode()).hexdigest()[:24]
    path = CACHE / f"recode_{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    t0 = time.time()
    proc = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--effort", EFFORT, "--tools", "", "--strict-mcp-config",
         "--setting-sources", "", "--no-session-persistence",
         "--output-format", "json", "--json-schema", json.dumps(SCHEMA)],
        input=prompt, capture_output=True, text=True, timeout=900, cwd="/tmp")
    seconds = time.time() - t0
    out = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
    coding = out.get("structured_output")
    if proc.returncode or out.get("is_error") or not isinstance(coding, dict):
        raise RuntimeError(f"claude -p failed ({proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[:500]}")
    usage = out.get("modelUsage", {}).get(MODEL, {})
    record = {"coding": coding, "seconds": round(seconds, 1),
              "prompt_version": PROMPT_VERSION, "model": MODEL, "effort": EFFORT,
              "input_tokens": usage.get("inputTokens", 0)
                              + usage.get("cacheCreationInputTokens", 0)
                              + usage.get("cacheReadInputTokens", 0),
              "output_tokens": usage.get("outputTokens", 0),
              "list_price_usd": out.get("total_cost_usd")}
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(record, indent=1))
    return record


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
        """(share of the quote's 4-grams found, token span of the densest match).

        The span is the median-anchored run of matched positions, so a stray common
        4-gram elsewhere in the paper does not stretch it.
        """
        sh = _shingles(_tokens(quote))
        if not sh:
            return 0.0, None
        # Anchor each found shingle at the position implied for the quote's start.
        starts = [p - k for k, s in enumerate(sh) for p in self.at.get(s, [])]
        found = sum(1 for s in sh if s in self.at)
        if not starts:
            return 0.0, None
        starts.sort()
        mid = starts[len(starts) // 2]
        n = len(sh) + SHINGLE - 1
        return found / len(sh), (mid, mid + n)


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
      present_*   share of the quote's 4-grams found in the source (title + abstract,
                  or any parse of the document), best over its " | " parts. A quote
                  under 0.5 is not in the text: flagged `fabricated_*`.
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
            res[f"present_{name}"] = round(max(h[0] for part in hits for h in part), 2)
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

    def best(q: str) -> tuple[float, int | None, tuple | None]:
        top = (0.0, None, None)
        for p in _parts(q):
            for i, s in enumerate(sources):
                share, span = s.locate(p)
                if share > top[0]:
                    top = (share, i, span)
        return top

    (sp, ip, span_p), (sb, ib, span_b) = best(q_pipe), best(q_blind)
    res["present_pipe"], res["present_blind"] = round(sp, 2), round(sb, 2)
    a, b = set(_tokens(q_pipe)), set(_tokens(q_blind))
    res["overlap"] = round(len(a & b) / max(min(len(a), len(b)), 1), 2)
    shared = _numbers(q_pipe) & _numbers(q_blind)
    res["shared_numbers"] = " ".join(sorted(shared))
    if span_p and span_b and ip == ib and sp >= 0.5 and sb >= 0.5:
        res["distance"] = max(0, max(span_p[0], span_b[0]) - min(span_p[1], span_b[1]))
    if res["overlap"] >= 0.6 or res["distance"] == 0:
        res["verdict"] = "same"
    elif res["distance"] is not None and res["distance"] <= 60:
        res["verdict"] = "adjacent"
    elif len(shared) >= 2:
        res["verdict"] = "numbers"
    else:
        res["verdict"] = "different"
    flags = [n for n, s in (("pipe", sp), ("blind", sb)) if s < 0.5]
    if flags:
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
    in the pilot, the kind humans corrected), and the whole work pairs off: the rule
    holds every row of a work whose two sides do not pair off completely, whichever side has the extra original: a different count
    of originals is the clearest sign that one side misread the paper, and it is
    where outcomes get scoped to the wrong set of originals.
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
                       quote_verdict="unpaired", released=False)
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
