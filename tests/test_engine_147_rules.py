"""Issue #147: the four measured rule changes, as engine routing behaviour.

One test per measured change, each asserting the NEW behaviour on concrete
strings taken from the evidence on branch `analysis/stage-b-eval`
(`analysis/stage_b_eval/`). Each test also carries the near-miss the change must
still block: a narrowing that stops blocking everything is not a narrowing.

The numbers behind these rules live in each spec's `measured` array; what is
tested here is that the rule does on a row what the measurement said it would.
"""

import json
from pathlib import Path

import pyarrow as pa
import pytest

from filter.engine.backends import eval_spec_batch, eval_spec_rows
from filter.engine.route import route_batch
from filter.engine.spec import load_specs
from search.snapshot_scan import _POOL_SCHEMA

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"


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
    """Whether *spec_id* claims *row*, asserted equal in both backends."""
    spec = next(s for s in specs if s.id == spec_id)
    by_row = eval_spec_rows(spec, [row])[0]
    batch = pa.Table.from_pylist([row], schema=_POOL_SCHEMA).to_batches()[0]
    by_batch = eval_spec_batch(spec, batch).to_pylist()[0]
    assert by_row == by_batch, f"{spec_id}: backends disagree on {row['title']!r}"
    return bool(by_row)


def _routed(specs: list, row: dict) -> dict:
    batch = pa.Table.from_pylist([row], schema=_POOL_SCHEMA).to_batches()[0]
    return route_batch(specs, batch).to_pylist()[0]


# ---------------------------------------------------------------------------
# 1. Phrase-morphology bundle T2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("abstract", [
    # `we <two words> replicate(d)` — 97 of the 319 no-phrase gold misses
    # (vocab_holes_report.md §3, candidate C2_we_gap_replicate).
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
])
def test_the_t2_arms_are_claimed_by_the_replication_phrase_rule(specs, abstract):
    """vocab_holes_report.md §5.1: T2 recovers 186 of the 319 gold positives that
    carry no matching phrase, for +9.3 admissions per million (+0.6%) and zero
    goldneg_screen hits."""
    row = _row("A new sample", abstract)
    assert _matches(specs, "phrase-replication", row)
    assert _routed(specs, row)["pile"] == "screen_cheap"


@pytest.mark.parametrize("abstract", [
    # C4 passive: "each treatment was replicated 22 times" (a biological
    # replicate) and "should be replicated in larger samples" (future work).
    "Each treatment was replicated 22 times across the grassland plots.",
    # C5 gerund: the virus/artifact sense.
    "Host genes play key roles in replicating the virus inside the cell.",
    # C9 replicab*: methodological praise.
    "The paper presents a replicable and highly replicability-focused model.",
    # C3 bare third person.
    "This paper replicates a widely used measurement protocol.",
    # C1's `able to replicate` arm, dropped in C1b: its cost was the whole
    # difference between C1 and C1b — "a prosthetic foot able to replicate the
    # function of the biological foot" (vocab_holes_report.md §4a).
    "A prosthetic foot able to replicate the function of the biological foot.",
])
def test_the_arms_t2_deliberately_leaves_out_are_still_not_phrases(specs, abstract):
    """The passive, gerund, `replicab*` and bare third-person arms are NOT taken:
    T4 buys 65 more gold positives at 13x T2's admissions (119.6 vs 9.3 per
    million), and their noise has no compact shape (vocab_holes_report.md §5.3)."""
    row = _row("Grassland ecology", abstract)
    assert not _matches(specs, "phrase-replication", row)


# ---------------------------------------------------------------------------
# 2. Title-phrase rescue
# ---------------------------------------------------------------------------


def test_a_title_phrase_rescues_an_abstract_only_technical_exclusion(specs):
    """exclusion_narrowing_report.md §4.1 (TO4 + TV3): where the exclusion fired
    only past the title/abstract join and the TITLE carries a replication phrase,
    the row goes to the tier that decides instead of being discarded."""
    row = _row("A Replication of 'The Role of Intrafirm Networks'",
               "We replicated the code of the earlier team's analysis on a new "
               "sample of firms.")
    assert _matches(specs, "technical-verb", row)        # the exclusion did fire
    routed = _routed(specs, row)
    assert routed["pile"] == "screen_cheap"
    assert routed["rule_id"] == "title-phrase-rescue"


def test_the_rescue_refuses_a_row_whose_title_is_itself_the_exclusion(specs):
    """The rescue's whole premise is that the exclusion matched PAST the title. A
    title that is itself the technical sense is not a paper claiming to be a
    replication, and the 500s band keeps it."""
    row = _row("Replication of the database schema for grid computing",
               "We describe replication of the database across grid nodes.")
    assert not _matches(specs, "title-phrase-rescue", row)
    assert _routed(specs, row)["pile"] == "discard"


def test_the_two_rescues_do_not_shadow_each_other(specs):
    """exclusion-rescue (650, phrase + author-year cite) outranks
    title-phrase-rescue (645, phrase in the title, no cite required). Both route to
    screen_cheap, so where both match the pile is identical and only the recorded
    rule differs — the higher precedence is not hiding a different outcome."""
    row = _row("A Replication of 'The Role of Intrafirm Networks'",
               "We replicated the code of Smith (2019) on a new sample of firms.")
    assert _matches(specs, "exclusion-rescue", row)
    assert _matches(specs, "title-phrase-rescue", row)
    routed = _routed(specs, row)
    assert routed["rule_id"] == "exclusion-rescue"
    assert routed["pile"] == "screen_cheap"


# ---------------------------------------------------------------------------
# 3. TECHNICAL_OBJECT / TECHNICAL_VERB narrowing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec_id,title,abstract", [
    ("technical-verb", "Replicating MOOC predictive models at scale",
     "We replicated the model of the original paper on a new MOOC cohort."),
    ("technical-verb", "A replication of an agent-based study",
     "We replicated the data of the published experiment exactly."),
    ("technical-object", "A Replication and Analysis of Tiebout Competition",
     "The analysis rests on replication of the data and replication of the method "
     "of the original agent-based study."),
    ("technical-object", "Cross-Model Replication Study",
     "A model replication of the published predictive model."),
])
def test_the_narrowed_technical_rules_release_the_reproduction_genre(
        specs, spec_id, title, abstract):
    """exclusion_narrowing_report.md §1 and §4.3-4.4: model/method/data/dataset is
    how a computational reproduction describes itself. TO2_both_tight recovers 18
    gold positives for 3.9 rows per million; TV1_tight_objects recovers 13 for 3.0
    — and between them they free 3 of the 5 indexed reproductions the pipeline
    loses (reproduction_coverage_report.md)."""
    row = _row(title, abstract)
    assert not _matches(specs, spec_id, row)
    assert _routed(specs, row)["pile"] != "discard"


@pytest.mark.parametrize("spec_id,title,abstract", [
    ("technical-object", "A Strategy for Database Replication in Ad Hoc Networks",
     "Database replication improves availability in ad hoc networks."),
    ("technical-object", "Replica placement for grid environments",
     "Replication of the pipeline across grid nodes reduces latency."),
    ("technical-verb", "Consistency management for distributed stores",
     "We replicated the database across three availability zones."),
    ("technical-verb", "Reproducible build pipelines",
     "We replicated the software of the vendor toolchain on our own cluster."),
])
def test_the_narrowed_technical_rules_still_block_the_storage_sense(
        specs, spec_id, title, abstract):
    """The near-miss each narrowing must still block. The matched-span census over
    5.6M real rows found the dominant referent of both patterns is distributed
    storage replication, not research methodology."""
    row = _row(title, abstract)
    assert _matches(specs, spec_id, row)
    assert _routed(specs, row)["pile"] == "discard"


# ---------------------------------------------------------------------------
# 4. GWAS scope ruling — BO1 and G1-minus-the-association-alternative
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", [
    "Replication of Genome-Wide Association Studies of Type 2 Diabetes "
    "Susceptibility in Japan",
    # Several of the gold rows write the dash as U+2010.
    "Replication of Genome‐Wide Association Studies in a Korean cohort",
    "Replication of Genome Wide association study loci for plasma glucose",
])
def test_bo1_stops_biological_of_killing_the_gwas_locus_genre(specs, title):
    """gwas_scope_classification.md §3: BO1 recovers 20 of the 36 BIOLOGICAL_OF
    kills — 17 qualifying, 1 internal, 2 unclear, and zero virology papers — for
    0.4 extra rows per million (~204 over the 510M-row snapshot). The clause was
    catching the gold genre 24 times and catching nothing else 0.4 times per
    million real rows."""
    row = _row(title, "We replicated eight loci previously reported in a European "
                      "population of comparable ancestry.")
    assert not _matches(specs, "biological-of", row)


@pytest.mark.parametrize("title,abstract", [
    ("Restriction of Replication of Oncolytic Herpes Simplex Virus",
     "The replication of the viral genome in host cells was measured by qPCR."),
    ("Inverted replication of vertebrate mitochondria",
     "Inhibition of the replication of genomic DNA was observed in the mutant."),
])
def test_bo1_leaves_the_organism_senses_the_clause_exists_for(specs, title, abstract):
    """BO1 exempts `genome-wide` and nothing else: the virology and molecular
    senses that make BIOLOGICAL_OF the pattern it is are untouched."""
    assert _matches(specs, "biological-of", _row(title, abstract))


def test_g1_lets_a_prior_report_stand_the_gwas_guard_down(specs):
    """gwas_scope_classification.md §3: G1 minus the `replicated the association`
    alternative recovers 12 papers, 12 of 12 qualifying under the maintainer's
    scope ruling and 0 internal designs, at zero measured extra admissions. 11-16
    genuine qualifying papers remain unreachable by any same-sentence regex,
    because they attribute the prior report in a different sentence — a screen
    judgment, not a keyword one."""
    row = _row("Replication of the Wellcome Trust genome-wide association study on "
               "essential hypertension in a Korean population",
               "We replicated eight loci previously reported by Smith et al. (2010) "
               "in a European sample of comparable size.")
    assert _matches(specs, "phrase-with-cite", row)
    assert _routed(specs, row)["pile"] == "screen_expensive"


def test_g1_still_suppresses_the_internal_two_stage_design(specs):
    """The maintainer's ruling puts two-stage discovery-plus-own-replication-cohort
    designs OUT of scope, and G1 as originally written leaked two of them through
    its `replicated the association` alternative (10.1002/hbm.22247,
    10.1038/s41598-023-31701-w). That alternative is not shipped, so the guard
    still holds on this shape."""
    row = _row("A metabolome-wide association study of serum laurylcarnitine and "
               "depression",
               "In 1411 participants of the KORA F4 study (discovery cohort) we "
               "identified an association. We replicated the association in an "
               "independent sample of 968 participants of the SHIP-Trend study "
               "(replication cohort), following the protocol of Jones et al. (2015).")
    assert not _matches(specs, "phrase-with-cite", row)
    assert _routed(specs, row)["pile"] != "screen_expensive"
