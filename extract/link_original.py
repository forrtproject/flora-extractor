"""
link_original.py — Single-DOI orchestration of Stage 3's resolution ladder.

Public API:
    run_for_doi(doi_r, cands_df=None, force=False,
                no_llm=False, no_pdf=False, classification=None) → dict

The returned dict is flat and its keys are grouped by the stage that produced them
(`_build_output` at the bottom of this file is the one place they are assembled).
`extract/run_extract.py` is the only consumer:
  doi_r          — the replication's DOI, as passed in
  (no prefix)    — pass-through of the input row (`_OA_PASSTHROUGH`)
  n_candidates / all_candidates_json — the OpenAlex re-query
  pdf_*          — PDF acquisition step
  grobid_*       — parsing step (the name predates parse_all's six parsers)
  resolution_* / resolved_* / targets / target_stage — what the ladder settled on
  llm_*          — attribution of whichever call produced the resolution
"""
from __future__ import annotations

import html
import json
import re
from functools import partial
from pathlib import Path
from typing import Optional

import pandas as pd

from shared.cache import clear_content_keys, write_json
from shared.config import GROBID_CACHE_DIR, LLM_CACHE_DIR, OA_CACHE_DIR, PARSE_CACHE_DIR, log
from shared.disambiguation import is_umbrella_paper, jaccard_similarity
from shared import token_counter
from shared.llm_client import (
    SCREEN_QUALIFYING, identify_targets_with_llm, screen_references_with_llm,
)
from shared.pdf_parsing import (
    parse_all as _parse_all,
    best_parse_result as _best_parse_shared,
    parse_result_is_empty,
    read_parse_cache,
)
from shared.openalex_client import author_matches, extract_author_year_patterns, find_all_candidates, fetch_opencitations_references, fetch_referenced_works_metadata, _search_crossref_by_title, _search_openalex_by_title
from shared.prompts import (
    TARGET_INTRO_CHARS, TARGET_METHODS_CHARS,
    _abstract_tail, rendered_reference_entries,
)
from shared.target_keys import assign_target_keys
from shared.pdf_sources import acquire_pdf, openalex_xml_has_content
from shared.utils import cache_key, clean_doi

# ── Unified rule-based resolver (runs before any LLM call) ───────────────────
# Combines citation-context scoring with a same-author/year title-Jaccard fallback
# into a single function so both paths share one code path.
#
# Path A — the abstract cites an author and a year:
#   Scores by author(+2) + year(+2) + title Jaccard(+≤1).
#   Resolves when best ≥ 4.0 AND gap ≥ 2.0, so only an exact author-and-year hit
#   with no rival within 2 points can win.
#
# Path B — all candidates share one author+year:
#   Falls back to title-Jaccard relative threshold (best > 0.05, best ≥ second×1.5).
#   That threshold is a tiebreak, not evidence, and it fires precisely where Path A
#   declined, so run_for_doi never lets it END the row (see _HELD_ONLY_METHODS).

_STOP_SURNAMES = {"and", "van", "von", "der", "den", "del", "the", "for"}

# ── Title-pattern resolver ─────────────────────────────────────────────────────
# Patterns that extract the original study name from a replication paper's title.
# Order matters: more specific patterns come first.

_TITLE_PATS: list[re.Pattern] = [
    # "A Direct Replication of TARGET" / "Failed Replication of TARGET" / "Replication Study of TARGET"
    re.compile(
        r"^(?:a\s+)?(?:direct\s+|close\s+|failed\s+|conceptual\s+)?replication"
        r"(?:\s+study)?\s+of\s+(.+)",
        re.IGNORECASE,
    ),
    # "Replicating TARGET"
    re.compile(r"^replicating\s+(.+)", re.IGNORECASE),
    # "A Reproduction of TARGET" / "Reproducing TARGET"
    re.compile(r"^(?:a\s+)?reproduction\s+of\s+(.+)", re.IGNORECASE),
    re.compile(r"^reproducing\s+(.+)", re.IGNORECASE),
    # "Revisiting TARGET" / "Re-examining TARGET" / "Reconsidering TARGET"
    re.compile(r"^(?:re-?examining|revisiting|reconsidering)\s+(.+)", re.IGNORECASE),
    # "Can we replicate TARGET?" / "Does TARGET replicate?"
    re.compile(r"^can\s+we\s+replicate\s+(.+?)[\?\.]*$", re.IGNORECASE),
    re.compile(r"^does\s+(.+?)\s+replicate[\?\.]*$", re.IGNORECASE),
    # "Testing the replicability of TARGET"
    re.compile(r"^testing\s+the\s+replicability\s+of\s+(.+)", re.IGNORECASE),
    # "TARGET: A Replication" / "TARGET: Replication and Extension"
    re.compile(
        r"^(.+?)\s*:\s*(?:a\s+)?(?:direct\s+)?replication(?:\s+and\s+extension)?[\?\.]*$",
        re.IGNORECASE,
    ),
]

_TITLE_TARGET_MIN_LEN = 8   # shorter targets are noise (e.g. "Revisiting X" or "Trust")


# ── The may-not-short-circuit gate (replaces classify_match_type) ─────────────
# run_for_doi is a first-success ladder, so a paper with one conspicuous target can
# resolve at a rule and terminate before anything enumerates the rest. No rule asserts
# multiple_original any more — a rule may only WITHHOLD the cheap path. A false
# positive costs one LLM call; a false negative silently drops N-1 originals.
#
# The counted noun must be a STUDY-like unit. "experiments" is excluded on purpose:
# "replications of 5 experiments from Smith (2009)" is five experiments of ONE
# original. There is no project-name pattern (Many Labs replicates one original across
# many labs), only a stated count.
_COUNT_ADJ  = (r"(?:\s+(?:original|independent|published|classic|contemporary|distinct"
               r"|previous|key|prior)(?:\s+and\s+\w+)?)*")
_COUNT_NOUN = r"(?:studi(?:es)?|findings?|papers?)"

# Abstracts state small counts in words at least as often as in digits ("we replicate
# two classic studies"), and a digits-only pattern read those as no count at all.
_COUNT_WORDS: dict[str, int] = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_COUNT_NUM = rf"(?:\d+|(?:{'|'.join(_COUNT_WORDS)})\b)"

# Each pattern must capture the count of studies in group 1.
_STUDY_COUNT_RES: tuple[re.Pattern, ...] = (
    # "replications of 28 classic studies"  /  "replication of two studies"
    re.compile(
        rf"\breplicat(?:ion|ions?)\s+of\s+({_COUNT_NUM}){_COUNT_ADJ}\s+{_COUNT_NOUN}\b",
        re.IGNORECASE,
    ),
    # "replicated 28 original findings"  /  "replicating ten classic studies"
    re.compile(
        rf"\b(?:replicated?|replicating)\s+(?:a\s+total\s+of\s+)?({_COUNT_NUM}){_COUNT_ADJ}"
        rf"\s+{_COUNT_NOUN}\b",
        re.IGNORECASE,
    ),
    # "28 classic and contemporary findings"  /  "three independent studies"
    re.compile(
        rf"\b({_COUNT_NUM})\s+(?:original|independent|published|classic|contemporary|distinct)"
        rf"(?:\s+and\s+\w+(?:\s+\w+)?)?\s+{_COUNT_NOUN}\b",
        re.IGNORECASE,
    ),
)

# 2 is a count: "we replicate two classic studies" is two originals, and the gate must
# withhold the pick there exactly as it does for 28. The upper bound guards digits
# only — "replications of 2019 studies" is a year — and no spelled-out word can hit it.
_COUNT_N_MIN, _COUNT_N_MAX = 2, 1900


def _study_count_stated(title_r: str, abstract_r: str) -> bool:
    """True when the title or abstract states a plausible study count (2 ≤ N < 1900).

    The count may be digits or a spelled-out numeral from two to twelve.

    Every match of every pattern is inspected, not just the first: a pattern whose
    first hit is a year ("replications of 2019 studies") used to shadow a real count
    the same pattern matched later in the same text ("replications of three studies").
    """
    for text in (title_r or "", abstract_r or ""):
        for pattern in _STUDY_COUNT_RES:
            for m in pattern.finditer(text):
                raw = (m.group(1) or "").lower()
                if raw in _COUNT_WORDS:
                    n = _COUNT_WORDS[raw]
                else:
                    try:
                        n = int(raw)
                    except (ValueError, TypeError):
                        continue
                if _COUNT_N_MIN <= n < _COUNT_N_MAX:
                    return True
    return False


def may_stop_at_a_rule(title_r: str, abstract_r: str, year_r: int) -> bool:
    """Whether a deterministic ladder stage may END the row.

    Exactly one distinct author-year pair across title and abstract, and no stated
    study count. Anything else must reach a call that can enumerate targets.
    """
    pairs = {(p["surname"], p["year"])
             for p in extract_author_year_patterns(title_r,    max_year=year_r)
                    + extract_author_year_patterns(abstract_r, max_year=year_r)}
    return len(pairs) == 1 and not _study_count_stated(title_r, abstract_r)


def _extract_title_target(title_r: str) -> "str | None":
    """
    Extract the original study target from a replication paper's title.
    Returns the target substring or None if no pattern matches / target too short.
    """
    title_r = title_r.strip()
    for pat in _TITLE_PATS:
        m = pat.match(title_r)
        if m:
            target = m.group(1).strip().rstrip("?:.,;\"'")
            if len(target) >= _TITLE_TARGET_MIN_LEN:
                return target
    return None


def _resolve_by_title_pattern(
    doi_r:      str,
    study_r:    str,
    candidates: list[dict],
) -> "dict | None":
    """
    Try to resolve the original study by matching the replication paper's title
    against candidate titles using Jaccard similarity.

    Returns the resolver dict when a single confident match exists, else None.
    The caller only asks whether this rung resolved, so "no pattern in the title",
    "no candidate close enough" and "close but not confident" are one answer.
    """
    target = _extract_title_target(study_r)
    if not target or not candidates:
        return None

    scored = sorted(
        candidates,
        key=lambda c: jaccard_similarity(c.get("title", ""), target),
        reverse=True,
    )

    best      = scored[0]
    best_score = jaccard_similarity(best.get("title", ""), target)
    sec_score  = jaccard_similarity(scored[1].get("title", ""), target) if len(scored) > 1 else 0.0

    if best_score < 0.4 or best_score < sec_score * 1.5:
        return None

    log.info("[%s] title_pattern resolved: %s (score=%.3f target=%r)",
             doi_r, best.get("doi"), best_score, target)
    return {
        **_unresolved("needs_fulltext"),
        "resolved":          True,
        "resolution_method": "title_pattern_match",
        "resolved_doi_o":    best.get("doi", ""),
        "resolved_title_o":  best.get("title", ""),
        "resolved_year_o":   best.get("year"),
        "resolved_author_o": best.get("first_author", ""),
        "resolution_score":  round(best_score, 4),
    }


def _extract_cit_contexts(text: str) -> list[dict]:
    """Return list of {surnames, year, raw} from all author-year citations.

    Citation detection delegates to shared.openalex_client.extract_author_year_patterns(),
    which — unlike the old local-only _CITATION_RE — also catches narrative citations
    like "Kim et al. (2014)", not just fully-parenthetical "(Antle, 2010)" ones. It is
    parenthesis-agnostic (its "bare" patterns match "Antle, 2010" whether or not literal
    parentheses surround it in the source text), so both citation styles are covered by
    one call. Surnames are re-derived from each match's raw text (not just the shared
    function's single primary surname) so multi-author citations keep every surname for
    _resolve_rule_based()'s author-matching score.
    """
    results: list[dict] = []
    seen: set[tuple] = set()
    for match in extract_author_year_patterns(text):
        raw = match["raw"]
        surnames = [
            t.lower()
            for t in re.findall(r"[A-Z][A-Za-z\-\xc0-ɏ]{2,}", raw)
            if t.lower() not in _STOP_SURNAMES
        ]
        year = match["year"]
        key = (tuple(sorted(surnames)), year)
        if key in seen:
            continue
        seen.add(key)
        results.append({"surnames": surnames, "year": year, "raw": raw})
    return results


def _resolve_rule_based(
    doi_r:      str,
    abstract_r: str,
    candidates: list[dict],
    year_r:     int,
    study_r:    str = "",
) -> dict:
    """
    Unified pre-LLM resolver covering both citation-context and same-author/year cases.

    Returns the same shape dict as identify_targets_with_llm().
    """
    base: dict = _unresolved("needs_fulltext")

    if not candidates:
        base["resolution_method"] = "no_candidates_found"
        return base

    # Single unambiguous candidate. This branch applies NO semantic check — it accepts
    # whatever the re-query left standing, at score 1.0 — and it is where 28 of the 29
    # rule-resolved rows in data/extracted.csv came from. An abstract that cites exactly
    # one author-year which is not the target ("following the design of Fiedler (2011),
    # we replicated…") passes may_stop_at_a_rule and would end the row on that citation.
    # So run_for_doi only ever HOLDS this pick (see _HELD_ONLY_METHODS).
    if len(candidates) == 1:
        c = candidates[0]
        if is_umbrella_paper(c.get("title", "")):
            return base
        return {**base,
                "resolved":          True,
                "resolution_method": "single_candidate_after_requery",
                "resolved_doi_o":    c.get("doi",          ""),
                "resolved_title_o":  c.get("title",        ""),
                "resolved_year_o":   c.get("year"),
                "resolved_author_o": c.get("first_author", ""),
                "resolution_score":  1.0}

    decoded    = html.unescape(abstract_r or "")
    citations  = [c for c in _extract_cit_contexts(decoded) if c["year"] <= year_r]

    # ── Path A: citation scoring (author + year) ──────────────────────────────
    if citations:
        scored: list[dict] = []
        for cand in candidates:
            cand_doi    = cand.get("doi", "")
            cand_title  = cand.get("title", "") or ""
            cand_year   = int(cand.get("year") or 0)
            cand_snames = [s.lower() for s in (cand.get("all_authors") or []) if s]
            if not cand_snames:
                fa = (cand.get("first_author") or "").lower()
                if fa:
                    cand_snames = [fa]

            best_base = 0.0
            best_cit: dict | None = None
            for cit in citations:
                auth_sc = 2.0 if any(author_matches(sn, cand_snames) for sn in cit["surnames"]) else 0.0
                yr_sc   = 2.0 if cit["year"] == cand_year else (1.0 if abs(cit["year"] - cand_year) == 1 else 0.0)
                if auth_sc == 0.0 and yr_sc == 0.0:
                    continue
                if auth_sc + yr_sc > best_base:
                    best_base = auth_sc + yr_sc
                    best_cit  = cit

            if best_cit is None or best_base < 2.0:
                continue
            scored.append({"cand": cand, "citation": best_cit, "base_score": best_base,
                           "cand_doi": cand_doi, "cand_title": cand_title,
                           "cand_year": cand_year, "cand_snames": cand_snames})

        for entry in scored:
            entry["total"] = round(
                entry["base_score"] + jaccard_similarity(entry["cand_title"], decoded), 4)

        scored.sort(key=lambda x: x["total"], reverse=True)

        if scored:
            best   = scored[0]
            second = scored[1]["total"] if len(scored) > 1 else 0.0
            gap    = best["total"] - second
            if best["total"] >= 4.0 and gap >= 2.0:
                log.info("[%s] rule_based resolved (citation-context): %s score=%.2f gap=%.2f",
                         doi_r, best["cand_doi"], best["total"], gap)
                return {**base,
                        "resolved":          True,
                        "resolution_method": "citation_context_match",
                        "resolved_doi_o":    best["cand_doi"],
                        "resolved_title_o":  best["cand_title"],
                        "resolved_year_o":   best["cand_year"],
                        "resolved_author_o": best["cand_snames"][0] if best["cand_snames"] else "",
                        "resolution_score":  round(min(best["total"] / 8.0, 1.0), 4)}

    # ── Path B: same-author/year cluster — title Jaccard relative threshold ───
    # Fires when all candidates share one surname and one year and Path A's strict
    # threshold was not met.
    surnames = {(c.get("first_author") or "").lower().split()[-1] for c in candidates if c.get("first_author")}
    years    = {c.get("year") for c in candidates}
    if len(surnames) == 1 and len(years) == 1:
        context = decoded + " " + (study_r or "")
        by_title = sorted(candidates,
                          key=lambda c: jaccard_similarity(c.get("title", ""), context),
                          reverse=True)
        best_sc  = jaccard_similarity(by_title[0].get("title", ""), context)
        sec_sc   = jaccard_similarity(by_title[1].get("title", ""), context) if len(by_title) > 1 else 0.0
        if best_sc > 0.05 and best_sc >= sec_sc * 1.5:
            c = by_title[0]
            log.info("[%s] rule_based resolved (same-author/year Jaccard): %s score=%.4f",
                     doi_r, c.get("doi"), best_sc)
            return {**base,
                    "resolved":          True,
                    "resolution_method": "same_author_year_title_overlap",
                    "resolved_doi_o":    c.get("doi",          ""),
                    "resolved_title_o":  c.get("title",        ""),
                    "resolved_year_o":   c.get("year"),
                    "resolved_author_o": c.get("first_author", ""),
                    "resolution_score":  round(best_sc, 4)}

    return base


# The two rule methods that may never END the ladder, however unambiguous the paper's
# own text looks. Neither carries a semantic check the way Path A's citation score and
# the title-pattern rung do: the lone-candidate branch accepts what is left after the
# re-query, and Path B breaks a tie Path A refused to break on a ≥0.05 token overlap.
# run_for_doi holds them instead, so a call that can enumerate targets gets to confirm
# or contradict: the abstract LLM whenever the abstract carries an author-year pattern,
# the reference-list pick otherwise. When nothing enumerating ever speaks the pick is
# restored at the exit (`_exit`, and the post-full-text restore), so no resolution is
# lost — the row costs one extra cached LLM call, and possibly a PDF, to get there.
_HELD_ONLY_METHODS = frozenset({"single_candidate_after_requery",
                                "same_author_year_title_overlap"})


# Columns to pass through from the input row (no renaming). Only columns
# some consumer actually reads — the output dict is not a place to park a field
# nothing downstream looks at.
_OA_PASSTHROUGH = [
    "study_r", "abstract_r", "year_r", "author_year_pattern_r",
    "openalex_id_r", "match_source", "match_status",
    "doi_o", "study_o", "year_o", "ref_o", "ref_r", "url_r",
    "outcome_confidence", "outcome_reasoning",
    "pathway_source", "validation_status",
]


def clear_pipeline_caches(doi_r: str) -> list[str]:
    """
    Delete all intermediate caches for *doi_r* except the PDF file itself.

    Cleared: every content-keyed cache the pipeline writes for this DOI — the target
    calls ("llm"), the screen ("classify"), the reference-list target pick
    ("reftarget"), the outcome coding ("outcome"), the OpenAlex candidate pool and the
    parsed full text — plus the GROBID section cache. --force that leaves any of these
    behind does not force a re-decision, it just re-runs the stages around the cached
    answer.

    Returns a list of the filenames that were actually deleted.
    """
    key = cache_key(doi_r)
    # These caches are content-keyed, so one DOI can have several entries (abstract
    # stage vs full text, different candidate lists) — all of them go, or a re-run
    # reads back the answer this call was meant to discard.
    deleted: list[str] = []
    for prefix in ("llm", "classify", "reftarget", "outcome"):
        deleted += clear_content_keys(LLM_CACHE_DIR, prefix, doi_r)
    deleted += clear_content_keys(OA_CACHE_DIR, "candidates", doi_r)
    targets = [GROBID_CACHE_DIR / f"{key}.json",
               PARSE_CACHE_DIR / f"parse_{key}.json"]
    targets += GROBID_CACHE_DIR.glob(f"{key}_direct_refs_*.json")
    targets += GROBID_CACHE_DIR.glob(f"{key}_img_refs_*.json")
    for path in targets:
        if path.exists():
            try:
                path.unlink()
                deleted.append(path.name)
            except Exception as e:
                log.warning("Could not delete cache %s: %s", path, e)
    return deleted


def _write_parse_cache(doi_r: str, parse_results: dict) -> None:
    """Persist parse_all results to PARSE_CACHE_DIR so run_extract._save_parse_cache() skips re-parsing.

    An all-empty parse is never written, and an all-empty cache left by an earlier
    PDF-less run is overwritten rather than preserved (audit B4).
    """
    out_file = PARSE_CACHE_DIR / f"parse_{cache_key(doi_r)}.json"
    if parse_result_is_empty(parse_results):
        return
    if out_file.exists():
        try:
            with out_file.open(encoding="utf-8") as fh:
                if not parse_result_is_empty(json.load(fh)):
                    return
        except Exception:
            return
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(out_file, parse_results, indent=2)
    except Exception as exc:
        log.debug("[%s] _write_parse_cache failed: %s", doi_r, exc)


def _cands_row(doi_r: str, cands_df: pd.DataFrame) -> dict:
    """Return openalex_candidates pass-through fields for *doi_r*."""
    out = {c: "" for c in _OA_PASSTHROUGH}
    if cands_df is None or cands_df.empty:
        return out
    matches = cands_df[cands_df["doi_r"].apply(clean_doi) == clean_doi(doi_r)]
    if matches.empty:
        return out
    row = matches.iloc[0]
    for col in _OA_PASSTHROUGH:
        out[col] = str(row.get(col, "") or "")
    return out


def _best_parse_result(parse_results: dict[str, dict]) -> dict:
    """Return the parse result with the richest content.

    Delegates to shared.pdf_parsing.best_parse_result so the scoring formula
    is identical to what the UI badge and _get_outcome use — one source of truth.
    Falls back to grobid (or first available result) if all methods errored.
    """
    best = _best_parse_shared(parse_results)
    if best is not None:
        return best
    return parse_results.get("grobid", next(iter(parse_results.values())))


def _unresolved(method: str, **extra) -> dict:
    """The resolver dict for "no original identified", in the shape every resolver returns.

    Giving every producer the same key set is what lets _build_output and _merge_row
    read the result without .get()-defending against a different shape per stage.
    """
    return {
        "resolved":          False,
        "resolution_method": method,
        "resolved_doi_o":    "",
        "resolved_title_o":  "",
        "resolved_year_o":   None,
        "resolved_author_o": "",
        "resolved_study_o":  "",
        "resolution_score":  0.0,
        **extra,
    }


def _first_author(authors) -> str:
    if isinstance(authors, list):
        return str(authors[0]).strip() if authors else ""
    return str(authors or "").split(",")[0].strip()


def _search_title_for_original(doi_r: str, target_desc: str,
                               study_r: str) -> "dict | None":
    """Resolve a target the screen named but could not match to a reference.

    target_desc is however the abstract referred to the replicated study, so it may
    carry authors and a year around the title. Both searches confirm the hit by
    title Jaccard, so the surrounding words cost recall rather than precision.
    Returns a resolver dict, or None when neither source gives a confident hit.
    """
    for search in (_search_crossref_by_title, _search_openalex_by_title):
        hit = search(target_desc)
        if not hit:
            continue
        doi_o = clean_doi(hit.get("doi", "") or "")
        # A replication whose title echoes the original's makes the original's own
        # record and the replication easy to confuse; never link a paper to itself.
        if not doi_o or doi_o == clean_doi(doi_r):
            continue
        if jaccard_similarity(hit.get("title", ""), study_r) > 0.9:
            continue
        return {
            "resolved":          True,
            "resolution_method": "llm_title_search_prepdf",
            "resolved_doi_o":    doi_o,
            "resolved_title_o":  hit.get("title", ""),
            "resolved_year_o":   hit.get("year"),
            # CrossRef returns authors as a string, OpenAlex as a list.
            "resolved_author_o": _first_author(hit.get("authors")),
            "resolution_score":  1.0,
        }
    return None


# What a target-identifying stage produced, as it has to survive to the row. Three
# ladder stages can enumerate targets and then fail to resolve a single link; without
# this the list died with the stage and the row was written target_pending with no
# record that anything had been found. The three attribution fields travel with the
# list: a row whose originals came from a stage the ladder later left behind still has
# to name the model that found them, or it cannot be reviewed.
_TARGET_KEYS = ("targets", "multi_target", "unidentified_count", "target_stage",
                "resolved_study_r", "llm_model", "llm_source", "llm_evidence")


def _enumerated(answer: dict) -> bool:
    """Whether a target-identifying call actually answered, as opposed to failing.

    target_stage is set by identify_targets_with_llm before it decides anything, so
    it is present on every answer — including a decline — and absent on a provider
    failure and on a rung whose target call never ran at all. That distinction is what
    the gate turns on: a withheld rule pick may only be overruled by a call that spoke.
    """
    return bool(answer.get("target_stage"))


def _certain_targets(answer: dict) -> list[dict]:
    """The targets an answer matched to a record and stands behind."""
    return [t for t in (answer.get("targets") or [])
            if t.get("match_certain") and t.get("record")]


def _norm(text) -> str:
    """A title/name reduced to comparable form: lowercase, single-spaced, no padding."""
    return " ".join(str(text or "").lower().split())


def _agrees_with_held(target: dict, held: dict) -> bool:
    """Whether a single enumerated target names the same work the gate withheld.

    Compared on the DOI when both have one; a reference parsed out of a PDF often has
    none, so the fallback is the identity the deterministic rule matched on — title,
    year and first author together, never the title alone.
    """
    record   = target.get("record") or {}
    doi      = clean_doi(str(record.get("doi") or ""))
    held_doi = clean_doi(str(held.get("resolved_doi_o") or ""))
    if doi and held_doi:
        return doi == held_doi
    title = _norm(record.get("title"))
    return bool(title) and (
        title == _norm(held.get("resolved_title_o"))
        and str(record.get("year") or "") == str(held.get("resolved_year_o") or "")
        and _norm(record.get("first_author")) == _norm(held.get("resolved_author_o")))


def _union_targets(*lists: list) -> list[dict]:
    """Every distinct target across several answers, in the order first seen.

    Deduplicated by @key first and by the mapped record's DOI second: the same work
    reached through two rungs carries two keys (the namespaces are per call), and
    writing it twice would give two rows one pair_id.
    """
    seen_keys: set = set()
    seen_dois: set = set()
    merged: list[dict] = []
    for target in [t for group in lists for t in (group or [])]:
        key = target.get("key")
        doi = clean_doi(str((target.get("record") or {}).get("doi") or ""))
        if (key and key in seen_keys) or (doi and doi in seen_dois):
            continue
        if key:
            seen_keys.add(key)
        if doi:
            seen_dois.add(doi)
        merged.append(target)
    return merged


def _as_target(resolution: dict) -> dict:
    """An accepted single link expressed as a target entry, so it can join a list."""
    return {
        "key":             None,
        "match_certain":   True,
        "target_as_named": str(resolution.get("resolved_title_o") or ""),
        "study_numbers":   str(resolution.get("resolved_study_o") or ""),
        "replication_study_numbers": str(resolution.get("resolved_study_r") or ""),
        "evidence_quote":  str(resolution.get("llm_evidence") or ""),
        "record": {"doi":          str(resolution.get("resolved_doi_o") or ""),
                   "title":        str(resolution.get("resolved_title_o") or ""),
                   "first_author": str(resolution.get("resolved_author_o") or ""),
                   "year":         resolution.get("resolved_year_o"),
                   "openalex_id":  ""},
    }


def run_for_doi(doi_r:              str,
                cands_df:           Optional[pd.DataFrame] = None,
                force:              bool = False,
                no_llm:             bool = False,
                no_pdf:             bool = False,
                classification:     Optional[dict] = None) -> dict:
    """
    Run the full disambiguation pipeline for *doi_r*.

    force=True clears all intermediate caches (LLM, GROBID, OpenAlex candidates)
    before running, but keeps the cached PDF so the download step is skipped.

    classification is the "is this a replication at all" verdict from
    classify_replication(). That screen belongs to Stage 2: run_extract reads the
    verdict off the input row and passes it in, so Stage 4.5 picks the target without
    voting again. A caller with no verdict (a batch tool, or run_extract's explicit
    --screen-here opt-in) leaves it None and the screen votes inside Stage 4.5.

    The ladder runs cheapest-first and returns at the first stage that resolves, so
    full-text acquisition is a last resort rather than the normal path:
      1.   Load the input-row data for this DOI
      2.   Re-query OpenAlex for candidate originals (from referenced_works)
      2.5  Title-pattern resolver ("A Replication of X" vs the candidate titles)
      3.   Rule-based resolver (citation context, same-author/year title overlap,
           lone candidate) — the last two are only ever held, never terminal
      4.   Abstract-level LLM over the candidates, when the abstract cites anyone
      4.5  Reference-list target pick, which also carries the screen verdict:
           an incomplete screen exits as target_pending/api_error, and a "discard"
           exits without a PDF as not_a_replication
      4.6  Title search on a target the screen named but could not match to a
           reference — gated on both voters qualifying AND confident, provisional
      5.   PDF acquisition (eleven tiers, 0–10 in acquire_pdf); no document →
           target_pending
      6.   parse cache, else parse_all over six parsers; richest result wins
      7.   Full-text LLM identification

    Returns a flat dict with all output columns.
    """
    doi_r = clean_doi(doi_r)

    if force:
        deleted = clear_pipeline_caches(doi_r)
        if deleted:
            log.info("[%s] Force rerun — cleared caches: %s", doi_r, ", ".join(deleted))

    # ── Stage 1: base data ───────────────────────────────────────────────────
    cands_row = _cands_row(doi_r, cands_df)

    study_r   = cands_row.get("study_r",   "")
    abstract_r = cands_row.get("abstract_r", "")
    pattern_r  = cands_row.get("author_year_pattern_r", "")
    oa_id_r    = cands_row.get("openalex_id_r", "")

    try:
        year_r = int(cands_row.get("year_r") or 2099)
    except (ValueError, TypeError):
        year_r = 2099

    # ── Stage 2: OpenAlex re-query ───────────────────────────────────────────
    candidates = find_all_candidates(
        doi_r, oa_id_r, study_r, abstract_r, year_r, pattern_r
    )
    # None is the reference list never arriving, which is not the same row as one
    # whose references hold no match: the rungs below run on no candidates either
    # way, but only one of them has been told the paper cites nothing relevant.
    if candidates is None:
        log.warning("[%s] OpenAlex never answered with a reference list — "
                    "continuing with no candidates", doi_r)
        candidates = []
    else:
        log.info("[%s] %d candidate(s) from OpenAlex re-query", doi_r, len(candidates))

    # Every exit from here on assembles the same output from the same base data;
    # only the resolution (and, past the PDF stage, the pdf/grobid/sections blocks)
    # differ, so bind the four constant arguments once.
    emit = partial(_build_output, doi_r, cands_row, candidates)

    # A deterministic stage may only END the row when the paper's own text rules out a
    # second target; otherwise its pick is WITHHELD — until a call that can enumerate
    # targets has spoken. If none ever does, nothing has contradicted the rule and the
    # pick stands: every exit below goes through _exit(), which restores it.
    # --no-llm has nothing that could ever enumerate, so withholding there buys no
    # information and costs a PDF download per rule-resolved row: the rule may stop.
    # The gate is necessary but not sufficient: the two rule methods in
    # _HELD_ONLY_METHODS are held whatever it says, because they carry no semantic
    # check at all — only Path A's citation score and the title-pattern rung may stop.
    may_stop = no_llm or may_stop_at_a_rule(study_r, abstract_r, year_r)
    held: dict = {}          # a deterministic pick the gate withheld
    seen: dict = {}          # the richest target answer any stage produced
    seen_certain = 0         # how many targets that answer matched and stands behind

    def _keep(answer: dict) -> None:
        """Record an enumerating answer, keeping the one that named the most originals.

        Replacing it with whatever spoke last dropped a rung's two certain targets the
        moment a later rung answered about one — the reference lists the rungs read are
        not the same list, and the shorter answer is not the newer truth.
        """
        nonlocal seen, seen_certain
        certain = len(_certain_targets(answer))
        if answer.get("targets") and certain >= seen_certain:
            seen = {k: v for k, v in answer.items() if k in _TARGET_KEYS}
            seen_certain = certain

    def _uncontradicted() -> bool:
        """Whether the withheld pick still stands against everything that enumerated.

        It stands when nothing enumerated at all, and when the one thing that did named
        at most one original and that original is the same work. It does NOT stand
        against two targets, nor against one target that names a different work — those
        are the answers the gate was waiting for. A single target that cannot be
        compared (no mapped record, or a record the identity check cannot match) counts
        as a contradiction too: an enumerator spoke and named something we cannot show
        is the held work, so restoring would overrule it on no evidence.
        """
        if not seen:
            return True
        targets = seen.get("targets") or []
        return (len(targets) <= 1
                and all(_agrees_with_held(t, held) for t in targets))

    def _exit(resolution: dict, pdf: dict = {}, grobid: dict = {},
              sections: dict = {}) -> dict:
        """An exit with no accepted link — restoring a withheld pick if nothing enumerated.

        --no-llm, --no-pdf, no document and no context all end the row without any call
        that could have found a second target. Before the gate they were unreachable for
        a rule-resolved paper, because the rule had already returned. Dropping the pick
        here would turn a configuration choice into a lost resolution, which the
        error-handling rule forbids; whatever the exit had to report travels with the
        restored row.

        An incomplete screen is NOT one of these and does not come through here: there
        the provider failure is what prevented the enumerating call, so the pick stays
        withheld and a re-run settles it.
        """
        if held and _uncontradicted():
            log.info("[%s] gate: restoring the withheld %s pick — nothing that could "
                     "enumerate targets contradicted it (%s)", doi_r,
                     held["resolution_method"], resolution.get("resolution_method"))
            return emit({**held,
                         "llm_error": str(resolution.get("llm_error", "") or ""),
                         "llm_reasoning": " | ".join(filter(None, [
                             str(held.get("llm_reasoning", "") or ""),
                             str(resolution.get("llm_reasoning", "") or "")])),
                         }, pdf, grobid, sections)
        return emit({**resolution, **seen}, pdf, grobid, sections)

    def _exit_resolved(resolution: dict, pdf: dict = {}, grobid: dict = {},
                       sections: dict = {}) -> dict:
        """An exit with one accepted link — unless an earlier call saw more originals.

        A later rung reads a different evidence block, so it can settle on ONE original
        for a paper an earlier successful call already saw two in. Returning that single
        link drops the other; the row is emitted unresolved with the union instead, and
        the per-target adapter writes every original including this one.
        """
        if seen_certain >= 2:
            merged = _union_targets(seen.get("targets"),
                                    _certain_targets(resolution) or [_as_target(resolution)])
            log.info("[%s] %s resolved one original, but an earlier call named %d — "
                     "writing every target instead", doi_r,
                     resolution.get("resolution_method"), seen_certain)
            return emit({**_unresolved("llm_multi_target"), **seen,
                         "targets":      merged,
                         "multi_target": True,
                         "llm_reasoning": resolution.get("llm_reasoning", ""),
                         }, pdf, grobid, sections)
        return emit(resolution, pdf, grobid, sections)

    # ── Stage 2.5: Title-pattern resolver ─────────────────────────────────────
    # Runs before citation scoring and before any LLM call.
    title_pat = _resolve_by_title_pattern(doi_r, study_r, candidates)
    if title_pat and title_pat.get("resolved"):
        if may_stop:
            return emit(title_pat, {}, {}, {})
        held = held or title_pat
        log.info("[%s] gate: %s withheld — the paper's text does not rule out a second "
                 "target", doi_r, title_pat["resolution_method"])
    # ── Stage 3: Rule-based resolver (citation-context + same-author/year) ──────
    stage3 = _resolve_rule_based(doi_r, abstract_r, candidates, year_r, study_r)
    if stage3["resolved"]:
        log.info("[%s] Resolved rule-based (%s): %s", doi_r,
                 stage3["resolution_method"], stage3["resolved_title_o"])
        # A _HELD_ONLY_METHODS pick never ends the row while an enumerating call can
        # still run. Under --no-llm none can, and holding there would only buy a PDF
        # download per rule-resolved row, so the rule stops exactly as before.
        may_stop_here = no_llm or (
            may_stop and stage3["resolution_method"] not in _HELD_ONLY_METHODS)
        if may_stop_here:
            return emit(stage3, {}, {}, {})
        held = held or stage3
        log.info("[%s] gate: %s withheld — the paper's text does not rule out a second "
                 "target", doi_r, stage3["resolution_method"])

    # ── Stage 4: Abstract-level LLM ──────────────────────────────────────────
    if not no_llm:
        abstract_patterns = extract_author_year_patterns(abstract_r, max_year=year_r)
        distinct_pairs    = {(p["surname"], p["year"]) for p in abstract_patterns}

        if abstract_r and distinct_pairs:
            log.info("[%s] Abstract has %d author-year patterns — early abstract LLM", doi_r, len(distinct_pairs))
            token_counter.set_stage("extract_abstract")
            # The real doi_r goes in: identify_targets_with_llm uses it as the
            # exclude_doi for its title search, and a suffixed one never matches the
            # paper's own DOI, so the "never link a paper to itself" guard could not
            # fire on this path. abstract_only is already part of the cache key, so
            # the abstract-stage and full-text answers stay separate without it.
            llm4 = identify_targets_with_llm(
                doi_r, study_r, abstract_r, candidates, [],
                abstract_only=True,
            )
            _keep(llm4)
            if llm4["resolved"]:
                log.info("[%s] Resolved by abstract LLM: %s", doi_r,
                         llm4["resolved_title_o"])
                return _exit_resolved(llm4)

    # ── Stage 4.5: Reference-list target pick ────────────────────────────────
    # Stage 3/4 can only fire when the abstract carries a parseable "(Author, Year)"
    # citation; without one, candidates is empty and every row would drop to the PDF
    # route. The referenced works are still available, so ask the LLM which reference
    # is the target. The "is this a replication at all" verdict normally arrives from
    # Stage 3's front door and is only voted here when a caller supplies none; the
    # branches below still handle every verdict, because run_for_doi is also called
    # from the batch tools, which have no front door.
    if not no_llm and (abstract_r or study_r):
        # No references is not a reason to skip: with them the call both screens and
        # resolves, without them it still answers "is this a replication at all".
        # None from either source is that source failing to answer, not a paper
        # with no references. The screen still runs on nothing — it can say "not a
        # replication" from the abstract alone — but the row must not read
        # afterwards as one whose reference list was searched and came up short.
        oa_refs: list[dict] | None = fetch_referenced_works_metadata(oa_id_r) if oa_id_r else []
        oc_refs: list[dict] | None = []
        refs = list(oa_refs or [])
        if not refs:
            oc_refs = fetch_opencitations_references(doi_r)
            refs = list(oc_refs or [])
        refs_unavailable = not refs and (oa_refs is None or oc_refs is None)
        token_counter.set_stage("extract_refscreen")
        screen = screen_references_with_llm(doi_r, study_r, abstract_r, refs,
                                            classification=classification,
                                            candidates=candidates)
        # The evidence of this rung IS the reference list, so it has to reach
        # _build_output; the screen dict carries the verdict, not the input.
        ref_sections = {"references": refs}
        if refs_unavailable:
            failed = ", ".join(name for name, got in
                               (("OpenAlex", oa_refs), ("OpenCitations", oc_refs))
                               if got is None)
            note = f"reference list unavailable ({failed} lookup failed)"
            log.warning("[%s] %s — screened without one", doi_r, note)
            screen["llm_evidence"] = "; ".join(filter(None, [
                note, screen.get("llm_evidence", ""),
            ]))
        _keep(screen)

        # A screen that did not get both votes is an API failure, not a verdict, and
        # must be caught before the gate below — a lone surviving vote is not a
        # decision. One vote → target_pending, so a re-run can screen the row once
        # the provider is back; no votes → api_error.
        if screen["resolution_method"] in {"llm_refscreen_partial", "llm_refscreen_failed"}:
            log.warning("[%s] Reference screen incomplete (%s): %s", doi_r,
                        screen["resolution_method"], screen.get("llm_error", ""))
            # Deliberately NOT through _exit(): the failure is what stopped the
            # reference-list target pick from running, so nothing here has earned the
            # right to settle a withheld pick. Restoring would read "we never asked" as
            # "we asked and nothing contradicted it". The row goes out unresolved and a
            # re-run does the enumeration once the provider answers.
            return emit({**screen, **seen}, {}, {}, ref_sections)

        # The gate is screen_gate(), defined once in shared/llm_client.py. The full
        # screen dict is the resolution so the discarded row still carries the models
        # that voted, their evidence and their reasoning — it is set aside for review,
        # and a row with no attribution is not reviewable.
        if screen.get("screen_verdict") == "discard":
            verdicts = "; ".join(
                f"{v['provider']}={v['classification']}/"
                f"{'confident' if v['confident'] else 'unconfident'}"
                for v in screen.get("votes", []))
            log.info("[%s] Reference screen: discard (%s) — skipping PDF", doi_r, verdicts)
            discard = emit({**screen, "resolution_method": "llm_not_a_replication"},
                           {}, {}, ref_sections)
            # _merge_row reads the row's link_evidence from llm_evidence, so set that.
            discard["llm_evidence"] = "; ".join(filter(None, [
                f"screen discard: {verdicts}" if verdicts else "screen discard",
                screen.get("llm_evidence", ""),
            ]))
            return discard

        if screen["resolved"]:
            log.info("[%s] Resolved from reference list: %s", doi_r,
                     screen["resolved_title_o"])
            return _exit_resolved(screen, {}, {}, ref_sections)

        # ── Stage 4.6: Title search on a named-but-unmatched target ──────────
        # The screen can recognise the target in the abstract yet fail to match it
        # to a reference — OpenAlex reference lists are frequently short or empty
        # (one sampled paper names its target and has a single reference). Searching
        # the named title is far cheaper than acquiring and parsing the PDF, and the
        # same search already runs after the PDF stage as llm_title_search.
        #
        # Gated on both voters giving a qualifying answer AND both standing behind
        # it. This is the one resolver that picks from the whole literature rather
        # than a supplied candidate list, and the errors it makes are systematic: a
        # paper that is not a replication at all gets confidently linked to a
        # landmark it merely cites. The result is still written as provisional
        # (link_method llm_title_search, link_confidence low, no outcome coded); the
        # gate decides whether to spend the two searches at all.
        target_desc = screen.get("target_description", "")
        votes = screen.get("votes", [])
        both_sure = (len(votes) == 2
                     and all(v["classification"] in SCREEN_QUALIFYING and v["confident"]
                             for v in votes))
        if target_desc and both_sure:
            hit = _search_title_for_original(doi_r, target_desc, study_r)
            if hit:
                log.info("[%s] Resolved by pre-PDF title search: %s", doi_r,
                         hit["resolved_title_o"])
                return _exit_resolved(hit, {}, {}, ref_sections)

    # ── Stage 5: PDF acquisition ─────────────────────────────────────────────
    if no_pdf:
        # Stages 2.5/3/4 didn't resolve — bail out without fulltext.
        log.info("[%s] no_pdf mode — abstract/rules insufficient, writing target_pending", doi_r)
        return _exit(_unresolved("needs_fulltext"))

    pdf = acquire_pdf(doi_r, study_r, openalex_id=oa_id_r)
    log.info("[%s] PDF: %s (%s)", doi_r, pdf["pdf_source"], pdf["pdf_url"])

    pdf_path       = Path(pdf["pdf_path"]) if pdf.get("pdf_path") else None
    oa_xml_content = pdf.get("openalex_xml")

    # Acquisition fails outright for most rows that get this far (16 of 26 attempts
    # in a sampled run returned pdf_source="none"). With neither a PDF nor OpenAlex
    # XML there is no full text to parse, so running the six-parser stack yields
    # empty sections and the LLM is asked to name an original from nothing — which
    # is exactly how a confident, fabricated doi_o gets produced. Stop here instead.
    #
    # A content-free XML result is no document: every OpenAlex XML result cached
    # before 2026-08 was an empty shell, and because a shell is truthy this guard
    # waved it through and the row was stamped llm_fulltext with no full text behind
    # it. openalex_xml_has_content() is what "we have a document" means here.
    if oa_xml_content and not openalex_xml_has_content(oa_xml_content):
        log.warning("[%s] OpenAlex XML is content-free (no sections, no references) "
                    "— treating it as no document", doi_r)
        oa_xml_content = None
        if pdf.get("pdf_source") == "openalex_xml":
            pdf = {**pdf, "pdf_source": "none"}

    if pdf_path is None and not oa_xml_content:
        log.info("[%s] no document acquired (%s) — writing target_pending",
                 doi_r, pdf.get("pdf_source", "none"))
        return _exit(_unresolved("no_fulltext_available"), pdf)

    # ── Stage 6: Parse all — pick richest result to send to LLM ─────────────
    # The parse cache was write-only here: every run re-ran all six parsers over a
    # document whose parse was already on disk, and the only reader was run_extract.
    # A hit is the same dict this call would have produced (an empty or transient-
    # failure cache reads as a miss — read_parse_cache's job).
    parse_results = read_parse_cache(doi_r, PARSE_CACHE_DIR)
    if parse_results is None:
        parse_results = _parse_all(doi_r, pdf_path, oa_xml=oa_xml_content, no_llm=no_llm)
        _write_parse_cache(doi_r, parse_results)
    else:
        log.debug("[%s] parse cache hit — six parsers skipped", doi_r)

    for method, r in parse_results.items():
        log.debug("[%s]   parse:%s refs=%d abstract=%d intro=%d error=%s",
                  doi_r, method, len(r.get("references") or []),
                  len(r.get("abstract") or ""), len(r.get("intro") or ""),
                  r.get("error"))

    best     = _best_parse_result(parse_results)
    best_src = best.get("source", "unknown")
    best_refs = best.get("references") or []
    log.info("[%s] parse_all best=%s refs=%d abstract=%d intro=%d",
             doi_r, best_src, len(best_refs),
             len(best.get("abstract") or ""), len(best.get("intro") or ""))

    sections = {
        "abstract":   best.get("abstract") or "",
        "intro":      best.get("intro")    or "",
        "methods":    "",
        "references": best_refs,
    }
    # A parse can carry the whole body and still have no section split: OpenAlex's
    # TEI lost its <head> elements to an HTML round-trip, so parse_tei_sections has
    # nothing to divide the text by and returns it whole in raw_text. Without this
    # the recovered text reached nothing — build_target_prompt only ever reads
    # abstract/intro/methods, so a document with body text but no abstract and no
    # references passed the "we have a document" guard and was then dropped as
    # no_context. Treat that body the way a PDF's raw text is treated and open the
    # INTRODUCTION block with it: the front of a paper is where it says what it is
    # re-testing, which is what this prompt asks about. Sliced here at the size
    # build_target_prompt sends, so the row stores exactly what the model read.
    if not sections["intro"] and not sections["methods"]:
        raw = str(best.get("raw_text") or "").strip()
        if raw:
            sections["intro"] = raw[:TARGET_INTRO_CHARS]
    # build_target_prompt sends the PDF abstract only as the tail the OpenAlex abstract
    # does not already carry — often "" when the two agree. Record that tail so the row
    # shows the evidence the model was given rather than the section it came from.
    sections["abstract_sent"] = _abstract_tail(abstract_r, sections["abstract"])
    grobid = {
        "grobid_status": f"parse_all:{best_src}",
        # The winning parser, on its own so a row can say which method produced the
        # text the LLM read without a reader parsing grobid_status apart.
        "parse_method":  best_src,
        "n_refs_parsed": len(best_refs),
        "sections":      sections,
    }

    # ── Stage 7: LLM identification ──────────────────────────────────────────
    if no_llm:
        log.info("[%s] no_llm mode — skipping LLM, writing target_pending", doi_r)
        return _exit(_unresolved("none", llm_error="no_llm mode"),
                     pdf, grobid, sections)

    # Guard: refuse to call the LLM when it would have nothing to reason from.
    _has_context = (
        abstract_r
        or candidates
        or (sections.get("intro") or "")
        or (sections.get("references") or [])
    )
    if not _has_context:
        log.warning("[%s] No context — skipping LLM, writing target_pending", doi_r)
        return _exit(_unresolved(
            "no_context",
            llm_error="no_context: abstract missing, PDF unavailable, no refs"),
            pdf, grobid, sections)

    token_counter.set_stage("extract_fulltext")
    llm = identify_targets_with_llm(
        doi_r, study_r, abstract_r, candidates, sections.get("references") or [],
        pdf_abstract   = sections.get("abstract", ""),
        intro          = sections.get("intro",    ""),
        methods        = sections.get("methods",  ""),
    )
    log.info("[%s] LLM: resolved=%s source=%s", doi_r,
             llm["resolved"], llm["llm_source"])
    _keep(llm)

    if held and not llm["resolved"] and _enumerated(llm) and _uncontradicted():
        # The call the gate was waiting for ran, and it did not contradict the rule:
        # it named nothing, or it named the same work. A provider failure is NOT that
        # answer — it goes out as llm_failed so a re-run can ask again, rather than
        # freezing an unconfirmed rule pick into a resolved row.
        log.info("[%s] gate: restoring the withheld %s pick — the target prompt did "
                 "not contradict it", doi_r, held["resolution_method"])
        return emit({**held,
                     "llm_error":     str(llm.get("llm_error", "") or ""),
                     "llm_reasoning": " | ".join(filter(None, [
                         str(held.get("llm_reasoning", "") or ""),
                         str(llm.get("llm_reasoning", "") or "")])),
                     }, pdf, grobid, sections)

    if llm["resolved"]:
        return _exit_resolved(llm, pdf, grobid, sections)
    return emit({**llm, **seen}, pdf, grobid, sections)


# ── Output builder ────────────────────────────────────────────────────────────

def _build_output(doi_r:     str,
                  cands_row: dict,
                  candidates: list[dict],
                  resolution: dict,
                  pdf:        dict,
                  grobid:     dict,
                  sections:   dict) -> dict:
    """Assemble the flat output dict from all pipeline stage results."""
    import json

    return {
        # ── Input ─────────────────────────────────────────────────────────────
        "doi_r"                 : doi_r,

        # ── openalex_candidates pass-through ──────────────────────────────────
        **{c: cands_row.get(c, "") for c in _OA_PASSTHROUGH},

        # ── OpenAlex re-query ─────────────────────────────────────────────────
        "n_candidates"          : len(candidates),
        "all_candidates_json"   : json.dumps(candidates, ensure_ascii=False),

        # ── PDF ───────────────────────────────────────────────────────────────
        "pdf_url"               : pdf.get("pdf_url",    ""),
        "pdf_source"            : pdf.get("pdf_source", "none"),
        "pdf_path"              : pdf.get("pdf_path",   ""),
        "pdf_ok"                : bool(pdf.get("pdf_ok", False)),
        "pdf_url_tried"         : json.dumps(pdf.get("pdf_url_tried", []),
                                             ensure_ascii=False),

        # ── GROBID ────────────────────────────────────────────────────────────
        "grobid_status"         : grobid.get("grobid_status", "not_attempted"),
        # Which of the six parsers won best_parse_result() — blank on every exit
        # that never parsed anything, which is what the row's parse_method records.
        "parse_method"          : grobid.get("parse_method", ""),
        "n_grobid_refs"         : grobid.get("n_refs_parsed",  0),
        # Stored at the sizes build_target_prompt sends, so a reviewer reads exactly
        # what the model read. The PDF abstract reaches the model only as the part the
        # OpenAlex abstract does not already carry, which is often nothing at all —
        # run_for_doi puts that tail in "abstract_sent". The reference list is sent
        # whole but stored truncated (full lists run to hundreds of entries);
        # n_references_sent keeps the dashboard honest about how much was left out.
        "grobid_abstract"       : sections.get("abstract_sent", "") or "",
        "grobid_intro"          : (sections.get("intro",    "")
                                   or "")[:TARGET_INTRO_CHARS],
        # run_for_doi never populates methods today (the parsers do not split it out),
        # so this slice is a contract for a section nothing currently supplies.
        "grobid_methods"        : (sections.get("methods",  "")
                                   or "")[:TARGET_METHODS_CHARS],
        "grobid_refs_json"      : json.dumps(
                                      (sections.get("references", []) or [])[:25],
                                      ensure_ascii=False),
        # What the prompt renders, not what the parser returned: a reference that is
        # also a candidate is shown once, in the candidate block, and one with neither
        # a title nor a DOI is dropped before the list is written.
        "n_references_sent"     : len(rendered_reference_entries(
                                      assign_target_keys(
                                          candidates,
                                          sections.get("references", []) or [])[0])),

        # ── Resolution ────────────────────────────────────────────────────────
        "resolution_method"     : resolution.get("resolution_method", "none"),
        "resolution_score"      : round(float(
                                      resolution.get("resolution_score", 0) or 0
                                  ), 4),
        "resolved_doi_o"        : resolution.get("resolved_doi_o",   ""),
        "resolved_title_o"      : resolution.get("resolved_title_o", ""),
        "resolved_year_o"       : resolution.get("resolved_year_o"),
        "resolved_author_o"     : resolution.get("resolved_author_o", ""),
        "resolved_study_o"      : resolution.get("resolved_study_o",  ""),
        "resolved_study_r"      : resolution.get("resolved_study_r",  ""),
        # Whether this row carries a single accepted link. run_extract routes on it:
        # a stage that named targets without accepting one of them is what the
        # per-target adapter exists for.
        "resolved"              : bool(resolution.get("resolved", False)),
        # The whole target answer, records included, so run_extract can write one row
        # per original rather than discarding everything past the first.
        "targets"               : resolution.get("targets", []) or [],
        "target_stage"          : resolution.get("target_stage", ""),
        "unidentified_count"    : int(resolution.get("unidentified_count") or 0),
        "multi_target"          : bool(resolution.get("multi_target", False)),
        "n_targets"             : len(resolution.get("targets", []) or []),

        # ── LLM ───────────────────────────────────────────────────────────────
        "llm_source"            : resolution.get("llm_source",     ""),
        "llm_model"             : resolution.get("llm_model",      ""),
        "llm_confidence"        : resolution.get("llm_confidence", ""),
        "llm_evidence"          : resolution.get("llm_evidence",   ""),
        "llm_reasoning"         : resolution.get("llm_reasoning",  ""),
        "llm_prompt"            : resolution.get("llm_prompt",     ""),
        "llm_error"             : resolution.get("llm_error",      ""),
    }
