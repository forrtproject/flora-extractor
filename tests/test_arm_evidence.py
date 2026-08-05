"""One test per seam of `analysis/arm_evidence.py`, over a tiny synthetic pool.

The tool is a measurement instrument, so what is worth testing is the places it
could silently report the wrong thing: exclusive-vs-total attribution, the
field selector that makes a title-position variant scoreable against the same
regex on the whole text, the cohort split that keeps a stale prompt's verdicts
out of a precision figure, the marker that stops a two-row cell reading like
evidence, and the missing routing store — the tool must still run before
anything has been routed. The scan's speed is deliberately not tested: it is
reported on every run instead.
"""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.arm_evidence import (
    MIN_LABELLED,
    DECORATION,
    pattern_arms,
    render,
    run,
)
from search.snapshot_scan import _POOL_SCHEMA

# Two arms with a deliberate overlap: `alpha` and `beta` share row W3, so total
# and exclusive counts differ for both.
_POOL = [
    # (work id, doi, title, abstract)
    ("W1", "10.1/a", "An alpha paper", "nothing else here"),
    ("W2", "10.1/b", "Ordinary title", "we say alpha in the abstract"),
    ("W3", "10.1/c", "An alpha and beta paper", "both"),
    ("W4", "10.1/d", "A beta paper", "only the second arm"),
    ("W5", "10.1/e", "Unrelated", "no arm matches this row"),
]


def _pool_row(work: str, doi: str, title: str, abstract: str) -> dict:
    return {"id": f"https://openalex.org/{work}", "doi": doi, "title": title,
            "display_name": title, "publication_year": 2024, "type": "article",
            "authorships": "[]", "primary_location": "{}", "open_access": "{}",
            "concepts": "[]", "abstract_text": abstract,
            "hit_token_title": True, "hit_token_abstract": False, "hit_concept": False}


@pytest.fixture
def env(tmp_path) -> dict:
    """A two-file pool, a flora list, a negatives CSV and two verdict cohorts."""
    pool = tmp_path / "pool"
    pool.mkdir()
    rows = [_pool_row(*row) for row in _POOL]
    pq.write_table(pa.Table.from_pylist(rows[:3], schema=_POOL_SCHEMA),
                   pool / "part-0001.parquet")
    pq.write_table(pa.Table.from_pylist(rows[3:], schema=_POOL_SCHEMA),
                   pool / "part-0002.parquet")

    flora = tmp_path / "flora.csv"
    flora.write_text("doi_r,alt_identifier_r\n10.1/a,\n10.1/c,\n10.1/d,\n10.1/zz,\n",
                     encoding="utf-8")

    negatives = tmp_path / "not_a_replication.csv"
    negatives.write_text(
        "doi_r,title_r,abstract_r\n"
        "10.9/n1,An alpha paper,x\n"
        "10.9/n2,A beta paper,x\n",
        encoding="utf-8")

    cache = tmp_path / "llm"
    cache.mkdir()
    _write_cohort(cache, "cur", "CURRENT PROMPT", "gemini+gpt", [
        ("An alpha paper", "proceed"),
        ("An alpha and beta paper", "discard"),
        ("A beta paper", "proceed"),
    ])
    _write_cohort(cache, "old", "STALE PROMPT", "old-model", [
        ("An alpha paper", "discard"),
        ("A beta paper", "discard"),
    ])

    return {"pool_dir": pool, "store": tmp_path / "absent.duckdb", "release": None,
            "flora_path": flora, "negatives_path": negatives, "cache_dir": cache,
            "aliases_path": tmp_path / "aliases.json", "workers": 2,
            "all_cohorts": False, "conjunct": None, "source": "test"}


def _write_cohort(cache: Path, tag: str, preamble: str, model: str,
                  items: list[tuple[str, str]]) -> None:
    for i, (title, verdict) in enumerate(items):
        prompt = (f"{preamble}\n\nTitle: {title}\n\nAbstract: an abstract\n\n"
                  "Respond with the JSON object only.")
        (cache / f"classify_{tag}{i}.json").write_text(
            json.dumps({"llm_prompt": prompt, "llm_model": model,
                        "screen_verdict": verdict}), encoding="utf-8")


ALPHA = r"\balpha\b"
BETA = r"\bbeta\b"


def test_exclusive_and_total_attribution(env):
    """W3 matches both arms, so it counts in `pool` for each and in neither's
    `pool_exclusive` — pool, FLoRA and negatives all use that split."""
    report = run(pattern_arms([ALPHA, BETA], None), **env)
    alpha, beta = report["arms"]

    assert (alpha["pool"], alpha["pool_exclusive"]) == (3, 2)   # W1, W2, W3
    assert (beta["pool"], beta["pool_exclusive"]) == (2, 1)     # W3, W4
    assert report["scan"]["matched"] == 4
    # FLoRA knows 10.1/a, /c, /d; /zz is not in the pool.
    assert report["flora"]["dois"] == 4 and report["flora"]["in_pool"] == 3
    assert (alpha["flora"], alpha["flora_exclusive"]) == (2, 1)
    assert (beta["flora"], beta["flora_exclusive"]) == (2, 1)
    assert alpha["yield_per_1k"] == 500.0                       # 1 exclusive / 2 rows
    assert (alpha["negatives"], beta["negatives"]) == (1, 1)


def test_field_selector_separates_title_from_text(env):
    """The position lever: `alpha` in W2's abstract counts for `text:` and not
    for `title:`, which is the comparison the selector exists to make."""
    report = run(pattern_arms([f"title:{ALPHA}", f"text:{ALPHA}"], None), **env)
    title_arm, text_arm = report["arms"]

    assert title_arm["pool"] == 2                                # W1, W3
    assert text_arm["pool"] == 3                                 # + W2 (abstract)
    assert title_arm["pool_exclusive"] == 0                      # every title hit is a text hit
    assert text_arm["pool_exclusive"] == 1                       # W2 alone


def test_cohorts_are_scored_one_at_a_time(env):
    """Two prompt templates are two instruments; the smaller one must not be
    folded into the larger one's proceed rate."""
    arms = pattern_arms([ALPHA, BETA], None)
    largest = run(arms, **env)
    assert largest["screen"]["cohorts"] == 2
    assert largest["screen"]["n"] == 3                           # the current-prompt cohort
    assert largest["screen"]["proceed"] == 2
    assert largest["arms"][0]["screen_n"] == 2                   # alpha titles in that cohort

    mixed = run(arms, **{**env, "all_cohorts": True})
    assert mixed["screen"]["n"] == 5 and mixed["screen"]["proceed"] == 2


def test_thin_cells_are_marked_as_decoration(env):
    """Every count and rate resting on fewer than MIN_LABELLED rows carries the
    marker, and the legend explaining it is printed."""
    report = run(pattern_arms([ALPHA, BETA], None), **env)
    text = render(report)

    assert f"{DECORATION} fewer than {MIN_LABELLED} labelled rows" in text
    assert "BIAS:" in text
    # Every labelled cell here rests on 1-2 rows; the pool columns are not labels
    # and carry no marker.
    row = next(line for line in text.splitlines() if line.startswith("arm01"))
    # flora, fl.excl, yield, neg, neg.excl and both screen rates.
    assert row.count(DECORATION) == 7
    assert f"3{DECORATION}" not in row     # `pool` = 3, unmarked
    assert f"2{DECORATION}" in row         # `flora` = 2, marked


def test_runs_without_a_routing_store(env):
    """No store means a missing column, not a crash."""
    report = run(pattern_arms([ALPHA], None), **env)

    assert report["release"] is None
    assert report["arms"][0]["admitted"] is None
    assert "unavailable" in render(report)
