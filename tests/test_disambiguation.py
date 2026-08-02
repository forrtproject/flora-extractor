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
    @pytest.mark.parametrize("a,b,lo,hi", [
        ("EGO DEPLETION", "ego depletion", 1.0, 1.0),   # identical, case-insensitively
        ("", "some text", 0.0, 0.0),
        ("apple banana cherry", "dog elephant frog", 0.0, 0.0),
        # words < 3 chars are excluded, so only "dog"/"cat" qualify → no overlap
        ("a of to dog", "a of to cat", 0.0, 0.0),
        ("ego depletion Baumeister 1998", "ego depletion original", 0.0001, 0.9999),
    ])
    def test_jaccard_similarity(self, a, b, lo, hi):
        assert lo <= jaccard_similarity(a, b) <= hi


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

    @pytest.mark.parametrize("case,cands,title,abstract,method", [
        # best_score == 0 → fails the > 0.05 floor
        ("near_miss", [("10.1000/a", "Study One Topic Alpha Beta Gamma", "2010", "Smith"),
                       ("10.1000/b", "Study Two Other Research Field Zeta", "2010", "Smith")],
         "A Replication", "generic abstract", None),
        # score_A == score_B → the 1.5× margin condition cannot be satisfied
        ("tie", [("10.1000/a", "Ego Depletion", "2010", "Smith"),
                 ("10.1000/b", "Ego Depletion", "2010", "Smith")],
         "Replication of Ego Depletion", "ego depletion", None),
        # different first-author surnames → Jaccard step skipped entirely
        ("different_surnames", [("10.1000/a", "Some Study Alpha Beta", "2010", "Smith"),
                                ("10.1000/b", "Another Study Gamma Delta", "2010", "Jones")],
         "Replication Study", "abstract text", "needs_fulltext"),
        ("no_candidates", [], "A Replication", "abstract", "no_candidates_found"),
    ])
    def test_unresolvable_cases(self, case, cands, title, abstract, method):
        result = resolve_same_author_year(
            "10.9999/rep", title, abstract,
            [self._cand(*c) for c in cands],
        )
        assert result["resolved"] is False
        if method:
            assert result["resolution_method"] == method

    def test_umbrella_paper_routed_to_fulltext(self):
        """A single ManyLabs candidate must not auto-resolve — needs full-text."""
        cands = [self._cand("10.1000/a", "ManyLabs Replication Project", "2015")]
        result = resolve_same_author_year("10.9999/rep", "A Replication", "abstract", cands)
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

    @pytest.mark.parametrize("ref_year,expected", [
        (2011, True),   # off by 1 — within tolerance
        (2013, False),  # off by 3 — blocks the match even with an identical title
    ])
    def test_year_tolerance(self, ref_year, expected):
        title = "Ego Depletion Self Control Resource Model"
        cands = [self._cand("10.1000/a", title, year=2010)]
        refs  = [self._ref(title, year=ref_year)]
        result = resolve_by_grobid_refs("10.9999/rep", cands, {"references": refs})
        assert result["resolved"] is expected

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

    @pytest.mark.parametrize("with_cands,with_refs", [(True, False), (False, True)])
    def test_missing_side_returns_unresolved(self, with_cands, with_refs):
        cands = [self._cand("10.1000/a", "Some Study")] if with_cands else []
        refs  = [self._ref("Some Study")] if with_refs else []
        result = resolve_by_grobid_refs("10.9999/rep", cands, {"references": refs})
        assert result["resolved"] is False
