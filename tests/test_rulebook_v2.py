"""What the `replication-claim` family matches and what it does not.

Patterns only — where a claimed row is routed is asserted in the policy table of
`tests/test_engine_spec.py`. `tests/test_spec_vocabulary.py` imports these helpers.

The twelve arms are spread over four tiers that differ only in how much they ask
for, so an arm is exercised through `_matches_family()` — does ANY tier claim this
string — and the narrowing each tier adds is asserted separately.
"""

import json
from pathlib import Path

import pytest

from filter.engine.backends import eval_spec_rows
from filter.engine.spec import load_specs

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"


@pytest.fixture(scope="module")
def specs() -> list:
    return load_specs(SPEC_DIR)


def _row(title: str, abstract: str, work: int = 1, doi: str = None,
         type_: str = "article") -> dict:
    return {
        "id": f"https://openalex.org/W{work}",
        "doi": doi if doi is not None else f"10.1000/v2.{work}",
        "title": title,
        "display_name": title,
        "publication_year": 2020,
        "type": type_,
        "authorships": "[]",
        "primary_location": "{}",
        "open_access": "{}",
        "concepts": json.dumps([]),
        "abstract_text": abstract,
        "hit_token_title": True,
        "hit_token_abstract": True,
        "hit_concept": False,
    }


CLAIM_TIERS = ("replication-claim-cited-title", "replication-claim-title-strong",
               "replication-claim-title-broad",
               "replication-claim-text", "replication-claim-residual")


def _matches_family(specs: list, row: dict) -> bool:
    """Whether any tier of the `replication-claim` family claims *row*."""
    return any(_matches(specs, tier, row) for tier in CLAIM_TIERS)


def _matches(specs: list, spec_id: str, row: dict) -> bool:
    """Whether *spec_id* claims *row*, through the engine's one evaluator."""
    spec = next(s for s in specs if s.id == spec_id)
    return bool(eval_spec_rows(spec, [row])[0])


# ---------------------------------------------------------------------------
# Rule B: the arms the phrase evidence bought
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("abstract", [
    # first person, with up to two words in the gap
    "We also replicated the anchoring effect documented in the original report.",
    "We have now replicated the original pattern in a larger sample.",
    # fail / unable / attempt to replicate, with an optional adverb
    "The present study failed to fully replicate the original depletion result.",
    "We were unable to replicate the effect in either sample.",
    "This is an attempt to replicate the classic finding in a new population.",
    # the failure nouns: a construction no verb arm reaches
    "This paper reviews documented failures to replicate the ego-depletion effect.",
    "Three failed replications of the facial-feedback effect are reported.",
    "We present a failed replication of an influential 2009 finding.",
    # negation
    "The original effect did not replicate in any of the three samples.",
    "The reported association could not be replicated in a preregistered test.",
    # qualifier family, replication study, replicate-and-extend, study replicated,
    # our replication, success*, replication attempt
    "We report a direct replication of the anchoring effect.",
    "A many-labs replication of the facial-feedback hypothesis is reported.",
    "This replication study re-tests the original claim in a new population.",
    "We replicate and extend the original finding to a clinical sample.",
    "The present study replicated the effect at a comparable sample size.",
    "Our replication used the original materials throughout.",
    "We report a successful replication of the classic effect.",
    "This replication attempt used the authors' own stimuli.",
])
def test_rule_b_claims_a_self_referential_replication_claim(specs, abstract):
    """One string per arm: the paper says that IT is or did a replication."""
    assert _matches_family(specs, _row("A new sample", abstract))


@pytest.mark.parametrize("abstract", [
    # "able to replicate" is not "unable to replicate", and B has no arm for the
    # engineering sense of replicating a function
    "A prosthetic foot able to replicate the function of the biological foot.",
    # no passive, gerund, `replicab*` or bare third-person arm: these are the
    # biological-replicate, virology, methodological-praise and protocol senses
    "Each treatment was replicated 22 times across the grassland plots.",
    "Host genes play key roles in replicating the virus inside the cell.",
    "The paper presents a replicable and highly replicability-focused model.",
    "This paper replicates a widely used measurement protocol.",
    # no sought/tried/wanted-to-replicate arm
    "The authors tried to replicate the depletion result and did not succeed.",
    # the two deliberate exclusions
    "This registered report of a new intervention was accepted in principle.",
    "We report a replication of the Smith effect.",
])
def test_rule_b_refuses_what_it_leaves_out(specs, abstract):
    """No arm of B claims these — the senses it excludes and the two phrases it
    deliberately does not carry (bare `replication of`, `registered report of`)."""
    assert not _matches_family(specs, _row("Grassland ecology", abstract))


# ---------------------------------------------------------------------------
# What each tier adds on top of the arms
# ---------------------------------------------------------------------------


_CLAIM_TITLE = "A direct replication of the anchoring effect"
_CITED_TITLE = "A direct replication of Smith et al. (2009)"


@pytest.mark.parametrize("tier,title,abstract,claimed", [
    # the arm is in the abstract only: neither title tier reaches it
    ("replication-claim-title-strong", "Anchoring in context",
     "We report a direct replication of Smith (2009).", False),
    ("replication-claim-cited-title", "Anchoring in context",
     "We report a direct replication of Smith (2009).", False),
    # the arm is in the title, and only the cited-title tier asks for the year too
    ("replication-claim-title-strong", _CLAIM_TITLE, "No target is named.", True),
    ("replication-claim-cited-title", _CLAIM_TITLE, "No target is named.", False),
    ("replication-claim-cited-title", _CITED_TITLE, "No target is named.", True),
    # the strong/residual split: a residual arm is claimed by its own tier only
    ("replication-claim-text", "Anchoring in context",
     "The original effect did not replicate in any of the three samples.", False),
    ("replication-claim-residual", "Anchoring in context",
     "The original effect did not replicate in any of the three samples.", True),
    ("replication-claim-text", "Anchoring in context",
     "This replication study re-tests the original claim.", True),
    ("replication-claim-residual", "Anchoring in context",
     "This replication study re-tests the original claim.", False),
    # outside a title the text tier asks for a strong arm and nothing else: a
    # named work in the abstract neither adds to the claim nor is required by it,
    # which is why the `replication-claim-cited` tier was dropped (rule_ideas.md)
    ("replication-claim-text", "Anchoring in context",
     "This replication study re-tests the claim of Smith (2009).", True),
])
def test_each_tier_asks_for_exactly_what_its_name_says(specs, tier, title, abstract,
                                                       claimed):
    assert _matches(specs, tier, _row(title, abstract)) is claimed


def test_the_citation_pattern_is_a_word_then_a_year_and_no_more(specs):
    """What the pattern actually requires, stated as three cases so the looseness is
    on the record rather than in the description only: a word of three or more
    characters, then spaces or commas, then a year. It is case-insensitive like
    every other spec regex, so `[A-Z]` describes the surface form and does not
    require a capital — and any word will do, not only a surname."""
    # any word, not only a name: this is the pattern's known looseness
    assert _matches(specs, "replication-claim-cited-title",
                    _row("A direct replication, 2009-2011 cohort", "No target."))
    # lowercase surname: the [A-Z] atom does not require a capital
    assert _matches(specs, "replication-claim-cited-title",
                    _row("A direct replication of smith 2009", "No target."))
    # punctuation between the word and the year breaks the pattern
    assert not _matches(specs, "replication-claim-cited-title",
                        _row("A direct replication: 2009", "No target."))
