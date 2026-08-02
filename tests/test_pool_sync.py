"""Tests for sharing the Stage A survivor pool and prebuilt corpora via a private
HF dataset repo.

One test per seam: the year sharding of remote paths, batched commits, push
idempotence, the pool provenance sidecar, partial and skipping pulls, the
prebuilt-build round trip and its provenance warnings, --years parsing, and the
two "you have not configured this" errors. ``huggingface_hub`` is never installed
here and never called: pool_sync imports it inside its functions, so a fake module
in ``sys.modules`` is the whole mock, and any real network call would fail loudly
rather than quietly succeed. The fake keeps the remote as a ``{path: bytes}``
store, so a push followed by a pull is a genuine round trip.
"""

import json
import sys
import types
from pathlib import Path

import pytest

from search import pool_sync as ps
from search import snapshot_scan as ss


# ---------------------------------------------------------------------------
# The fake huggingface_hub
# ---------------------------------------------------------------------------


class _Entry:
    def __init__(self, path: str, size: int) -> None:
        self.path = path
        self.size = size


class _Op:
    def __init__(self, path_in_repo=None, path_or_fileobj=None) -> None:
        self.path_in_repo = path_in_repo
        self.payload = path_or_fileobj


class _FakeApi:
    def __init__(self, store: dict[str, bytes], calls: dict) -> None:
        self._store = store
        self._calls = calls

    def create_repo(self, repo_id, repo_type=None, private=None, exist_ok=None):
        self._calls.setdefault("create_repo", []).append(repo_id)

    def list_repo_tree(self, repo_id, repo_type=None, recursive=False):
        return [_Entry(p, len(b)) for p, b in self._store.items()]

    def create_commit(self, repo_id=None, repo_type=None, operations=None,
                      commit_message=None):
        self._calls.setdefault("commits", []).append(
            [op.path_in_repo for op in operations])
        for op in operations:
            self._store[op.path_in_repo] = (
                op.payload if isinstance(op.payload, bytes) else Path(op.payload).read_bytes())


def _fake_hub(monkeypatch, remote: dict[str, int], token: str = "hf_test",
              store: dict = None) -> dict:
    """Install a fake ``huggingface_hub``; returns the record of calls made.

    *remote* gives sizes for parquet files (their content is filler of that size);
    *store* adds exact byte content for anything whose content matters (the JSON
    sidecars).
    """
    contents: dict[str, bytes] = {p: b"x" * s for p, s in remote.items()}
    contents.update(store or {})
    calls: dict = {"remote": contents}
    module = types.ModuleType("huggingface_hub")

    module.get_token = lambda: token or None
    module.HfApi = lambda token=None: _FakeApi(contents, calls)
    module.CommitOperationAdd = _Op
    module.list_repo_files = lambda repo_id, repo_type=None, token=None: sorted(contents)

    def hf_hub_download(repo_id=None, filename=None, repo_type=None, token=None,
                        local_dir=None):
        if filename not in contents:
            raise FileNotFoundError(filename)   # what HF raises for an absent file
        calls.setdefault("downloaded", []).append(filename)
        target = Path(local_dir) / filename       # mirrors the remote's folders
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[filename])
        return str(target)

    module.hf_hub_download = hf_hub_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    return calls


def _uploaded(calls: dict) -> list[str]:
    """Every path committed, across all commits."""
    return [path for commit in calls.get("commits", []) for path in commit]


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """The pool manifest is derived from the scan ledger — never the real one."""
    ledger = {"snapshot_date": "2026-07-01", "files": {
        "https://openalex.s3.amazonaws.com/data/parquet/works/updated_date=2019-01-01/part_0000.parquet":
            {"content_length": 100, "record_count": 10, "kept": 2, "status": "done"}}}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(ss, "_LEDGER_PATH", path, raising=False)
    return ledger


def _pool(tmp_path, files: dict[str, int]):
    """A local pool directory holding *files* (name -> byte size)."""
    pool = tmp_path / "pool"
    pool.mkdir()
    for name, size in files.items():
        (pool / name).write_bytes(b"x" * size)
    return pool


# ---------------------------------------------------------------------------
# Remote layout
# ---------------------------------------------------------------------------


def test_remote_path_shards_by_partition_year():
    assert ps._remote_path("part-2016-06-24-part_0000.parquet") == \
        "2016/part-2016-06-24-part_0000.parquet"
    assert ps._remote_path("part-2024-01-02-part_0011.parquet") == \
        "2024/part-2024-01-02-part_0011.parquet"


def test_remote_path_of_malformed_name_is_shared_under_unknown():
    """An unparseable date must not silently drop the file from the transfer."""
    assert ps._remote_path("part-notadate-part_0000.parquet") == \
        "unknown/part-notadate-part_0000.parquet"
    assert ps._remote_path("stray.parquet") == "unknown/stray.parquet"


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def test_push_skips_same_size_remote_and_uploads_the_rest(tmp_path, monkeypatch):
    pool = _pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 10,
                            "part-2020-01-01-part_0000.parquet": 20,
                            "part-2021-01-01-part_0000.parquet": 30})
    calls = _fake_hub(monkeypatch, {
        "2019/part-2019-01-01-part_0000.parquet": 10,   # unchanged — skipped
        "2020/part-2020-01-01-part_0000.parquet": 99,   # size differs — re-uploaded
    })

    n = ps.push_pool(pool, repo="me/pool")

    assert n == 2
    assert sorted(p for p in _uploaded(calls) if p.endswith(".parquet")) == [
        "2020/part-2020-01-01-part_0000.parquet",
        "2021/part-2021-01-01-part_0000.parquet"]
    # Re-running the same push is a no-op — that is what makes it resumable.
    assert ps.push_pool(pool, repo="me/pool") == 0


def test_push_batches_files_into_few_commits(tmp_path, monkeypatch):
    """One commit per file would put a single pool push at the few-thousand-commit
    mark where HF says repo UX degrades; uploads go up in batches instead."""
    monkeypatch.setattr(ps, "FLORA_HF_COMMIT_BATCH", 50)
    pool = _pool(tmp_path, {f"part-2019-01-01-part_{i:04d}.parquet": 3 for i in range(120)})
    calls = _fake_hub(monkeypatch, {})

    assert ps.push_pool(pool, repo="me/pool") == 120

    commits = calls["commits"]
    assert len(commits) == 3, "120 files + the manifest at 50 per commit"
    assert all(len(c) <= 50 for c in commits)
    assert len(_uploaded(calls)) == 121, "every file, plus pool_manifest.json, exactly once"
    assert ps._POOL_MANIFEST in _uploaded(calls)


def test_push_dry_run_uploads_nothing(tmp_path, monkeypatch):
    pool = _pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 10})
    calls = _fake_hub(monkeypatch, {})

    assert ps.push_pool(pool, repo="me/pool", dry_run=True) == 1
    assert "commits" not in calls


# ---------------------------------------------------------------------------
# The pool provenance sidecar
# ---------------------------------------------------------------------------


def _remote_manifest(stage_a: str) -> dict:
    return {ps._POOL_MANIFEST: json.dumps({"stage_a_fingerprint": stage_a}).encode("utf-8")}


def test_push_records_the_gate_the_pool_was_scanned_under(tmp_path, monkeypatch):
    pool = _pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 10})
    calls = _fake_hub(monkeypatch, {})

    ps.push_pool(pool, repo="me/pool")

    manifest = json.loads(calls["remote"][ps._POOL_MANIFEST])
    assert manifest["stage_a_fingerprint"] == ss.stage_a_fingerprint()
    assert manifest["snapshot_date"] == "2026-07-01"
    assert (manifest["ledger_files"], manifest["ledger_records"], manifest["ledger_kept"]) \
        == (1, 10, 2)
    assert manifest["ledger_hash"] == ss.ledger_hash(ss.load_ledger())


def test_push_refuses_to_mix_two_stage_a_gates(tmp_path, monkeypatch):
    """A pool half-scanned under each of two gates is complete under neither, and
    nothing downstream could see it — so this is the case that refuses."""
    pool = _pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 10})
    calls = _fake_hub(monkeypatch, {}, store=_remote_manifest("deadbeef" * 8))

    with pytest.raises(RuntimeError, match="DIFFERENT Stage A gate"):
        ps.push_pool(pool, repo="me/pool")
    assert "commits" not in calls


def test_push_force_overrides_the_gate_conflict(tmp_path, monkeypatch, caplog):
    pool = _pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 10})
    calls = _fake_hub(monkeypatch, {}, store=_remote_manifest("deadbeef" * 8))

    assert ps.push_pool(pool, repo="me/pool", force=True) == 1
    assert "2019/part-2019-01-01-part_0000.parquet" in _uploaded(calls)
    assert json.loads(calls["remote"][ps._POOL_MANIFEST])["stage_a_fingerprint"] \
        == ss.stage_a_fingerprint()


def test_push_against_a_matching_gate_proceeds(tmp_path, monkeypatch):
    pool = _pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 10})
    calls = _fake_hub(monkeypatch, {}, store=_remote_manifest(ss.stage_a_fingerprint()))

    assert ps.push_pool(pool, repo="me/pool") == 1
    assert "2019/part-2019-01-01-part_0000.parquet" in _uploaded(calls)


def test_pull_warns_on_a_gate_mismatch_but_still_downloads(tmp_path, monkeypatch, caplog):
    """A colleague may legitimately want someone else's pool — they must simply know."""
    calls = _fake_hub(monkeypatch, {"2019/part-2019-01-01-part_0000.parquet": 6},
                      store=_remote_manifest("deadbeef" * 8))

    with caplog.at_level("WARNING"):
        n = ps.pull_pool(tmp_path / "pool", repo="me/pool")

    assert n == 1
    assert "DIFFERENT Stage A gate" in caplog.text


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def test_pull_years_requests_only_those_years(tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    calls = _fake_hub(monkeypatch, {
        "2018/part-2018-01-01-part_0000.parquet": 5,
        "2019/part-2019-01-01-part_0000.parquet": 6,
        "2019/part-2019-06-02-part_0001.parquet": 7,
        "2020/part-2020-01-01-part_0000.parquet": 8,
    })

    n = ps.pull_pool(pool, repo="me/pool", years=[2019])

    assert n == 2
    assert calls["downloaded"] == ["2019/part-2019-01-01-part_0000.parquet",
                                   "2019/part-2019-06-02-part_0001.parquet"]
    # The local pool is flat — admit_from_pool globs a single directory.
    assert sorted(p.name for p in pool.glob("*.parquet")) == [
        "part-2019-01-01-part_0000.parquet", "part-2019-06-02-part_0001.parquet"]
    assert not (pool / "2019").exists()


def test_pull_skips_files_already_present_at_the_same_size(tmp_path, monkeypatch):
    pool = _pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 6,     # matches remote
                            "part-2020-01-01-part_0000.parquet": 1})    # truncated
    calls = _fake_hub(monkeypatch, {
        "2019/part-2019-01-01-part_0000.parquet": 6,
        "2020/part-2020-01-01-part_0000.parquet": 8,
    })

    n = ps.pull_pool(pool, repo="me/pool")

    assert n == 1
    assert calls["downloaded"] == ["2020/part-2020-01-01-part_0000.parquet"]
    assert (pool / "part-2020-01-01-part_0000.parquet").stat().st_size == 8


def test_pull_for_an_absent_year_says_so(tmp_path, monkeypatch):
    _fake_hub(monkeypatch, {"2019/part-2019-01-01-part_0000.parquet": 6})
    with pytest.raises(ValueError, match="2031"):
        ps.pull_pool(tmp_path / "pool", repo="me/pool", years=[2031])


# ---------------------------------------------------------------------------
# The prebuilt candidates artifact
# ---------------------------------------------------------------------------


def _build(tmp_path, rows_per_chunk=(2, 1), stage_b=None, row_builder=None) -> Path:
    """A local build directory: chunked parquet plus the manifest describing it."""
    import pandas as pd

    from shared.schema import CANDIDATES_COLS

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    chunks = []
    n = 0
    for i, rows in enumerate(rows_per_chunk):
        name = f"candidates-{i:04d}.parquet"
        frame = pd.DataFrame(
            [{**{c: "" for c in CANDIDATES_COLS}, "doi_r": f"10.1/{n + j}"}
             for j in range(rows)], columns=CANDIDATES_COLS)
        frame.to_parquet(build_dir / name, compression="zstd", index=False)
        chunks.append({"name": name, "rows": rows})
        n += rows
    manifest = {
        "build_hash": "b" * 64,
        "stage_a_fingerprint": ss.stage_a_fingerprint(),
        "stage_b_fingerprint": stage_b or ss.stage_b_fingerprint(),
        "row_builder_version": row_builder or ss.ROW_BUILDER_VERSION,
        "snapshot_date": "2026-07-01", "ledger_hash": "l" * 64,
        "rows": n, "chunk_rows": 2, "chunks": chunks, "created_at": "2026-08-02T00:00:00+00:00",
    }
    (build_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return build_dir


def test_push_build_uploads_under_its_hash_and_moves_latest(tmp_path, monkeypatch):
    build_dir = _build(tmp_path)
    calls = _fake_hub(monkeypatch, {})

    assert ps.push_build(build_dir, repo="me/pool") == 2

    paths = _uploaded(calls)
    assert f"builds/{'b' * 64}/candidates-0000.parquet" in paths
    assert f"builds/{'b' * 64}/manifest.json" in paths
    assert json.loads(calls["remote"][ps._LATEST_BUILD])["build_hash"] == "b" * 64
    # Re-pushing the same build re-uploads no chunk.
    assert ps.push_build(build_dir, repo="me/pool") == 0


def test_pull_build_follows_latest_and_merges_with_enrich_false(tmp_path, monkeypatch):
    """The round trip a collaborator actually runs: push a build, then pull it back
    with no build hash in hand and let latest.json say which one that is."""
    calls = _fake_hub(monkeypatch, {})
    ps.push_build(_build(tmp_path), repo="me/pool")
    monkeypatch.setattr(ps, "DATA_DIR", tmp_path)

    merged = []

    def capture(df, path, enrich=True):
        assert enrich is False, "build rows carry OpenAlex's own abstract"
        assert path == tmp_path / "candidates.csv"
        merged.append(df)
        return len(df)

    n = ps.pull_build(tmp_path / "builds", repo="me/pool", merge_fn=capture)

    assert n == 3
    assert [len(df) for df in merged] == [2, 1], "one merge per chunk"
    assert sorted(d for df in merged for d in df["doi_r"]) == ["10.1/0", "10.1/1", "10.1/2"]
    # The chunks land flat under the build's own directory, not in a nested mirror.
    assert sorted(p.name for p in (tmp_path / "builds" / ("b" * 64)).glob("*.parquet")) == [
        "candidates-0000.parquet", "candidates-0001.parquet"]


def test_pull_build_by_hash_never_reads_latest(tmp_path, monkeypatch):
    calls = _fake_hub(monkeypatch, {})
    ps.push_build(_build(tmp_path), repo="me/pool")
    monkeypatch.setattr(ps, "DATA_DIR", tmp_path)
    calls.pop("downloaded", None)

    ps.pull_build(tmp_path / "builds", repo="me/pool", build_hash="b" * 64,
                  merge_fn=lambda df, path, enrich=True: len(df))

    assert ps._LATEST_BUILD not in calls["downloaded"]


def test_pull_build_warns_when_the_build_was_admitted_under_other_rules(
        tmp_path, monkeypatch, caplog):
    """The provenance check is the point: a CSV passed around says nothing about the
    vocabulary that produced it, a build says exactly that."""
    _fake_hub(monkeypatch, {})
    ps.push_build(_build(tmp_path, stage_b="cafe" * 16, row_builder="v99"), repo="me/pool")
    monkeypatch.setattr(ps, "DATA_DIR", tmp_path)

    with caplog.at_level("WARNING"):
        ps.pull_build(tmp_path / "builds", repo="me/pool",
                      merge_fn=lambda df, path, enrich=True: len(df))

    assert "ADMITTED UNDER DIFFERENT RULES" in caplog.text
    assert "DIFFERENT ROW BUILDER" in caplog.text


def test_pull_build_without_any_build_says_what_to_do_instead(tmp_path, monkeypatch):
    _fake_hub(monkeypatch, {})
    with pytest.raises(ValueError, match="admit-from-pool"):
        ps.pull_build(tmp_path / "builds", repo="me/pool")


# ---------------------------------------------------------------------------
# --years parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec,expected", [
    ("2019", [2019]),
    ("2019,2021", [2019, 2021]),
    ("2019-2021", [2019, 2020, 2021]),
    ("2019,2021-2023", [2019, 2021, 2022, 2023]),
    (" 2020 , 2020 ", [2020]),
])
def test_parse_years(spec, expected):
    assert ps.parse_years(spec) == expected


@pytest.mark.parametrize("spec", ["", "twenty", "2021-2019", "2019-"])
def test_parse_years_rejects_nonsense(spec):
    with pytest.raises(ValueError):
        ps.parse_years(spec)


# ---------------------------------------------------------------------------
# Configuration errors name the variable to set
# ---------------------------------------------------------------------------


def test_missing_repo_names_the_env_var(tmp_path, monkeypatch):
    _fake_hub(monkeypatch, {})
    monkeypatch.setattr(ps, "FLORA_POOL_REPO", "")
    with pytest.raises(ValueError, match="FLORA_POOL_REPO"):
        ps.push_pool(_pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 3}))
    with pytest.raises(ValueError, match="FLORA_POOL_REPO"):
        ps.pull_pool(tmp_path / "pool")


def test_missing_token_names_the_env_var(tmp_path, monkeypatch):
    _fake_hub(monkeypatch, {"2019/part-2019-01-01-part_0000.parquet": 3}, token="")
    with pytest.raises(ValueError, match="HF_TOKEN"):
        ps.push_pool(_pool(tmp_path, {"part-2019-01-01-part_0000.parquet": 3}),
                     repo="me/pool")
    with pytest.raises(ValueError, match="HF_TOKEN"):
        ps.pull_pool(tmp_path / "pool", repo="me/pool")
