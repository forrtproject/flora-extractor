"""Tests for title-pattern disambiguation in link_original.py."""
import pytest

from extract.link_original import _extract_title_target, _resolve_by_title_pattern
from shared.disambiguation import jaccard_similarity


class TestExtractTitleTarget:
    @pytest.mark.parametrize("title,expected_contains", [
        ("Replication of the ego depletion effect",        "ego depletion effect"),
        ("A Direct Replication of the pen-in-mouth effect","pen-in-mouth effect"),
        ("Failed Replication of the IAT effect",           "IAT effect"),
        ("Replicating Milgram's obedience study",           "Milgram"),
        ("Revisiting the weapons effect",                  "weapons effect"),
        ("Re-examining the anchoring and adjustment effect","anchoring and adjustment effect"),
        ("Reconsidering ego depletion",                    "ego depletion"),
        ("The pen-in-mouth effect: A Replication",         "pen-in-mouth effect"),
        ("The pen-in-mouth effect: Replication and Extension","pen-in-mouth effect"),
        ("Does power posing increase testosterone? Replication attempt",  None),
        ("Can we replicate the Mozart effect?",             "Mozart effect"),
        ("Testing the replicability of social priming",     "social priming"),
        ("A Reproduction of the embodied cognition effect", "embodied cognition effect"),
    ])
    def test_extract_target(self, title, expected_contains):
        result = _extract_title_target(title)
        if expected_contains is None:
            assert result is None or len(result) < 15
        else:
            assert result is not None, f"Expected match for: {title!r}"
            assert expected_contains.lower() in result.lower(), (
                f"Expected {expected_contains!r} in {result!r} for title {title!r}"
            )

    def test_no_match_returns_none(self):
        assert _extract_title_target("A meta-analysis of social priming effects") is None

    def test_short_target_returns_none(self):
        result = _extract_title_target("Revisiting X")
        assert result is None  # "X" is < 15 chars

    def test_generic_title_returns_none(self):
        assert _extract_title_target("Many Labs 2: Investigating Variation in Replicability") is None


class TestResolveByTitlePattern:
    _CANDIDATES = [
        {"doi": "10.1037/ego", "title": "Ego depletion: Is the active self a limited resource?",
         "year": 1998, "first_author": "Baumeister"},
        {"doi": "10.1037/sleep", "title": "Sleep deprivation and cognitive performance",
         "year": 2005, "first_author": "Harrison"},
        {"doi": "10.1037/social", "title": "Social facilitation effects in competitive tasks",
         "year": 2003, "first_author": "Zajonc"},
    ]

    def test_resolves_when_single_strong_match(self):
        result = _resolve_by_title_pattern(
            "10.1234/rep",
            "Replication of the ego depletion effect: Is the active self a limited resource?",
            self._CANDIDATES,
        )
        assert result is not None
        assert result["resolved"] is True
        assert result["resolved_doi_o"] == "10.1037/ego"
        assert result["resolution_method"] == "title_pattern_match"

    def test_returns_none_when_no_pattern_in_title(self):
        result = _resolve_by_title_pattern(
            "10.1234/rep",
            "A meta-analysis of sleep deprivation studies",
            self._CANDIDATES,
        )
        assert result is None

    def test_returns_hint_when_multiple_close_matches(self):
        candidates = [
            {"doi": "10.1/a", "title": "Ego depletion part one study",
             "year": 1998, "first_author": "A"},
            {"doi": "10.1/b", "title": "Ego depletion part two study",
             "year": 1998, "first_author": "A"},
        ]
        result = _resolve_by_title_pattern(
            "10.1234/rep",
            "Replication of ego depletion study",
            candidates,
        )
        if result:
            assert result.get("resolved") is not True

    def test_returns_none_when_no_candidates(self):
        result = _resolve_by_title_pattern(
            "10.1234/rep",
            "Replication of ego depletion",
            [],
        )
        assert result is None
