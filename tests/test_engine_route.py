"""Wave B: the two backends, routing, work ids and routing releases.

One test per seam. The corpus below is crafted so every shipped spec — shadow
ones included — claims at least one row: the backend-equality test is only worth
running over rows that actually exercise the bundle.
"""

import json
from pathlib import Path

import pyarrow as pa
import pytest

from filter.engine.backends import eval_spec_batch, eval_spec_rows, verify_backends
from filter.engine.release import read_release, routing_release, write_release
from filter.engine.route import eval_all, route_batch
from filter.engine.spec import load_specs
from filter.engine.workids import load_aliases, resolve, work_id
from search.snapshot_scan import _POOL_SCHEMA

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"


# ---------------------------------------------------------------------------
# Fixtures: pool-shaped rows
# ---------------------------------------------------------------------------


def _row(work="https://openalex.org/W1", doi=None, title="A study of bees",
         abstract="Bees are nice.", type_="article", year=2024,
         concepts=()) -> dict:
    """One survivor-pool row (`_POOL_SCHEMA`); *concepts* are bare ids."""
    return {
        "id": work,
        "doi": doi,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "type": type_,
        "authorships": "[]",
        "primary_location": "{}",
        "open_access": "{}",
        "concepts": json.dumps([{"id": f"https://openalex.org/{c}",
                                 "display_name": "Concept", "score": 0.9}
                                for c in concepts]),
        "abstract_text": abstract,
        "hit_token_title": True,
        "hit_token_abstract": False,
        "hit_concept": bool(concepts),
    }


def _batch(rows: list[dict]) -> pa.RecordBatch:
    return pa.Table.from_pylist(rows, schema=_POOL_SCHEMA).to_batches()[0]


_CITE = "as reported by Smith et al. (2019)"

CORPUS = [
    # structural discards
    _row(work="https://openalex.org/W1", type_="dataset",
         title="Replication Data for: Bees", abstract="A direct replication of Smith (2019)."),
    _row(work="https://openalex.org/W2", doi="https://doi.org/10.7910/DVN/ABC",
         title="Replication Data for: Wasps", abstract="Deposit."),
    _row(work="https://openalex.org/W3", doi="10.6084/m9.figshare.123",
         title="Replication data", abstract="Figshare record."),
    _row(work="https://openalex.org/W4", doi="10.7287/peerj.10325v0.1/reviews/2",
         title="A replication of the Smith effect", abstract="Review object."),
    _row(work="https://openalex.org/W5", type_="peer-review",
         title="A replication of the Smith effect", abstract="Peer review."),
    # 500s exclusions
    _row(work="https://openalex.org/W6",
         title="Review for: A replication of the Smith effect", abstract="Editorial."),
    _row(work="https://openalex.org/W7", title="Open materials",
         abstract="Data and code are available on OSF to reproduce the results in this paper."),
    _row(work="https://openalex.org/W8", title="DNA replication in yeast",
         abstract="Molecular biology."),
    _row(work="https://openalex.org/W9", title="Replication fork stalling",
         abstract="Molecular biology."),
    _row(work="https://openalex.org/W10", title="On the replication of enteroviruses",
         abstract="Virology."),
    _row(work="https://openalex.org/W11", title="Replication of the code",
         abstract="Software note."),
    _row(work="https://openalex.org/W12", title="Replicated the dataset",
         abstract="Software note."),
    # the #44 rescue: an exclusion, a phrase and a cite
    _row(work="https://openalex.org/W13", title="Reanalysis of a classic finding",
         abstract=f"We replicated the code of Smith (2019); this is a replication "
                  f"of the original findings {_CITE}."),
    # routing rules
    _row(work="https://openalex.org/W14", title="A direct replication of the Smith effect",
         abstract=f"We report a direct replication of the anchoring effect, {_CITE}."),
    _row(work="https://openalex.org/W15", title="A direct replication",
         abstract="We report a direct replication of the anchoring effect."),
    _row(work="https://openalex.org/W16", title="Computational check",
         abstract="We reproduced the original results of the published analysis."),
    _row(work="https://openalex.org/W17", title="Replikationsstudie über Bienen",
         abstract="Eine Untersuchung."),
    _row(work="https://openalex.org/W18", title="Anchoring in context",
         abstract="A study of judgement.", concepts=("C12590798",)),
    # shadow arms
    _row(work="https://openalex.org/W19", title="Réplication des abeilles",
         abstract="Une etude."),
    _row(work="https://openalex.org/W20", title="Cell biology",
         abstract="The cells reproduce quickly."),
    # GWAS guard: phrase and cite, but the expensive route is refused
    _row(work="https://openalex.org/W21", title="A replication cohort study",
         abstract=f"We replicated the SNP association in a replication cohort, {_CITE}."),
    # no abstract, and a plain negative
    _row(work="https://openalex.org/W22",
         title="A direct replication of the Smith effect", abstract=None),
    _row(work="https://openalex.org/W23", title="A study of bees", abstract="Bees are nice."),
]


@pytest.fixture(scope="module")
def specs() -> list:
    return load_specs(SPEC_DIR)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def test_the_two_backends_agree_on_the_shipped_bundle_over_the_corpus(specs):
    table = pa.Table.from_pylist(CORPUS, schema=_POOL_SCHEMA)
    assert verify_backends(specs, table) == []


def test_every_shipped_spec_claims_at_least_one_corpus_row(specs):
    batch = _batch(CORPUS)
    unexercised = [spec.id for spec in specs
                   if not any(eval_spec_batch(spec, batch).to_pylist())]
    assert unexercised == []


def test_the_backends_ignore_the_loader_only_pyre_regex(specs):
    # biological-of ships a lookaround `pyre_regex` next to its RE2 decomposition;
    # the decomposition is wider, and both backends must evaluate the wider one.
    spec = next(s for s in specs if s.id == "biological-of")
    rows = [_row(title="Replication of the original study of DNA repair", abstract="")]
    assert eval_spec_rows(spec, rows) == [True]
    assert eval_spec_batch(spec, _batch(rows)).to_pylist() == [True]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _routed(specs, rows: list[dict]) -> list[dict]:
    return route_batch(specs, _batch(rows)).to_pylist()


def test_a_dataset_row_carrying_a_replication_phrase_discards_on_precedence(specs):
    row = _row(type_="dataset", title="Replication Data for: Bees",
               abstract="A direct replication of Smith (2019).")
    routed = _routed(specs, [row])[0]
    assert (routed["pile"], routed["rule_id"], routed["precedence"]) \
        == ("discard", "dataset-type", 950)


def test_a_phrase_in_the_title_with_no_abstract_routes_pending_no_text(specs):
    routed = _routed(specs, [_row(title="A direct replication of the Smith effect",
                                  abstract=None)])[0]
    assert (routed["pile"], routed["pending_reason"], routed["rule_id"]) \
        == ("pending", "no_text", "phrase-replication")


def test_a_deposit_doi_with_no_abstract_still_discards(specs):
    routed = _routed(specs, [_row(doi="https://doi.org/10.7910/DVN/ABC",
                                  title="Replication Data for: Wasps", abstract=None)])[0]
    assert (routed["pile"], routed["pending_reason"], routed["rule_id"]) \
        == ("discard", "", "deposit-doi-prefixes")
    assert routed["evidence"] == "10.7910"


def test_a_concept_only_row_routes_to_the_cheap_tier(specs):
    routed = _routed(specs, [_row(title="Anchoring in context",
                                  abstract="A study of judgement.",
                                  concepts=("C12590798",))])[0]
    assert (routed["pile"], routed["rule_id"]) == ("screen_cheap", "concept-replication")
    assert routed["evidence"] == "concept_ids=C12590798"


def test_the_same_row_without_the_concept_is_pending_rather_than_discarded(specs):
    # Issue #148 regression: Stage 2 wrote the concept arm `false_positive`
    # terminally. Nothing may discard a row for lacking a topical signal.
    routed = _routed(specs, [_row(title="Anchoring in context",
                                  abstract="A study of judgement.")])[0]
    assert routed["pile"] == "pending"
    assert routed["pending_reason"] == "no_filter_matched"
    assert routed["rule_id"] == ""


def test_matched_rules_lists_every_non_shadow_match_and_shadow_specs_never_win(specs):
    row = _row(title="On the replication of enteroviruses",
               abstract="We reproduced the original results of the published analysis.")
    routed = _routed(specs, [row])[0]
    shadow = {spec.id for spec in specs if spec.shadow}
    assert routed["rule_id"] not in shadow
    assert not shadow & set(routed["matched_rules"])
    assert routed["matched_rules"] == ["phrase-reproduction", "phrase-replication",
                                       "title-stem"]
    # ...while the shadow arms did evaluate, for the evaluations table.
    assert eval_all(specs, _batch([row]))["biological-of"].to_pylist() == [True]


def test_precomputed_evaluations_are_reused_rather_than_recomputed(specs):
    batch = _batch([_row(type_="dataset", title="Replication Data for: Bees")])
    evals = eval_all(specs, batch)
    evals["dataset-type"] = pa.array([False])
    assert route_batch(specs, batch, evals=evals).to_pylist()[0]["rule_id"] != "dataset-type"


# ---------------------------------------------------------------------------
# Work ids
# ---------------------------------------------------------------------------


def test_work_id_parses_the_three_accepted_forms_and_rejects_junk():
    assert work_id("https://openalex.org/W123") == 123
    assert work_id("W123") == 123
    assert work_id(" 123 ") == 123
    for junk in ("", "10.1037/abc", "A123", "https://openalex.org/"):
        with pytest.raises(ValueError):
            work_id(junk)


def test_an_alias_rekeys_a_routed_row(specs, tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"version": 1, "aliases": {"7": 4210170740}}))
    aliases = load_aliases(path)
    assert resolve(7, aliases) == 4210170740
    assert resolve(8, aliases) == 8
    routed = route_batch(specs, _batch([_row(work="https://openalex.org/W7")]),
                         aliases=aliases).to_pylist()[0]
    assert routed["work_id"] == 4210170740


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

_INPUTS = {
    "pool_manifest_hash": "pool1",
    "overlay_hash": None,
    "bundle_hash": "bundle1",
    "engine_version": "1",
    "alias_release": "alias1",
    "schema_version": "csv-v1",
}


def test_routing_release_is_stable_under_key_order_and_moves_with_any_input():
    baseline = routing_release(**_INPUTS)
    assert routing_release(**dict(reversed(list(_INPUTS.items())))) == baseline
    for key in _INPUTS:
        changed = dict(_INPUTS, **{key: "changed"})
        assert routing_release(**changed) != baseline


def test_a_written_release_round_trips_with_its_id_and_timestamp(tmp_path):
    release = dict(_INPUTS, created_at="2026-08-04T12:00:00+00:00")
    path = write_release(release, cache_dir=tmp_path)
    release_id = routing_release(**_INPUTS)
    assert path == tmp_path / "releases" / f"{release_id}.json"
    record = read_release(release_id, cache_dir=tmp_path)
    assert record["release_id"] == release_id
    assert record["created_at"] == "2026-08-04T12:00:00+00:00"
    assert {k: record[k] for k in _INPUTS} == _INPUTS
