"""Spec layer: the shipped bundle, validation refusals, RE2 safety, bundle hash."""

import json
from pathlib import Path

import pytest

from filter.engine.spec import bundle_hash, load_specs, re2_safe, validate_spec

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"

# The starter bundle of docs/filter-engine.md, with the 500s band expanded to the
# seven original exclusion-pattern ids.
EXPECTED = {
    "deposit-doi-prefixes": ("discard", 960),
    "non-article-doi": ("discard", 955),
    "dataset-type": ("discard", 950),
    "non-article-type": ("discard", 945),
    "exclusion-rescue": ("screen_cheap", 650),
    "editorial-artifact": ("discard", 555),
    "data-availability": ("discard", 550),
    "biological": ("discard", 545),
    "structural": ("discard", 544),
    "biological-of": ("discard", 543),
    "technical-object": ("discard", 541),
    "technical-verb": ("discard", 540),
    "phrase-with-cite": ("screen_expensive", 350),
    "phrase-reproduction": ("screen_cheap", 262),
    "phrase-replication": ("screen_cheap", 260),
    "title-stem": ("screen_cheap", 240),
    "concept-replication": ("screen_cheap", 220),
    "reproduce-verb-arms": ("screen_cheap", 210),
    "nfd-stems": ("screen_cheap", 205),
}


def _valid_spec(**overrides) -> dict:
    spec = {
        "id": "example",
        "description": "why this rule exists",
        "match": {"text_regex": r"\bexample\b"},
        "pile": "screen_cheap",
        "vocabulary": None,
        "precedence": 250,
        "shadow": False,
        "measured": [],
    }
    spec.update(overrides)
    return spec


def test_the_shipped_bundle_loads_with_the_documented_ids_piles_and_precedences():
    specs = load_specs(SPEC_DIR)
    assert {s.id: (s.pile, s.precedence) for s in specs} == EXPECTED


def test_specs_are_returned_highest_precedence_first():
    specs = load_specs(SPEC_DIR)
    assert [s.precedence for s in specs] == sorted((s.precedence for s in specs),
                                                   reverse=True)


def test_a_duplicate_id_across_two_files_is_rejected(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_valid_spec()))
    (tmp_path / "b.json").write_text(json.dumps(_valid_spec(precedence=251)))
    with pytest.raises(ValueError, match="duplicate id"):
        load_specs(tmp_path)


def test_a_discard_without_measured_evidence_is_rejected_unless_it_is_shadow():
    unmeasured = _valid_spec(pile="discard", precedence=900)
    assert any("measured entry or shadow" in e for e in validate_spec(unmeasured))
    assert validate_spec({**unmeasured, "shadow": True}) == []
    guessed = {**unmeasured,
               "measured": [{"level": "heuristic", "rationale": "a guess"}]}
    assert any("heuristic evidence may not discard" in e for e in validate_spec(guessed))


def test_pending_is_not_a_legal_spec_pile():
    errors = validate_spec(_valid_spec(pile="pending"))
    assert any("'pending' is never a spec target" in e for e in errors)


@pytest.mark.parametrize("pattern", [
    r"replicat(?=ion)", r"replicat(?!ion)", r"(?<=de )replicat", r"(?<!de )replicat",
    r"(replicat)\1", r"(?>replicat)+", r"replicat*+", r"replicat++", r"replicat?+",
    r"(?(1)a|b)", r"(?P<x>a)(?P=x)", r"\Greplicat",
])
def test_re2_safe_rejects_each_banned_construct(pattern):
    assert re2_safe(pattern) is False


@pytest.mark.parametrize("pattern", [
    r"(?i)replicat|reproduc", r"(?:close|exact)\s+replications?\b",
    r"\breplicat\w*\b", r"[\w'-]{3,}\s+et\s+al\.?,?\s+(?:19|20)\d{2}\b",
    r"a{2,3}?", r"\(\s*(?:19|20)\d{2}\s*\)", r"(?P<year>(?:19|20)\d{2})",
])
def test_re2_safe_accepts_ordinary_syntax_including_inline_flags(pattern):
    assert re2_safe(pattern) is True


def test_a_regex_that_does_not_compile_is_rejected():
    # RE2-safety says nothing about balance: an unterminated group passes every
    # banned-construct check and then explodes in whichever backend compiles it.
    errors = validate_spec(_valid_spec(match={"text_regex": r"\breplication of (?:a|b"}))
    assert any("does not compile" in e for e in errors)


def test_the_pyre_regex_key_is_rejected_outside_a_decomposed_match():
    flat = _valid_spec(match={"text_regex": r"\ba\b", "pyre_regex": r"\ba(?=b)"})
    assert any("only valid on a decomposed match" in e for e in validate_spec(flat))

    decomposed = _valid_spec(match={"any_of": [{"text_regex": r"\ba\b"}],
                                    "pyre_regex": r"\ba(?=b)"})
    assert validate_spec(decomposed) == []

    nested = _valid_spec(match={"any_of": [{"text_regex": r"\ba\b",
                                            "pyre_regex": r"\ba(?=b)"}]})
    assert any("unknown key 'pyre_regex'" in e for e in validate_spec(nested))


def test_bundle_hash_follows_file_bytes_and_not_load_order(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    a = json.dumps(_valid_spec(id="a"))
    b = json.dumps(_valid_spec(id="b", precedence=251))
    (first / "a.json").write_text(a)
    (first / "b.json").write_text(b)
    (second / "b.json").write_text(b)     # written in the opposite order
    (second / "a.json").write_text(a)
    assert bundle_hash(first) == bundle_hash(second)

    (second / "a.json").write_text(a + "\n")
    assert bundle_hash(first) != bundle_hash(second)
