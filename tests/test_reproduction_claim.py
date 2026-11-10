"""The reproduction-claim arms and their known ambiguous surface forms."""

from pathlib import Path

import pytest

from filter.engine.backends import eval_spec_rows
from filter.engine.route import route_batch
from filter.engine.spec import load_specs
from tests.test_engine_route import _batch, _row


@pytest.fixture(scope="module")
def specs():
    return load_specs(Path(__file__).resolve().parents[1] / "filter" / "spec")


@pytest.mark.parametrize(
    ("title", "abstract", "expected"),
    [
        ("Replication Report on Lee (2021)", "We examine Lee's estimates.", True),
        ("A reproduction report", "Numerical checks of prior estimates.", True),
        ("A Computational and Robustness Reproduction of Lee (2021)", "Checks.", True),
        ("Robustness Reproducibility of Lee (2021)", "Checks.", True),
        (
            "A Comment on Lee (2021)",
            "I reproduced their original findings using their code.",
            True,
        ),
        (
            "A reanalysis of Lee's findings",
            "We have successfully reproduced the original estimates.",
            True,
        ),
        (
            "A replication of the wage effect",
            "We were able to reproduce the paper's main results.",
            True,
        ),
        (
            "A Comment on Lee (2021)",
            "We first conduct a computational reproduction.",
            True,
        ),
        (
            "Checking published analyses",
            "We assess computational reproducibility.",
            True,
        ),
        ("A new technique", "We reproduce the original results.", False),
        (
            "A Comment on Lee (2021)",
            "Lee and Smith reproduced the original findings.",
            False,
        ),
        (
            "A Comment on Lee (2021)",
            "This paper may be reproduced without permission of the authors.",
            False,
        ),
        (
            "A Comment on Lee (2021)",
            "We find that species which reproduce at older ages differ in their lifespans.",
            False,
        ),
        ("A new gene", "We reanalyzed published data to identify novel genes.", False),
        (
            "Discussion of reproducibility",
            "We discuss computational reproducibility.",
            False,
        ),
        (
            "Response to a replication report",
            "We thank the replicators for their comments.",
            False,
        ),
        (
            "A Reply to Lee",
            "We reproduced their published results and performed additional checks.",
            True,
        ),
    ],
)
def test_claim_patterns(specs, title, abstract, expected):
    spec = next(s for s in specs if s.id == "reproduction-claim")
    assert eval_spec_rows(spec, [_row(title=title, abstract=abstract)]) == [expected]


def test_structural_discards_and_text_gate_still_apply(specs):
    rows = [
        _row(
            work="W1",
            title="Reproduction Report",
            doi="10.7910/dvn/example",
            abstract="We reproduce the original results.",
        ),
        _row(work="W2", title="Reproduction Report", abstract=""),
        _row(
            work="W3",
            title="Reproduction Report",
            abstract="We reproduce the original results.",
        ),
    ]
    routed = route_batch(specs, _batch(rows)).to_pylist()
    assert routed[0]["pile"] == "discard"
    assert (routed[1]["pile"], routed[1]["pending_reason"]) == ("pending", "no_text")
    assert (routed[2]["pile"], routed[2]["rule_id"]) == (
        "screen_expensive",
        "reproduction-claim",
    )


@pytest.mark.parametrize(
    "claim",
    [
        "We conducted a computational reproduction.",
        "This paper assessed computational reproducibility.",
        "This study has investigated computational reproducibility.",
        "This report undertook a computational reproduction.",
        "This paper tests computational reproducibility.",
        "We tested the computational reproducibility of published results.",
        "We assess the extent of computational reproducibility.",
    ],
)
def test_inflected_computational_claims(specs, claim):
    spec = next(s for s in specs if s.id == "reproduction-claim")
    assert eval_spec_rows(
        spec, [_row(title="Checking published results", abstract=claim)]
    ) == [True]


@pytest.mark.parametrize(
    "claim",
    [
        "We present a framework to improve computational reproducibility.",
        "We provide computational reproducibility materials.",
        "We tested a framework to improve computational reproducibility.",
        "This paper assesses a tool for computational reproducibility.",
    ],
)
def test_tooling_is_not_a_computational_reproduction_claim(specs, claim):
    spec = next(s for s in specs if s.id == "reproduction-claim")
    assert eval_spec_rows(
        spec, [_row(title="A new software framework", abstract=claim)]
    ) == [False]


@pytest.mark.parametrize("separator", [". ", "; ", "\n"])
@pytest.mark.parametrize("tooling_first", [True, False])
def test_tooling_clause_does_not_cancel_a_genuine_claim(
    specs, separator: str, tooling_first: bool
) -> None:
    clauses = [
        "We tested a framework to improve computational reproducibility",
        "We assessed the computational reproducibility of the published findings",
    ]
    if not tooling_first:
        clauses.reverse()
    spec = next(s for s in specs if s.id == "reproduction-claim")
    assert eval_spec_rows(
        spec, [_row(title="Checking published results", abstract=separator.join(clauses))]
    ) == [True]


def test_tooling_after_the_computational_phrase_remains_ambiguous(specs) -> None:
    spec = next(s for s in specs if s.id == "reproduction-claim")
    assert eval_spec_rows(
        spec,
        [_row(
            title="A new software framework",
            abstract="We tested a computational reproducibility framework.",
        )],
    ) == [True]


def test_replay_counts_discard_to_pending_downgrades(tmp_path: Path) -> None:
    import gzip
    import json

    from analysis.i4r_rule_replay import replay

    candidates = tmp_path / "candidates.jsonl.gz"
    with gzip.open(candidates, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "id": "https://openalex.org/W1",
            "title": "Reproduction Report",
            "abstract_text": "",
            "type": "grant",
            "i4r_numbers": [],
        }) + "\n")
    result = replay(candidates, Path(__file__).resolve().parents[1] / "filter/spec")
    assert result["new_screen_work_ids"] == 0
    assert result["still_no_text"] == 1
    assert result["discard_pile_changes"] == 1
