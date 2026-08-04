"""
Tests for shared/disambiguation.py — Jaccard thresholds and heuristics.

All cases are hand-crafted; no API calls are made.
Run: python -m pytest tests/test_disambiguation.py -v
"""
import pytest

from shared.disambiguation import jaccard_similarity


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
