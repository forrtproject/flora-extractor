"""Which rule claims which vocabulary, over the shipped bundle.

Each test names a text and asserts the exact set of rules that may claim it:
molecular-biology prose, the homograph collocations, bare `replication of`,
`registered report of`, reproduction vocabulary, the editorial-artifact anchor,
the `.suppl` DOI arm, decomposed Unicode, and the reproduction senses
`reproduction-signal` must keep refusing.
"""

import unicodedata
from pathlib import Path

import pytest

from filter.engine.spec import load_specs
from tests.test_rulebook_v2 import _matches, _row

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"

# The two admission rules that decide a row on replication vocabulary.
# `replication-probe` carries unmeasured candidate vocabularies and is out of
# scope for these cases.
_ADMISSION = ("replication-claim", "replication-signal")


@pytest.fixture(scope="module")
def specs() -> list:
    return load_specs(SPEC_DIR)


def _discards(specs: list) -> list[str]:
    return [s.id for s in specs if s.pile == "discard"]


# ---------------------------------------------------------------------------
# The whitelist's argument: the wrong sense is never admitted
# ---------------------------------------------------------------------------


def test_dna_replication_prose_reaches_no_admission_rule(specs):
    """Molecular-biology prose in the abstract is claimed by no rule: no arm of B
    fires on it, and C's arms are a title stem, the concept ids and bare
    `replications? of`, none of which this row carries. (C's bare arm would claim
    "replication of the viral genome"; that arm is pinned separately below.)"""
    row = _row("Chromosome dynamics in yeast",
               "DNA replication forks stall at the origin under replication "
               "stress, and replication timing was measured by sequencing.")
    for rule in _ADMISSION:
        assert not _matches(specs, rule, row), rule
    for rule in _discards(specs):
        assert not _matches(specs, rule, row), rule


def test_a_dna_title_reaches_only_the_cheap_title_stem_and_never_a_discard(specs):
    """A title carrying the English stem reaches the cheap discard-only tier
    through C's stem arm — the arm cannot tell "DNA Replication in Yeast" from
    "Replication in Psychology". No rule discards the row and none buys it two
    voters."""
    row = _row("DNA Replication in Yeast",
               "We map origin firing across the genome by sequencing.")
    assert _matches(specs, "replication-signal", row)
    assert not _matches(specs, "replication-claim", row)
    for rule in _discards(specs):
        assert not _matches(specs, rule, row), rule


@pytest.mark.parametrize("title,abstract", [
    ("A direct replication of the classic cell phone driving study",
     "We report a direct replication of the classic cell phone driving study."),
    ("A conceptual replication of the parasite stress theory of values",
     "We report a conceptual replication of the parasite stress theory of values."),
])
def test_a_homograph_collocation_is_claimed_by_b_and_discarded_by_nothing(
        specs, title, abstract):
    """"cell phone" and "parasite stress" are genuine replications naming a
    biological homograph. No rule discards them, and B claims both on its
    qualifier arm."""
    row = _row(title, abstract)
    assert _matches(specs, "replication-claim", row)
    for rule in _discards(specs):
        assert not _matches(specs, rule, row), rule


# ---------------------------------------------------------------------------
# Where each vocabulary lands
# ---------------------------------------------------------------------------


def test_bare_replication_of_is_a_cheap_signal_and_never_an_expensive_one(specs):
    """Bare `replication of` is C's last arm and no arm of B: on its own it buys
    the cheap discard-only tier, never two voters."""
    row = _row("Bees and anchoring",
               "This paper offers a replication of an earlier result.")
    assert not _matches(specs, "replication-claim", row)
    assert _matches(specs, "replication-signal", row)


def test_registered_report_of_is_admitted_by_nothing(specs):
    """`registered report of` is not a B arm and matches none of C's arms."""
    row = _row("Stage 1 protocol for an intervention trial",
               "This registered report of a new intervention was accepted in "
               "principle before data collection.")
    for rule in _ADMISSION:
        assert not _matches(specs, rule, row), rule


@pytest.mark.parametrize("abstract", [
    "A computational reproduction of the published estimates is presented.",
    "We reproduced the analysis of the original authors from their posted code.",
])
def test_reproduction_vocabulary_belongs_to_its_own_rule(specs, abstract):
    """Reproduction phrases are claimed by `reproduction-signal` and by neither
    admission rule. The title is neutral on purpose: C's stem arm includes
    `reproduc`, so a title saying "reproduction" does reach the cheap tier."""
    row = _row("An independent check of published estimates", abstract)
    for rule in _ADMISSION:
        assert not _matches(specs, rule, row), rule
    assert _matches(specs, "reproduction-signal", row)


# ---------------------------------------------------------------------------
# The definitional discards
# ---------------------------------------------------------------------------


def test_the_editorial_anchor_claims_the_artifact_and_spares_the_paper(specs):
    """`not-a-paper-title` is start-anchored: it claims a title that announces the
    genre and then echoes its parent, and not a paper that discusses the genre."""
    artifact = _row("Review for: A replication of the Smith effect",
                    "Reviewer comments on the submitted manuscript.")
    assert _matches(specs, "not-a-paper-title", artifact)

    paper = _row("Editorial independence and replication in psychology",
                 "We survey how journal policy shapes replication practice.")
    for rule in _discards(specs):
        assert not _matches(specs, rule, paper), rule
    # It is a paper about replication, not a paper claiming to BE one: C's title
    # stem admits it to the cheap tier and B does not.
    assert _matches(specs, "replication-signal", paper)
    assert not _matches(specs, "replication-claim", paper)


@pytest.mark.parametrize("doi,claimed", [
    ("10.1000/jexp.2019.4471.suppl", True),
    ("https://doi.org/10.1000/jexp.2019.4471.suppl", True),
    ("10.1000/jexp.2019.4471.supplement", False),
    ("10.1000/jexp.2019.4471.suppl/tables", False),
    ("10.1000/suppl.4471", False),
])
def test_the_suppl_arm_is_terminal_only(specs, doi, claimed):
    """The `.suppl` arm is terminal-anchored: a DOI that merely contains the
    string is not claimed."""
    row = _row("Supplementary material", "Tables S1-S4.", doi=doi)
    assert _matches(specs, "not-a-paper-doi", row) is claimed


# ---------------------------------------------------------------------------
# NFC normalisation (the `nfd-stems` retirement)
# ---------------------------------------------------------------------------


def test_a_decomposed_title_still_matches_the_multilingual_stem_arm(specs):
    """The stem arm spells its accented stems composed; a title spelled
    "re" + U+0301 + "plicat" must still match, in both backends."""
    decomposed = unicodedata.normalize("NFD", "Réplication directe d'une étude")
    assert any(ord(ch) == 0x301 for ch in decomposed), "the fixture is not decomposed"
    assert _matches(specs, "replication-signal",
                    _row(decomposed, "Une etude de l'effet d'ancrage."))


# ---------------------------------------------------------------------------
# reproduction-signal: the anchored forms, and the senses they exclude
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,abstract", [
    # Social reproduction.
    ("The Reproduction of Mothering",
     "Reproduction of the social order in schools is examined."),
    ("The Work of Art in the Age of Mechanical Reproduction",
     "An essay on mechanical reproduction of the artwork."),
    # The epidemiological basic reproduction number.
    ("The basic reproduction number of influenza",
     "We estimate the basic reproduction number of influenza across seasons."),
    # Animal breeding.
    ("Sexual reproduction of corals under thermal stress",
     "Corals that reproduce early show higher survival; the species reproduces "
     "successfully in captivity each spring."),
])
def test_phrase_reproduction_refuses_the_other_senses_of_reproduction_of(
        specs, title, abstract):
    """The rule ships anchored forms, so the senses that dominate bare
    "reproduction of" — social reproduction, R₀, animal breeding — are refused."""
    assert not _matches(specs, "reproduction-signal", _row(title, abstract))


def test_phrase_reproduction_reanaly_stem_does_not_claim_reanalgesia(specs):
    """The false friend of the `re-?analy[sz]`/`re-?analyz` arms: a different
    `re-` word that merely starts with "rean". The arms match the analysis stem,
    not the prefix."""
    row = _row("Reanalgesia protocols after surgery",
               "Reanalgesia protocols after surgery were compared across wards.")
    assert not _matches(specs, "reproduction-signal", row)
