"""Wave B: the backend, routing, work ids and routing releases.

One test per seam, in two layers that do not overlap.

The BACKEND tests run the shipped bundle over a corpus crafted so every shipped
spec — shadow ones included — claims at least one row. They assert what a pattern
matches, never where a row is routed. There is one evaluator (pyarrow compute),
so what they pin is that every entry point reaches it: `eval_spec_rows()` is the
row-shaped door onto `eval_spec_batch()`, not a second implementation.

The ROUTING tests run the synthetic bundle of `tests/engine_bundle.py`, because
precedence, the shadow contract and the `no_text` downgrade are properties of
`route_batch()` rather than of any rule. Where the shipped rules route is
asserted once, in the policy table of `tests/test_engine_spec.py`.
"""

import json
import unicodedata
from pathlib import Path

import pyarrow as pa
import pytest

from filter.engine.backends import eval_spec_batch, eval_spec_rows, match_evidence
from filter.engine.release import read_release, routing_release, write_release
from filter.engine.route import eval_all, route_batch
from filter.engine.spec import FilterSpec, load_specs
from filter.engine.workids import load_aliases, resolve, work_id
from search.snapshot_scan import _POOL_SCHEMA
from tests import engine_bundle

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
    # definitional discards (960 / 955 / 940)
    _row(work="https://openalex.org/W1", type_="dataset",
         title="Replication Data for: Bees", abstract="A direct replication of Smith (2019)."),
    _row(work="https://openalex.org/W2", doi="https://doi.org/10.7910/DVN/ABC",
         title="Replication Data for: Wasps", abstract="Deposit."),
    _row(work="https://openalex.org/W3", doi="10.1000/jexp.2019.4471.suppl",
         title="Supplementary tables", abstract="Tables S1-S4."),
    _row(work="https://openalex.org/W4", doi="10.7287/peerj.10325v0.1/reviews/2",
         title="A replication of the Smith effect", abstract="Review object."),
    _row(work="https://openalex.org/W5", type_="peer-review",
         title="A replication of the Smith effect", abstract="Peer review."),
    _row(work="https://openalex.org/W6",
         title="Review for: A replication of the Smith effect", abstract="Editorial."),
    # the metadata-crosswalk discard (500)
    _row(work="https://openalex.org/W7", type_="paratext",
         title="Front matter", abstract="Contents of the replication special issue."),
    # rows the deleted exclusion band used to claim: kept because they are the
    # senses the whitelist must NOT admit, and a backend that started disagreeing
    # on molecular or storage prose would be a real divergence
    _row(work="https://openalex.org/W8", title="DNA replication in yeast",
         abstract="Molecular biology."),
    _row(work="https://openalex.org/W9", title="Replication fork stalling",
         abstract="Molecular biology."),
    _row(work="https://openalex.org/W10", title="On the replication of enteroviruses",
         abstract="Virology."),
    _row(work="https://openalex.org/W11", title="Replication of the code",
         abstract="Software note."),
    _row(work="https://openalex.org/W12", title="Open materials",
         abstract="Data and code are available on OSF to reproduce the results in this paper."),
    _row(work="https://openalex.org/W13", title="Reanalysis of a classic finding",
         abstract=f"We replicated the code of Smith (2019); this is a replication "
                  f"of the original findings {_CITE}."),
    # rule B, one row per shape the arms were measured on. W14's title carries
    # both a claim arm and an author-year cite, which is the live tier's shape.
    _row(work="https://openalex.org/W14",
         title="A direct replication of the Smith (2019) effect",
         abstract=f"We report a direct replication of the anchoring effect, {_CITE}."),
    _row(work="https://openalex.org/W15", title="A replication study of anchoring",
         abstract="We failed to replicate the original result; our replication "
                  "attempt could not be replicated either."),
    # reproduction-signal, the one live admission rule
    _row(work="https://openalex.org/W16", title="Computational check",
         abstract="We reproduced the original results of the published analysis."),
    # rule C: multilingual title stem, English title stem, concept, bare phrase
    _row(work="https://openalex.org/W17", title="Replikationsstudie über Bienen",
         abstract="Eine Untersuchung."),
    _row(work="https://openalex.org/W18", title="Anchoring in context",
         abstract="A study of judgement.", concepts=("C12590798",)),
    _row(work="https://openalex.org/W19", title="Réplication des abeilles",
         abstract="Une etude."),
    _row(work="https://openalex.org/W20", title="Bees and judgement",
         abstract="This paper offers a replication of an earlier result."),
    # rule D: one probe arm, so the shadow rule is exercised too
    _row(work="https://openalex.org/W21", title="Revisiting the Tiebout hypothesis",
         abstract="We repeated the analysis on a new panel of municipalities."),
    # the GWAS shape the deleted guard existed for: no arm admits it now
    _row(work="https://openalex.org/W22", title="A replication cohort study",
         abstract=f"The SNP association held in a replication cohort, {_CITE}."),
    # no abstract, and a plain negative
    _row(work="https://openalex.org/W23",
         title="A direct replication of the Smith effect", abstract=None),
    _row(work="https://openalex.org/W24", title="A study of bees", abstract="Bees are nice."),
    # the OSF pair: overlay text whose first line is the registration template,
    # one completed record and one protocol (the protocol's own responses carry a
    # claim arm, which is the whole reason its discard outranks the 700s)
    _row(work="https://openalex.org/W25", doi="10.17605/OSF.IO/AB12D",
         title="Registered study",
         abstract="OSF registration template: Replication Recipe (Brandt et al., "
                  "2013): Post-Completion\n\nitem33: informative failure to replicate"),
    _row(work="https://openalex.org/W26", doi="10.17605/OSF.IO/CD34E",
         title="Registered study",
         abstract="OSF registration template: OSF Preregistration\n\nWe will run a "
                  "direct replication of the Smith (2019) effect."),
    # figshare-attachment (956): the prefix alone must not discard (D1), the
    # attachment-shaped title on the prefix must
    _row(work="https://openalex.org/W27", doi="10.6084/m9.figshare.123456",
         title="Supplementary material from \"A direct replication of the Smith "
               "(2019) effect\"",
         abstract="Tables and analysis code accompanying the replication."),
]


# Non-ASCII rows for the backend-equality check: accented Latin, NFD-decomposed
# text, CJK, Hangul, Cyrillic, fullwidth forms, and accented surnames in the
# citation position — the shapes where a Unicode `\w`/`\b` and an ASCII one part
# company. Several rows deliberately carry an English phrase too, so the cite
# clause is reached rather than short-circuited by the phrase block.
_NFD = unicodedata.normalize("NFD", "réplication de l'étude")

UNICODE_CORPUS = [
    _row(title="Réplication d'une étude classique",
         abstract="Nous avons répliqué l'effet d'ancrage, d'après Müller (2019)."),
    _row(title="Replikationsstudie über Bienen",
         abstract="Eine direkte Replikation der Ergebnisse, vgl. Müller et al. (2019)."),
    _row(title=_NFD, abstract=_NFD + " des résultats, cf. García et al. (2020)."),
    _row(title="Reprodução computacional dos resultados",
         abstract="Reproduzimos os resultados originais, cf. Gonçalves (2021)."),
    _row(title="追試研究：アンカリング効果の再現",
         abstract="本研究は Smith (2019) の追試である。"),
    _row(title="반복검증 연구", abstract="우리는 원래 결과를 재현했다 (Kim et al., 2020)."),
    _row(title="Репликация исследования",
         abstract="Мы воспроизвели результаты, Иванов (2018)."),
    _row(title="A direct replication of the Müller effect",
         abstract="We replicated the original findings, as reported by "
                  "García et al. (2020)."),
    _row(title="Replicación directa del efecto de anclaje",
         abstract="Reproducimos los resultados originales de Peña (2019)."),
    _row(title="DNA-Replikation in Hefé",
         abstract="Molekularbiologie: die Zellen reproduzieren sich."),
    _row(title="Ｒｅｐｌｉｃａｔｉｏｎ ｏｆ ｔｈｅ Ｓｍｉｔｈ ｅｆｆｅｃｔ",
         abstract="Fullwidth forms."),
    _row(title="replication of the original study",
         abstract="A replication of the original findings, Smith (2019)."),
    _row(title="Ré́plication", abstract="Double-accent junk, Smith et al. (2019)."),
    _row(title="Реплика́ция и replication of the original",
         abstract="We replicated the original findings, as reported by Müller (2019)."),
]


@pytest.fixture(scope="module")
def specs() -> list:
    return load_specs(SPEC_DIR)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("corpus", ["CORPUS", "UNICODE_CORPUS"])
def test_the_row_entry_point_is_the_batch_backend(specs, corpus):
    """`eval_spec_rows()` must be a door onto `eval_spec_batch()`, not a second
    implementation of it. Asserted over both corpora and every shipped spec —
    including UNICODE_CORPUS, where a second engine would part company first:
    accented and non-Latin titles, NFD, and accented surnames in the citation
    position, `\\w`/`\\b` being Unicode-aware in `re` and ASCII in RE2."""
    rows = globals()[corpus]
    batch = _batch(rows)
    for spec in specs:
        assert eval_spec_rows(spec, rows) == eval_spec_batch(spec, batch).to_pylist(), spec.id


def test_evidence_is_recovered_for_matched_rows_and_never_decides_one(specs):
    """`match_evidence()` reports WHERE a matched row matched. Every row the
    backend claims gets a non-empty string, and no row it did not claim is asked."""
    spec = next(s for s in specs if s.id == "replication-claim-text")
    rows = [_row(title="A replication of the Müller effect",
                 abstract="We successivement répliqué; we thereby replicated the "
                          "original findings of Müller.")]
    batch = _batch(rows)
    assert eval_spec_rows(spec, rows) == [True]
    assert eval_spec_batch(spec, batch).to_pylist() == [True]
    assert all(match_evidence(spec, batch))


def test_every_shipped_spec_claims_at_least_one_corpus_row(specs):
    batch = _batch(CORPUS)
    unexercised = [spec.id for spec in specs
                   if not any(eval_spec_batch(spec, batch).to_pylist())]
    assert unexercised == []


def test_the_backend_ignores_the_loader_only_pyre_regex():
    """The evaluator reads the decomposition, never the loader-only `pyre_regex` —
    which RE2 could not run anyway. No shipped rule carries the key, so the spec
    is built here."""
    spec = FilterSpec.from_dict({
        "id": "pyre-example",
        "description": "the decomposition is wider than the lookaround original",
        "match": {"any_of": [{"text_regex": r"\breplication of the original\b"}],
                  "pyre_regex": r"\breplication of the original(?! study)\b"},
        "pile": "screen_cheap",
        "precedence": 250,
    })
    rows = [_row(title="Replication of the original study of DNA repair", abstract="")]
    assert eval_spec_rows(spec, rows) == [True]
    assert eval_spec_batch(spec, _batch(rows)).to_pylist() == [True]


# ---------------------------------------------------------------------------
# Routing mechanics — synthetic bundle (tests/engine_bundle.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def syn() -> list:
    return engine_bundle.specs()


@pytest.fixture(scope="module")
def syn_routed(syn) -> dict:
    """The synthetic pool routed once, keyed by work id."""
    batch = pa.Table.from_pylist(engine_bundle.POOL_ROWS,
                                 schema=_POOL_SCHEMA).to_batches()[0]
    return {row["work_id"]: row for row in route_batch(syn, batch).to_pylist()}


def test_every_row_lands_in_the_pile_its_bundle_sends_it_to(syn_routed):
    """The whole routing table at once: four piles, a precedence conflict, a row
    nothing claims and two rows with no abstract."""
    assert {work: (row["pile"], row["rule_id"])
            for work, row in syn_routed.items()} == engine_bundle.EXPECTED_ROUTING


def test_the_highest_precedence_match_wins_and_the_rest_are_still_recorded(syn_routed):
    row = syn_routed[1]        # a dataset whose abstract also carries the phrase
    assert (row["rule_id"], row["precedence"]) == ("syn-dataset", 950)
    assert row["matched_rules"] == ["syn-dataset", "syn-replication"]


def test_a_shadow_spec_evaluates_and_is_never_a_match_a_winner_or_a_pile(syn, syn_routed):
    """The shadow contract: syn-shadow outranks every routing rule and claims both
    row 1 and row 6, and changes neither. Row 6 is claimed by nothing else, so if
    shadow could route it would show here as a discard rather than as pending."""
    batch = pa.Table.from_pylist(engine_bundle.POOL_ROWS,
                                 schema=_POOL_SCHEMA).to_batches()[0]
    evaluated = eval_all(syn, batch)["syn-shadow"].to_pylist()
    assert [evaluated[0], evaluated[5]] == [True, True]
    for row in syn_routed.values():
        assert row["rule_id"] != "syn-shadow"
        assert "syn-shadow" not in row["matched_rules"]
    assert (syn_routed[6]["pile"], syn_routed[6]["pending_reason"]) \
        == ("pending", "no_filter_matched")


def test_a_row_no_rule_claims_is_pending_rather_than_discarded(syn_routed):
    # Issue #148 regression: Stage 2 wrote the unclaimed arm `false_positive`
    # terminally. Nothing may discard a row for lacking a signal.
    row = syn_routed[6]
    assert (row["pile"], row["pending_reason"], row["rule_id"]) \
        == ("pending", "no_filter_matched", "")


def test_an_empty_abstract_downgrades_a_screening_route_but_not_a_discard(syn_routed):
    """Absence of evidence must not convert into a proceed — and must not convert
    into a discard either, since a discard rule reads no abstract."""
    screening, discarding = syn_routed[7], syn_routed[8]
    assert (screening["pile"], screening["pending_reason"], screening["rule_id"]) \
        == ("pending", "no_text", "syn-replication")
    assert (discarding["pile"], discarding["pending_reason"], discarding["rule_id"]) \
        == ("discard", "", "syn-deposit")


def test_the_winning_rule_reports_what_it_matched(syn_routed):
    """Evidence is recovered for the winner only, in each of its two forms."""
    assert syn_routed[8]["evidence"] == "10.7910"            # doi prefix
    assert syn_routed[1]["evidence"] == "type=dataset"       # field
    assert syn_routed[5]["evidence"] == "concept_ids=C12590798"
    assert syn_routed[4]["evidence"] == "direct replication"  # regex


def test_precomputed_evaluations_are_reused_rather_than_recomputed(syn):
    batch = pa.Table.from_pylist([engine_bundle.POOL_ROWS[0]],
                                 schema=_POOL_SCHEMA).to_batches()[0]
    evals = eval_all(syn, batch)
    evals["syn-dataset"] = pa.array([False])
    assert route_batch(syn, batch, evals=evals).to_pylist()[0]["rule_id"] \
        == "syn-replication"


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


def test_an_alias_rekeys_a_routed_row(syn, tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"version": 1, "aliases": {"7": 4210170740}}))
    aliases = load_aliases(path)
    assert resolve(7, aliases) == 4210170740
    assert resolve(8, aliases) == 8
    routed = route_batch(syn, _batch([_row(work="https://openalex.org/W7")]),
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
