"""M3: text overlays over the survivor pool, and the backfill that fills them.

One test per seam. The pool fixture holds one row the bundle would route to a
keyword pile if it had text and downgrades to `pending/no_text` because it does
not — that row is the whole milestone, so every test here is about it.

The bundle is the synthetic one (`tests/engine_bundle.py`): the downgrade is a
property of `route_batch()`, not of any shipped rule.
"""

import argparse
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from filter.engine import backfill, cli, overlay
from filter.engine.export import StaleBundleError, check_release_binding
from filter.engine.overlay import OverlayError, freeze, validate, worklist
from filter.engine.pool_reader import iter_pool_batches, overlay_manifest_hash
from filter.engine.release import routing_release
from filter.engine.store import build_routing, open_store
from search.snapshot_scan import _POOL_SCHEMA
from tests import engine_bundle

_REPLICATION = "We report a direct replication of the anchoring effect."


def _row(work: int, doi=None, title="A study of bees", abstract="Bees are nice.",
         type_="article", year=2024) -> dict:
    return {
        "id": f"https://openalex.org/W{work}",
        "doi": doi,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "type": type_,
        "authorships": json.dumps([{"author": {"display_name": "A. Author"}}]),
        "primary_location": json.dumps({"source": {"display_name": "J. Repl."}}),
        "open_access": json.dumps({"oa_url": None}),
        "concepts": json.dumps([]),
        "abstract_text": abstract,
        "hit_token_title": True,
        "hit_token_abstract": False,
        "hit_concept": False,
    }


# W1 has no abstract and a replication title: no_text today, routable once text
# arrives. W2 has its own abstract, which an overlay must not touch.
POOL_ROWS = [
    _row(1, doi="10.1234/one", title="A direct replication of the Smith effect",
         abstract=None, year=2021),
    _row(2, doi="10.1234/two", title="A direct replication of the Smith effect",
         abstract=_REPLICATION, year=2022),
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
    return engine_bundle.specs()


def _overlay(dir_path: Path, rows: list[dict], name="overlay-0000.parquet") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=overlay.OVERLAY_SCHEMA),
                   dir_path / name)
    return dir_path


def _overlay_row(work_id: int, text: str, source="epmc") -> dict:
    return {"work_id": work_id, "abstract_text": text, "source": source,
            "fetched_at": "2026-08-04T00:00:00+00:00"}


# ---------------------------------------------------------------------------
# Coalesce
# ---------------------------------------------------------------------------


def test_an_overlay_row_wins_over_pool_text_present_or_absent(pool, tmp_path):
    """Every overlay row was written deliberately by a backfill this project ran,
    so it replaces the pool's text — the rows that need REPLACING rather than
    filling are the boilerplate abstracts ("International audience", keyword
    lists) a fill-only overlay would leave in front of the voters."""
    overlay_dir = _overlay(tmp_path / "ov", [
        _overlay_row(1, "Recovered abstract for the first work."),
        _overlay_row(2, "Backfilled text replacing the pool's boilerplate."),
    ])
    freeze(overlay_dir)

    batches = list(iter_pool_batches(pool, overlay_dir))
    texts = dict(zip(
        [b for batch in batches for b in batch.column("id").to_pylist()],
        [t for batch in batches for t in batch.column("abstract_text").to_pylist()]))
    assert texts["https://openalex.org/W1"] == "Recovered abstract for the first work."
    assert texts["https://openalex.org/W2"] == ("Backfilled text replacing the "
                                                "pool's boilerplate.")

    # And with no overlay the stream is the pool untouched.
    plain = list(iter_pool_batches(pool))
    assert plain[0].column("abstract_text").to_pylist() == [None, _REPLICATION]


# ---------------------------------------------------------------------------
# Worklist → overlay → re-route: the invalidation seam
# ---------------------------------------------------------------------------


def test_a_no_text_row_is_worklisted_and_an_overlay_reroutes_it_under_a_new_release(
        pool, specs, tmp_path):
    con = open_store(Path(":memory:"))
    build_routing(con, pool, specs, "rel-a")
    assert [r[0] for r in con.execute(
        "SELECT pile FROM routing WHERE release_id='rel-a' AND work_id=1").fetchall()] \
        == ["pending"]

    wl = tmp_path / "worklist.parquet"
    assert worklist(con, "rel-a", pool, wl) == 1
    listed = overlay.read_worklist(wl)
    assert listed == [{"work_id": 1, "doi": "10.1234/one",
                       "title": "A direct replication of the Smith effect",
                       "year": 2021}]

    overlay_dir = _overlay(tmp_path / "ov", [_overlay_row(1, _REPLICATION)])
    manifest = freeze(overlay_dir)

    # A re-route under the overlay lands the row in a keyword pile...
    build_routing(con, pool, specs, "rel-b", batches=iter_pool_batches(pool, overlay_dir))
    pile, reason = con.execute(
        "SELECT pile, pending_reason FROM routing WHERE release_id='rel-b' "
        "AND work_id=1").fetchone()
    assert pile in {"screen_expensive", "screen_cheap"} and reason == ""

    # ...and the release id it must be recorded under is a different id.
    common = dict(pool_manifest_hash="pool-x", bundle_hash="bundle-x",
                  engine_version="1", alias_release="alias-x", schema_version="csv:x")
    assert routing_release(overlay_hash=None, **common) \
        != routing_release(overlay_hash=manifest["overlay_hash"], **common)
    assert overlay_manifest_hash(overlay_dir) == manifest["overlay_hash"]
    assert overlay_manifest_hash(None) is None


def test_build_routing_reads_the_pool_itself_when_no_batches_are_supplied(pool, specs):
    """The M3 seam must leave the M1 call byte-identical in outcome."""
    con = open_store(Path(":memory:"))
    plain = build_routing(con, pool, specs, "rel-a")
    fed = build_routing(con, pool, specs, "rel-b",
                        batches=iter_pool_batches(pool, overlay_dir=None))
    assert plain == fed


# ---------------------------------------------------------------------------
# Freeze / validate
# ---------------------------------------------------------------------------


def test_freezing_is_stable_and_a_new_chunk_mints_a_new_manifest_and_moves_latest(
        tmp_path):
    overlay_dir = _overlay(tmp_path / "ov", [_overlay_row(1, "one")])
    first = freeze(overlay_dir)
    assert freeze(overlay_dir)["overlay_hash"] == first["overlay_hash"]
    first_name = f"overlay_manifest-{first['overlay_hash'][:12]}.json"
    assert (overlay_dir / first_name).exists()

    _overlay(overlay_dir, [_overlay_row(2, "two", source="s2")], "overlay-0001.parquet")
    second = freeze(overlay_dir)
    assert second["overlay_hash"] != first["overlay_hash"]
    assert second["rows"] == 2 and second["sources"] == {"epmc": 1, "s2": 1}
    # The old manifest survives; the pointer names the new one.
    assert (overlay_dir / first_name).exists()
    assert json.loads((overlay_dir / overlay.POINTER_NAME).read_text())["manifest"] \
        == f"overlay_manifest-{second['overlay_hash'][:12]}.json"
    assert overlay.read_manifest(overlay_dir) == second


def test_one_work_in_two_chunks_is_refused_rather_than_last_writer_wins(tmp_path):
    overlay_dir = _overlay(tmp_path / "ov", [_overlay_row(1, "one")])
    _overlay(overlay_dir, [_overlay_row(1, "one again")], "overlay-0001.parquet")

    errors = validate(overlay_dir)
    assert any("work_id 1 appears in both" in e for e in errors)
    with pytest.raises(OverlayError, match="work_id 1 appears in both"):
        freeze(overlay_dir)


def test_routing_under_unfrozen_chunks_is_refused(tmp_path):
    overlay_dir = _overlay(tmp_path / "ov", [_overlay_row(1, "one")])
    with pytest.raises(OverlayError, match="no overlay_manifest.json"):
        overlay_manifest_hash(overlay_dir)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Point fetch_abstracts' cache/checkpoint sidecars at tmp_path."""
    import search.fetch_abstracts as fa
    cache = tmp_path / "abstracts"
    cache.mkdir()
    monkeypatch.setattr(fa, "ABSTRACT_CACHE_DIR", cache)
    monkeypatch.setattr(fa, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(fa, "FOUND_INDEX_PATH", tmp_path / "found.txt")
    return cache


@pytest.fixture
def wl(tmp_path) -> Path:
    path = tmp_path / "worklist.parquet"
    pq.write_table(pa.Table.from_pylist(
        [{"work_id": 1, "doi": "10.1234/one", "title": "T1", "year": 2021},
         {"work_id": 2, "doi": "10.1234/two", "title": "T2", "year": 2022},
         {"work_id": 3, "doi": "10.7910/DVN/ABC", "title": "Data", "year": 2023}],
        schema=overlay.WORKLIST_SCHEMA), path)
    return path


def test_a_dry_run_fetches_nothing_and_prices_every_source(
        wl, isolated_cache, capsys, monkeypatch):
    """`--run` is the only thing that spends; the default only estimates (§6)."""
    def _never(*args, **kwargs):
        raise AssertionError("a dry run fetched something")

    for name in ("_fetch_openalex_batch", "_fetch_epmc_batch", "_fetch_s2_batch",
                 "_fetch_crossref_abstract", "_fetch_scopus_abstract"):
        monkeypatch.setattr(backfill, name, _never)

    assert backfill.main(["--worklist", str(wl), "--overlay-dir",
                          str(wl.parent / "ov")]) == 0
    out = capsys.readouterr().out
    assert "2 worklist row(s) actionable" in out       # the dataset DOI is dropped
    assert "1 dataset-DOI row(s) dropped" in out
    assert "DRY RUN" in out
    assert {e["source"] for e in backfill.estimate(backfill._rows(wl)[0])} \
        == set(backfill.SOURCE_ORDER)
    # Two DOIs, one Europe PMC batch; OpenAlex sees both ids in one batch too.
    priced = {e["source"]: e for e in backfill.estimate(backfill._rows(wl)[0])}
    assert priced["epmc"]["targets"] == 2 and priced["epmc"]["requests"] == 1
    assert priced["crossref"]["requests"] == 2
    assert not (wl.parent / "ov").exists()


def test_a_run_writes_an_overlay_chunk_and_a_rerun_resumes_without_refetching(
        wl, isolated_cache, tmp_path, monkeypatch):
    calls = {"epmc": 0}

    def fake_epmc(dois: list[str]):
        calls["epmc"] += 1
        return {d: (f"Recovered {d}" if d == "10.1234/one" else None) for d in dois}

    monkeypatch.setattr(backfill, "_fetch_epmc_batch", fake_epmc)
    overlay_dir = tmp_path / "ov"
    result = backfill.run(wl, overlay_dir, sources=("epmc",))

    assert result == {"rows": 1, "chunk": str(overlay_dir / "overlay-0000.parquet"),
                      "by_source": {"epmc": 1}}
    written = pq.read_table(overlay_dir / "overlay-0000.parquet").to_pylist()
    assert [(r["work_id"], r["abstract_text"], r["source"]) for r in written] \
        == [(1, "Recovered 10.1234/one", "epmc")]

    # Resume: both DOIs are checkpointed, so no call is made, and the work
    # already in a chunk is not written into a second one.
    again = backfill.run(wl, overlay_dir, sources=("epmc",))
    assert calls["epmc"] == 1
    assert again["rows"] == 0 and again["chunk"] == ""
    assert validate(overlay_dir) == []


# ---------------------------------------------------------------------------
# The standard overlay location: safe by default
# ---------------------------------------------------------------------------


def test_the_default_overlay_is_the_standard_dir_only_when_it_holds_chunks(
        tmp_path, monkeypatch):
    """No flag means "read the overlay if there is one" — never a silent bare run,
    which is how an overlay-only rule comes to match nothing."""
    populated = _overlay(tmp_path / "standard", [_overlay_row(1, "text")])
    monkeypatch.setattr(cli, "OVERLAY_DIR", populated)
    assert cli._overlay(argparse.Namespace(overlay=None, no_overlay=False)) == populated

    # An explicit bare run, and an explicit other directory, both win.
    assert cli._overlay(argparse.Namespace(overlay=None, no_overlay=True)) is None
    other = _overlay(tmp_path / "other", [_overlay_row(2, "text")])
    assert cli._overlay(argparse.Namespace(overlay=other, no_overlay=False)) == other

    # An empty standard dir and a missing one are both "no overlay", not errors.
    (tmp_path / "empty").mkdir()
    for path in (tmp_path / "empty", tmp_path / "never-created"):
        monkeypatch.setattr(cli, "OVERLAY_DIR", path)
        assert cli._overlay(argparse.Namespace(overlay=None, no_overlay=False)) is None


def test_route_reads_the_standard_overlay_and_says_which_one_it_read(
        pool, specs, tmp_path, monkeypatch, capsys):
    """The production failure this exists for: routing with no --overlay left the
    overlay-only rules matching nothing, and printed nothing about it."""
    overlay_dir = _overlay(tmp_path / "standard", [_overlay_row(1, _REPLICATION)])
    manifest = freeze(overlay_dir)
    monkeypatch.setattr(cli, "OVERLAY_DIR", overlay_dir)
    monkeypatch.setattr(cli, "load_specs", lambda spec_dir: specs)
    monkeypatch.setattr(cli, "load_aliases", lambda path: {})
    monkeypatch.setattr(cli, "bundle_hash", lambda spec_dir: "bundle-x")
    monkeypatch.setattr(cli, "alias_release", lambda path: "alias-x")
    store = tmp_path / "engine.duckdb"
    args = argparse.Namespace(spec_dir=tmp_path, pool=pool, store=store,
                              pool_manifest_hash="pool-x", overlay=None,
                              no_overlay=False)

    assert cli.cmd_route(args) == 0
    out = capsys.readouterr().out
    assert f"overlay: {overlay_dir} (hash {manifest['overlay_hash'][:12]})" in out

    con = open_store(store, read_only=True)
    pile, reason = con.execute(
        "SELECT pile, pending_reason FROM routing WHERE work_id=1").fetchone()
    con.close()
    assert pile in {"screen_expensive", "screen_cheap"} and reason == ""

    # --no-overlay is the same command run bare: the row is back to no_text, and
    # the line says so rather than leaving the reader to guess.
    args.no_overlay = True
    args.store = store2 = tmp_path / "bare.duckdb"
    assert cli.cmd_route(args) == 0
    assert "overlay: none" in capsys.readouterr().out
    con = open_store(store2, read_only=True)
    assert con.execute(
        "SELECT pile, pending_reason FROM routing WHERE work_id=1").fetchone() \
        == ("pending", "no_text")
    con.close()


def test_a_release_routed_under_an_overlay_refuses_a_handoff_without_it(tmp_path):
    """The binding every reading command passes through: the overlay decides what
    the rules read, so a release cannot be handed off under different text."""
    overlay_dir = _overlay(tmp_path / "ov", [_overlay_row(1, _REPLICATION)])
    routed_hash = freeze(overlay_dir)["overlay_hash"]

    check_release_binding(tmp_path, "rel-a", None, None, overlay_dir, routed_hash)
    with pytest.raises(StaleBundleError, match="overlay_hash"):
        check_release_binding(tmp_path, "rel-a", None, None, None, routed_hash)
