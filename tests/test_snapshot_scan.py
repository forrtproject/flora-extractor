"""Tests for the OpenAlex bulk-parquet snapshot scanner (issue #129, Stage 1).

One test per seam of the plan: manifest, ledger, crash recovery, the two-stage
gate, row construction, the enrichment bypass, snapshot opt-in, and pilot dedup.

Nothing here touches the network, the real ``cache/`` or the real ``data/``:
parquet fixtures are written into tmp_path by the tests themselves (matching the
real snapshot schema for the projected columns — note ``abstract_inverted_index``
is a raw JSON *string* column there, not a map), and every cache/ledger/index
path is monkeypatched into tmp_path.
"""


import json
import sys
import types

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from shared.schema import CANDIDATES_COLS
from search import openalex_search as oa
from search import run_search as rs
from search import snapshot_scan as ss


# ---------------------------------------------------------------------------
# Fixtures: parquet records shaped like the real works snapshot
# ---------------------------------------------------------------------------

_SOURCE = pa.struct([("display_name", pa.string())])
_SCHEMA = pa.schema([
    ("id", pa.string()),
    ("doi", pa.string()),
    ("title", pa.string()),
    ("display_name", pa.string()),
    ("publication_year", pa.int32()),
    ("authorships", pa.list_(pa.struct([("author", pa.struct([("display_name", pa.string())]))]))),
    ("primary_location", pa.struct([("source", _SOURCE), ("landing_page_url", pa.string())])),
    ("open_access", pa.struct([("oa_url", pa.string())])),
    ("concepts", pa.list_(pa.struct([("id", pa.string()),
                                     ("display_name", pa.string()),
                                     ("score", pa.float32())]))),
    ("abstract_inverted_index", pa.string()),
])


def _inverted(text: str) -> str:
    """The snapshot's raw JSON-string form of an inverted index for *text*."""
    index: dict[str, list[int]] = {}
    for pos, word in enumerate(text.split()):
        index.setdefault(word, []).append(pos)
    return json.dumps(index)


def _record(work_id="https://openalex.org/W1", doi=None, title="A study of bees",
            year=2024, abstract=None, concepts=(), authors=("Alice Smith",),
            journal="Journal of Bees", oa_url="https://example.org/p.pdf") -> dict:
    """One parquet-shaped snapshot record. *abstract* is already a JSON string."""
    return {
        "id": work_id,
        "doi": doi,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "authorships": [{"author": {"display_name": a}} for a in authors],
        "primary_location": {"source": {"display_name": journal},
                             "landing_page_url": "https://example.org/landing"},
        "open_access": {"oa_url": oa_url},
        "concepts": [{"id": c, "display_name": "Concept", "score": 0.9} for c in concepts],
        "abstract_inverted_index": abstract,
    }


def _table(records: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(records, schema=_SCHEMA)


def _write_parquet(path, records: list[dict]) -> str:
    pq.write_table(_table(records), path)
    return str(path)


def _mask(arr) -> list[bool]:
    """Gate masks may arrive with nulls where a column was null — read those as False."""
    return [bool(v) for v in arr.to_pylist()]


@pytest.fixture
def snap_env(tmp_path, monkeypatch):
    """Redirect every path the scanner writes to into tmp_path.

    The ledger, the manifest cache, candidates.csv and the candidates index are all
    process-global in production; a test that forgot one would corrupt the real
    pipeline state (see the CANDIDATES_INDEX idiom in tests/test_search.py).
    """
    snap_dir = tmp_path / "snapshot"
    snap_dir.mkdir()
    monkeypatch.setattr(ss, "SNAPSHOT_CACHE_DIR", snap_dir, raising=False)
    monkeypatch.setattr(ss, "_MANIFEST_PATH", snap_dir / "manifest.json", raising=False)
    monkeypatch.setattr(ss, "_LEDGER_PATH", snap_dir / "ledger.json", raising=False)
    monkeypatch.setattr(ss, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(rs.CANDIDATES_INDEX, "path", tmp_path / "candidates_index.txt")
    return types.SimpleNamespace(
        snap_dir=snap_dir,
        ledger=snap_dir / "ledger.json",
        manifest=snap_dir / "manifest.json",
        candidates=tmp_path / "candidates.csv",
        index=tmp_path / "candidates_index.txt",
        tmp=tmp_path,
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _manifest_urls(manifest: dict) -> list[str]:
    """URLs out of a manifest, whether files[] holds strings or {url, meta} dicts."""
    out = []
    for entry in manifest["files"]:
        out.append(entry if isinstance(entry, str) else (entry.get("url") or entry.get("key")))
    return out


def test_manifest_maps_s3_urls_to_https(snap_env, monkeypatch):
    """The manifest ships s3:// URLs, which no HTTP reader can open — they must be
    rewritten to the public bucket before anything tries to read a partition."""
    payload = {
        "date": "2026-06-26", "entity": "works",
        "files": [
            {"url": f"s3://openalex/data/parquet/works/updated_date=2016-0{i}-24/part_0000.parquet",
             "meta": {"content_length": 100 + i, "record_count": i}}
            for i in range(1, 4)
        ],
    }
    monkeypatch.setattr(ss.requests, "get", lambda *a, **kw: _Resp(payload))

    manifest = ss.fetch_manifest(refresh=True)
    urls = _manifest_urls(manifest)

    assert len(urls) == 3
    assert all(u.startswith("https://openalex.s3.amazonaws.com/data/parquet/works/") for u in urls)
    assert not any(u.startswith("s3://") for u in urls)
    assert snap_env.manifest.exists(), "the manifest must be cached, not re-fetched per run"


def test_manifest_without_files_raises(snap_env, monkeypatch):
    """A manifest we cannot read must fail loudly: yielding zero files silently would
    look exactly like 'the whole corpus is already scanned'."""
    monkeypatch.setattr(ss.requests, "get", lambda *a, **kw: _Resp({"date": "2026-06-26"}))
    with pytest.raises(Exception):
        ss.fetch_manifest(refresh=True)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_ledger_round_trip_and_needs_scan(snap_env):
    """A done file is skipped, a rewritten file (new content_length) is rescanned, and
    a file left 'merging' by a crash is unfinished business, not done work."""
    url = "https://openalex.s3.amazonaws.com/data/parquet/works/a/part_0000.parquet"
    ss.save_ledger({"gate_fingerprint": "abc", "files": {
        url: {"content_length": 500, "record_count": 9, "status": "done"},
    }})

    ledger = ss.load_ledger()
    assert ledger["files"][url]["content_length"] == 500
    assert ledger["gate_fingerprint"] == "abc"

    assert ss._needs_scan(url, {"content_length": 500}, ledger) is False
    assert ss._needs_scan(url, {"content_length": 999}, ledger) is True
    assert ss._needs_scan("https://openalex.s3.amazonaws.com/other.parquet",
                          {"content_length": 500}, ledger) is True

    ledger["files"][url]["status"] = "merging"
    assert ss._needs_scan(url, {"content_length": 500}, ledger) is True


def test_crash_between_csv_and_index_append_adds_no_duplicates(snap_env, monkeypatch):
    """A1: the merge appends to the CSV and *then* to the index. A crash in between
    leaves a stale-but-non-empty index that load_or_build() trusts, so a rescan would
    re-append the rows the index never learned about. Recovery from a 'merging' ledger
    entry must rebuild the index from the CSV first."""
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(work_id="https://openalex.org/W1", doi="https://doi.org/10.1/a",
                title="A direct replication of Smith (2009)"),
        _record(work_id="https://openalex.org/W2", doi="https://doi.org/10.1/b",
                title="A direct replication of Jones (2011)"),
    ])

    # Run 1, crashed: both rows reached the CSV, only the first row's keys reached
    # the index, and the ledger entry never advanced past "merging".
    def half_written_merge(df, out_path, enrich=True):
        df.to_csv(out_path, mode="a", index=False, header=not out_path.exists(),
                  encoding="utf-8")
        rs.CANDIDATES_INDEX.append(rs.row_keys(df.iloc[0]))
        return len(df)

    ss.scan_snapshot(files=[parquet], merge_fn=half_written_merge,
                     index_loader=rs._load_or_build_candidates_index)
    ledger = ss.load_ledger()
    ledger["files"][parquet]["status"] = "merging"
    ss.save_ledger(ledger)

    before = len(pd.read_csv(snap_env.candidates))
    assert before == 2

    n_merged = ss.scan_snapshot(files=[parquet], merge_fn=rs._merge_into_candidates_csv,
                                index_loader=rs._load_or_build_candidates_index)

    assert n_merged == 0, "a rescan after a crash must add nothing"
    assert len(pd.read_csv(snap_env.candidates)) == before
    assert ss.load_ledger()["files"][parquet]["status"] == "done"
    assert ss.load_ledger().get("gate_fingerprint"), "A8: the ledger records which gate produced it"


def test_merge_failure_raises_and_leaves_the_ledger_mid_merge(snap_env):
    """A merge that half-wrote the CSV must NOT be retried or skipped: the retry would
    append the same rows again, and popping the ledger entry would erase the 'merging'
    signal recovery depends on. It has to come straight out of scan_snapshot."""
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(work_id="https://openalex.org/W1", doi="https://doi.org/10.1/a",
                title="A direct replication of Smith (2009)"),
        _record(work_id="https://openalex.org/W2", doi="https://doi.org/10.1/b",
                title="A direct replication of Jones (2011)"),
    ])

    attempts = []

    def merge_then_die(df, out_path, enrich=True):
        attempts.append(len(df))
        df.to_csv(out_path, mode="a", index=False, header=not out_path.exists(),
                  encoding="utf-8")
        raise RuntimeError("disk gave out before the index append")

    with pytest.raises(RuntimeError, match="disk gave out"):
        ss.scan_snapshot(files=[parquet], merge_fn=merge_then_die,
                         index_loader=rs._load_or_build_candidates_index)

    assert attempts == [2], "a local-write failure must not be retried"
    assert ss.load_ledger()["files"][parquet]["status"] == "merging"
    assert len(pd.read_csv(snap_env.candidates)) == 2

    # The follow-up run sees "merging", rebuilds the index from the CSV, and adds nothing.
    n_merged = ss.scan_snapshot(files=[parquet], merge_fn=rs._merge_into_candidates_csv,
                                index_loader=rs._load_or_build_candidates_index)
    assert n_merged == 0
    assert len(pd.read_csv(snap_env.candidates)) == 2
    assert ss.load_ledger()["files"][parquet]["status"] == "done"


def test_old_gate_fingerprint_is_never_overwritten(snap_env, caplog):
    """The mismatch warning must keep firing every run until the user deletes the
    ledger — scanning one file under the new gate does not make the files marked done
    under the old one any less stale."""
    old_files = {f"https://openalex.s3.amazonaws.com/old_{i}.parquet":
                 {"content_length": 10 + i, "status": "done", "kept": 0} for i in range(3)}
    ss.save_ledger({"snapshot_date": "", "gate_fingerprint": "OLD-GATE", "files": old_files})

    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(doi="https://doi.org/10.1/a", title="A direct replication of Smith (2009)"),
    ])

    with caplog.at_level("WARNING"):
        ss.scan_snapshot(files=[parquet], merge_fn=rs._merge_into_candidates_csv,
                         index_loader=rs._load_or_build_candidates_index)

    assert any("DIFFERENT gate" in r.message for r in caplog.records), \
        "the mismatch must be reported"
    ledger = ss.load_ledger()
    assert ledger["gate_fingerprint"] == "OLD-GATE"
    assert ledger["files"][parquet]["status"] == "done"
    assert all(ledger["files"][u]["status"] == "done" for u in old_files)


def test_explicit_files_never_fetch_the_manifest(snap_env, monkeypatch):
    """Passing `files` pins the work to scan, so the manifest — a real network call —
    must not be fetched. The guard is a requests.get that blows up if anything tries."""
    def _no_network(*args, **kwargs):
        raise AssertionError(f"unexpected network call: {args} {kwargs}")

    monkeypatch.setattr(ss.requests, "get", _no_network)
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(doi="https://doi.org/10.1/a", title="A direct replication of Smith (2009)"),
    ])

    assert ss.scan_snapshot(files=[parquet], pilot_csv=snap_env.tmp / "pilot.csv") == 1
    assert ss.scan_snapshot(files=[parquet], merge_fn=rs._merge_into_candidates_csv,
                            index_loader=rs._load_or_build_candidates_index) == 1
    assert not snap_env.manifest.exists()


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_token_gate_matches_title_and_raw_abstract_json(snap_env):
    """Stage A runs the token regex over the title and over the *raw* inverted-index
    JSON, so an abstract-only signal survives without reconstructing every abstract."""
    batch = _table([
        _record(title="A direct replication of Smith (2009)"),
        _record(title="Foraging patterns in bees",
                abstract=_inverted("We assess the reproducibility of the original result")),
        _record(title="A re-analysis of the 2009 bee data"),
        _record(title="Foraging patterns in bees",
                abstract=_inverted("We describe how bees find flowers")),
    ])

    assert _mask(ss._gate_mask(batch)) == [True, True, True, False]


def test_gate_is_order_blind_but_stage_b_is_not(snap_env):
    """The raw JSON has no word order, so Stage A cannot tell "replications of" from
    "replications ... of" — it passes both. Stage B, run on the reconstructed text,
    is what rejects the accidental co-occurrence."""
    abstract = _inverted("Several replications were run independently of the pilot")
    rec = _record(title="Notes on honeybee foraging", abstract=abstract)

    assert _mask(ss._gate_mask(_table([rec]))) == [True]
    reconstructed = oa._reconstruct_abstract(json.loads(abstract))
    assert ss._admit(False, rec["title"], reconstructed) is False


def test_concept_hit_without_any_token(snap_env):
    """The concept arm is the recall arm: a paper OpenAlex classifies as Replication
    keeps its place even when neither title nor abstract says any replication word.
    Snapshot concept ids are URL-form and must be normalised before comparison (A5)."""
    rec = _record(title="Foraging patterns in bees",
                  abstract=_inverted("We describe how bees find flowers"),
                  concepts=("https://openalex.org/C12590798",))
    batch = _table([rec])

    assert _mask(ss._concept_mask(batch)) == [True]
    assert _mask(ss._gate_mask(batch)) == [False]
    # Nothing in the text admits it — the concept hit alone bypasses Stage B.
    assert ss._admit(False, rec["title"], "We describe how bees find flowers") is False
    assert ss._admit(True, rec["title"], "We describe how bees find flowers") is True


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------

def test_row_from_snapshot_matches_the_api_row(snap_env):
    """Snapshot rows and API rows land in the same CSV, so the only field allowed to
    differ is the source tag."""
    rec = _record(work_id="https://openalex.org/W123", doi="https://doi.org/10.1234/ABC.567",
                  title="A direct replication study", year=2024,
                  authors=("Alice Smith", "Bob Jones"), journal="Journal of Replications",
                  abstract=_inverted("This is a replication abstract"))
    api_json = {**rec, "abstract_inverted_index": json.loads(rec["abstract_inverted_index"])}

    row = ss._row_from_snapshot(rec)
    expected = oa._extract_row(api_json)

    assert row["source"] == ss.SOURCE_TAG_SNAPSHOT == "openalex_snapshot"
    for col in CANDIDATES_COLS:
        if col == "source":
            continue
        assert row[col] == expected[col], col


def test_snapshot_row_has_exactly_the_candidates_schema(snap_env):
    row = ss._row_from_snapshot(_record(doi="https://doi.org/10.1/a"))
    assert list(row.keys()) == CANDIDATES_COLS


# ---------------------------------------------------------------------------
# Merge: the enrichment bypass
# ---------------------------------------------------------------------------

def test_merge_enrich_false_skips_enrichment_and_returns_the_appended_count(
        snap_env, monkeypatch):
    """Snapshot rows already carry OpenAlex's abstract; sending 400 GB of them through
    the CrossRef/S2 backfill would be the run's dominant cost. The blank ones are
    handled later by `fetch_abstracts --skip-openalex`."""
    called = []
    monkeypatch.setattr(rs, "enrich_abstracts", lambda df: called.append(len(df)) or df)

    df = pd.DataFrame([{**{c: "" for c in CANDIDATES_COLS}, "doi_r": "10.1/a", "title_r": "A"}],
                      columns=CANDIDATES_COLS)
    n = rs._merge_into_candidates_csv(df, snap_env.candidates, enrich=False)

    assert n == 1
    assert called == [], "enrich=False must not reach the abstract backfill"

    other = pd.DataFrame([{**{c: "" for c in CANDIDATES_COLS}, "doi_r": "10.1/b", "title_r": "B"}],
                         columns=CANDIDATES_COLS)
    assert rs._merge_into_candidates_csv(other, snap_env.candidates) == 1
    assert called == [1], "the default path still enriches"


# ---------------------------------------------------------------------------
# Opt-in
# ---------------------------------------------------------------------------

def test_snapshot_runs_only_when_explicitly_requested(monkeypatch, tmp_path):
    """A2: a bare `python -m search.run_search` must never start a 400+ GB scan, so
    the snapshot is not part of "all sources" — only an explicit --source starts it."""
    calls = []
    fake = types.ModuleType("search.snapshot_scan")
    fake.scan_snapshot = lambda **kw: calls.append(kw) or 0
    monkeypatch.setitem(sys.modules, "search.snapshot_scan", fake)

    empty = pd.DataFrame(columns=CANDIDATES_COLS)
    monkeypatch.setattr(rs, "_harvest_oa_cache", lambda: empty)
    monkeypatch.setattr(rs, "_harvest_s2_cache", lambda: empty)
    monkeypatch.setattr(rs, "fetch_openalex_candidates", lambda **kw: empty)
    monkeypatch.setattr(rs, "fetch_semantic_scholar_candidates", lambda **kw: empty)
    monkeypatch.setattr(rs, "fetch_openalex_concept_candidates", lambda **kw: empty)
    monkeypatch.setattr(rs, "is_engine_enabled", lambda: False)
    monkeypatch.setattr(rs, "deduplicate_candidates", lambda df: df)
    monkeypatch.setattr(rs, "_merge_into_candidates_csv", lambda df, path, **kw: 0)

    rs.run_search(sources=None)
    assert calls == [], "the default run must not touch the snapshot"

    rs.run_search(sources={"openalex_snapshot"}, snapshot_max_files=2)
    assert len(calls) == 1
    assert calls[0]["max_files"] == 2


# ---------------------------------------------------------------------------
# Pilot mode
# ---------------------------------------------------------------------------

def test_pilot_dedups_against_its_own_csv(snap_env):
    """A6: pilot mode keeps no ledger, so re-running the same partition is expected —
    it must dedup against the pilot CSV in memory rather than growing it."""
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(doi="https://doi.org/10.1/a", title="A direct replication of Smith (2009)"),
    ])
    pilot = snap_env.tmp / "pilot.csv"

    first = ss.scan_snapshot(files=[parquet], pilot_csv=pilot)
    assert first == 1
    df = pd.read_csv(pilot, encoding="utf-8-sig")
    assert list(df.columns) == CANDIDATES_COLS
    assert df.loc[0, "source"] == ss.SOURCE_TAG_SNAPSHOT

    assert ss.scan_snapshot(files=[parquet], pilot_csv=pilot) == 0
    assert len(pd.read_csv(pilot, encoding="utf-8-sig")) == 1
    assert not snap_env.ledger.exists(), "pilot mode must not write the production ledger"
