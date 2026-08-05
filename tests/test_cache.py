"""
Tests for shared/cache.py — the content-complete LLM cache key.

CLAUDE.md's rule is that a cache key must name everything the cached answer depends
on, and that one paper's entries must be purgeable together. Both halves were only
ever exercised indirectly, through callers that mock the cache away: content_key()
and clear_content_keys() had no direct test, so a key that stopped varying with its
inputs — the failure that silently answers one question with another's answer —
would not have shown up anywhere.
"""
import re

import pytest

from shared.cache import (
    clear_content_keys, content_key, read_cache, read_cache_migrating, write_cache,
)


class TestContentKey:
    def test_key_shape_is_prefix_doihash_contenthash(self):
        key = content_key("outcome", "10.1/abc", "v1", "gemini-flash", "the prompt")
        assert re.fullmatch(r"outcome_[0-9a-f]+_[0-9a-f]+", key), key

    def test_the_same_inputs_give_the_same_key(self):
        args = ("outcome", "10.1/abc", "v1", "gemini-flash", "the prompt")
        assert content_key(*args) == content_key(*args)

    @pytest.mark.parametrize("parts", [
        ("v2", "gemini-flash", "the prompt"),      # prompt version changed
        ("v1", "gpt-5-mini", "the prompt"),        # answering model changed
        ("v1", "gemini-flash", "other prompt"),    # inputs sent changed
        ("v1", "gemini-flash", "the prompt", ""),  # an extra part appended
    ])
    def test_any_changed_part_changes_the_key(self, parts):
        """Every part must be load-bearing: an input that drops out of the hash is an
        answer replayed for a question that was never asked."""
        base = content_key("outcome", "10.1/abc", "v1", "gemini-flash", "the prompt")
        assert content_key("outcome", "10.1/abc", *parts) != base

    def test_parts_do_not_run_together(self):
        """Concatenation without a separator would make ("ab","c") and ("a","bc")
        the same key."""
        assert (content_key("p", "10.1/a", "ab", "c")
                != content_key("p", "10.1/a", "a", "bc"))

    def test_the_doi_hash_alone_does_not_decide_the_key(self):
        """The DOI hash is in the name so entries can be globbed, never as the key."""
        assert (content_key("outcome", "10.1/abc", "prompt A")
                != content_key("outcome", "10.1/abc", "prompt B"))

    def test_a_different_doi_changes_the_middle_segment(self):
        a = content_key("outcome", "10.1/abc", "same")
        b = content_key("outcome", "10.1/xyz", "same")
        assert a.split("_")[1] != b.split("_")[1]
        assert a.split("_")[2] == b.split("_")[2]


class TestClearContentKeys:
    def test_clears_exactly_one_papers_entries_under_one_prefix(self, tmp_path):
        keep_other_doi = content_key("outcome", "10.1/other", "p")
        keep_other_prefix = content_key("classify", "10.1/abc", "p")
        targets = [content_key("outcome", "10.1/abc", "p1"),
                   content_key("outcome", "10.1/abc", "p2")]
        for key in targets + [keep_other_doi, keep_other_prefix]:
            write_cache(tmp_path, key, {"k": key})

        deleted = clear_content_keys(tmp_path, "outcome", "10.1/abc")

        assert sorted(deleted) == sorted(f"{k}.json" for k in targets)
        assert read_cache(tmp_path, keep_other_doi) is not None
        assert read_cache(tmp_path, keep_other_prefix) is not None
        assert all(read_cache(tmp_path, k) is None for k in targets)

    def test_clearing_an_uncached_paper_is_a_no_op(self, tmp_path):
        assert clear_content_keys(tmp_path, "outcome", "10.1/never-cached") == []


class TestReadCacheMigrating:
    """Declared equivalences (issue #171): keys stay strict, but a call site may name
    legacy keys a maintainer reviewed as the same computation. The registered case at
    the classify call site is a mislabelled model component; the case this class
    covers is the other one the mechanism must serve without extra code — a legacy
    PROMPT VERSION, where a prompt edit was judged answer-preserving."""

    def _keys(self):
        return (content_key("outcome", "10.1/abc", "v2", "m", "the prompt"),
                content_key("outcome", "10.1/abc", "v1", "m", "the prompt"))

    def test_a_legacy_prompt_version_answers_and_is_refiled_with_provenance(self, tmp_path):
        current, legacy = self._keys()
        write_cache(tmp_path, legacy, {"outcome": "success"})

        got = read_cache_migrating(tmp_path, current, [legacy],
                                   {"prompt_version": "v2", "model": "m"})

        assert got["outcome"] == "success"
        assert got["cache_migrated"] == {"prompt_version": "v2", "model": "m",
                                         "from_key": legacy}
        # Re-filed under the current key, and the legacy entry is left for other
        # checkouts and the shared HF cache to keep hitting.
        assert read_cache(tmp_path, current) == got
        assert read_cache(tmp_path, legacy) == {"outcome": "success"}

    def test_the_current_key_wins_and_is_not_annotated(self, tmp_path):
        current, legacy = self._keys()
        write_cache(tmp_path, current, {"outcome": "failure"})
        write_cache(tmp_path, legacy, {"outcome": "success"})

        assert read_cache_migrating(tmp_path, current, [legacy], {}) == {"outcome": "failure"}

    def test_no_declared_key_hits_is_a_plain_miss(self, tmp_path):
        current, legacy = self._keys()
        assert read_cache_migrating(tmp_path, current, [legacy], {}) is None
        assert not list(tmp_path.glob("*.json"))
