"""Tests for the OpenAlex bulk-parquet snapshot scanner (issue #129, Stage 1).

One test per seam of the plan: manifest, ledger, crash recovery, the two-stage
gate, row construction, the enrichment bypass, and snapshot opt-in.

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

    The ledger and the manifest cache are process-global in production; a test that
    forgot one would corrupt the real pipeline state.
    """
    snap_dir = tmp_path / "snapshot"
    snap_dir.mkdir()
    monkeypatch.setattr(ss, "SNAPSHOT_CACHE_DIR", snap_dir, raising=False)
    monkeypatch.setattr(ss, "_MANIFEST_PATH", snap_dir / "manifest.json", raising=False)
    monkeypatch.setattr(ss, "_LEDGER_PATH", snap_dir / "ledger.json", raising=False)
    monkeypatch.setattr(ss, "DATA_DIR", tmp_path, raising=False)
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
    ss.save_ledger({"search_gate_fingerprint": "abc", "files": {
        url: {"content_length": 500, "record_count": 9, "status": "done"},
    }})

    ledger = ss.load_ledger()
    assert ledger["files"][url]["content_length"] == 500
    assert ss.ledger_gate_fingerprint(ledger) == "abc"

    assert ss._needs_scan(url, {"content_length": 500}, ledger) is False
    assert ss._needs_scan(url, {"content_length": 999}, ledger) is True
    assert ss._needs_scan("https://openalex.s3.amazonaws.com/other.parquet",
                          {"content_length": 500}, ledger) is True

    ledger["files"][url]["status"] = "merging"
    assert ss._needs_scan(url, {"content_length": 500}, ledger) is True


def test_a_failed_rescan_leaves_the_previous_record_standing(snap_env, monkeypatch, caplog):
    """OpenAlex rewrites partitions in place, so a done file is re-targeted when its
    content_length moves. If that rescan cannot be read, dropping the ledger entry would
    erase the record of the bytes that WERE consumed — and with it the rows already in
    candidates.csv, which a later run would then merge a second time."""
    url = "https://openalex.s3.amazonaws.com/works/updated_date=2024-01-01/part_0000.parquet"
    done = {"content_length": 100, "record_count": 10, "status": "done", "kept": 7,
            "scanned_at": "2024-02-02T00:00:00+00:00"}
    ss.save_ledger({"snapshot_date": "2024-02-01",
                    "search_gate_fingerprint": ss.search_gate_fingerprint(),
                    "files": {url: done}})

    monkeypatch.setattr(ss, "fetch_manifest", lambda: {
        "meta": {"updated_date": "2024-03-01"},
        "entries": [{"url": url, "meta": {"content_length": 200, "record_count": 20}}]})
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss, "_open_parquet", lambda u: (_ for _ in ()).throw(OSError("no route")))

    with caplog.at_level("ERROR"):
        assert ss.scan_snapshot() == 0

    assert ss.load_ledger()["files"][url] == done, \
        "a failed rescan must not erase what the previous scan consumed"


def test_a_ledger_from_another_gate_refuses_to_scan(snap_env):
    """Warning and continuing produced a mixed-generation pool: this gate's survivors
    appended to partitions read under another gate, whose rejects are in no pool at
    all. Nothing downstream can tell, so the scan refuses and says what the options
    are — and touches neither the ledger nor the pool on the way out."""
    old_files = {f"https://openalex.s3.amazonaws.com/old_{i}.parquet":
                 {"content_length": 10 + i, "status": "done", "kept": 0} for i in range(3)}
    ss.save_ledger({"snapshot_date": "", "search_gate_fingerprint": "OLD-GATE",
                    "files": old_files})

    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(doi="https://doi.org/10.1/a", title="A direct replication of Smith (2009)"),
    ])
    pool = snap_env.tmp / "pool"

    with pytest.raises(RuntimeError) as exc:
        ss.scan_snapshot(files=[parquet], survivor_pool=pool)
    assert "DIFFERENT" in str(exc.value)
    assert "--force-gate" in str(exc.value), "the message must name the way through"

    assert not pool.exists(), "a refused scan writes no pool file"
    assert ss.load_ledger()["files"].keys() == old_files.keys(), \
        "a refused scan leaves the ledger exactly as it found it"


def test_force_gate_scans_and_keeps_the_old_fingerprint(snap_env, caplog):
    """--force-gate is the operator saying they mean the mixture. The scan proceeds
    and says so — and the ledger keeps naming the gate most of its files were read
    under, because overwriting it would hide the mixture from the next run."""
    old_files = {"https://openalex.s3.amazonaws.com/old_0.parquet":
                 {"content_length": 10, "status": "done", "kept": 0}}
    ss.save_ledger({"snapshot_date": "", "search_gate_fingerprint": "OLD-GATE",
                    "files": old_files})
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(doi="https://doi.org/10.1/a", title="A direct replication of Smith (2009)"),
    ])

    with caplog.at_level("WARNING"):
        assert ss.scan_snapshot(files=[parquet], force_gate=True) == 1

    assert any("force-gate" in r.getMessage() for r in caplog.records)
    ledger = ss.load_ledger()
    assert ss.ledger_gate_fingerprint(ledger) == "OLD-GATE"
    assert ledger["files"][parquet]["status"] == "done"


def test_a_corrupt_ledger_is_a_hard_error(snap_env):
    """An unreadable ledger silently became an empty one, which reads as "nothing has
    been scanned" — a 725 GB rescan, and a second pass over partitions whose pool
    files are already on disk. A missing ledger still means a fresh start."""
    snap_env.ledger.write_text('{"files": {"a": {"status": "do', encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        ss.load_ledger()
    assert str(snap_env.ledger) in str(exc.value)

    snap_env.ledger.unlink()
    assert ss.load_ledger()["files"] == {}


def test_a_ledger_written_under_the_old_key_name_still_resumes(snap_env):
    """A 510M-row scan checkpointed as `stage_a_fingerprint` must not look like a new
    gate after the rename. Same value, older key name: the scan runs (a mismatch would
    now refuse to), and the partitions already marked done stay done."""
    old_files = {f"https://openalex.s3.amazonaws.com/old_{i}.parquet":
                 {"content_length": 10 + i, "status": "done", "kept": 0} for i in range(3)}
    ss.save_ledger({"snapshot_date": "",
                    "stage_a_fingerprint": ss.search_gate_fingerprint(),
                    "stage_b_fingerprint": "A-RETIRED-STAGE-B-VALUE",
                    "files": old_files})
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(doi="https://doi.org/10.1/a", title="A direct replication of Smith (2009)"),
    ])

    # No refusal: the legacy key name must never demand a 725 GB rescan.
    assert ss.scan_snapshot(files=[parquet]) == 1

    ledger = ss.load_ledger()
    assert ledger["stage_a_fingerprint"] == ss.search_gate_fingerprint()
    assert "search_gate_fingerprint" not in ledger, \
        "a ledger that already names the gate is left alone"
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

    assert ss.scan_snapshot(files=[parquet]) == 1
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


def test_the_gate_is_order_blind(snap_env):
    """The raw JSON has no word order, so the search gate cannot tell "replications of"
    from "replications ... of" — it passes both, and Stage 2 sorts them out over the
    reconstructed text the pool stores."""
    abstract = _inverted("Several replications were run independently of the pilot")
    rec = _record(title="Notes on honeybee foraging", abstract=abstract)

    assert _mask(ss._gate_mask(_table([rec]))) == [True]


def test_concept_ids_do_not_match_longer_ids_in_the_json_branch(snap_env):
    """Some snapshot builds ship `concepts` as a JSON string, matched by regex. An
    unanchored id is a prefix of longer ids — C9893847 inside C98938470 — and a concept
    hit admits the work on its own, so the false hit would enter the pool unchallenged."""
    real = sorted(ss._CONCEPT_IDS_BARE)[0]
    longer = pa.table({"concepts": [json.dumps(
        [{"id": f"https://openalex.org/{real}0", "display_name": "Other", "score": 0.9}])]})
    exact = pa.table({"concepts": [json.dumps(
        [{"id": f"https://openalex.org/{real}", "display_name": "Concept", "score": 0.9}])]})

    assert _mask(ss._concept_mask(longer)) == [False]
    assert _mask(ss._concept_mask(exact)) == [True]


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


# ---------------------------------------------------------------------------
# Malformed records
# ---------------------------------------------------------------------------

def test_an_inverted_index_that_is_not_a_dict_is_no_abstract(snap_env):
    """The snapshot ships the index as a JSON string, and a few records hold valid JSON
    that is not a {word: [positions]} object. Reconstructing it raised AttributeError,
    which the partition-read handler mistook for a transport failure."""
    for junk in ("[1, 2, 3]", '"an abstract"', "0", "not json at all"):
        rec = _record(title="A direct replication of Smith (2009)", abstract=junk)
        assert ss._abstract_text(rec["abstract_inverted_index"]) is None
        assert ss._row_from_snapshot(rec)["abstract_r"] is None


def test_a_defective_record_costs_one_row_not_the_partition(snap_env, monkeypatch, caplog):
    """A per-record parse defect fails identically on every retry, so retrying and then
    skipping the partition throws away its other ~200k sound records — under a run that
    reports success. The record is dropped; the partition is consumed."""
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", [
        _record(work_id="https://openalex.org/W1", doi="https://doi.org/10.1/a",
                oa_url="https://example.org/a.pdf",
                title="A direct replication of Smith (2009)"),
        _record(work_id="https://openalex.org/W2", doi="https://doi.org/10.1/b",
                oa_url="https://example.org/b.pdf",
                title="A direct replication of Jones (2011)"),
        _record(work_id="https://openalex.org/W3", doi="https://doi.org/10.1/c",
                oa_url="https://example.org/c.pdf",
                title="A direct replication of Brown (2013)"),
    ])

    opens = []
    real_open = ss._open_parquet
    monkeypatch.setattr(ss, "_open_parquet", lambda url: opens.append(url) or real_open(url))

    real_admit = ss._admitted_row

    def explode_on_w2(rec, counters, abstract=None):
        if rec.get("id") == "https://openalex.org/W2":
            raise AttributeError("'list' object has no attribute 'items'")
        return real_admit(rec, counters, abstract=abstract)

    monkeypatch.setattr(ss, "_admitted_row", explode_on_w2)

    with caplog.at_level("WARNING"):
        merged = ss.scan_snapshot(files=[parquet])

    assert merged == 2, "the two sound records must still be admitted"
    assert len(opens) == 1, "a record defect is not a read failure — no retry"
    assert ss.load_ledger()["files"][parquet]["kept"] == 2
    assert any("https://openalex.org/W2" in r.getMessage() for r in caplog.records), \
        "the skipped record must be named"


def test_a_defective_record_does_not_stop_the_pool(snap_env, monkeypatch):
    """The pool holds search-gate survivors, and one unreadable record must not cost
    the others their place in it — a truncated pool would silently narrow Stage 2."""
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", _POOL_RECORDS)
    pool = snap_env.tmp / "pool"

    real_admit = ss._admitted_row

    def explode_on_w2(rec, counters, abstract=None):
        if rec.get("id") == "https://openalex.org/W2":
            raise ValueError("malformed record")
        return real_admit(rec, counters, abstract=abstract)

    monkeypatch.setattr(ss, "_admitted_row", explode_on_w2)
    ss.scan_snapshot(files=[parquet], survivor_pool=pool)

    assert list(pd.read_parquet(pool)["id"]) == [f"https://openalex.org/W{i}" for i in (1, 2, 3, 4)]
# Stage 1 no longer has a keyword decision: a search-gate survivor is admitted, and
# every exclusion, rescue and tier assignment belongs to the engine's spec bundle.
# This table is what that buys — the same texts, routed by the ONE rule set. Written
# as a table rather than a property so the near-misses are visible, and each case
# names the pile the ENGINE must put it in, because "not discarded" is satisfied by
# too many things to be a contract.
_GATE_CASES = [
    # (title, abstract, search gate keeps it, pile the engine routes it to)
    # The bundle is live at its ends and shadow in the middle (CONVENTIONS.md),
    # so a weak-signal row reads `pending` and its comment names the shadow rule
    # that will claim it once that rule is switched on.
    #
    # replication-claim-cited-title, live: a claim arm AND an author-year cite,
    # both in the TITLE
    ("A direct replication of Smith (2010)",
     "We report a direct replication of the anchoring effect.",
     True, "screen_expensive"),
    # the same claim without a cited work in the title: the qualifier arm is
    # live since replication-claim-title-strong's promotion (2026-08-05)
    ("A direct replication of the anchoring effect",
     "We report a direct replication of Smith (2010).", True, "screen_expensive"),
    # the same claim in the abstract only is still a shadow tier's
    # (replication-claim-text) and so reads `pending`
    ("We replicate prior findings",
     "We replicate prior findings in a new population, naming no target.",
     True, "pending"),
    # replication-signal (shadow) — title stem only, bound for screen_cheap
    ("Reproducibility of the X effect",
     "Bees forage over long distances when the hive is disturbed.",
     True, "pending"),
    # reproduction-signal is shadow, so a reproduction-only row is claimed by no
    # live rule and lands in the coverage gap
    ("A computational reproduction",
     "We re-ran the authors' code and reproduced the published results.",
     True, "pending"),
    # both vocabularies in one row: the claim is in the abstract, so only the
    # shadow tiers of the claim family reach it
    ("A computational reproduction",
     "We replicated the code of Smith (2019) and re-ran every analysis.",
     True, "pending"),
    # the molecular sense: admitted by nothing, discarded by nothing
    ("Origins of DNA replication in yeast",
     "We map replication forks and origin firing across the genome.",
     True, "pending"),
    # a live discard: the title announces the genre and echoes its parent
    ("Review for: A replication of the anchoring effect",
     "Reviewer comments on the submitted manuscript.", True, "discard"),
    # a stem in the abstract alone is not a signal — no rule claims these
    ("Foraging patterns in bees",
     "Replicability was not assessed in this observational field study.",
     True, "pending"),
    ("Notes on honeybee foraging",
     "Several replications were run independently of the pilot.", True, "pending"),
    # no replication token anywhere: the search gate itself never sees this one
    ("A field experiment on consumer choice", "Prices were randomised by store.",
     False, "pending"),
]


def _engine_pile(title: str, abstract: str) -> str:
    """Route one synthetic pool row through the shipped spec bundle."""
    from pathlib import Path

    from filter.engine.route import route_batch
    from filter.engine.spec import load_specs

    record = {
        "id": "https://openalex.org/W1", "doi": "10.1/x", "title": title,
        "display_name": title, "publication_year": 2020, "type": "article",
        "authorships": json.dumps([]), "primary_location": json.dumps({}),
        "open_access": json.dumps({}), "concepts": json.dumps([]),
        "abstract_text": abstract, "hit_token_title": True,
        "hit_token_abstract": False, "hit_concept": False,
    }
    specs = load_specs(Path(__file__).resolve().parent.parent / "filter" / "spec")
    batch = pa.RecordBatch.from_pylist([record], schema=ss._POOL_SCHEMA)
    return route_batch(specs, batch).to_pylist()[0]["pile"]


@pytest.mark.parametrize("title,abstract,gated,pile", _GATE_CASES)
def test_stage1_admits_everything_and_the_engine_sorts_it(snap_env, title, abstract,
                                                          gated, pile):
    """One rule set, and it lives in Stage 2. Stage 1 decides on the search gate alone
    and keeps every survivor — including the ones the engine will discard — and the
    engine names the pile, so a spec edit that moved a row between tiers is visible."""
    batch = _table([_record(title=title, abstract=_inverted(abstract))])
    assert _mask(ss._gate_mask(batch)) == [gated], \
        "the search gate is the whole of Stage 1's decision"
    assert _engine_pile(title, abstract) == pile


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------

def test_snapshot_row_has_exactly_the_candidates_schema(snap_env):
    row = ss._row_from_snapshot(_record(doi="https://doi.org/10.1/a"))
    assert list(row.keys()) == CANDIDATES_COLS


# ---------------------------------------------------------------------------
# Merge: the enrichment bypass
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Opt-in
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Opt-in
# ---------------------------------------------------------------------------

def test_a_bare_invocation_never_starts_a_scan(monkeypatch):
    """A2: `python -m search.run_search` with no flags must never start a 400+ GB
    read. --scan is required — there is no other mode — and argparse must say so."""
    import runpy

    fake = types.ModuleType("search.snapshot_scan")
    fake.scan_snapshot = lambda **kw: pytest.fail("a bare invocation must not scan")
    monkeypatch.setitem(sys.modules, "search.snapshot_scan", fake)
    monkeypatch.setattr(sys, "argv", ["run_search"])

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("search.run_search", run_name="__main__")
    assert exc.value.code == 2


def test_the_dashboard_is_refreshed_even_when_the_scan_dies(monkeypatch):
    """The machine holding the pool is the only one that can write Stage 1's stats —
    a request path never scans column data. So the refresh runs in `finally`: a
    Ctrl-C 300 files in must still leave the dashboard showing what was scanned."""
    import runpy

    refreshed = []
    fake = types.ModuleType("search.snapshot_scan")
    fake.scan_snapshot = lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt())
    monkeypatch.setitem(sys.modules, "search.snapshot_scan", fake)

    import shared.dashboard_cache as dc
    monkeypatch.setattr(dc, "refresh",
                        lambda stage, pool_dir=None: refreshed.append(stage))
    monkeypatch.setattr(sys, "argv", ["run_search", "--scan"])

    with pytest.raises(KeyboardInterrupt):
        runpy.run_module("search.run_search", run_name="__main__")
    assert refreshed == [dc.POOL_STAGE]


def test_the_dashboard_is_refreshed_from_the_pool_this_run_wrote(monkeypatch, tmp_path):
    """`--survivor-pool` moves where the scan writes, so it must move where the
    stats are read from too. Publishing SNAPSHOT_POOL_DIR's numbers after scanning
    somewhere else is silently wrong — the dashboard would show a stale pool's
    counts as if they were this run's."""
    import runpy

    elsewhere = tmp_path / "other-pool"
    seen = {}
    fake = types.ModuleType("search.snapshot_scan")
    fake.scan_snapshot = lambda **kw: 0
    monkeypatch.setitem(sys.modules, "search.snapshot_scan", fake)

    import shared.dashboard_cache as dc
    monkeypatch.setattr(dc, "refresh",
                        lambda stage, pool_dir=None: seen.update(stage=stage, pool=pool_dir))
    monkeypatch.setattr(sys, "argv",
                        ["run_search", "--scan", "--survivor-pool", str(elsewhere)])

    runpy.run_module("search.run_search", run_name="__main__")
    assert seen["stage"] == dc.POOL_STAGE
    assert seen["pool"] == elsewhere


def test_scan_reaches_the_scanner_with_the_operator_flags(monkeypatch, tmp_path):
    calls = []
    fake = types.ModuleType("search.snapshot_scan")
    fake.scan_snapshot = lambda **kw: calls.append(kw) or 0
    monkeypatch.setitem(sys.modules, "search.snapshot_scan", fake)
    monkeypatch.setattr(sys, "argv", ["run_search", "--scan", "--snapshot-max-files", "2",
                                      "--survivor-pool", str(tmp_path / "pool")])
    import runpy
    runpy.run_module("search.run_search", run_name="__main__")

    assert len(calls) == 1
    assert calls[0]["max_files"] == 2
    assert calls[0]["survivor_pool"] == tmp_path / "pool"


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def test_a_retried_partition_is_counted_once(snap_env, monkeypatch, caplog):
    """A read that dies late has already counted its rows, and the retry re-reads the
    partition from row zero. Counting both passes inflated every number the run
    reports — scanned, survivors, admitted, kept — by a whole partition."""
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", _POOL_RECORDS)
    pool = snap_env.tmp / "pool"
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)

    real_open = ss._open_parquet
    attempts = {"n": 0}

    class _DiesAtTheEnd:
        """Yields the partition's batches in full, then fails — like a connection
        dropped after the last range request."""

        def __init__(self, pf):
            self._pf = pf

        @property
        def schema_arrow(self):
            return self._pf.schema_arrow

        def iter_batches(self, **kwargs):
            yield from self._pf.iter_batches(**kwargs)
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("connection reset")

    monkeypatch.setattr(ss, "_open_parquet", lambda url: _DiesAtTheEnd(real_open(url)))

    with caplog.at_level("INFO"):
        merged = ss.scan_snapshot(files=[parquet], survivor_pool=pool)

    assert attempts["n"] == 2, "the partition must actually have been read twice"
    assert merged == 4, "four of the five records survive the gate — once, not twice"
    assert ss.load_ledger()["files"][parquet]["kept"] == 4
    assert len(pd.read_parquet(pool)) == 4

    report = next(r.getMessage() for r in caplog.records
                  if "Snapshot gate report" in r.getMessage())
    assert f"{len(_POOL_RECORDS)} row(s) scanned" in report
    assert "4 survivor(s)" in report and "4 admitted" in report


# ---------------------------------------------------------------------------
# The Stage A survivor pool
# ---------------------------------------------------------------------------

_POOL_RECORDS = [
    _record(work_id="https://openalex.org/W1", doi="https://doi.org/10.1/a", oa_url="https://example.org/a.pdf",
            title="A direct replication of Smith (2009)"),
    _record(work_id="https://openalex.org/W2", doi="https://doi.org/10.1/b", oa_url="https://example.org/b.pdf",
            title="Foraging patterns in bees",
            abstract=_inverted("We assess the reproducibility of the original result")),
    _record(work_id="https://openalex.org/W3", doi="https://doi.org/10.1/c", oa_url="https://example.org/c.pdf",
            title="Nectar chemistry in alpine meadows",
            abstract=_inverted("We describe how bees find flowers"),
            concepts=("https://openalex.org/C12590798",)),
    # Stage A keeps this one (the raw index has both words), Stage B rejects it.
    _record(work_id="https://openalex.org/W4", doi="https://doi.org/10.1/d", oa_url="https://example.org/d.pdf",
            title="Notes on honeybee foraging",
            abstract=_inverted("Several replications were run independently of the pilot")),
    # Never a survivor: nothing for Stage A to see.
    _record(work_id="https://openalex.org/W5", doi="https://doi.org/10.1/e", oa_url="https://example.org/e.pdf",
            title="Wing morphology of bumblebees",
            abstract=_inverted("We measured wings")),
]


def test_pool_stores_every_stage_a_survivor_with_why_it_survived(snap_env):
    """The pool is what makes a Stage B change cheap, so it must hold every Stage A
    survivor — including the ones Stage B rejects — and record which arm kept each."""
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", _POOL_RECORDS)
    pool = snap_env.tmp / "pool"

    ss.scan_snapshot(files=[parquet], survivor_pool=pool)

    files = list(pool.glob("*.parquet"))
    assert len(files) == 1, "one pool file per partition"
    assert files[0].name == ss._pool_file_name(parquet)
    assert not list(pool.glob("*.tmp")), "the temp file must not survive a clean run"

    df = pd.read_parquet(files[0])
    assert list(df.columns) == ss._POOL_SCHEMA.names
    assert list(df["id"]) == [f"https://openalex.org/W{i}" for i in (1, 2, 3, 4)], \
        "W5 has no Stage A signal; W4 survives Stage A even though Stage B rejects it"

    by_id = df.set_index("id")
    w1, w2, w3 = (by_id.loc[f"https://openalex.org/W{i}"] for i in (1, 2, 3))
    assert (bool(w1.hit_token_title), bool(w1.hit_token_abstract), bool(w1.hit_concept)) \
        == (True, False, False)
    assert (bool(w2.hit_token_title), bool(w2.hit_token_abstract), bool(w2.hit_concept)) \
        == (False, True, False)
    assert (bool(w3.hit_token_title), bool(w3.hit_token_abstract), bool(w3.hit_concept)) \
        == (False, False, True)

    # The abstract is stored as reading-order text, not as the inverted index.
    assert w2.abstract_text == "We assess the reproducibility of the original result"
    assert json.loads(w1.authorships)[0]["author"]["display_name"] == "Alice Smith"


def test_rescanning_a_partition_overwrites_its_pool_file(snap_env):
    """A partition re-read after a crash must replace its pool file — a second copy
    would double every one of its rows the next time the pool is read."""
    parquet = _write_parquet(snap_env.tmp / "part_0000.parquet", _POOL_RECORDS)
    pool = snap_env.tmp / "pool"

    ss.scan_snapshot(files=[parquet], survivor_pool=pool)
    first = pd.read_parquet(pool)

    # The crash: the ledger says this partition was left mid-scan, so it is re-read.
    ledger = ss.load_ledger()
    ledger["files"][parquet]["status"] = "merging"
    ss.save_ledger(ledger)
    ss.scan_snapshot(files=[parquet], survivor_pool=pool)

    assert len(list(pool.glob("*.parquet"))) == 1
    assert len(pd.read_parquet(pool)) == len(first) == 4


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_counts_progress_and_never_fetches_the_manifest(snap_env, monkeypatch):
    """--status reports done-vs-total from the cached manifest and the ledger only.

    The whole point of the command is that it can be run against a scan in flight, so
    it must read local state and make no call of its own — hence the network guard.
    """
    def _no_network(*a, **kw):
        raise AssertionError("--status must not touch the network")

    monkeypatch.setattr(ss.requests, "get", _no_network)

    snap_env.manifest.write_text(json.dumps({"entries": [
        {"url": "https://openalex.s3.amazonaws.com/works/updated_date=2024-01-01/part_0000.parquet",
         "meta": {"content_length": 1_000, "record_count": 10}},
        {"url": "https://openalex.s3.amazonaws.com/works/updated_date=2024-01-02/part_0000.parquet",
         "meta": {"content_length": 3_000, "record_count": 30}},
    ]}), encoding="utf-8")
    snap_env.ledger.write_text(json.dumps({"snapshot_date": "2024-02-01", "files": {
        "https://openalex.s3.amazonaws.com/works/updated_date=2024-01-01/part_0000.parquet":
            {"content_length": 1_000, "record_count": 10, "status": "done", "kept": 7,
             "scanned_at": "2024-02-02T00:00:00+00:00"},
        "https://openalex.s3.amazonaws.com/works/updated_date=2024-01-02/part_0000.parquet":
            {"content_length": 3_000, "record_count": 30, "status": "merging"},
    }}), encoding="utf-8")

    status = ss.scan_status(snap_env.tmp / "pool")

    assert (status["files_done"], status["files_total"]) == (1, 2)
    assert (status["bytes_done"], status["bytes_total"]) == (1_000, 4_000)
    assert (status["records_done"], status["records_total"]) == (10, 40)
    assert status["rows_kept"] == 7
    assert len(status["files_in_flight"]) == 1
