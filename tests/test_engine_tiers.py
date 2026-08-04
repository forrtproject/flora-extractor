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

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from filter.engine import handoff as handoff_mod
from filter.engine import tiers
from filter.engine.claims import PENDING_UPLOAD, UPLOADED, ClaimConflict
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
    monkeypatch.setattr(tiers, "_push_response", lambda path: False)


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
    """Patch `shared.prescreen._vote` so voter *i* always answers `answers[i]`.

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

    return patch("shared.prescreen._vote", side_effect=vote), seen


# ---------------------------------------------------------------------------
# Reading the pile
# ---------------------------------------------------------------------------


def test_the_pile_reader_attaches_the_pool_text(con, pool):
    works = tiers.pile_works(con, RELEASE, "screen_cheap", pool)
    assert {w.work_id for w in works} == {21, 22}
    assert all(w.abstract for w in works)
    assert all(w.title for w in works)


# ---------------------------------------------------------------------------
# The dry run (issue #146 §6)
# ---------------------------------------------------------------------------


def test_dry_run_spends_nothing_and_prints_an_estimate(con, pool, capsys):
    """The default. Nothing claimed, no voter asked, and a number to decide on."""
    calls: list = []
    client = _client(calls)
    with patch("shared.prescreen._vote") as vote:
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
                              "engine_tier": "screen_cheap"}
    assert calls[-1] == ("release_claim", ("claim-1", "complete"))


def test_a_claim_conflict_refuses_without_spending(con, pool):
    calls: list = []
    client = _client(calls)
    client.claim.side_effect = ClaimConflict("screen_cheap", "3 works already held")
    with patch("shared.prescreen._vote") as vote:
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
    with patch("shared.prescreen._vote") as vote:
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
        response_hash, state = real_write(blob)
        written.append(("write_response", response_hash,
                        (tmp_path / "responses" / f"{response_hash}.json").exists()))
        calls.append(("write_response", response_hash))
        return response_hash, state

    patcher, _ = _cheap_votes("yes")
    with patcher, patch.object(tiers, "_write_response", spy):
        tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True)

    order = [c[0] for c in calls]
    assert order.index("write_response") < order.index("record_verdict")
    assert all(exists for _, _, exists in written), "verdict named a blob not on disk"
    # No HF push was possible, and the state says so rather than claiming otherwise.
    states = [c[1]["response_state"] for c in calls if c[0] == "record_verdict"]
    assert states and set(states) == {PENDING_UPLOAD}


def test_an_uploaded_blob_is_recorded_as_uploaded(con, pool, monkeypatch):
    monkeypatch.setattr(tiers, "_push_response", lambda path: True)
    calls: list = []
    patcher, _ = _cheap_votes("yes")
    with patcher:
        tiers.run_screen_cheap(con, _client(calls), RELEASE, pool_dir=pool, run=True)

    states = [c[1]["response_state"] for c in calls if c[0] == "record_verdict"]
    assert set(states) == {UPLOADED}
    assert all(c[1]["response_hash"] for c in calls if c[0] == "record_verdict")


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


def test_an_exhausted_budget_fails_the_claim_and_keeps_what_was_decided(con, pool):
    calls: list = []
    client = _client(calls)
    seen = {"n": 0}

    def budget():
        seen["n"] += 1
        if seen["n"] > 1:
            raise TokenBudgetExhausted("spent")

    patcher, _ = _cheap_votes("yes")
    with patcher, patch.object(tiers, "check_openai_budget", budget):
        with pytest.raises(TokenBudgetExhausted):
            tiers.run_screen_cheap(con, client, RELEASE, pool_dir=pool, run=True)

    assert ("release_claim", ("claim-1", "failed")) in calls
    assert ("release_claim", ("claim-1", "complete")) not in calls
    # The first work was decided before the budget ran out; its verdicts stay.
    assert [c for c in calls if c[0] == "record_verdict"]


# ---------------------------------------------------------------------------
# Modes, and what they do to the handoff
# ---------------------------------------------------------------------------


def _handoff(con, pool, tmp_path, client=None, name="filtered.csv"):
    drop, record_types = ((set(), {}) if client is None
                          else handoff_mod.decisions(client, RELEASE))
    spec_dir = engine_bundle.write_bundle(tmp_path / "bundle")
    out = tmp_path / name
    manifest = handoff_mod.write_handoff(con, pool, out, RELEASE, drop=drop,
                                         record_types=record_types,
                                         specs=engine_bundle.specs(),
                                         spec_dir=spec_dir, created_at="2026-08-04")
    return out, manifest


def _decided_client(ran_tier: str, mode: str, verdicts: list[dict]) -> MagicMock:
    """A client that reports one finished run of *ran_tier* in *mode*.

    Asked about any other tier it answers with nothing, which is what a real
    deployment that has only run one of them looks like.
    """
    client = MagicMock()
    client.claims.side_effect = lambda release_id=None, tier=None, status=None: (
        [{"id": "claim-1", "tier": ran_tier, "meta": {"mode": mode}}]
        if tier in (None, ran_tier) else [])
    client.verdicts.side_effect = lambda t, claim_ids=None: (
        verdicts if t == ran_tier else [])
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
    return [m for _, m, _ in screen_voters()]


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
