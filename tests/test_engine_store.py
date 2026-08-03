"""Wave C: the routing store, the pile export and the CLI.

One test per seam. The fixture pool is a handful of rows chosen so the shipped
bundle sends them to four different piles — a store test over rows that all land
in one pile would not notice the store losing a column.
"""

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from filter.engine import cli
from filter.engine.export import export_pile, load_conventions
from filter.engine.spec import load_specs
from filter.engine.store import (
    build_routing, open_store, pile_counts, rule_hits, sample_pile,
)
from search.snapshot_scan import _POOL_SCHEMA
from shared.schema import ENGINE_EXPORTED_COLS, validate_csv_columns

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"

_CITE = "as reported by Smith et al. (2019)"


def _row(work: int, doi=None, title="A study of bees", abstract="Bees are nice.",
         type_="article", year=2024, concepts=()) -> dict:
    return {
        "id": f"https://openalex.org/W{work}",
        "doi": doi,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "type": type_,
        "authorships": json.dumps([{"author": {"display_name": "A. Author"}}]),
        "primary_location": json.dumps({"source": {"display_name": "J. Repl."},
                                        "landing_page_url": "https://example.org/1"}),
        "open_access": json.dumps({"oa_url": None}),
        "concepts": json.dumps([{"id": f"https://openalex.org/{c}"} for c in concepts]),
        "abstract_text": abstract,
        "hit_token_title": True,
        "hit_token_abstract": False,
        "hit_concept": bool(concepts),
    }


# Four piles: a structural discard, an expensive route, two cheap routes (one of
# them vocabulary-bearing), and a row nothing claims.
POOL_ROWS = [
    _row(1, type_="dataset", title="Replication Data for: Bees", year=2020),
    _row(2, title="A direct replication of the Smith effect",
         abstract=f"We report a direct replication of the anchoring effect, {_CITE}.",
         year=2021),
    _row(3, title="Computational check", year=2022,
         abstract="We reproduced the original results of the published analysis."),
    _row(4, title="A direct replication", year=2023,
         abstract="We report a direct replication of the anchoring effect."),
    _row(5, title="Anchoring in context", abstract="A study of judgement.",
         year=2024, concepts=("C12590798",)),
    _row(6, title="A study of bees", year=2025),
    _row(7, title="A direct replication of the Smith effect", abstract=None, year=2026),
]


@pytest.fixture
def pool(tmp_path) -> Path:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    pq.write_table(pa.Table.from_pylist(POOL_ROWS, schema=_POOL_SCHEMA),
                   pool_dir / "2024.parquet")
    return pool_dir


@pytest.fixture
def specs() -> list:
    return load_specs(SPEC_DIR)


@pytest.fixture
def routed(pool, specs):
    con = open_store(Path(":memory:"))
    build_routing(con, pool, specs, "rel-a")
    return con


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_building_the_same_release_twice_replaces_rather_than_duplicates(pool, specs):
    con = open_store(Path(":memory:"))
    first = build_routing(con, pool, specs, "rel-a")
    once = pile_counts(con, "rel-a")
    once_hits = rule_hits(con, "rel-a")

    assert build_routing(con, pool, specs, "rel-a") == first
    assert pile_counts(con, "rel-a") == once
    assert rule_hits(con, "rel-a") == once_hits
    assert sum(once.values()) == len(POOL_ROWS)


def test_the_store_records_shadow_specs_that_never_won_a_pile(routed, specs):
    shadow = {spec.id for spec in specs if spec.shadow}
    hits = rule_hits(routed, "rel-a")
    assert shadow & set(hits), "no shadow spec matched the fixture pool"
    piled = {row["rule_id"] for row in sample_pile(routed, "rel-a", "screen_cheap", 99, 1)}
    assert not shadow & piled


def test_sampling_a_pile_is_stable_under_its_seed(routed):
    full = sample_pile(routed, "rel-a", "screen_cheap", 99, seed=17)
    first = sample_pile(routed, "rel-a", "screen_cheap", 1, seed=17)
    assert first == sample_pile(routed, "rel-a", "screen_cheap", 1, seed=17)
    assert first == full[:1], "a smaller n must truncate the same order, not reorder it"
    assert {row["pile"] for row in full} == {"screen_cheap"}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _exported(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)


def test_a_cheap_pile_export_writes_the_engine_columns_in_order_with_a_bom(
        routed, pool, tmp_path):
    out = tmp_path / "cheap.csv"
    manifest = export_pile(routed, pool, "screen_cheap", out, "rel-a",
                           created_at="2026-08-04T00:00:00+00:00")

    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    df = _exported(out)
    assert list(df.columns) == ENGINE_EXPORTED_COLS
    assert manifest["rows"] == len(df)
    assert set(df["source"]) == {"openalex_snapshot"}
    assert set(df["release_id"]) == {"rel-a"}
    assert set(df["filter_confidence"]) == {"medium"}
    # phrase-reproduction names a vocabulary; the concept arm does not.
    by_rule = dict(zip(df["route_rule"], df["filter_status"]))
    assert by_rule["phrase-reproduction"] == "reproduction"
    assert by_rule["concept-replication"] == "needs_review"
    assert df["filter_method"].iloc[0] == "engine:rel-a"
    assert df["filter_evidence"].iloc[0].startswith("rule:")


def test_the_export_satisfies_the_filtered_stage_schema(routed, pool, tmp_path):
    out = tmp_path / "cheap.csv"
    export_pile(routed, pool, "screen_cheap", out, "rel-a")
    assert validate_csv_columns(list(_exported(out).columns), "filtered") == []


def test_a_discard_export_carries_the_false_positive_verdict_and_its_provenance(
        routed, pool, tmp_path):
    out = tmp_path / "discard.csv"
    export_pile(routed, pool, "discard", out, "rel-a")
    df = _exported(out)
    assert list(df["filter_status"]) == ["false_positive"]
    assert list(df["filter_confidence"]) == ["high"]
    assert list(df["oa_type"]) == ["dataset"]
    assert list(df["route_rule"]) == ["dataset-type"]
    assert "dataset-type" in df["matched_rules"].iloc[0].split("|")


def test_the_pending_pile_is_refused_rather_than_exported(routed, pool, tmp_path):
    with pytest.raises(ValueError, match="not exported"):
        export_pile(routed, pool, "pending", tmp_path / "pending.csv", "rel-a")
    assert not (tmp_path / "pending.csv").exists()


def test_an_existing_manifest_is_never_overwritten(routed, pool, tmp_path):
    out = tmp_path / "cheap.csv"
    export_pile(routed, pool, "screen_cheap", out, "rel-a")
    with pytest.raises(FileExistsError):
        export_pile(routed, pool, "screen_cheap", out, "rel-a")


def test_year_bounds_drop_rows_outside_them(routed, pool, tmp_path):
    everything = tmp_path / "all.csv"
    export_pile(routed, pool, "screen_cheap", everything, "rel-a")
    bounded = tmp_path / "bounded.csv"
    manifest = export_pile(routed, pool, "screen_cheap", bounded, "rel-a",
                           from_year=2023, to_year=2023)
    assert manifest["rows"] < len(_exported(everything))
    assert set(_exported(bounded)["year_r"]) == {"2023"}


def test_the_conventions_file_maps_every_pile_the_router_can_produce():
    piles = load_conventions()["piles"]
    assert set(piles) >= {"discard", "screen_expensive", "screen_cheap",
                          "needs_human", "pending"}
    assert piles["pending"]["exported"] is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_the_specs_command_lists_the_bundle(capsys):
    assert cli.main(["specs"]) == 0
    out = capsys.readouterr().out
    assert "dataset-type" in out and "bundle " in out


def test_route_then_export_runs_end_to_end_over_a_pool(pool, tmp_path, capsys):
    store = tmp_path / "engine.duckdb"
    assert cli.main(["route", "--pool", str(pool), "--store", str(store),
                     "--pool-manifest-hash", "test-pool"]) == 0
    assert "screen_cheap" in capsys.readouterr().out

    out = tmp_path / "cheap.csv"
    assert cli.main(["export", "--pile", "screen_cheap", "--out", str(out),
                     "--pool", str(pool), "--store", str(store)]) == 0
    assert Path(str(out) + ".manifest.json").exists()
    assert list(_exported(out).columns) == ENGINE_EXPORTED_COLS
