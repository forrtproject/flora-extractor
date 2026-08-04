"""What `replication-claim` matches and what it does not.

Patterns only — where a claimed row is routed is asserted in the policy table of
`tests/test_engine_spec.py`. `_matches()` asserts the two backends agree on every
string it is given; `tests/test_spec_vocabulary.py` imports both helpers.
"""

import json
from pathlib import Path

import pyarrow as pa
import pytest

from filter.engine.backends import eval_spec_batch, eval_spec_rows
from filter.engine.spec import load_specs
from search.snapshot_scan import _POOL_SCHEMA

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


def _matches(specs: list, spec_id: str, row: dict) -> bool:
    """Whether *spec_id* claims *row*, asserted equal in both backends."""
    spec = next(s for s in specs if s.id == spec_id)
    by_row = eval_spec_rows(spec, [row])[0]
    batch = pa.Table.from_pylist([row], schema=_POOL_SCHEMA).to_batches()[0]
    by_batch = eval_spec_batch(spec, batch).to_pylist()[0]
    assert by_row == by_batch, f"{spec_id}: backends disagree on {row['title']!r}"
    return bool(by_row)


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
    assert _matches(specs, "replication-claim", _row("A new sample", abstract))


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
    assert not _matches(specs, "replication-claim", _row("Grassland ecology", abstract))
