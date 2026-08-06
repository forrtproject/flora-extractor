"""Wave D: the claimed LLM tiers, the handoff, and Stage 3's contract (#146 M4).

One test per seam, and every seam here is about money or evidence: that a dry run
spends nothing, that the claim happens before the first voter, that a refused
claim refuses cleanly, that the two voter answers turn into the right verdict,
that validation mode changes nothing and live mode does, that an exhausted budget
fails the claim without losing what was already decided, that a raw response is on
disk before the row naming it exists, and that the file Stage 3 reads is one it
accepts.

Every network call — LLM, Supabase, Hugging Face — is mocked. Nothing here talks
to anything.
"""

import argparse
import hashlib
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from filter.engine import handoff as handoff_mod
from filter.engine import tiers
from filter.engine.claims import PENDING_UPLOAD, ClaimConflict, UnknownRelease
from filter.engine.export import export_pile
from filter.engine.store import build_routing, open_store
from search.snapshot_scan import _POOL_SCHEMA
from shared.schema import ENGINE_EXPORTED_COLS, validate_csv_columns
from shared.token_usage import TokenBudgetExhausted
from tests import engine_bundle

# The synthetic bundle, not `filter/spec/`: every seam here is tier machinery
# (claiming, spending, verdicts, the handoff), not which shipped rule sent a row
# to a pile. The shipped bundle also could not serve — its admission rules are
# shadow, so it routes nothing to a screening tier.
RELEASE = "rel-m4"

_CITE = "as reported by Smith et al. (2019)"

# `_decided_client`'s default: stamp the claim with whatever generation the code
# is at now, as a real run does. A sentinel rather than None, because None is the
# legacy claim a test asks for on purpose.
_CURRENT = object()


def _row(work: int, title: str, abstract: str, year: int = 2024,
         concepts: tuple = ()) -> dict:
    return {
        "id": f"https://openalex.org/W{work}",
        "doi": f"10.1234/w{work}",
        "title": title,
        "display_name": title,
        "publication_year": year,
        "type": "article",
        "authorships": json.dumps([{"author": {"display_name": "A. Author"}}]),
        "primary_location": json.dumps({"source": {"display_name": "J. Repl."},
                                        "landing_page_url": "https://example.org/1"}),
        "open_access": json.dumps({"oa_url": None}),
        "concepts": json.dumps([{"id": f"https://openalex.org/{c}"}
                                for c in concepts]),
        "abstract_text": abstract,
        "hit_token_title": True,
        "hit_token_abstract": False,
        "hit_concept": False,
    }


# Two works per screen pile, so an ordering assertion has something to order and a
# discard has something to leave behind.
POOL_ROWS = [
    _row(11, "A direct replication of the Smith effect",
         f"We report a direct replication of the anchoring effect, {_CITE}."),
    _row(12, "A direct replication of the Jones effect",
         f"We report a direct replication of the framing effect, {_CITE}."),
    # The cheap pile's rows are deliberately free of hard signals and comfortably
    # over PRESCREEN_MIN_ABSTRACT_CHARS, so the tier actually asks its voters
    # about them rather than bypassing (which is its own test, below).
    _row(21, "Replicability in the social sciences", concepts=("C12590798",),
         abstract=
         "We survey how researchers describe their own methods sections across "
         "three decades of published work, coding each article for the presence "
         "of a materials appendix, a data statement and a power analysis, and we "
         "relate those codes to the journal's editorial policy at the time."),
    _row(22, "Reproducibility of the X effect", concepts=("C12590798",),
         abstract=
         "Bees forage over long distances when the hive is disturbed, and the "
         "colony recovers within a fortnight of the disturbance ending. We "
         "tracked eleven hives across two summers and measured foraging radius, "
         "return rate and brood temperature under three disturbance regimes."),
]


@pytest.fixture
def pool(tmp_path) -> Path:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    pq.write_table(pa.Table.from_pylist(POOL_ROWS, schema=_POOL_SCHEMA),
                   pool_dir / "2024.parquet")
    return pool_dir


@pytest.fixture
def con(pool):
    store = open_store(Path(":memory:"))
    build_routing(store, pool, engine_bundle.specs(), RELEASE)
    return store


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Response blobs and run reports go to tmp_path, never the real cache/."""
    monkeypatch.setattr(tiers, "RESPONSES_DIR", tmp_path / "responses")
    monkeypatch.setattr(tiers, "RUNS_DIR", tmp_path / "runs")
    # No Hugging Face unless a test installs one. This is the real skip-the-upload
    # flag, so every test here also exercises the "uploads are off" configuration:
    # the blobs stay on disk, the verdicts stay pending, the run is unaffected.
    monkeypatch.setattr(tiers, "ENGINE_TIER_HF_UPLOAD", False)


def _client(calls: list) -> MagicMock:
    """A claims client that records the order of everything asked of it."""
    client = MagicMock()
    client.claim.side_effect = lambda *a, **k: calls.append(("claim", a, k)) or "claim-1"
    client.record_verdict.side_effect = \
        lambda **k: calls.append(("record_verdict", k)) or "verdict-1"
    client.release_claim.side_effect = \
        lambda *a: calls.append(("release_claim", a)) or "ok"
    client.claimed_work_ids.return_value = set()
    client.verdicts.return_value = []
    client.claims.return_value = []
    return client


def _cheap_votes(*answers):
    """Patch the tier's voting seam so voter *i* always answers `answers[i]`.

    Per voter rather than per call, because the tier's rule is about which VOTER
    said what — a sequence would make "voter 2 was never asked" and "voter 2 said
    yes" the same fixture.
    """
    from shared.prescreen import prescreen_voters
    models = [m for _, m in prescreen_voters()]
    seen: list = []

    def vote(prompt, provider, model, doi_r, title):
        seen.append(("vote", model, title))
        index = models.index(model)
        return answers[index] if index < len(answers) else answers[-1]

    return patch("filter.engine.tiers.prescreen_vote", side_effect=vote), seen


# ---------------------------------------------------------------------------
# Reading the pile
# ---------------------------------------------------------------------------


def test_the_pile_reader_attaches_the_pool_text(con, pool):
    works = tiers.pile_works(con, RELEASE, "screen_cheap", pool)
    assert {w.work_id for w in works} == {21, 22}
    assert all(w.abstract for w in works)
    assert all(w.title for w in works)


def test_the_pile_reader_stops_scanning_once_it_has_every_wanted_work(
        con, pool, monkeypatch):
    """A batch of 500 must not cost a scan of 5.1M pool rows. The order is the
    pool's either way — the scan stops, it does not choose differently."""
    real = tiers.iter_pool_batches
    exhausted = []

    def counting(*args, **kwargs):
        yield from real(*args, **kwargs)
        exhausted.append(True)

    monkeypatch.setattr(tiers, "iter_pool_batches", counting)
    works = tiers.pile_works(con, RELEASE, "screen_cheap", pool)
    assert {w.work_id for w in works} == {21, 22}
    assert not exhausted


# ---------------------------------------------------------------------------
# The dry run (issue #146 §6)
# ---------------------------------------------------------------------------


def test_dry_run_spends_nothing_and_prints_an_estimate(con, pool, capsys):
    """The default. Nothing claimed, no voter asked, and a number to decide on."""
    calls: list = []
    client = _client(calls)
    with patch("filter.engine.tiers.prescreen_vote") as vote:
        report = tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool)

    assert report["dry_run"] is True
    assert vote.call_count == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "row(s) → tier screen_cheap ≈ $" in out
    assert "per 100,000 rows" in out
    assert report["estimate"]["rows"] == 2
    assert report["estimate"]["tokens_per_row"]["p50"] > 0


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def test_a_run_claims_before_it_asks_any_voter(con, pool):
    calls: list = []
    client = _client(calls)
    patcher, seen = _cheap_votes("yes")
    with patcher:
        tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True,
                               batch_label="wave-1")

    assert calls[0][0] == "claim", "a voter was asked before the batch was claimed"
    assert seen, "no voter was asked at all"
    _, args, kwargs = calls[0]
    assert args[0] == RELEASE and args[1] == "screen_cheap"
    assert kwargs["meta"] == {"batch": "wave-1", "mode": "validation",
                              "engine_tier": "screen_cheap",
                              "generation": tiers.screening_generation("screen_cheap")}
    assert calls[-1] == ("release_claim", ("claim-1", "complete"))


def _route_args(pool: Path, tmp_path: Path, monkeypatch) -> argparse.Namespace:
    from filter.engine import cli

    monkeypatch.setattr(cli, "load_specs", lambda spec_dir: engine_bundle.specs())
    monkeypatch.setattr(cli, "load_aliases", lambda path: {})
    monkeypatch.setattr(cli, "bundle_hash", lambda spec_dir: "bundle-x")
    monkeypatch.setattr(cli, "alias_release", lambda path: "alias-x")
    return argparse.Namespace(spec_dir=tmp_path, pool=pool,
                              store=tmp_path / "engine.duckdb",
                              pool_manifest_hash="pool-x", overlay=None,
                              no_overlay=True)


def test_route_registers_the_release_it_just_wrote(pool, tmp_path, monkeypatch,
                                                   capsys):
    """Without this row in `engine_releases` the claim RPC rejects every batch."""
    from filter.engine import cli

    client = MagicMock()
    registered: list = []
    client.register_release.side_effect = lambda record: registered.append(record)
    monkeypatch.setattr("filter.engine.claims.ClaimsClient", lambda: client)

    assert cli.cmd_route(_route_args(pool, tmp_path, monkeypatch)) == 0

    assert len(registered) == 1
    assert registered[0]["release_id"] and registered[0]["bundle_hash"] == "bundle-x"
    assert "registered with the state authority" in capsys.readouterr().out


def test_route_without_a_state_authority_warns_and_still_routes(pool, tmp_path,
                                                                monkeypatch, capsys):
    """Routing stays usable offline; the warning says what will be missing."""
    from filter.engine import cli
    from filter.engine.claims import ClaimsNotConfigured

    monkeypatch.setattr("filter.engine.claims.ClaimsClient",
                        MagicMock(side_effect=ClaimsNotConfigured("SUPABASE_URL unset")))

    assert cli.cmd_route(_route_args(pool, tmp_path, monkeypatch)) == 0

    out = capsys.readouterr().out
    assert "WARNING: release not registered" in out and "screen --run" in out
    # The routing itself happened, and the local record is there for the claim
    # path to register from.
    assert list((tmp_path / "releases").glob("*.json"))


def test_an_unregistered_release_is_registered_and_the_claim_retried_once(con, pool):
    """`route` writes the release locally; only a registered one can be claimed.

    The production failure: every `screen --run` died with `unknown_release`
    because nothing ever inserted the row. The claim path repairs it from the
    record on disk rather than telling the operator to re-route.
    """
    calls: list = []
    client = _client(calls)
    _write_release_record(pool.parent)
    claimed: list = []

    def claim(*args, **kwargs):
        calls.append(("claim", args, kwargs))
        claimed.append(args)
        if len(claimed) == 1:
            raise UnknownRelease("unknown_release: rel — refresh and re-route")
        return "claim-1"

    client.claim.side_effect = claim
    client.register_release.side_effect = lambda record: calls.append(
        ("register_release", record["release_id"])) or record["release_id"]

    patcher, _ = _cheap_votes("yes")
    with patcher:
        report = tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True,
                                        cache_dir=pool.parent)

    assert [c[0] for c in calls[:3]] == ["claim", "register_release", "claim"]
    assert calls[1][1] == RELEASE
    assert report["claim_id"] == "claim-1"
    assert report["decided"] == 2


def test_a_failed_registration_names_registration_not_re_routing(con, pool):
    """The old message told the operator to re-route, which fixes nothing."""
    from filter.engine.claims import ClaimsError

    client = _client([])
    _write_release_record(pool.parent)
    client.claim.side_effect = UnknownRelease("unknown_release: rel")
    client.register_release.side_effect = ClaimsError("HTTP 401: no insert rights")

    with patch("filter.engine.tiers.prescreen_vote") as vote:
        with pytest.raises(SystemExit) as excinfo:
            tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True,
                                   cache_dir=pool.parent)

    message = str(excinfo.value)
    assert vote.call_count == 0
    assert "registering it failed" in message and "HTTP 401" in message
    assert "re-route" not in message


def _write_release_record(cache_dir: Path) -> dict:
    """The record `route` leaves beside the store, filed under this test's id.

    Written directly rather than through `write_release()`, which derives the id
    from the six inputs: the store here is routed under RELEASE, and the record
    has to be the one `read_release(RELEASE)` finds.
    """
    from filter.engine.release import releases_dir

    record = {"release_id": RELEASE, "pool_manifest_hash": "pool-x",
              "overlay_hash": None, "bundle_hash": "bundle-x",
              "engine_version": "e", "alias_release": "alias-x",
              "schema_version": "csv:1", "created_at": "2026-08-05"}
    path = releases_dir(cache_dir) / f"{RELEASE}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def test_a_claim_conflict_refuses_without_spending(con, pool):
    calls: list = []
    client = _client(calls)
    client.claim.side_effect = ClaimConflict("screen_cheap", "3 works already held")
    with patch("filter.engine.tiers.prescreen_vote") as vote:
        with pytest.raises(SystemExit) as excinfo:
            tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True)

    assert vote.call_count == 0
    assert "screen_cheap" in str(excinfo.value)
    assert "nothing here was claimed or spent" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The cheap tier's one rule
# ---------------------------------------------------------------------------


def test_two_noes_discard_and_one_keep_proceeds(con, pool):
    """The whole semantic of the discard-only tier, both ways round."""
    calls: list = []
    patcher, _ = _cheap_votes("no", "no")
    with patcher:
        both_no = tiers.run_screen_cheap(con, _client(calls), RELEASE, pool_dir=pool,
                                         run=True)
    assert both_no["outcomes"] == {"discard": 2}

    patcher, seen = _cheap_votes("yes")
    with patcher:
        one_keep = tiers.run_screen_cheap(con, _client([]), RELEASE, pool_dir=pool,
                                          run=True)
    assert one_keep["outcomes"] == {"proceed": 2}
    # Voter 2 is not asked once the row can no longer be discarded.
    assert len(seen) == 2, "voter 2 was paid for an answer that could change nothing"


def test_a_hard_signal_row_is_never_asked_of_a_small_model(con, pool):
    """The three rows `prescreen_bypass()` refuses to let a 3B model end. A bypass
    is recorded as a verdict — deciding not to ask is a decision — and costs
    nothing, because no voter is called."""
    calls: list = []
    signal = tiers.Work(21, "10.1/x", "A systematic replication study",
                        "The purpose of this systematic replication study was to "
                        "re-test the original effect in a new sample drawn from "
                        "the same population, using the authors' own materials "
                        "and a preregistered analysis plan agreed in advance with "
                        "the original team.", "screen_cheap")
    with patch("filter.engine.tiers.prescreen_vote") as vote:
        outcome, votes = tiers._cheap_judge(signal)

    assert vote.call_count == 0
    assert outcome == "proceed"
    assert [v["model"] for v in votes] == ["prescreen_bypass"]
    assert votes[0]["quote"].startswith("hard_signal:")
    assert calls == []


def test_a_non_answer_is_not_a_no(con, pool):
    """An unreadable reply must fall through to proceed, never to the terminal side."""
    patcher, _ = _cheap_votes(None, "no")
    with patcher:
        report = tiers.run_screen_cheap(con, _client([]), RELEASE, pool_dir=pool,
                                        run=True)
    assert report["outcomes"] == {"proceed": 2}


# ---------------------------------------------------------------------------
# Evidence before the verdict that names it (§4)
# ---------------------------------------------------------------------------


def test_the_response_blob_is_on_disk_before_the_verdict_row(con, pool, tmp_path):
    calls: list = []
    client = _client(calls)
    written: list = []
    real_write = tiers._write_response

    def spy(blob):
        response_hash, path = real_write(blob)
        written.append(("write_response", response_hash,
                        (tmp_path / "responses" / f"{response_hash}.json").exists()))
        calls.append(("write_response", response_hash))
        return response_hash, path

    patcher, _ = _cheap_votes("yes")
    with patcher, patch.object(tiers, "_write_response", spy):
        tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True)

    order = [c[0] for c in calls]
    assert order.index("write_response") < order.index("record_verdict")
    assert all(exists for _, _, exists in written), "verdict named a blob not on disk"
    # No HF push was possible, and the state says so rather than claiming otherwise.
    states = [c[1]["response_state"] for c in calls if c[0] == "record_verdict"]
    assert states and set(states) == {PENDING_UPLOAD}
    assert all(c[1]["response_hash"] for c in calls if c[0] == "record_verdict")


# ---------------------------------------------------------------------------
# The upload: batched commits, and a state that never overclaims
# ---------------------------------------------------------------------------


class _FakeHub:
    """The two calls `_ResponseUploader` makes, and a record of them."""

    def __init__(self, fail: bool = False) -> None:
        self.commits: list[list[str]] = []
        self.fail = fail

    def install(self, monkeypatch) -> None:
        import sys
        import types
        module = types.ModuleType("huggingface_hub")
        module.CommitOperationAdd = lambda path_in_repo=None, path_or_fileobj=None: (
            types.SimpleNamespace(path_in_repo=path_in_repo, payload=path_or_fileobj))
        module.HfApi = lambda token=None: self
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)
        monkeypatch.setattr(tiers, "ENGINE_TIER_HF_UPLOAD", True)
        monkeypatch.setattr(tiers, "FLORA_POOL_REPO", "org/pool")
        monkeypatch.setenv("HF_TOKEN", "hf_test")

    def create_commit(self, repo_id=None, repo_type=None, operations=None,
                      commit_message=None):
        if self.fail:
            raise RuntimeError("HTTP 429 Too Many Requests")
        self.commits.append([op.path_in_repo for op in operations])


def _many_works(n: int) -> list:
    """Works the cheap tier actually asks its voters about — no hard signal, and
    comfortably over PRESCREEN_MIN_ABSTRACT_CHARS, or a bypass would answer for it."""
    abstract = (
        "We survey how researchers describe their own methods sections across three "
        "decades of published work, coding each article for the presence of a "
        "materials appendix, a data statement and a power analysis, and we relate "
        "those codes to the journal's editorial policy at the time of publication.")
    return [tiers.Work(1000 + i, f"10.1/w{i}", f"Study {i}", abstract, "screen_cheap")
            for i in range(n)]


def test_blobs_are_committed_in_batches_not_one_commit_each(con, pool, monkeypatch):
    """The defect this replaced: one HF commit per response blob (429s all round)."""
    hub = _FakeHub()
    hub.install(monkeypatch)
    monkeypatch.setattr(tiers, "FLORA_HF_COMMIT_BATCH", 10)
    calls: list = []
    client = _client(calls)
    works = _many_works(12)                       # 12 works × 2 votes = 24 blobs

    patcher, _ = _cheap_votes("no", "no")
    with patcher:
        tiers.run_tier(tiers.SCREEN_CHEAP, client, RELEASE, works,
                       mode="validation", batch_label="b", run=True)

    committed = [name for commit in hub.commits for name in commit]
    assert len(committed) == 24
    # Two full batches of ten and a final flush of four — not 24 commits.
    assert [len(c) for c in hub.commits] == [10, 10, 4]
    # Every verdict row goes in pending; the state is corrected once a commit took
    # the bytes, and never before.
    states = {c[1]["response_state"] for c in calls if c[0] == "record_verdict"}
    assert states == {PENDING_UPLOAD}
    marked = {h for call in client.mark_uploaded.call_args_list for h in call.args[0]}
    assert len(marked) == 24


def test_blobs_land_in_a_sharded_remote_folder(con, pool, monkeypatch):
    """A full expensive-pile screen is ~9,300 blobs and Hugging Face asks for fewer
    than 10k entries per folder, so the remote path is sharded on the hash's first
    two characters. The on-disk layout stays flat."""
    hub = _FakeHub()
    hub.install(monkeypatch)
    client = _client([])

    patcher, _ = _cheap_votes("no", "no")
    with patcher:
        tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True)

    committed = [name for commit in hub.commits for name in commit]
    assert committed
    for name in committed:
        prefix, filename = name.removeprefix("responses/").split("/")
        assert filename.endswith(".json") and filename.startswith(prefix)
        assert len(prefix) == 2
        # The blob itself is still flat on disk, where the reconciler looks for it.
        assert (tiers.RESPONSES_DIR / filename).is_file()


def test_a_failed_commit_never_says_uploaded(con, pool, monkeypatch):
    hub = _FakeHub(fail=True)
    hub.install(monkeypatch)
    calls: list = []
    client = _client(calls)

    patcher, _ = _cheap_votes("no", "no")
    with patcher:
        report = tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True)

    assert client.mark_uploaded.call_count == 0
    assert report["responses"] == {"uploaded": 0, PENDING_UPLOAD: 4}
    states = {c[1]["response_state"] for c in calls if c[0] == "record_verdict"}
    assert states == {PENDING_UPLOAD}


# ---------------------------------------------------------------------------
# The thread pool
# ---------------------------------------------------------------------------


def test_the_pool_loses_no_verdict_and_double_counts_none(con, pool, monkeypatch):
    """Every work is decided exactly once and the counters agree with the calls."""
    monkeypatch.setattr(tiers, "ENGINE_TIER_WORKERS", 8)
    calls: list = []
    client = _client(calls)
    works = _many_works(40)

    patcher, _ = _cheap_votes("no", "no")
    with patcher:
        report = tiers.run_tier(tiers.SCREEN_CHEAP, client, RELEASE, works,
                                mode="validation", batch_label="b", run=True)

    recorded = [c[1] for c in calls if c[0] == "record_verdict"]
    assert report["decided"] == 40
    assert report["outcomes"] == {"discard": 40}
    assert report["verdicts"] == len(recorded) == 80       # two voters per work
    assert sorted({c["work_id"] for c in recorded}) == [w.work_id for w in works]
    assert report["discarded_work_ids"] == sorted(w.work_id for w in works)
    assert report["by_pile"] == {"screen_cheap": 40}
    client.release_claim.assert_called_once_with("claim-1", "complete")


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


def test_an_exhausted_budget_fails_the_claim_and_keeps_what_was_decided(con, pool):
    """The budget is checked per work — under a pool too — and running out ends the
    run without taking the verdicts already written down with it."""
    calls: list = []
    client = _client(calls)
    lock = threading.Lock()
    allowed = 5
    seen = {"n": 0}

    def budget():
        with lock:
            seen["n"] += 1
            spent = seen["n"] > allowed
        if spent:
            raise TokenBudgetExhausted("spent")

    works = _many_works(40)
    patcher, _ = _cheap_votes("no", "no")
    with patcher, patch.object(tiers, "check_openai_budget", budget):
        with pytest.raises(TokenBudgetExhausted):
            tiers.run_tier(tiers.SCREEN_CHEAP, client, RELEASE, works,
                           mode="validation", batch_label="b", run=True)

    assert ("release_claim", ("claim-1", "failed")) in calls
    assert ("release_claim", ("claim-1", "complete")) not in calls
    # The works that passed the check were decided, and their verdicts stay; the
    # ones behind the exhaustion were never examined, so they were never spent on.
    recorded = [c for c in calls if c[0] == "record_verdict"]
    assert len(recorded) == 2 * allowed
    assert len({c[1]["work_id"] for c in recorded}) == allowed


# ---------------------------------------------------------------------------
# Modes, and what they do to the handoff
# ---------------------------------------------------------------------------


def _handoff(con, pool, tmp_path, client=None, name="filtered.csv",
             screened_only=False):
    drop, screen, decided = ((set(), {}, set()) if client is None
                             else handoff_mod.decisions(client))
    spec_dir = engine_bundle.write_bundle(tmp_path / "bundle")
    out = tmp_path / name
    manifest = handoff_mod.write_handoff(con, pool, out, RELEASE, drop=drop,
                                         screen=screen,
                                         decided=decided if screened_only else None,
                                         specs=engine_bundle.specs(),
                                         spec_dir=spec_dir, created_at="2026-08-04")
    return out, manifest


def _decided_client(ran_tier: str, mode: str, verdicts: list[dict],
                    release: str = RELEASE,
                    generation: object = _CURRENT) -> MagicMock:
    """A client that reports one finished run of *ran_tier* in *mode* under *release*.

    Asked about any other tier it answers with nothing, which is what a real
    deployment that has only run one of them looks like. `release_id` is filtered
    the way PostgREST filters it — omitted means every release — so a caller that
    scopes to the wrong release really does see nothing here.

    The claim carries the CURRENT screening generation unless a test says
    otherwise, so a fixture using placeholder model names still reads as a
    verdict today's code would stand behind; `None` makes it a legacy claim,
    judged on its models instead.
    """
    client = MagicMock()
    meta = {"mode": mode}
    if generation is _CURRENT:
        meta["generation"] = tiers.screening_generation(ran_tier)
    elif generation is not None:
        meta["generation"] = generation
    client.claims.side_effect = lambda release_id=None, tier=None, status=None: (
        [{"id": "claim-1", "release_id": release, "tier": ran_tier, "meta": meta}]
        if tier in (None, ran_tier) and release_id in (None, release) else [])
    client.verdicts.side_effect = lambda t, claim_ids=None: (
        [dict(v, claim_id="claim-1") for v in verdicts] if t == ran_tier else [])
    return client


def test_validation_mode_records_verdicts_but_the_handoff_is_unaffected(
        con, pool, tmp_path):
    calls: list = []
    client = _client(calls)
    patcher, _ = _cheap_votes("no", "no")
    with patcher:
        report = tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True,
                                        mode="validation")

    assert report["outcomes"] == {"discard": 2}
    assert len([c for c in calls if c[0] == "record_verdict"]) == 4
    # The re-validation comparison issue #146 §2 asks for.
    assert report["revalidation"] == {"discards": 2, "by_pile": {"screen_cheap": 2}}

    # A validation claim carries mode: validation, so decisions() sees nothing.
    validating = _decided_client("screen_cheap", "validation",
                                 [{"work_id": 21, "verdict": "no", "model": "m1"},
                                  {"work_id": 21, "verdict": "no", "model": "m2"}])
    out, manifest = _handoff(con, pool, tmp_path, validating)
    assert manifest["dropped_by_tier_verdict"] == 0
    assert manifest["rows"] == 4


def test_live_mode_drops_the_discarded_rows_from_the_handoff(con, pool, tmp_path):
    live = _decided_client("screen_cheap", "live",
                           [{"work_id": 21, "verdict": "no", "model": "m1"},
                            {"work_id": 21, "verdict": "no", "model": "m2"},
                            {"work_id": 22, "verdict": "no", "model": "m1"},
                            {"work_id": 22, "verdict": "yes", "model": "m2"}])
    out, manifest = _handoff(con, pool, tmp_path, live)

    assert manifest["dropped_by_tier_verdict"] == 1
    assert manifest["rows"] == 3
    ids = [r["openalex_id_r"] for r in _read(out)]
    assert "https://openalex.org/W21" not in ids
    assert "https://openalex.org/W22" in ids


def test_a_verdict_from_an_earlier_release_still_decides_the_handoff(
        con, pool, tmp_path):
    """A verdict follows the WORK: a re-route mints a new release id without
    unasking a voter, and the tier's checkpoint would skip these works anyway."""
    voter1, voter2 = _voter_models()
    earlier = _decided_client(
        "screen_expensive", "live",
        [{"work_id": 11, "verdict": "none", "model": voter1, "confidence": "confident"},
         {"work_id": 11, "verdict": "none", "model": voter2, "confidence": "confident"},
         {"work_id": 12, "verdict": "replication", "model": voter1,
          "confidence": "confident"},
         {"work_id": 12, "verdict": "replication", "model": voter2,
          "confidence": "confident"}],
        release="rel-before-the-reroute")
    # Scoped to this release the run is invisible — that is the bug being fixed.
    assert tiers.tier_decisions(earlier, RELEASE, "screen_expensive") == {}

    out, manifest = _handoff(con, pool, tmp_path, earlier, screened_only=True)

    assert manifest["dropped_by_tier_verdict"] == 1
    assert [r["openalex_id_r"] for r in _read(out)] == ["https://openalex.org/W12"]


def test_a_validation_claim_from_another_release_still_contributes_nothing(
        con, pool, tmp_path):
    """Reading across releases widens WHICH runs are seen, not which modes."""
    validating = _decided_client("screen_cheap", "validation",
                                 [{"work_id": 21, "verdict": "no", "model": "m1"},
                                  {"work_id": 21, "verdict": "no", "model": "m2"}],
                                 release="rel-before-the-reroute")
    assert handoff_mod.decisions(validating) == (set(), {}, set())

    out, manifest = _handoff(con, pool, tmp_path, validating)
    assert manifest["dropped_by_tier_verdict"] == 0
    assert manifest["rows"] == 4


def _cheap_ran_on_21() -> MagicMock:
    """One live cheap run that judged W21 and never reached W22 or the expensive pile."""
    return _decided_client("screen_cheap", "live",
                           [{"work_id": 21, "verdict": "no", "model": "m1"},
                            {"work_id": 21, "verdict": "yes", "model": "m2"}])


def _expensive_ran_on_11() -> MagicMock:
    """One live expensive run that judged W11 and reached nothing else."""
    voter1, voter2 = _voter_models()
    return _decided_client(
        "screen_expensive", "live",
        [{"work_id": 11, "verdict": "replication", "model": voter1,
          "confidence": "confident"},
         {"work_id": 11, "verdict": "replication", "model": voter2,
          "confidence": "confident"}])


def test_an_unscreened_row_does_not_travel_and_is_counted(con, pool, tmp_path):
    """The default: routing says "ask an LLM", and only the LLM's answer admits."""
    out, manifest = _handoff(con, pool, tmp_path, _expensive_ran_on_11(),
                             screened_only=True)

    assert manifest["screened_only"] is True
    assert manifest["rows"] == 1
    assert manifest["skipped_unscreened"] == 3
    assert manifest["dropped_by_tier_verdict"] == 0
    # rows + the two absences account for every work the piles hold.
    assert manifest["rows"] + manifest["skipped_unscreened"] \
        + manifest["dropped_by_tier_verdict"] == 4
    assert [r["openalex_id_r"] for r in _read(out)] == ["https://openalex.org/W11"]


def test_a_cheap_verdict_never_admits_a_row_to_stage_three(con, pool, tmp_path):
    """The hole this closes: the cheap tier's `proceed` means "on to the expensive
    screen", and a bypass means "we did not ask". Neither is a screen. Counting
    either as decided would hand Stage 3 — which does not screen any more — a row
    the validated pair has never seen."""
    passed = _decided_client("screen_cheap", "live",
                             [{"work_id": 21, "verdict": "no", "model": "m1"},
                              {"work_id": 21, "verdict": "yes", "model": "m2"},
                              {"work_id": 22, "verdict": tiers.PROCEED,
                               "model": "prescreen_bypass"}])
    drop, screen, decided = handoff_mod.decisions(passed)
    assert (drop, screen, decided) == (set(), {}, set())

    out, manifest = _handoff(con, pool, tmp_path, passed, screened_only=True)
    assert manifest["rows"] == 0
    assert manifest["skipped_unscreened"] == 4
    assert _read(out) == []


def test_as_routed_still_exports_the_unscreened_rows(con, pool, tmp_path):
    out, manifest = _handoff(con, pool, tmp_path, _expensive_ran_on_11())

    assert manifest["screened_only"] is False
    assert manifest["rows"] == 4
    assert manifest["skipped_unscreened"] == 0
    assert "https://openalex.org/W22" in [r["openalex_id_r"] for r in _read(out)]
    # A row no expensive run settled travels with its screen columns blank, which
    # is what Stage 3 reads as "nothing has screened this".
    unscreened = [r for r in _read(out)
                  if r["openalex_id_r"] == "https://openalex.org/W22"][0]
    assert unscreened["screen_verdict"] == ""


def test_a_single_vote_is_not_a_verdict(con, pool, tmp_path):
    """The expensive gate needs two votes; one leaves the work unsettled, and an
    unsettled work is unscreened, not a proceed."""
    half = _decided_client("screen_expensive", "live", [
        {"work_id": 11, "verdict": "replication", "model": _voter_models()[0],
         "confidence": "confident"},
    ])
    drop, screen, decided = handoff_mod.decisions(half)

    assert decided == set()
    assert drop == set()
    assert screen == {}


def test_screened_only_without_supabase_refuses(monkeypatch, tmp_path, capsys):
    """No claims client means no verdicts, so screened-only would write an empty
    file. The command says why instead."""
    from filter.engine import cli
    from filter.engine.claims import ClaimsNotConfigured

    monkeypatch.setattr("filter.engine.claims.ClaimsClient",
                        MagicMock(side_effect=ClaimsNotConfigured("SUPABASE_URL unset")))
    monkeypatch.setattr(cli, "open_store", MagicMock())
    monkeypatch.setattr(cli, "_resolve_release", lambda con, release: RELEASE)
    monkeypatch.setattr(cli, "read_release", lambda release_id, cache_dir=None: {})
    args = argparse.Namespace(store=tmp_path / "engine.duckdb", release=None,
                              as_routed=False, out=str(tmp_path / "filtered.csv"),
                              pool=tmp_path, spec_dir=tmp_path, overlay=None,
                              no_overlay=True,
                              from_year=None, to_year=None)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_handoff(args)
    assert "--as-routed" in str(exc.value)


@pytest.mark.parametrize("out", [None, "elsewhere.csv"],
                         ids=["default", "--out wins"])
def test_as_routed_writes_its_own_file_not_the_screened_one(monkeypatch, tmp_path, out):
    """The two modes write the same columns, so only the name says whether every row
    in the file was screened. An as-routed run must not take the screened name."""
    from filter.engine import cli
    from filter.engine.claims import ClaimsNotConfigured

    monkeypatch.setattr("filter.engine.claims.ClaimsClient",
                        MagicMock(side_effect=ClaimsNotConfigured("SUPABASE_URL unset")))
    monkeypatch.setattr(cli, "open_store", MagicMock())
    monkeypatch.setattr(cli, "_resolve_release", lambda con, release: RELEASE)
    monkeypatch.setattr(cli, "read_release", lambda release_id, cache_dir=None: {})
    written: list = []
    monkeypatch.setattr(
        "filter.engine.handoff.write_handoff",
        lambda con, pool, out_csv, release, **kw: written.append(out_csv) or {
            "rows": 0, "rows_per_pile": {}, "dropped_by_tier_verdict": 0,
            "typed_by_tier_verdict": 0, "skipped_unscreened": 0,
            "screened_only": False, "release_id": RELEASE, "sha256": "0" * 8})

    args = argparse.Namespace(store=tmp_path / "engine.duckdb", release=None,
                              as_routed=True, out=out, pool=tmp_path,
                              spec_dir=tmp_path, overlay=None, no_overlay=True,
                              from_year=None, to_year=None)
    assert cli.cmd_handoff(args) == 0
    assert written == [Path(out or handoff_mod.HANDOFF_UNSCREENED_CSV)]
    assert written[0] != Path(handoff_mod.HANDOFF_CSV)


# ---------------------------------------------------------------------------
# Stage 3's contract
# ---------------------------------------------------------------------------


def _read(path: Path) -> list[dict]:
    import csv
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_the_handoff_is_a_file_stage_three_accepts(con, pool, tmp_path):
    out, manifest = _handoff(con, pool, tmp_path)
    rows = _read(out)

    assert list(rows[0].keys()) == ENGINE_EXPORTED_COLS
    assert validate_csv_columns(list(rows[0].keys()), "filtered") == []
    # screen_expensive first: a --limit'ed Stage 3 run meets the strongest signal
    # before the murky residue.
    assert [r["openalex_id_r"] for r in rows[:2]] == [
        "https://openalex.org/W11", "https://openalex.org/W12"]
    assert manifest["rows_per_pile"] == {"screen_expensive": 2, "screen_cheap": 2}

    # The seam Stage 3 actually reads the file through.
    from extract.run_extract import _iter_filtered_rows
    read_back = list(_iter_filtered_rows(out))
    assert len(read_back) == 4
    assert all(row["abstract_r"] for row in read_back)


def test_the_screen_verdict_travels_to_stage_three_intact(con, pool, tmp_path,
                                                          monkeypatch):
    """The move: the screen runs here, so what it decided has to reach Stage 3 on
    the row. This is the round trip — verdict rows in, `SCREEN_COLS` written, and
    the dict Stage 3 rebuilds from them, which is what its ladder and its
    title-search gate read."""
    from shared import llm_client

    voter1, voter2 = _voter_models()
    live = _decided_client("screen_expensive", "live", [
        {"work_id": 11, "verdict": "replication", "model": voter1,
         "confidence": "confident", "quote": "we conducted a direct replication"},
        {"work_id": 11, "verdict": "both", "model": voter2,
         "confidence": "unconfident", "quote": "a partial re-analysis | study 2"},
    ])
    # The two descriptive fields the verdict table does not hold come from the
    # classify cache, which is content-complete and so provably this row's answer.
    monkeypatch.setattr(
        llm_client, "cached_classification",
        lambda doi, title, abstract: {"categories": ["clearly_declared", "other"],
                                      "llm_reasoning": "gemini: states its design"})
    out, _ = _handoff(con, pool, tmp_path, live, screened_only=True)

    row = _read(out)[0]
    assert row["screen_verdict"] == "proceed"
    assert row["screen_record_type"] == "replication"
    assert row["screen_categories"] == "clearly_declared|other"
    # Both voters' quotes, attributed and " || "-joined: the gate is the pair's
    # decision, and a quote may itself contain the "|" screen_votes joins on.
    assert row["screen_evidence"] == (
        f"{voter1}: we conducted a direct replication || "
        f"{voter2}: a partial re-analysis | study 2")
    assert row["screen_votes"] == (f"{voter1}=replication/confident|"
                                   f"{voter2}=both/unconfident")
    # The type reaches Stage 3's paper-type field too.
    assert (row["filter_status"], row["filter_method"]) == ("replication", "screen")

    import pandas as pd

    from extract.run_extract import _screen_from_row
    screen = _screen_from_row(pd.Series(row))
    assert screen["screen_verdict"] == "proceed"
    assert screen["record_type"] == "replication"
    assert screen["categories"] == ["clearly_declared", "other"]
    assert [(v["classification"], v["confident"]) for v in screen["votes"]] == [
        ("replication", True), ("both", False)]


def test_a_missing_classify_cache_costs_two_columns_and_no_money(con, pool, tmp_path,
                                                                 monkeypatch):
    """A checkout without the cache must not re-buy the screen at handoff time. The
    categories and the reasoning are simply blank; nothing downstream decides on
    either, and the verdict itself is unaffected."""
    from shared import llm_client

    voter1, voter2 = _voter_models()
    live = _decided_client("screen_expensive", "live", [
        {"work_id": 11, "verdict": "replication", "model": voter1,
         "confidence": "confident"},
        {"work_id": 11, "verdict": "replication", "model": voter2,
         "confidence": "confident"},
    ])
    called: list = []
    monkeypatch.setattr(llm_client, "call_model",
                        lambda *a, **k: called.append(a) or (None, "", "blocked"))
    monkeypatch.setattr(llm_client, "LLM_CACHE_DIR", tmp_path / "empty-cache")
    out, _ = _handoff(con, pool, tmp_path, live, screened_only=True)

    row = _read(out)[0]
    assert (row["screen_categories"], row["screen_reasoning"]) == ("", "")
    assert row["screen_verdict"] == "proceed"
    assert called == []


def test_a_failed_handoff_write_leaves_the_previous_file_intact(tmp_path):
    """The handoff is rewritten over the file Stage 3 reads, so it goes through a
    sibling temp file: a run that dies mid-write must not destroy a working input."""
    class Unwritable:
        def __str__(self) -> str:
            raise RuntimeError("disk full")

    out = tmp_path / "filtered.csv"
    handoff_mod._publish(
        out, handoff_mod._write_csv_tmp(
            out, [{col: "old" for col in ENGINE_EXPORTED_COLS}]), {"rows": 1})
    before = out.read_bytes()

    with pytest.raises(RuntimeError, match="disk full"):
        handoff_mod._write_csv_tmp(out, [{**{c: "" for c in ENGINE_EXPORTED_COLS},
                                          "doi_r": Unwritable()}])
    assert out.read_bytes() == before
    assert not (tmp_path / "filtered.csv.tmp").exists()


def test_a_crash_between_the_two_renames_never_leaves_an_unbound_csv(
        con, pool, tmp_path, monkeypatch):
    """#21: the CSV and its manifest are two files and no filesystem makes the pair
    atomic, so the ordering decides which half-state exists. Stage 3 reads
    `filtered.csv` by name without checking the manifest, so the state that must
    not exist is a NEW csv nothing describes. The manifest goes first: a crash
    before the CSV rename leaves the PREVIOUS handoff — a valid input — under a
    manifest whose sha256 visibly disagrees with it."""
    out, first = _handoff(con, pool, tmp_path)
    manifest_path = Path(str(out) + ".manifest.json")
    assert json.loads(manifest_path.read_text())["sha256"] == first["sha256"]
    previous_csv = out.read_bytes()

    seen: list = []
    real_replace = handoff_mod.os.replace

    def crash_between(src, dst):
        seen.append(dst)
        real_replace(src, dst)
        if len(seen) == 1:
            raise RuntimeError("killed between the two renames")

    monkeypatch.setattr(handoff_mod.os, "replace", crash_between)
    with pytest.raises(RuntimeError, match="killed between"):
        _handoff(con, pool, tmp_path, _expensive_ran_on_11(), screened_only=True)

    # The manifest moved first; the CSV on disk is still the previous handoff, and
    # the mismatch is visible to anyone who checks.
    assert out.read_bytes() == previous_csv
    published = json.loads(manifest_path.read_text())
    assert published["sha256"] != first["sha256"]
    assert published["sha256"] != hashlib.sha256(out.read_bytes()).hexdigest()


def test_a_live_expensive_verdict_types_the_row_stage_three_reads(con, pool, tmp_path):
    """`filter_status` is Stage 3's paper-type field, and the front door decides it.
    Running that door here means Stage 3 reads the answer instead of guessing."""
    live = _decided_client("screen_expensive", "live", [
        {"work_id": 11, "verdict": "reproduction", "model": _voter_models()[0],
         "confidence": "confident"},
        {"work_id": 11, "verdict": "reproduction", "model": _voter_models()[1],
         "confidence": "confident"},
        {"work_id": 12, "verdict": "none", "model": _voter_models()[0],
         "confidence": "confident"},
        {"work_id": 12, "verdict": "none", "model": _voter_models()[1],
         "confidence": "confident"},
    ])
    out, manifest = _handoff(con, pool, tmp_path, live)
    rows = {r["openalex_id_r"]: r for r in _read(out)}

    assert rows["https://openalex.org/W11"]["filter_status"] == "reproduction"
    assert rows["https://openalex.org/W11"]["filter_method"] == "screen"
    # Two confident "none" votes are the gate's discard; the row does not travel.
    assert "https://openalex.org/W12" not in rows
    assert manifest["dropped_by_tier_verdict"] == 1


def _voter_models() -> list[str]:
    from shared.llm_client import screen_voters
    return [m for _, m, _, _ in screen_voters()]


def test_export_pile_still_writes_one_pile(con, pool, tmp_path):
    """The row iterator now serves two callers; the single-pile export is unchanged."""
    manifest = export_pile(con, pool, "screen_cheap", tmp_path / "cheap.csv", RELEASE,
                           specs=engine_bundle.specs(),
                           spec_dir=engine_bundle.write_bundle(tmp_path / "bundle"),
                           created_at="2026-08-04")
    rows = _read(tmp_path / "cheap.csv")
    assert manifest["rows"] == len(rows) == 2
    assert {r["openalex_id_r"] for r in rows} == {
        "https://openalex.org/W21", "https://openalex.org/W22"}


# ---------------------------------------------------------------------------
# Replaying the gate from the stored rows
# ---------------------------------------------------------------------------


def test_the_gate_replayed_from_stored_rows_matches_the_live_votes():
    """`tier_decisions()` recomputes the gate so a stored outcome cannot disagree
    with the votes — which only holds if the rows carry everything the gate reads.
    The soft-discard branch reads `confidence`, so a replay that dropped it would
    proceed where the live run discarded, silently and only on this shape."""
    from shared.llm_client import screen_gate

    voter1, voter2 = _voter_models()
    # One confident "none" against an unconfident qualifying answer: the branch
    # that exists ONLY because a voter declined to stand behind its answer.
    live_votes = [{"classification": "none", "confident": True, "categories": []},
                  {"classification": "replication", "confident": False,
                   "categories": []}]
    assert screen_gate(live_votes) == "discard"

    stored = [{"work_id": 11, "model": voter1, "verdict": "none",
               "confidence": "confident"},
              {"work_id": 11, "model": voter2, "verdict": "replication",
               "confidence": "unconfident"}]
    client = _decided_client("screen_expensive", "live", stored)
    replayed = tiers.tier_decisions(client, RELEASE, "screen_expensive")[11]
    assert (replayed["outcome"], replayed["record_type"]) == ("discard", "replication")

    # The same votes, both stood behind, are a real split and proceed. Without the
    # confidence column both cases would read as unconfident and both would
    # discard, so this is the pair that pins the column down.
    stood_behind = [dict(row, confidence="confident") for row in stored]
    assert tiers._expensive_decision(stood_behind)["outcome"] == "proceed"
    assert screen_gate([dict(v, confident=True) for v in live_votes]) == "proceed"


def test_the_stored_verdict_read_carries_the_confidence_column():
    """The replay is only as good as the SELECT feeding it."""
    from filter.engine.claims import ClaimsClient

    client = ClaimsClient(url="https://example.supabase.co", key="k")
    with patch.object(ClaimsClient, "_get_paged", return_value=[]) as paged:
        client.verdicts("screen_expensive")
    assert "confidence" in paged.call_args[0][1]["select"].split(",")


# ---------------------------------------------------------------------------
# The screening generation
# ---------------------------------------------------------------------------


def test_the_generation_moves_with_a_voter_model_and_with_the_prompt(monkeypatch):
    """The two things a verdict depends on, and nothing else."""
    before = tiers.screening_generation("screen_expensive")
    assert tiers.screening_generation("screen_expensive") == before
    assert tiers.screening_generation("screen_cheap") != before

    monkeypatch.setattr(tiers, "SCREENING_MODEL_2", "some-other-model")
    assert tiers.screening_generation("screen_expensive") != before

    monkeypatch.undo()
    # `prompt_version` itself is the seam: it is lru_cached over the prompt text,
    # so an edited prompt reaches this hash only through the version it returns.
    monkeypatch.setattr("shared.prompts.prompt_version",
                        lambda name: "edited" if name == "build_classify_prompt"
                        else "unchanged")
    assert tiers.screening_generation("screen_expensive") != before


def _expensive_votes(work: int = 11) -> list[dict]:
    voter1, voter2 = _voter_models()
    return [{"work_id": work, "verdict": "none", "model": voter1,
             "confidence": "confident"},
            {"work_id": work, "verdict": "none", "model": voter2,
             "confidence": "confident"}]


def test_a_verdict_from_another_generation_neither_settles_nor_blocks(
        con, pool, tmp_path):
    """A model or prompt change unasks the question the old answer answered: the
    work is screenable again and stops steering the handoff."""
    stale = _decided_client("screen_expensive", "live", _expensive_votes(),
                            generation="an-older-code-state")

    assert tiers.tier_decisions(stale, None, "screen_expensive") == {}
    assert tiers.decided_work_ids(stale, "screen_expensive") == set()
    assert handoff_mod.decisions(stale) == (set(), {}, set())

    # The same rows under the current generation do all three.
    current = _decided_client("screen_expensive", "live", _expensive_votes())
    assert tiers.decided_work_ids(current, "screen_expensive") == {11}
    drop, _, decided = handoff_mod.decisions(current)
    assert drop == {11} and decided == {11}


def test_a_legacy_verdict_counts_when_its_models_are_todays(con, pool, tmp_path):
    """Rows written before the field exists: the prompt is unknowable, the model
    pair is recorded and is the dominant determinant, so it is grandfathered."""
    legacy = _decided_client("screen_expensive", "live", _expensive_votes(),
                             generation=None)
    assert tiers.decided_work_ids(legacy, "screen_expensive") == {11}
    assert handoff_mod.decisions(legacy)[0] == {11}

    # A legacy row from models nobody screens with any more is not grandfathered.
    other = _decided_client("screen_expensive", "live",
                            [{"work_id": 11, "verdict": "none", "model": "old-model-1",
                              "confidence": "confident"},
                             {"work_id": 11, "verdict": "none", "model": "old-model-2",
                              "confidence": "confident"}],
                            generation=None)
    assert tiers.decided_work_ids(other, "screen_expensive") == set()
    assert handoff_mod.decisions(other) == (set(), {}, set())


# ---------------------------------------------------------------------------
# An incomplete screen is a failure, not a decision (#3)
# ---------------------------------------------------------------------------


def test_an_incomplete_screen_is_not_decided_and_is_asked_again(con, pool):
    """The strand this closes. One voter 429s, so the work has verdict rows but no
    gate decision. It must not count as decided — a work skipped by the tier and
    held back by the handoff is retired by a five-minute outage, and a transient
    failure is never a definitive miss. The next ordinary run re-claims it, with no
    flag for an operator to remember."""
    voter1, voter2 = _voter_models()
    half = _decided_client("screen_expensive", "live", [
        {"work_id": 11, "verdict": "replication", "model": voter1,
         "confidence": "confident"},
        {"work_id": 11, "verdict": "no_answer", "model": voter2},
        # W12 was screened whole in the same run and stays decided.
        {"work_id": 12, "verdict": "replication", "model": voter1,
         "confidence": "confident"},
        {"work_id": 12, "verdict": "replication", "model": voter2,
         "confidence": "confident"},
    ])
    assert tiers.decided_work_ids(half, "screen_expensive") == {12}
    assert tiers.incomplete_work_ids(half, "screen_expensive") == {11}

    # And the batch the next run sends contains it again.
    half.claimed_work_ids.return_value = set()
    works = tiers._batch(con, half, RELEASE, "screen_expensive", None, None,
                         pool, None, None, run=True)
    assert {w.work_id for w in works} == {11}


def test_a_cheap_tier_whose_voters_failed_decided_nothing(con, pool):
    """Same rule on the discard-only tier: a `no_answer` is the absence of an
    answer, so neither a pair of them nor a `no` beside one is a decision — the
    missing answer is precisely the second `no` that would have discarded. Only a
    voter who declined to say no settles the work, because after a keep voter 2 is
    never asked."""
    failed = _decided_client("screen_cheap", "live", [
        {"work_id": 21, "verdict": "no_answer", "model": "m1"},
        {"work_id": 21, "verdict": "no_answer", "model": "m2"},
        {"work_id": 22, "verdict": "no", "model": "m1"},
        {"work_id": 22, "verdict": "no_answer", "model": "m2"},
    ])
    assert tiers.decided_work_ids(failed, "screen_cheap") == set()
    assert tiers.incomplete_work_ids(failed, "screen_cheap") == {21, 22}

    kept = _decided_client("screen_cheap", "live",
                           [{"work_id": 21, "verdict": "yes", "model": "m1"}])
    assert tiers.decided_work_ids(kept, "screen_cheap") == {21}


def test_one_voter_answering_twice_is_never_a_second_vote(con, pool):
    """The retry hole: pooling rows across claims is what makes an incomplete
    screen claimable again, but a re-run re-asks BOTH voters and the one that
    already answered answers again from cache. Two rows from one voter must not
    add up to the two the gate needs, or the work settles on a decision no second
    voter ever contributed to — and stops being claimable."""
    voter1, voter2 = _voter_models()
    retried = _decided_client("screen_expensive", "live", [
        # Attempt one: voter 1 answered, voter 2 failed.
        {"work_id": 11, "verdict": "replication", "model": voter1,
         "confidence": "confident", "created_at": "2026-08-01T10:00:00Z"},
        {"work_id": 11, "verdict": "no_answer", "model": voter2,
         "created_at": "2026-08-01T10:00:01Z"},
        # Attempt two, a new claim: voter 1 answers again, voter 2 fails again.
        {"work_id": 11, "verdict": "replication", "model": voter1,
         "confidence": "confident", "created_at": "2026-08-02T10:00:00Z"},
        {"work_id": 11, "verdict": "no_answer", "model": voter2,
         "created_at": "2026-08-02T10:00:01Z"},
    ])
    assert tiers.decided_work_ids(retried, "screen_expensive") == set()
    assert tiers.incomplete_work_ids(retried, "screen_expensive") == {11}
    assert handoff_mod.decisions(retried) == (set(), {}, set())

    # The later answer is the one kept, and it stays in voter 1's call-order slot,
    # so the record type still breaks a split on the FIRST qualifying voter.
    rebuilt = tiers._votes_from_rows([
        {"work_id": 11, "verdict": "reproduction", "model": voter1,
         "confidence": "confident", "created_at": "2026-08-01T10:00:00Z"},
        {"work_id": 11, "verdict": "replication", "model": voter1,
         "confidence": "confident", "created_at": "2026-08-02T10:00:00Z"},
        {"work_id": 11, "verdict": "reproduction", "model": voter2,
         "confidence": "confident", "created_at": "2026-08-02T10:00:01Z"},
    ])
    assert [(v["model"], v["classification"]) for v in rebuilt] == [
        (voter1, "replication"), (voter2, "reproduction")]
    assert tiers._expensive_decision([])["outcome"] == tiers.INCOMPLETE


@pytest.mark.parametrize("said,expected", [
    (["yes"], tiers.PROCEED),                       # voter 2 is never asked
    (["no", "yes"], tiers.PROCEED),                 # both answered
    (["no", "no"], tiers.DISCARD),                  # the two explicit noes
    (["no", "no_answer"], tiers.INCOMPLETE),        # the answer that mattered failed
    (["no"], tiers.INCOMPLETE),                     # the second call never happened
    (["no_answer"], tiers.INCOMPLETE),
    (["no_answer", "no_answer"], tiers.INCOMPLETE),
])
def test_the_cheap_tier_only_decides_when_the_missing_answer_could_not_matter(
        said, expected):
    """The tier asks voter 2 only after a `no`, so `no` + a failure is exactly the
    case where the second `no` that would have discarded never arrived. Filing it
    as a proceed checkpoints a swallowed failure as this tier's decision."""
    models = ["m1", "m2"]
    rows = [{"work_id": 21, "verdict": verdict, "model": models[i]}
            for i, verdict in enumerate(said)]
    assert tiers._cheap_decision(rows)["outcome"] == expected


def test_a_retried_cheap_no_is_one_voter_not_two(con, pool):
    """The same retry hole on the discard-only tier, where it would DISCARD a row
    on one voter's answer counted twice."""
    twice = [{"work_id": 21, "verdict": "no", "model": "m1",
              "claim_id": "claim-1", "created_at": "2026-08-01T10:00:00Z"},
             {"work_id": 21, "verdict": "no", "model": "m1",
              "claim_id": "claim-2", "created_at": "2026-08-02T10:00:00Z"}]
    assert tiers._cheap_decision(twice)["outcome"] == tiers.INCOMPLETE
    assert tiers._cheap_decision(
        twice + [{"work_id": 21, "verdict": "no", "model": "m2",
                  "claim_id": "claim-2", "created_at": "2026-08-02T10:00:01Z"}]
    )["outcome"] == tiers.DISCARD


def test_unattributed_legacy_votes_count_within_one_claim_and_never_across(con, pool):
    """A legacy row's blank model says nothing about WHICH voter it is, so it can
    be neither deduplicated nor pooled: two such rows in one claim are the two
    voters of one screening, two in different claims are indistinguishable from
    one voter retried."""
    one_screening = [
        {"work_id": 11, "verdict": "replication", "model": "", "claim_id": "c1",
         "confidence": "confident", "created_at": "2026-08-01T10:00:00Z"},
        {"work_id": 11, "verdict": "replication", "model": "", "claim_id": "c1",
         "confidence": "confident", "created_at": "2026-08-01T10:00:01Z"},
    ]
    assert tiers._expensive_decision(one_screening)["outcome"] == "proceed"
    assert [v["model"] for v in tiers._votes_from_rows(one_screening)] \
        == [tiers.UNKNOWN_MODEL, tiers.UNKNOWN_MODEL]

    two_attempts = [
        dict(one_screening[0], claim_id="c1"),
        dict(one_screening[1], claim_id="c2", created_at="2026-08-02T10:00:00Z"),
    ]
    assert tiers._expensive_decision(two_attempts)["outcome"] == tiers.INCOMPLETE


def test_an_incomplete_screen_is_counted_where_an_operator_sees_it(capsys):
    """A strand nobody can see is a strand nobody fixes, so `screen` prints it."""
    from filter.engine import cli

    client = _decided_client("screen_expensive", "live", [
        {"work_id": 11, "verdict": "replication", "model": _voter_models()[0],
         "confidence": "confident"},
    ])
    cli._print_incomplete(client, "screen_expensive", indent="  ")
    assert "incomplete screens 1" in capsys.readouterr().out


def test_a_failed_expensive_call_records_an_attributable_placeholder(con, pool):
    """#20: `llm_model` can come back blank, and a blank model is read as a value
    downstream — sorted out of call order, printed as an empty vote, attributed to
    a provider by `provider_for('')`. It is recorded as UNKNOWN_MODEL instead."""
    work = tiers.Work(work_id=11, doi="10.1/x", title="T", abstract="A",
                      pile="screen_expensive")
    with patch("filter.engine.tiers.classify_replication",
               return_value={"screen_verdict": "", "votes": [], "llm_model": "",
                             "llm_error": "429"}):
        outcome, votes = tiers._expensive_judge(work)

    assert outcome == tiers.INCOMPLETE
    assert [v["model"] for v in votes] == [tiers.UNKNOWN_MODEL]

    # And a legacy row that DID store a blank model still votes, unattributed:
    # its classification and confidence are evidence, only the name is missing.
    rebuilt = tiers._votes_from_rows(
        [{"work_id": 11, "verdict": "replication", "model": "",
          "confidence": "confident"}])
    assert [(v["model"], v["classification"]) for v in rebuilt] == [
        (tiers.UNKNOWN_MODEL, "replication")]


def test_a_cheap_discard_never_overrules_an_expensive_proceed(con, pool, tmp_path):
    """#7: `write_handoff` checks the drop set first, so a live cheap discard of a
    work the validated pair PASSED would drop it — the weaker model overruling the
    stronger one, which the design forbids in exactly this direction."""
    voter1, voter2 = _voter_models()
    both = MagicMock()
    both.claims.side_effect = lambda release_id=None, tier=None, status=None: [
        {"id": f"claim-{t}", "release_id": RELEASE, "tier": t,
         "meta": {"mode": "live", "generation": tiers.screening_generation(t)}}
        for t in ("screen_cheap", "screen_expensive") if tier in (None, t)]
    verdicts = {
        "screen_expensive": [
            {"claim_id": "claim-screen_expensive", "work_id": 11,
             "verdict": "replication", "model": voter1, "confidence": "confident"},
            {"claim_id": "claim-screen_expensive", "work_id": 11,
             "verdict": "replication", "model": voter2, "confidence": "confident"}],
        # The same work, discarded by the cheap tier — which may only send a row ON
        # to the expensive screen, never past its answer.
        "screen_cheap": [
            {"claim_id": "claim-screen_cheap", "work_id": 11, "verdict": "no",
             "model": "m1"},
            {"claim_id": "claim-screen_cheap", "work_id": 11, "verdict": "no",
             "model": "m2"},
            {"claim_id": "claim-screen_cheap", "work_id": 21, "verdict": "no",
             "model": "m1"},
            {"claim_id": "claim-screen_cheap", "work_id": 21, "verdict": "no",
             "model": "m2"}],
    }
    both.verdicts.side_effect = lambda t, claim_ids=None: verdicts.get(t, [])

    drop, screen, decided = handoff_mod.decisions(both)
    # W21 has no expensive verdict, so the cheap discard still applies to it.
    assert drop == {21}
    assert decided == {11}

    out, manifest = _handoff(con, pool, tmp_path, both, screened_only=True)
    assert [r["openalex_id_r"] for r in _read(out)] == ["https://openalex.org/W11"]


# ---------------------------------------------------------------------------
# Reconciling blobs an earlier run left pending
# ---------------------------------------------------------------------------


def _pending_client(rows: list[dict]) -> MagicMock:
    client = MagicMock()
    client.pending_responses.return_value = rows
    return client


def _blob(tmp_path: Path, name: str) -> str:
    (tmp_path / f"{name}.json").write_text("{}", encoding="utf-8")
    return name


def test_reconcile_uploads_the_pending_blobs_and_marks_only_those(tmp_path):
    on_disk = [_blob(tmp_path, "a" * 64), _blob(tmp_path, "b" * 64)]
    client = _pending_client([
        {"id": "v1", "work_id": 11, "tier": "screen_expensive",
         "response_hash": on_disk[0], "response_state": PENDING_UPLOAD},
        # Two rows may name one blob; it is committed once.
        {"id": "v2", "work_id": 12, "tier": "screen_expensive",
         "response_hash": on_disk[0], "response_state": PENDING_UPLOAD},
        {"id": "v3", "work_id": 13, "tier": "screen_cheap",
         "response_hash": on_disk[1], "response_state": PENDING_UPLOAD},
    ])
    commits: list = []
    with patch.object(tiers, "RESPONSES_DIR", tmp_path), \
         patch.object(tiers, "_hf_upload_target", return_value="org/repo"), \
         patch("huggingface_hub.HfApi") as api, \
         patch("huggingface_hub.CommitOperationAdd", lambda **kw: kw):
        api.return_value.create_commit.side_effect = lambda **kw: commits.append(kw)
        report = tiers.reconcile_responses(client, run=True)

    assert report["pending_rows"] == 3
    assert (report["pending_blobs"], report["on_disk"], report["missing"]) == (2, 2, 0)
    assert report["committed"] == report["marked"] == 2
    assert len(commits) == 1
    assert sorted(client.mark_uploaded.call_args[0][0]) == sorted(on_disk)


def test_reconcile_reports_a_pending_row_whose_blob_is_gone(tmp_path):
    """The disk cache is expendable; the row is honest. Counted, not fatal."""
    present = _blob(tmp_path, "c" * 64)
    client = _pending_client([
        {"id": "v1", "work_id": 11, "tier": "screen_expensive",
         "response_hash": present, "response_state": PENDING_UPLOAD},
        {"id": "v2", "work_id": 12, "tier": "screen_expensive",
         "response_hash": "d" * 64, "response_state": PENDING_UPLOAD},
    ])
    with patch.object(tiers, "RESPONSES_DIR", tmp_path), \
         patch.object(tiers, "_hf_upload_target", return_value="org/repo"), \
         patch("huggingface_hub.HfApi"), \
         patch("huggingface_hub.CommitOperationAdd", lambda **kw: kw):
        report = tiers.reconcile_responses(client, run=True)

    assert (report["on_disk"], report["missing"]) == (1, 1)
    assert report["missing_hashes"] == ["d" * 64]
    assert client.mark_uploaded.call_args[0][0] == [present]
    assert "MISSING" in tiers.render_reconcile(report)


def test_reconcile_dry_run_commits_nothing_and_marks_nothing(tmp_path):
    client = _pending_client([
        {"id": "v1", "work_id": 11, "tier": "screen_expensive",
         "response_hash": _blob(tmp_path, "e" * 64),
         "response_state": PENDING_UPLOAD}])
    with patch.object(tiers, "RESPONSES_DIR", tmp_path), \
         patch("huggingface_hub.HfApi") as api:
        report = tiers.reconcile_responses(client, run=False)

    assert report["dry_run"] and report["on_disk"] == 1
    assert report["committed"] == report["marked"] == 0
    api.assert_not_called()
    client.mark_uploaded.assert_not_called()
    assert "DRY RUN" in tiers.render_reconcile(report)


# ---------------------------------------------------------------------------
# The tier registry
# ---------------------------------------------------------------------------


@pytest.fixture
def foreign_tier(monkeypatch):
    """A tier defined the way a tier outside `filter/engine` would define one.

    The seam the registry exists for: `run_tier()` and the checkpoint readers must
    work off nothing but the spec — no screen models, no classify prompt, no pile
    in the routing store, and no import of the package the tier lives in. Its own
    prices, its own generation, its own decision rule, its own worker count and
    lease.
    """
    monkeypatch.setitem(tiers._TIER_SPECS, "outside", tiers.TierSpec(
        name="outside",
        judge=lambda work: ("done", [{"model": "m-outside", "verdict": "done",
                                      "blob": {"work_id": work.work_id}}]),
        decide=lambda rows: {"outcome": "done" if rows else tiers.INCOMPLETE,
                             "record_type": "", "votes": []},
        generation=lambda: "gen-outside",
        accepts_legacy=lambda rows: False,
        estimate=lambda works: {"tier": "outside", "rows": len(works)},
        render_estimate=lambda est: f"outside: {est['rows']} row(s)",
        workers=2, ttl_seconds=90, batch_size=1))
    return tiers._TIER_SPECS["outside"]


def test_a_tier_from_outside_the_engine_runs_on_the_shared_spine(foreign_tier):
    calls: list = []
    client = _client(calls)
    works = [tiers.Work(90 + i, f"10.9/w{i}", f"T{i}", "text", "outside")
             for i in range(3)]

    report = tiers.run_tier(foreign_tier, client, RELEASE, works, mode="live",
                            batch_label="b", run=True)

    assert report["tier"] == "outside" and report["decided"] == 3
    assert report["outcomes"] == {"done": 3}
    assert report["workers"] == 2                      # the spec's, not the default
    _, args, kwargs = calls[0]
    assert args[1] == "outside"
    assert kwargs["meta"]["generation"] == "gen-outside"
    assert kwargs["ttl_seconds"] == 90


def test_a_foreign_tier_dry_run_prices_itself(foreign_tier, capsys):
    """No shared price table to be missing from: the spec brings its own."""
    report = tiers.run_tier(foreign_tier, MagicMock(), RELEASE, [], mode="live",
                            batch_label="b", run=False)
    assert report["dry_run"] and capsys.readouterr().out == "outside: 0 row(s)\n"


def test_the_checkpoint_readers_use_the_spec_of_the_tier_they_are_asked_about(
        foreign_tier):
    client = MagicMock()
    client.claims.return_value = [{"id": "c1", "tier": "outside",
                                   "meta": {"generation": "gen-outside"}}]
    client.verdicts.return_value = [{"claim_id": "c1", "work_id": 91,
                                     "verdict": "done", "model": "m-outside"}]
    assert tiers.decided_work_ids(client, "outside") == {91}
    assert tiers.incomplete_work_ids(client, "outside") == set()


def test_an_unregistered_tier_is_named_rather_than_judged_by_the_expensive_rule():
    """The old dispatch was `_cheap_decision if tier == cheap else _expensive`, so
    an unknown name silently got the expensive tier's rule applied to its rows."""
    with pytest.raises(ValueError, match="unknown tier: nope"):
        tiers.tier_spec("nope")
