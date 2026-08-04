"""Spec layer: the shipped bundle, validation refusals, RE2 safety, bundle hash.

This is where the SHIPPED rules are asserted — their policy (ids, piles,
precedences) and, at the end of the file, their patterns: the four measured #147
changes, each on concrete strings taken from the evidence on branch
`analysis/stage-b-eval` (`analysis/stage_b_eval/`), each carrying the near-miss
the change must still block, because a narrowing that stops blocking everything is
not a narrowing. The numbers behind the rules live in each spec's `measured`
array; what is tested here is that the rule does on a row what the measurement
said it would.

Engine MECHANICS are not tested here — they run against the synthetic bundle of
`tests/engine_bundle.py`, so a deliberate policy change breaks only this file.
"""

import json
from pathlib import Path

import pyarrow as pa
import pytest

from filter.engine.backends import eval_spec_rows
from filter.engine.route import route_batch
from filter.engine.spec import bundle_hash, load_specs, re2_safe, validate_spec
from search.snapshot_scan import _POOL_SCHEMA

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"

# The starter bundle of docs/filter-engine.md, with the 500s band expanded to the
# seven original exclusion-pattern ids.
EXPECTED = {
    "deposit-doi-prefixes": ("discard", 960),
    "non-article-doi": ("discard", 955),
    "dataset-type": ("discard", 950),
    "non-article-type": ("discard", 945),
    "exclusion-rescue": ("screen_cheap", 650),
    # #147 item 2: the second rescue, one band-neighbour below exclusion-rescue.
    "title-phrase-rescue": ("screen_cheap", 645),
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


def test_re2_safe_rejects_each_banned_construct():
    """One case per construct RE2 cannot express: the four lookarounds, a
    backreference, an atomic group, the three possessive quantifiers, a conditional,
    a named backreference and `\\G`."""
    banned = [
        r"replicat(?=ion)", r"replicat(?!ion)", r"(?<=de )replicat", r"(?<!de )replicat",
        r"(replicat)\1", r"(?>replicat)+", r"replicat*+", r"replicat++", r"replicat?+",
        r"(?(1)a|b)", r"(?P<x>a)(?P=x)", r"\Greplicat",
    ]
    for pattern in banned:
        assert re2_safe(pattern) is False, f"{pattern!r} should be refused"


def test_re2_safe_accepts_ordinary_syntax_including_inline_flags():
    """The shapes the shipped bundle is written in: alternation, non-capturing and
    named groups, inline flags, character classes, and lazy bounded repetition —
    none of which needs anything RE2 lacks."""
    ordinary = [
        r"(?i)replicat|reproduc", r"(?:close|exact)\s+replications?\b",
        r"\breplicat\w*\b", r"[\w'-]{3,}\s+et\s+al\.?,?\s+(?:19|20)\d{2}\b",
        r"a{2,3}?", r"\(\s*(?:19|20)\d{2}\s*\)", r"(?P<year>(?:19|20)\d{2})",
    ]
    for pattern in ordinary:
        assert re2_safe(pattern) is True, f"{pattern!r} should be accepted"


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


# ---------------------------------------------------------------------------
# The shipped patterns: the four measured changes of issue #147
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def specs() -> list:
    return load_specs(SPEC_DIR)


def _row(title: str, abstract: str, work: int = 1) -> dict:
    return {
        "id": f"https://openalex.org/W{work}",
        "doi": f"10.1000/e147.{work}",
        "title": title,
        "display_name": title,
        "publication_year": 2020,
        "type": "article",
        "authorships": "[]",
        "primary_location": "{}",
        "open_access": "{}",
        "concepts": json.dumps([]),
        "abstract_text": abstract,
        "hit_token_title": True,
        "hit_token_abstract": True,
        "hit_concept": False,
    }


def _matches(specs: list, spec_id: str, row: dict) -> bool:
    """Whether *spec_id* claims *row*.

    One backend only: that the two agree over the shipped bundle is proved
    wholesale by `test_engine_route.py`, not once per pattern here.
    """
    spec = next(s for s in specs if s.id == spec_id)
    return bool(eval_spec_rows(spec, [row])[0])


def _routed(specs: list, row: dict) -> dict:
    batch = pa.Table.from_pylist([row], schema=_POOL_SCHEMA).to_batches()[0]
    return route_batch(specs, batch).to_pylist()[0]


def test_the_replication_phrase_rule_takes_the_t2_arms_and_leaves_the_rest(specs):
    """vocab_holes_report.md §5.1: T2 recovers 186 of the 319 gold positives that
    carry no matching phrase, for +9.3 admissions per million (+0.6%) and zero
    goldneg_screen hits. The arms it leaves out are §5.3's: T4 buys 65 more gold
    positives at 13x T2's admissions (119.6 vs 9.3 per million), and their noise has
    no compact shape."""
    taken = [
        # `we <two words> replicate(d)` — 97 of the 319 no-phrase gold misses
        # (candidate C2_we_gap_replicate).
        "We also replicated the anchoring effect documented in the original report.",
        "We have now replicated the original pattern in a larger sample.",
        # matrix verb + `to replicate` — 63 misses (C1b_matrix_verb_no_able).
        "We sought to replicate the feedback intervention effect in a new cohort.",
        "The authors tried to replicate the depletion result and did not succeed.",
        # fail/unable/attempt + adverb + replicate (C12_fail_to_replicate_forms).
        "The present study failed to fully replicate the original depletion result.",
        # failure nouns (C6_failure_nouns) and the attempted-replication noun (C13).
        "This paper reviews documented failures to replicate the ego-depletion effect.",
        "Three failed replications of the facial-feedback effect are reported.",
    ]
    for abstract in taken:
        row = _row("A new sample", abstract)
        assert _matches(specs, "phrase-replication", row), abstract
        assert _routed(specs, row)["pile"] == "screen_cheap", abstract

    left = [
        # C4 passive: a biological replicate, and future work.
        "Each treatment was replicated 22 times across the grassland plots.",
        # C5 gerund: the virus/artifact sense.
        "Host genes play key roles in replicating the virus inside the cell.",
        # C9 replicab*: methodological praise.
        "The paper presents a replicable and highly replicability-focused model.",
        # C3 bare third person.
        "This paper replicates a widely used measurement protocol.",
        # C1's `able to replicate` arm, dropped in C1b: its cost was the whole
        # difference between C1 and C1b (vocab_holes_report.md §4a).
        "A prosthetic foot able to replicate the function of the biological foot.",
    ]
    for abstract in left:
        assert not _matches(specs, "phrase-replication",
                            _row("Grassland ecology", abstract)), abstract


def test_the_narrowed_technical_rules_release_the_reproduction_genre_and_still_block_storage(
        specs):
    """exclusion_narrowing_report.md §1 and §4.3-4.4: model/method/data/dataset is
    how a computational reproduction describes itself. TO2_both_tight recovers 18
    gold positives for 3.9 rows per million; TV1_tight_objects recovers 13 for 3.0 —
    and between them they free 3 of the 5 indexed reproductions the pipeline loses
    (reproduction_coverage_report.md). The near-misses below are what each narrowing
    must still block: the matched-span census over 5.6M real rows found the dominant
    referent of both patterns is distributed storage, not research methodology."""
    released = [
        ("technical-verb", "Replicating MOOC predictive models at scale",
         "We replicated the model of the original paper on a new MOOC cohort."),
        ("technical-verb", "A replication of an agent-based study",
         "We replicated the data of the published experiment exactly."),
        ("technical-object", "A Replication and Analysis of Tiebout Competition",
         "The analysis rests on replication of the data and replication of the "
         "method of the original agent-based study."),
        ("technical-object", "Cross-Model Replication Study",
         "A model replication of the published predictive model."),
    ]
    for spec_id, title, abstract in released:
        row = _row(title, abstract)
        assert not _matches(specs, spec_id, row), title
        assert _routed(specs, row)["pile"] != "discard", title

    blocked = [
        ("technical-object", "A Strategy for Database Replication in Ad Hoc Networks",
         "Database replication improves availability in ad hoc networks."),
        ("technical-object", "Replica placement for grid environments",
         "Replication of the pipeline across grid nodes reduces latency."),
        ("technical-verb", "Consistency management for distributed stores",
         "We replicated the database across three availability zones."),
        ("technical-verb", "Reproducible build pipelines",
         "We replicated the software of the vendor toolchain on our own cluster."),
    ]
    for spec_id, title, abstract in blocked:
        row = _row(title, abstract)
        assert _matches(specs, spec_id, row), title
        assert _routed(specs, row)["pile"] == "discard", title


def test_bo1_spares_the_gwas_locus_genre_and_keeps_the_organism_senses(specs):
    """gwas_scope_classification.md §3: BO1 recovers 20 of the 36 BIOLOGICAL_OF
    kills — 17 qualifying, 1 internal, 2 unclear, and zero virology papers — for 0.4
    extra rows per million (~204 over the 510M-row snapshot). It exempts
    `genome-wide` and nothing else, so the virology and molecular senses that make
    BIOLOGICAL_OF the pattern it is are untouched."""
    gwas_abstract = ("We replicated eight loci previously reported in a European "
                     "population of comparable ancestry.")
    spared = [
        "Replication of Genome-Wide Association Studies of Type 2 Diabetes "
        "Susceptibility in Japan",
        # Several of the gold rows write the dash as U+2010.
        "Replication of Genome‐Wide Association Studies in a Korean cohort",
        "Replication of Genome Wide association study loci for plasma glucose",
    ]
    for title in spared:
        assert not _matches(specs, "biological-of", _row(title, gwas_abstract)), title

    kept = [
        ("Restriction of Replication of Oncolytic Herpes Simplex Virus",
         "The replication of the viral genome in host cells was measured by qPCR."),
        ("Inverted replication of vertebrate mitochondria",
         "Inhibition of the replication of genomic DNA was observed in the mutant."),
    ]
    for title, abstract in kept:
        assert _matches(specs, "biological-of", _row(title, abstract)), title


def test_the_title_phrase_rescue_reopens_an_abstract_only_exclusion_and_no_more(specs):
    """exclusion_narrowing_report.md §4.1 (TO4 + TV3): where the exclusion fired only
    past the title/abstract join and the TITLE carries a replication phrase, the row
    goes to the tier that decides instead of being discarded. The rescue's whole
    premise is that the exclusion matched PAST the title, so a title that is itself
    the technical sense stays in the 500s band. And where the older exclusion-rescue
    (650, phrase + author-year cite) outranks title-phrase-rescue (645, no cite
    required), both route to screen_cheap — the higher precedence is not hiding a
    different outcome, only a different recorded rule."""
    rescued = _row("A Replication of 'The Role of Intrafirm Networks'",
                   "We replicated the code of the earlier team's analysis on a new "
                   "sample of firms.")
    assert _matches(specs, "technical-verb", rescued)      # the exclusion did fire
    assert _routed(specs, rescued)["pile"] == "screen_cheap"
    assert _routed(specs, rescued)["rule_id"] == "title-phrase-rescue"

    refused = _row("Replication of the database schema for grid computing",
                   "We describe replication of the database across grid nodes.")
    assert not _matches(specs, "title-phrase-rescue", refused)
    assert _routed(specs, refused)["pile"] == "discard"

    both = _row("A Replication of 'The Role of Intrafirm Networks'",
                "We replicated the code of Smith (2019) on a new sample of firms.")
    assert _matches(specs, "exclusion-rescue", both)
    assert _matches(specs, "title-phrase-rescue", both)
    assert _routed(specs, both)["rule_id"] == "exclusion-rescue"
    assert _routed(specs, both)["pile"] == "screen_cheap"


def test_g1_admits_a_prior_report_and_still_suppresses_the_internal_two_stage_design(
        specs):
    """gwas_scope_classification.md §3: G1 minus the `replicated the association`
    alternative recovers 12 papers, 12 of 12 qualifying under the maintainer's scope
    ruling and 0 internal designs, at zero measured extra admissions. That ruling
    puts two-stage discovery-plus-own-replication-cohort designs OUT of scope, and G1
    as originally written leaked two of them through the dropped alternative
    (10.1002/hbm.22247, 10.1038/s41598-023-31701-w). (11-16 genuine qualifying papers
    remain unreachable by any same-sentence regex, because they attribute the prior
    report in a different sentence — a screen judgment, not a keyword one.)"""
    prior_report = _row(
        "Replication of the Wellcome Trust genome-wide association study on "
        "essential hypertension in a Korean population",
        "We replicated eight loci previously reported by Smith et al. (2010) "
        "in a European sample of comparable size.")
    assert _matches(specs, "phrase-with-cite", prior_report)
    assert _routed(specs, prior_report)["pile"] == "screen_expensive"

    internal = _row(
        "A metabolome-wide association study of serum laurylcarnitine and depression",
        "In 1411 participants of the KORA F4 study (discovery cohort) we identified "
        "an association. We replicated the association in an independent sample of "
        "968 participants of the SHIP-Trend study (replication cohort), following "
        "the protocol of Jones et al. (2015).")
    assert not _matches(specs, "phrase-with-cite", internal)
    assert _routed(specs, internal)["pile"] != "screen_expensive"
