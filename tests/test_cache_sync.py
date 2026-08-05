"""Tests for sharing the API caches through the private HF dataset repo.

One test per seam: deterministic packing (what makes an incremental push
possible), the skip of unchanged shards, the refusal to overwrite a local entry,
the abstract store's round trip, the unproven-miss rule, the manifest that cannot
be read, and the tar member that must never be written outside the cache
directory.

The fake ``huggingface_hub`` is the one ``tests/test_pool_sync.py`` builds — both
modules import the hub inside their functions, so a fake in ``sys.modules`` is the
whole mock and a real network call would fail loudly rather than quietly succeed.
The abstract store is pointed at a throwaway database by the autouse fixture in
``conftest.py``, so nothing here can touch the real one.
"""

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from shared import abstract_store, cache_sync as cs
from tests.test_pool_sync import _fake_hub, _HubHTTPError


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every path the module touches, under tmp_path."""
    monkeypatch.setattr(cs, "FLORA_POOL_REPO", "me/flora", raising=False)
    monkeypatch.setattr(cs, "_PULL_STATE", tmp_path / "pulled.json")
    monkeypatch.setattr(cs, "ABSTRACT_DB_PATH", abstract_store.ABSTRACT_DB_PATH)
    monkeypatch.setattr(cs.PARTS["llm"], "directory", tmp_path / "llm")
    (tmp_path / "llm").mkdir()
    return tmp_path


def _llm_entry(tmp_path: Path, name: str, payload: str) -> Path:
    path = tmp_path / "llm" / name
    path.write_text(payload)
    return path


def _manifest(calls: dict) -> dict:
    return json.loads(calls["remote"][cs._MANIFEST].decode("utf-8"))


# ---------------------------------------------------------------------------
# Packing and pushing
# ---------------------------------------------------------------------------


def test_pack_shard_is_byte_identical_across_runs(_isolated_cache):
    """The push decides what to transfer by hashing the shard, so a tar that
    embedded mtimes would re-upload every shard to share ten files."""
    files = [_llm_entry(_isolated_cache, "a.json", "A"),
             _llm_entry(_isolated_cache, "b.json", "B")]
    assert cs.pack_shard(files) == cs.pack_shard(list(reversed(files)))


def test_push_skips_shards_the_remote_already_holds(_isolated_cache, monkeypatch):
    _llm_entry(_isolated_cache, "a.json", "A")
    calls = _fake_hub(monkeypatch, {})
    assert cs.push_cache([cs.PARTS["llm"]]) == 1

    # Second push, nothing changed locally: the manifest is rewritten, no shard is.
    calls2 = _fake_hub(monkeypatch, {}, store=dict(calls["remote"]))
    assert cs.push_cache([cs.PARTS["llm"]]) == 0
    assert [p for commit in calls2["commits"] for p in commit] == [cs._MANIFEST]


def test_push_records_what_each_source_actually_recovered(_isolated_cache, monkeypatch):
    """Not which keys were configured — entitlement is IP-bound, so a configured
    key can still be refused. Hits are the only evidence a source was readable."""
    abstract_store.record("scopus:10.1/a", "Body")
    abstract_store.record("scopus:10.1/b", None)
    abstract_store.record("s2:10.1/c", None)
    calls = _fake_hub(monkeypatch, {})
    cs.push_cache([cs.PARTS[cs.ABSTRACTS_PART]])

    sources = _manifest(calls)["abstract_sources"]
    assert sources["scopus"] == {"hits": 1, "misses": 1}
    assert sources["s2"] == {"hits": 0, "misses": 1}


# ---------------------------------------------------------------------------
# The abstract store round trip
# ---------------------------------------------------------------------------


def _remote_store(monkeypatch, entries: dict[str, object], sources: dict,
                  tmp_path: Path) -> dict:
    """A remote holding a gzipped abstract store built from *entries*, and a
    manifest whose `abstract_sources` is *sources*."""
    import sqlite3

    remote_db = tmp_path / "remote.sqlite"
    conn = sqlite3.connect(remote_db)
    conn.execute("CREATE TABLE abstracts (ident TEXT PRIMARY KEY, abstract TEXT, "
                 "fetched_at TEXT NOT NULL)")
    conn.executemany("INSERT INTO abstracts VALUES (?, ?, '2026-01-01T00:00:00')",
                     list(entries.items()))
    conn.commit()
    conn.close()
    manifest = {"parts": {cs.ABSTRACTS_PART: {"db": "sha"}}, "abstract_sources": sources}
    return _fake_hub(monkeypatch, {}, store={
        cs._ABSTRACTS_REMOTE: gzip.compress(remote_db.read_bytes()),
        cs._MANIFEST: json.dumps(manifest).encode("utf-8")})


def test_pull_merges_rather_than_replacing_the_local_store(_isolated_cache, monkeypatch):
    """A puller's own abstracts must survive, and an identifier already answered
    locally keeps its local answer — file-replacement could do neither."""
    abstract_store.record("epmc:10.1/mine", "mine")
    abstract_store.record("epmc:10.1/both", "local answer")
    _remote_store(monkeypatch, {"epmc:10.1/both": "remote answer",
                                "epmc:10.1/theirs": "theirs"}, {}, _isolated_cache)

    cs.pull_cache([cs.PARTS[cs.ABSTRACTS_PART]])

    assert abstract_store.lookup("epmc:10.1/mine") == (True, "mine")
    assert abstract_store.lookup("epmc:10.1/both") == (True, "local answer")
    assert abstract_store.lookup("epmc:10.1/theirs") == (True, "theirs")


def test_pull_drops_a_miss_the_producer_never_proved(_isolated_cache, monkeypatch):
    """Zero hits alongside real misses in a gated namespace is the signature of a
    machine that could not read that source. Its "no abstract" is not a fact this
    machine should inherit — and because the row IS the checkpoint, not importing
    it is all it takes for this machine to fetch the DOI itself."""
    monkeypatch.setattr(cs, "capabilities", lambda: {"scopus": True, "s2": True,
                                                     "osf": True})
    _remote_store(monkeypatch, {"scopus:10.1/a": None, "scopus:10.1/b": "Body"},
                  {"scopus": {"hits": 0, "misses": 400}}, _isolated_cache)

    cs.pull_cache([cs.PARTS[cs.ABSTRACTS_PART]])

    assert abstract_store.lookup("scopus:10.1/a") == (False, None)   # unproven miss
    assert abstract_store.lookup("scopus:10.1/b") == (True, "Body")  # a hit is a hit


def test_pull_keeps_a_miss_from_a_source_needing_no_credential(
        _isolated_cache, monkeypatch):
    """Europe PMC needs no key, so its miss means the same thing on every machine
    — and not re-buying a known miss is most of the value of sharing."""
    monkeypatch.setattr(cs, "capabilities", lambda: {"scopus": True, "s2": True,
                                                     "osf": True})
    _remote_store(monkeypatch, {"epmc:10.1/a": None}, {}, _isolated_cache)
    cs.pull_cache([cs.PARTS[cs.ABSTRACTS_PART]])
    assert abstract_store.lookup("epmc:10.1/a") == (True, None)


def test_a_push_carrying_no_abstracts_marks_nothing_unreliable(
        _isolated_cache, monkeypatch):
    """Zero hits because the pusher shared only the LLM cache is not evidence that
    their Scopus was unreadable."""
    monkeypatch.setattr(cs, "capabilities", lambda: {"scopus": True, "s2": True,
                                                     "osf": True})
    assert cs._unreliable_namespaces({"parts": {"llm": {}}, "abstract_sources": {}}) == set()


# ---------------------------------------------------------------------------
# Pulling the file parts
# ---------------------------------------------------------------------------


def test_pull_never_overwrites_a_local_entry(_isolated_cache, monkeypatch):
    """Keys are content-complete, so a name that exists locally holds the same
    answer; overwriting could only change the mtime."""
    local = _llm_entry(_isolated_cache, "a.json", "mine")
    blob = cs.pack_shard([Path(_isolated_cache / "llm" / "a.json")])
    shard = cs.PARTS["llm"].shard_of("a.json")
    local.write_text("mine")          # the shard captured it; now it is local state
    _fake_hub(monkeypatch, {}, store={
        cs._remote_shard(cs.PARTS["llm"], shard): blob,
        cs._MANIFEST: json.dumps({"parts": {"llm": {shard: "sha"}}}).encode()})

    cs.pull_cache([cs.PARTS["llm"]])
    assert local.read_text() == "mine"


def test_pull_refuses_when_the_manifest_cannot_be_read(_isolated_cache, monkeypatch):
    """An unanswered Hub is not an empty repo, and the manifest is what says which
    sources the pushing machine could actually read."""
    _fake_hub(monkeypatch, {},
              download_errors={cs._MANIFEST: _HubHTTPError("503 Service Unavailable")})
    with pytest.raises(RuntimeError, match="503"):
        cs.pull_cache([cs.PARTS["llm"]])


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

    _fake_hub(monkeypatch, {}, store={
        cs._remote_shard(cs.PARTS["llm"], "0"): packed.getvalue(),
        cs._MANIFEST: json.dumps({"parts": {"llm": {"0": "sha"}}}).encode()})

    cs.pull_cache([cs.PARTS["llm"]])
    assert not (_isolated_cache / "escaped.json").exists()
    assert not (_isolated_cache / "llm" / "escaped.json").exists()


def test_parse_parts_names_the_known_parts():
    assert [p.name for p in cs.parse_parts("llm,abstracts")] == ["llm", "abstracts"]
    with pytest.raises(ValueError, match="doi_verify"):
        cs.parse_parts("nonsense")
