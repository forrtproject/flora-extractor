"""Tests for sharing the API caches through the private HF dataset repo.

One test per seam: deterministic packing (what makes an incremental push
possible), the skip of unchanged shards, the refusal to overwrite a local entry,
the unproven-miss rule and its checkpoint half, the manifest that cannot be read,
and the tar member that must never be written outside the cache directory.

The fake ``huggingface_hub`` is the one ``tests/test_pool_sync.py`` builds — both
modules import the hub inside their functions, so a fake in ``sys.modules`` is the
whole mock and a real network call would fail loudly rather than quietly succeed.
"""

import gzip
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from shared import cache_sync as cs
from tests.test_pool_sync import _fake_hub, _HubHTTPError


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every path the module touches, under tmp_path."""
    monkeypatch.setattr(cs, "FLORA_POOL_REPO", "me/flora", raising=False)
    monkeypatch.setattr(cs, "_PULL_STATE", tmp_path / "pulled.json")
    monkeypatch.setattr(cs, "CHECKPOINT_PATH", tmp_path / "done.txt")
    monkeypatch.setattr(cs, "ABSTRACT_CACHE_DIR", tmp_path / "abstracts")
    monkeypatch.setattr(cs.PARTS["abstracts"], "directory", tmp_path / "abstracts")
    monkeypatch.setattr(cs.PARTS["llm"], "directory", tmp_path / "llm")
    (tmp_path / "abstracts").mkdir()
    (tmp_path / "llm").mkdir()
    return tmp_path


def _abstract(tmp_path: Path, name: str, ident: str, abstract) -> Path:
    path = tmp_path / "abstracts" / name
    path.write_text(json.dumps({"ident": ident, "abstract": abstract}))
    return path


def _shard_bytes(calls: dict, remote: str) -> bytes:
    return calls["remote"][remote]


def _members(blob: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


def _push_then_manifest(calls: dict) -> dict:
    return json.loads(calls["remote"][cs._MANIFEST].decode("utf-8"))


# ---------------------------------------------------------------------------
# Packing and pushing
# ---------------------------------------------------------------------------


def test_pack_shard_is_byte_identical_across_runs(_isolated_cache):
    """The push decides what to transfer by hashing the shard, so a tar that
    embedded mtimes would re-upload all 256 abstract shards to share ten files."""
    files = [_abstract(_isolated_cache, "a.json", "epmc:10.1/a", "A"),
             _abstract(_isolated_cache, "b.json", "epmc:10.1/b", "B")]
    assert cs.pack_shard(files) == cs.pack_shard(list(reversed(files)))


def test_push_skips_shards_the_remote_already_holds(_isolated_cache, monkeypatch):
    _abstract(_isolated_cache, "a.json", "epmc:10.1/a", "A")
    calls = _fake_hub(monkeypatch, {})
    assert cs.push_cache([cs.PARTS["abstracts"]]) == 1

    # Second push, nothing changed locally: the manifest is rewritten, no shard is.
    calls2 = _fake_hub(monkeypatch, {}, store=dict(calls["remote"]))
    assert cs.push_cache([cs.PARTS["abstracts"]]) == 0
    committed = [p for commit in calls2["commits"] for p in commit]
    assert committed == [cs._MANIFEST]


def test_push_records_what_each_source_actually_recovered(_isolated_cache, monkeypatch):
    """Not which keys were configured — entitlement is IP-bound, so a configured
    key can still be refused. Hits are the only evidence a source was readable."""
    _abstract(_isolated_cache, "a.json", "scopus:10.1/a", "Body")
    _abstract(_isolated_cache, "b.json", "scopus:10.1/b", cs._MISS)
    _abstract(_isolated_cache, "c.json", "s2:10.1/c", cs._MISS)
    calls = _fake_hub(monkeypatch, {})
    cs.push_cache([cs.PARTS["abstracts"]])

    sources = _push_then_manifest(calls)["abstract_sources"]
    assert sources["scopus"] == {"hits": 1, "misses": 1}
    assert sources["s2"] == {"hits": 0, "misses": 1}


# ---------------------------------------------------------------------------
# Pulling
# ---------------------------------------------------------------------------


def _remote_with(monkeypatch, tmp_path, entries: dict[str, tuple[str, object]],
                 sources: dict) -> dict:
    """A remote holding one abstracts shard built from *entries*, plus a manifest
    whose `abstract_sources` is *sources*."""
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    paths = []
    for name, (ident, abstract) in entries.items():
        path = staging / name
        path.write_text(json.dumps({"ident": ident, "abstract": abstract}))
        paths.append(path)
    blob = cs.pack_shard(paths)
    shard = cs.PARTS["abstracts"].shard_of(paths[0].name)
    remote = cs._remote_shard(cs.PARTS["abstracts"], shard)
    # Every entry must land in the one shard this helper builds.
    assert all(cs.PARTS["abstracts"].shard_of(p.name) == shard for p in paths)
    manifest = {"parts": {"abstracts": {shard: "sha"}}, "abstract_sources": sources}
    return _fake_hub(monkeypatch, {},
                     store={remote: blob,
                            cs._MANIFEST: json.dumps(manifest).encode("utf-8")})


def test_pull_never_overwrites_a_local_entry(_isolated_cache, monkeypatch):
    """Keys are content-complete, so a name that exists locally holds the same
    answer; overwriting could only change the mtime."""
    local = _abstract(_isolated_cache, "a.json", "epmc:10.1/a", "mine")
    _remote_with(monkeypatch, _isolated_cache,
                 {"a.json": ("epmc:10.1/a", "theirs")}, {})
    cs.pull_cache([cs.PARTS["abstracts"]])
    assert json.loads(local.read_text())["abstract"] == "mine"


def test_pull_imports_hits_from_a_source_the_producer_could_read(
        _isolated_cache, monkeypatch):
    _remote_with(monkeypatch, _isolated_cache,
                 {"a.json": ("scopus:10.1/a", "Body")},
                 {"scopus": {"hits": 1, "misses": 0}})
    cs.pull_cache([cs.PARTS["abstracts"]])
    got = json.loads((_isolated_cache / "abstracts" / "a.json").read_text())
    assert got["abstract"] == "Body"


def test_pull_drops_a_miss_the_producer_never_proved(_isolated_cache, monkeypatch):
    """Zero hits in a gated namespace is the signature of a machine that could not
    read that source. Its "no abstract" is not a fact this machine should inherit
    — and the checkpoint line has to go with it, or the DOI never re-enters the
    worklist and the abstract is never fetched anyway."""
    monkeypatch.setattr(cs, "capabilities", lambda: {"scopus": True, "s2": True,
                                                     "osf": True})
    cs.CHECKPOINT_PATH.write_text("scopus:10.1/a\nepmc:10.1/b\n")
    _remote_with(monkeypatch, _isolated_cache,
                 {"a.json": ("scopus:10.1/a", cs._MISS)},
                 {"scopus": {"hits": 0, "misses": 400}})
    cs.pull_cache([cs.PARTS["abstracts"]])

    assert not (_isolated_cache / "abstracts" / "a.json").exists()
    assert cs.CHECKPOINT_PATH.read_text().splitlines() == ["epmc:10.1/b"]


def test_pull_keeps_a_miss_from_a_source_needing_no_credential(
        _isolated_cache, monkeypatch):
    """Europe PMC needs no key, so its miss means the same thing on every machine
    — and not re-buying a known miss is most of the value of sharing."""
    monkeypatch.setattr(cs, "capabilities", lambda: {"scopus": True, "s2": True,
                                                     "osf": True})
    _remote_with(monkeypatch, _isolated_cache,
                 {"a.json": ("epmc:10.1/a", cs._MISS)}, {})
    cs.pull_cache([cs.PARTS["abstracts"]])
    assert (_isolated_cache / "abstracts" / "a.json").exists()


def test_a_push_carrying_no_abstracts_does_not_reopen_anything(
        _isolated_cache, monkeypatch):
    """Zero hits because the pusher shared only the LLM cache is not evidence that
    their Scopus was unreadable — and acting on it would delete the PULLER's own
    checkpoint lines for a source the push never touched."""
    monkeypatch.setattr(cs, "capabilities", lambda: {"scopus": True, "s2": True,
                                                     "osf": True})
    cs.CHECKPOINT_PATH.write_text("scopus:10.1/a\n")
    _fake_hub(monkeypatch, {}, store={
        cs._MANIFEST: json.dumps({"parts": {"llm": {}}, "abstract_sources": {}}).encode()})

    cs.pull_cache([cs.PARTS["abstracts"]])
    assert cs.CHECKPOINT_PATH.read_text().splitlines() == ["scopus:10.1/a"]


def test_pull_refuses_when_the_manifest_cannot_be_read(_isolated_cache, monkeypatch):
    """An unanswered Hub is not an empty repo, and the manifest is what says which
    sources the pushing machine could actually read."""
    _fake_hub(monkeypatch, {},
              download_errors={cs._MANIFEST: _HubHTTPError("503 Service Unavailable")})
    with pytest.raises(RuntimeError, match="503"):
        cs.pull_cache([cs.PARTS["abstracts"]])


def test_pull_refuses_a_member_that_would_escape_the_cache_dir(
        _isolated_cache, monkeypatch):
    """The archives are ours, but a dataset repo is still input."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name="../escaped.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())

    remote = cs._remote_shard(cs.PARTS["abstracts"], "00")
    _fake_hub(monkeypatch, {}, store={
        remote: packed.getvalue(),
        cs._MANIFEST: json.dumps({"parts": {"abstracts": {"00": "sha"}}}).encode()})

    cs.pull_cache([cs.PARTS["abstracts"]])
    assert not (_isolated_cache / "escaped.json").exists()
    assert not (_isolated_cache / "abstracts" / "escaped.json").exists()


def test_parse_parts_names_the_known_parts(monkeypatch):
    assert [p.name for p in cs.parse_parts("llm,abstracts")] == ["llm", "abstracts"]
    with pytest.raises(ValueError, match="doi_verify"):
        cs.parse_parts("nonsense")
