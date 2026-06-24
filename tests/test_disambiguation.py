"""
Tests for shared/disambiguation.py — Jaccard thresholds and heuristics.

All cases are hand-crafted; no API calls are made.
Run: python -m pytest tests/test_disambiguation.py -v
"""
import pytest

from shared.disambiguation import (
    jaccard_similarity,
    resolve_same_author_year,
    resolve_by_grobid_refs,
)


# ── jaccard_similarity ────────────────────────────────────────────────────────

class TestJaccardSimilarity:
    def test_identical_strings_return_1(self):
        assert jaccard_similarity("ego depletion Baumeister", "ego depletion Baumeister") == 1.0

    def test_empty_string_returns_0(self):
        assert jaccard_similarity("", "some text") == 0.0
        assert jaccard_similarity("some text", "") == 0.0

    def test_no_overlap_returns_0(self):
        assert jaccard_similarity("apple banana cherry", "dog elephant frog") == 0.0

    def test_partial_overlap(self):
        sim = jaccard_similarity("ego depletion Baumeister 1998", "ego depletion original")
        assert 0.0 < sim < 1.0

    def test_short_words_excluded(self):
        # Words < 3 chars are excluded from the token set ("a", "of", etc.)
        sim = jaccard_similarity("a of to dog", "a of to cat")
        assert sim == 0.0  # only "dog" and "cat" qualify; no overlap

    def test_case_insensitive(self):
        assert jaccard_similarity("EGO DEPLETION", "ego depletion") == 1.0


# ── resolve_same_author_year ──────────────────────────────────────────────────

class TestResolveSameAuthorYear:
    def _cand(self, doi, title, year="2010", author="Smith"):
        return {"doi": doi, "title": title, "year": year, "first_author": author}

    def test_single_candidate_resolves_immediately(self):
        cands = [self._cand("10.1000/a", "Ego Depletion and Self-Control", "1998", "Baumeister")]
        result = resolve_same_author_year(
            "10.9999/rep",
            "A Replication of Baumeister 1998",
            "We replicated Baumeister (1998) exactly.",
            cands,
        )
        assert result["resolved"] is True
        assert result["resolved_doi_o"] == "10.1000/a"
        assert result["resolution_method"] == "single_candidate_after_requery"
        assert result["resolution_score"] == 1.0

    def test_clear_match_high_overlap(self):
        """Best candidate has clear title overlap with the replication context."""
        cands = [
            self._cand("10.1000/a", "Ego Depletion and Self-Control", "1998"),
            self._cand("10.1000/b", "Social Facilitation Studies", "1998"),
        ]
        result = resolve_same_author_year(
            "10.9999/rep",
            "Replication of Ego Depletion Self-Control Study",
            "We replicated Ego Depletion and Self-Control (Smith, 1998).",
            cands,
        )
        # Jaccard(A, context) ≈ 0.56, Jaccard(B, context) = 0 → clear winner
        assert result["resolved"] is True
        assert result["resolved_doi_o"] == "10.1000/a"

    def test_near_miss_context_gives_zero_scores(self):
        """When context has no word overlap with either candidate, no winner."""
        cands = [
            self._cand("10.1000/a", "Study One Topic Alpha Beta Gamma", "2010"),
            self._cand("10.1000/b", "Study Two Other Research Field Zeta", "2010"),
        ]
        # "generic abstract" shares no tokens with either candidate title
        result = resolve_same_author_year(
            "10.9999/rep", "A Replication", "generic abstract", cands
        )
        # best_score == 0, fails the > 0.05 floor → unresolved
        assert result["resolved"] is False

    def test_umbrella_paper_routed_to_fulltext(self):
        """A single ManyLabs candidate must not auto-resolve — needs full-text."""
        cands = [self._cand("10.1000/a", "ManyLabs Replication Project", "2015")]
        result = resolve_same_author_year("10.9999/rep", "A Replication", "abstract", cands)
        assert result["resolved"] is False
        assert result["resolution_method"] == "needs_fulltext"

    def test_empty_candidate_list(self):
        result = resolve_same_author_year("10.9999/rep", "A Replication", "abstract", [])
        assert result["resolved"] is False
        assert result["resolution_method"] == "no_candidates_found"

    def test_tie_between_equal_candidates(self):
        """Two candidates with identical titles produce equal Jaccard scores;
        the 1.5× margin condition (best >= second * 1.5) cannot be satisfied."""
        cands = [
            self._cand("10.1000/a", "Ego Depletion", "2010"),
            self._cand("10.1000/b", "Ego Depletion", "2010"),
        ]
        result = resolve_same_author_year(
            "10.9999/rep", "Replication of Ego Depletion", "ego depletion", cands
        )
        # score_A == score_B → margin condition fails → unresolved
        assert result["resolved"] is False

    def test_different_surnames_skips_jaccard(self):
        """When candidates have different first-author surnames the Jaccard step
        is skipped entirely and the result routes to full-text."""
        cands = [
            self._cand("10.1000/a", "Some Study Alpha Beta", "2010", "Smith"),
            self._cand("10.1000/b", "Another Study Gamma Delta", "2010", "Jones"),
        ]
        result = resolve_same_author_year(
            "10.9999/rep", "Replication Study", "abstract text", cands
        )
        assert result["resolved"] is False
        assert result["resolution_method"] == "needs_fulltext"

    def test_all_candidates_json_always_present(self):
        """all_candidates_json must be serialized regardless of resolution outcome."""
        import json
        cands = [self._cand("10.1000/a", "Some Study", "2010")]
        result = resolve_same_author_year("10.9999/rep", "Title", "abstract", cands)
        parsed = json.loads(result["all_candidates_json"])
        assert isinstance(parsed, list)
        assert len(parsed) == 1


# ── resolve_by_grobid_refs ────────────────────────────────────────────────────

class TestResolveByGrobidRefs:
    def _cand(self, doi, title, year=2010, author="Smith"):
        return {"doi": doi, "title": title, "year": year, "first_author": author}

    def _ref(self, title, year=2010, authors=None):
        return {"title": title, "year": year, "authors": authors or ["Smith, J."]}

    def test_high_overlap_ref_resolves(self):
        cands = [self._cand("10.1000/a", "Ego Depletion Is the Active Self Limited")]
        refs  = [self._ref("Ego Depletion Is the Active Self a Limited Resource")]
        result = resolve_by_grobid_refs("10.9999/rep", cands, {"references": refs})
        assert result["resolved"] is True
        assert result["resolved_doi_o"] == "10.1000/a"
        assert result["resolution_method"] == "grobid_ref_match"

    def test_no_refs_returns_unresolved(self):
        cands = [self._cand("10.1000/a", "Some Study")]
        result = resolve_by_grobid_refs("10.9999/rep", cands, {"references": []})
        assert result["resolved"] is False

    def test_year_mismatch_blocks_match(self):
        """Year off by 2 blocks the match even with identical titles."""
        cands = [self._cand("10.1000/a", "Ego Depletion Study Alpha Beta Gamma", year=2010)]
        refs  = [self._ref("Ego Depletion Study Alpha Beta Gamma", year=2013)]
        result = resolve_by_grobid_refs("10.9999/rep", cands, {"references": refs})
        assert result["resolved"] is False

    def test_year_within_one_matches(self):
        """Year off by exactly 1 is within tolerance and should not block the match."""
        cands = [self._cand("10.1000/a", "Ego Depletion Self Control Resource Model", year=2010)]
        refs  = [self._ref("Ego Depletion Self Control Resource Model", year=2011)]
        result = resolve_by_grobid_refs("10.9999/rep", cands, {"references": refs})
        assert result["resolved"] is True

    def test_author_bonus_lowers_threshold(self):
        """When the first-author surname matches, the Jaccard threshold drops from
        0.45 to 0.30, allowing a moderate-overlap pair to resolve."""
        # sim("Alpha Beta Gamma Delta Epsilon", "Alpha Beta Gamma Sigma Omega Lambda") = 3/8 = 0.375
        # 0.375 < 0.45 (no match without author) but > 0.30 (match with author)
        cands = [self._cand("10.1000/a", "Alpha Beta Gamma Delta Epsilon",
                             year=2010, author="Baumeister")]
        refs  = [self._ref("Alpha Beta Gamma Sigma Omega Lambda",
                            year=2010, authors=["Baumeister, R."])]
        result = resolve_by_grobid_refs("10.9999/rep", cands, {"references": refs})
        assert result["resolved"] is True

    def test_empty_candidates_returns_unresolved(self):
        refs = [self._ref("Some Study")]
        result = resolve_by_grobid_refs("10.9999/rep", [], {"references": refs})
        assert result["resolved"] is False
