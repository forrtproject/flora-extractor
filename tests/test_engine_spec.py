"""Spec layer: the shipped bundle, validation refusals, RE2 safety, bundle hash."""

import json
from pathlib import Path

import pytest

from filter.engine.spec import bundle_hash, load_specs, re2_safe, validate_spec

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"

# ---------------------------------------------------------------------------
# The policy table
# ---------------------------------------------------------------------------
#
# This is the ONE place the shipped bundle's policy is written down: for every
# rule id, where it routes, at what precedence, under which vocabulary, and
# whether it is shadow. A deliberate policy change — flipping a rule to shadow
# because its evidence turned out to be unsound, moving a precedence — is a
# one-line edit here with a readable diff, and it must not break any other test.
# Tests elsewhere assert PATTERNS (does this regex claim this string) and
# MECHANICS (against synthetic specs); neither may re-assert what lives here.
#
EXPECTED = {
    # id: (pile, precedence, vocabulary, shadow)
    "not-a-paper-doi": ("discard", 960, None, False),
    "deposit-registrant": ("discard", 958, None, False),
    "not-a-paper-title": ("discard", 955, None, False),
    "not-a-report-type": ("discard", 940, None, False),
    "replication-claim": ("screen_expensive", 700, None, False),
    "not-a-study-type": ("discard", 500, None, False),
    "replication-signal": ("screen_cheap", 300, "replication", True),
    "reproduction-signal": ("screen_cheap", 262, "reproduction", True),
    "replication-probe": ("screen_cheap", 100, "replication", True),
}

_ADMISSION = "replication-claim"
_ABOVE_ADMISSION = ("not-a-paper-doi", "deposit-registrant", "not-a-paper-title",
                    "not-a-report-type")
_BELOW_ADMISSION = ("not-a-study-type",)


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


def test_the_shipped_bundle_matches_the_policy_table():
    """Every rule's pile, precedence, vocabulary and shadow flag, in one place."""
    specs = load_specs(SPEC_DIR)
    assert {s.id: (s.pile, s.precedence, s.vocabulary, s.shadow)
            for s in specs} == EXPECTED


def test_specs_are_returned_highest_precedence_first():
    specs = load_specs(SPEC_DIR)
    assert [s.precedence for s in specs] == sorted((s.precedence for s in specs),
                                                   reverse=True)


def test_admission_sits_between_the_two_kinds_of_discard():
    """The definitional discards outrank admission; the metadata-crosswalk discard
    does not, so a mistyped replication is screened rather than deleted."""
    precedence = {s.id: s.precedence for s in load_specs(SPEC_DIR)}
    for rule in _ABOVE_ADMISSION:
        assert precedence[rule] > precedence[_ADMISSION], rule
    for rule in _BELOW_ADMISSION:
        assert precedence[rule] < precedence[_ADMISSION], rule


def test_only_one_rule_routes_to_the_expensive_screen():
    """`replication-claim` is the sole route to two voters."""
    expensive = [s.id for s in load_specs(SPEC_DIR) if s.pile == "screen_expensive"]
    assert expensive == [_ADMISSION]


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


def test_the_bundle_hash_binds_the_conventions_that_name_the_piles(tmp_path):
    """conventions.json is not a filter, but it decides what a pile is CALLED in an
    export — so a release that did not bind it could be exported under a different
    status mapping than it was routed under."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "a.json").write_text(json.dumps(_valid_spec(id="a")))
    without = bundle_hash(bundle)

    conventions = bundle / "conventions.json"
    conventions.write_text(json.dumps({"piles": {"discard": {"exported": True}}}))
    with_policy = bundle_hash(bundle)
    assert with_policy != without

    conventions.write_text(json.dumps({"piles": {"discard": {"exported": False}}}))
    assert bundle_hash(bundle) != with_policy

    # aliases.json is deliberately NOT bound here: it has its own release input.
    (bundle / "aliases.json").write_text(json.dumps({"version": 1, "aliases": {}}))
    assert bundle_hash(bundle) != with_policy  # unchanged by the alias file
    conventions.write_text(json.dumps({"piles": {"discard": {"exported": True}}}))
    assert bundle_hash(bundle) == with_policy
