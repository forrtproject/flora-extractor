"""The extract tier: one test per seam, and every seam is about money or evidence.

That the judge turns each terminal state into the right verdict and a payload that
rebuilds its rows; that `target_pending` and `api_error` do NOT settle a work, so a
provider outage cannot become a permanent hole in the corpus; that the worklist
subtracts what it must; that a lost claim lease stops the run; and that the
generation is pinned by its INPUTS, so a bump is a deliberate diff.

Every network call — LLM, OpenAlex, Supabase — is mocked. Nothing here talks to
anything.
"""

import threading
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from extract import tier as tier_mod
from extract.tier import (API_ERROR, NOT_A_REPLICATION, NO_ORIGINAL_FOUND,
                          PROVISIONAL, RESOLVED, TARGET_PENDING, ExtractWork,
                          extract_generation, generation_inputs)
from filter.engine.claims import ClaimLeaseLost
from shared.schema import EXTRACTED_COLS, FILTERED_COLS, SCREEN_COLS

_INPUT = {
    "doi_r": "10.1000/repl", "title_r": "A replication of something",
    "abstract_r": "We re-test the anchoring effect in a new sample.",
    "year_r": "2024", "authors_r": "A. Author", "journal_r": "J. Repl",
    "url_r": "", "openalex_id_r": "https://openalex.org/W99", "source": "openalex",
    "ref_r": "Author · 2024 · J. Repl",
    "filter_status": "replication", "filter_method": "engine:abc",
    "filter_evidence": "rule:x", "filter_confidence": "high",
    "screen_verdict": "proceed", "screen_record_type": "replication",
    "screen_categories": "direct_replication", "screen_votes": "m1=replication/confident",
    "screen_evidence": "m1: we replicate", "screen_reasoning": "m1: clear",
}


def _work(work_id: int = 99, **overrides) -> ExtractWork:
    row = {**{c: "" for c in list(FILTERED_COLS) + list(SCREEN_COLS)}, **_INPUT,
           **overrides}
    return ExtractWork(work_id=work_id, doi=row["doi_r"], title=row["title_r"],
                       abstract=row["abstract_r"], pile="screen_expensive", row=row)


def _extracted(**overrides) -> dict:
    """One finished EXTRACTED_COLS row, as `_process_row` + `_finalise_row` return it."""
    row = {c: "" for c in EXTRACTED_COLS}
    row.update({c: _INPUT.get(c, "") for c in FILTERED_COLS})
    row.update({"pair_id": "p1", "oa_work_id_r": "W99", "type": "replication",
                "screen_categories": "direct_replication",
                "original_rank": 1, "n_originals": 1,
                "original_match_type": "single_original",
                "original_match_confidence": "high",
                "link_method": "llm_references", "link_confidence": "high",
                "link_evidence": "the model named @orig1999",
                "doi_o": "10.1000/orig", "title_o": "The original",
                "doi_o_verification": "verified", "outcome": "success"})
    row.update(overrides)
    return row


def _judge_over(rows: list[dict], observed: "dict | None" = None):
    """Run `_judge` with `_process_row`/`_finalise_row` replaced by fixed answers."""
    work = _work()

    def process_row(row, doi_r, **kwargs):
        assert isinstance(row, pd.Series), "the judge must hand over a row the "\
                                           "Stage 3 pipeline recognises"
        kwargs["observed"].update(observed or {})
        return rows

    with patch("extract.run_extract._process_row", side_effect=process_row), \
         patch("extract.run_extract._finalise_row", side_effect=lambda r: r):
        return tier_mod._judge(work)


# ---------------------------------------------------------------------------
# The judge: terminal state → (verdict kind, payload)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("link_method,expected", [
    ("llm_references", RESOLVED),
    ("llm_cited_candidates", RESOLVED),
    ("title_pattern_match", RESOLVED),
    ("llm_title_search", PROVISIONAL),
    ("not_a_replication", NOT_A_REPLICATION),
    ("no_original_found", NO_ORIGINAL_FOUND),
    ("target_pending", TARGET_PENDING),
    ("api_error", API_ERROR),
])
def test_each_ending_becomes_its_own_result_verdict(link_method, expected):
    outcome, votes = _judge_over([_extracted(link_method=link_method)])
    assert outcome == expected
    results = [v for v in votes if v["verdict"] == expected]
    assert len(results) == 1, "a work gets exactly one result row per run"
    assert results[0]["prompt_hash"] == extract_generation()
    assert results[0]["payload"]["kind"] == "result"


def test_a_resolved_row_outranks_a_provisional_one_from_the_same_paper():
    """A paper with one confirmed original and one title-search guess resolved
    something; filing it as provisional would understate what the run achieved."""
    outcome, _ = _judge_over([_extracted(link_method="llm_references"),
                              _extracted(link_method="llm_title_search")])
    assert outcome == RESOLVED


def test_the_payload_rebuilds_the_rows_the_judge_produced():
    rows = [_extracted(), _extracted(doi_o="10.1000/second", original_rank=2,
                                     n_originals=2)]
    _, votes = _judge_over(rows)
    payload = [v for v in votes if v["verdict"] == RESOLVED][0]["payload"]
    rebuilt = tier_mod.render_payload(payload)
    assert len(rebuilt) == 2
    for original, back in zip(rows, rebuilt):
        for col in EXTRACTED_COLS:
            assert str(back[col]) == str(original.get(col, "")), col


def test_an_llm_rung_writes_one_evidence_row_per_call_it_made():
    """Evidence rows name the calls; the result row names the conclusion."""
    _, votes = _judge_over([_extracted(link_llm_model="gpt-5.4-mini",
                                       outcome_llm_model="gpt-5.4-mini")],
                           observed={"target_stage": "reftarget",
                                     "link_llm_model": "gpt-5.4-mini"})
    evidence = [v for v in votes if v["verdict"] == "evidence"]
    assert {v["payload"]["call"] for v in evidence} == {"target", "outcome"}
    assert all(v["model"] == "gpt-5.4-mini" for v in evidence)
    assert all(v["prompt_hash"] for v in evidence)
    assert all(v["payload"]["rung"] == "reftarget" for v in evidence)


def test_a_rule_resolved_row_makes_no_llm_call_and_writes_no_evidence():
    _, votes = _judge_over([_extracted(link_method="title_pattern_match",
                                       link_llm_model="", outcome_llm_model="keyword")])
    assert [v for v in votes if v["verdict"] == "evidence"] == []


# ---------------------------------------------------------------------------
# The checkpoint
# ---------------------------------------------------------------------------


def _rows(*verdicts) -> list[dict]:
    return [{"id": f"v{i}", "verdict": v, "created_at": f"2026-08-0{i + 1}T00:00:00Z"}
            for i, v in enumerate(verdicts)]


@pytest.mark.parametrize("verdict,settles", [
    (RESOLVED, True), (PROVISIONAL, True), (NOT_A_REPLICATION, True),
    (NO_ORIGINAL_FOUND, True), (TARGET_PENDING, False), (API_ERROR, False),
])
def test_only_a_conclusive_ending_settles_a_work(verdict, settles):
    """`target_pending` and `api_error` are the two endings a re-run is meant to
    redo. Counting either as settled turns a five-minute outage into a permanent
    hole in the corpus."""
    assert tier_mod._decide(_rows(verdict))["settles"] is settles


def test_evidence_rows_alone_never_settle_a_work():
    """The screens' `decided_work_ids` would call this decided — it counts any row
    that adds up to a decision — and the work would never be extracted."""
    decision = tier_mod._decide(_rows("evidence", "evidence"))
    assert decision["settles"] is False and decision["row"] is None


def test_the_latest_result_row_decides_not_the_pooled_ones():
    """A result row is a whole answer about a work, so two are two runs."""
    decision = tier_mod._decide(_rows(TARGET_PENDING, RESOLVED))
    assert decision["outcome"] == RESOLVED and decision["settles"] is True
    decision = tier_mod._decide(_rows(RESOLVED, TARGET_PENDING))
    assert decision["outcome"] == TARGET_PENDING and decision["settles"] is False


# ---------------------------------------------------------------------------
# The generation
# ---------------------------------------------------------------------------


def test_the_generation_is_pinned_by_its_inputs():
    """A bump must read as a diff of WHAT changed, not as one hex string becoming
    another. The names are the pin; the values move with the code they name."""
    inputs = generation_inputs()
    assert set(inputs) == {"ladder", "prompts", "models"}
    assert set(inputs["prompts"]) == {
        "build_target_outcome_prompt", "build_repro_target_outcome_prompt",
        "build_outcome_prompt", "build_repro_outcome_prompt"}
    assert set(inputs["models"]) == {"linking", "outcome", "pdf_parse"}
    assert isinstance(inputs["ladder"], int)
    # The efforts are IN the model ids, or two runs at different reasoning levels
    # would share a generation (`cache_model_id`).
    from shared.config import LINKING_EFFORT
    assert LINKING_EFFORT in inputs["models"]["linking"]
    assert len(extract_generation()) == 16


def test_a_changed_prompt_mints_a_new_generation(monkeypatch):
    before = extract_generation()
    monkeypatch.setattr(tier_mod, "_GENERATION_PROMPTS",
                        ("build_outcome_prompt",))
    assert extract_generation() != before


def test_no_extract_verdict_predates_the_generation_field():
    """This tier's first verdict row is written by the commit that adds it, so a
    claim with no generation is not legacy — it is unattributable."""
    assert tier_mod._accepts_legacy([{"model": "gpt-5.4-mini"}]) is False


# ---------------------------------------------------------------------------
# The worklist
# ---------------------------------------------------------------------------


def _run_worklist(monkeypatch, *, rows, screen, drop=frozenset(),
                  claimed=frozenset(), settled=frozenset(), **kwargs):
    client = MagicMock()
    client.claimed_work_ids.return_value = set(claimed)
    monkeypatch.setattr(tier_mod, "check_release_binding", lambda *a, **k: None)
    monkeypatch.setattr(tier_mod, "load_conventions", lambda: {})
    monkeypatch.setattr(tier_mod, "load_specs", lambda spec_dir: [])
    monkeypatch.setattr(tier_mod, "iter_export_rows",
                        lambda *a, **k: iter(rows))
    monkeypatch.setattr(tier_mod, "decisions", lambda c: (set(drop), screen))
    monkeypatch.setattr(tier_mod, "settled_work_ids", lambda c, mode="live": set(settled))
    monkeypatch.setattr(tier_mod, "screen_columns", lambda row, decision: {})
    monkeypatch.setattr(tier_mod, "_flora_skip_dois", lambda d: set())
    monkeypatch.setattr(tier_mod, "_load_validated_skip", lambda p: (set(), set()))
    return tier_mod.extract_works(None, client, None, "rel-1", **kwargs)


_PROCEED = {"outcome": "proceed", "record_type": "replication", "votes": []}


def _export_rows(*ids):
    return [("screen_expensive", i, {**_INPUT, "doi_r": f"10.1000/w{i}"}) for i in ids]


def test_the_worklist_is_the_screen_proceeds_minus_what_is_done_or_held(monkeypatch):
    works = _run_worklist(
        monkeypatch,
        rows=_export_rows(1, 2, 3, 4, 5),
        screen={1: _PROCEED, 2: _PROCEED, 3: _PROCEED, 4: _PROCEED},
        # 5 was never screened, 4 was screened and discarded, 3 is already settled,
        # 2 is held by another runner's live claim.
        drop={4}, settled={3}, claimed={2})
    assert [w.work_id for w in works] == [1]
    assert isinstance(works[0], ExtractWork)
    assert works[0].row["doi_r"] == "10.1000/w1"


def test_an_unscreened_work_never_reaches_the_tier(monkeypatch):
    """Routing says 'this deserves an LLM's attention'; only the validated pair says
    'this reaches Stage 3'."""
    works = _run_worklist(monkeypatch, rows=_export_rows(1, 2), screen={2: _PROCEED})
    assert [w.work_id for w in works] == [2]


def test_the_flora_and_validation_skip_lists_are_honoured(monkeypatch):
    monkeypatch.setattr(tier_mod, "check_release_binding", lambda *a, **k: None)
    monkeypatch.setattr(tier_mod, "load_conventions", lambda: {})
    monkeypatch.setattr(tier_mod, "load_specs", lambda spec_dir: [])
    monkeypatch.setattr(tier_mod, "iter_export_rows",
                        lambda *a, **k: iter(_export_rows(1, 2, 3)))
    monkeypatch.setattr(tier_mod, "decisions",
                        lambda c: (set(), {1: _PROCEED, 2: _PROCEED, 3: _PROCEED}))
    monkeypatch.setattr(tier_mod, "settled_work_ids", lambda c, mode="live": set())
    monkeypatch.setattr(tier_mod, "screen_columns", lambda row, decision: {})
    monkeypatch.setattr(tier_mod, "_flora_skip_dois", lambda d: {"10.1000/w1"})
    monkeypatch.setattr(tier_mod, "_load_validated_skip",
                        lambda p: (set(), {"10.1000/w2"}))
    client = MagicMock()
    client.claimed_work_ids.return_value = set()
    works = tier_mod.extract_works(None, client, None, "rel-1")
    assert [w.work_id for w in works] == [3]


def test_redo_re_admits_a_settled_work(monkeypatch):
    works = _run_worklist(monkeypatch, rows=_export_rows(1),
                          screen={1: _PROCEED}, settled={1})
    assert works == []
    works = _run_worklist(monkeypatch, rows=_export_rows(1),
                          screen={1: _PROCEED}, settled={1}, redo=[1])
    assert [w.work_id for w in works] == [1]


def test_a_work_this_run_already_judged_is_not_offered_again(monkeypatch):
    """`target_pending` does not settle, so the checkpoint hands it straight back —
    and the rebuild between batches would judge it again, and again. A re-run is how
    an unsettled work gets another chance; the same run is not. Observed on
    2026-08-07: 51 unsettled works judged eight times in twenty minutes."""
    works = _run_worklist(monkeypatch, rows=_export_rows(1, 2),
                          screen={1: _PROCEED, 2: _PROCEED},
                          settled=set(), attempted={1})
    assert [w.work_id for w in works] == [2]


def test_a_redone_work_is_still_dropped_once_this_run_has_judged_it(monkeypatch):
    """--redo re-admits past the checkpoint; it must not re-admit past the run's own
    memory, or the redo set loops for exactly the same reason."""
    works = _run_worklist(monkeypatch, rows=_export_rows(1),
                          screen={1: _PROCEED}, settled={1}, redo=[1],
                          attempted={1})
    assert works == []


# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------


def test_a_lost_lease_stops_the_run_rather_than_carrying_on():
    """The works are re-claimable and another machine may hold them, so nothing more
    is claimed. What was already written stands — expiry frees works, it never
    retracts evidence."""
    client = MagicMock()
    client.renew_claim.side_effect = ClaimLeaseLost("claim_lease_lost")
    beat = tier_mod._Heartbeat(client, "claim-1", ttl_seconds=3)
    beat._interval = 0.01
    with beat:
        for _ in range(200):
            if beat.lost:
                break
            threading.Event().wait(0.01)
    assert beat.lost is True


def test_a_transport_failure_is_not_a_lost_lease():
    """A 503 from PostgREST does not mean the claim expired, and there are three
    renewals before it could."""
    from filter.engine.claims import ClaimsError

    client = MagicMock()
    client.renew_claim.side_effect = ClaimsError("503")
    beat = tier_mod._Heartbeat(client, "claim-1", ttl_seconds=3)
    beat._interval = 0.01
    with beat:
        threading.Event().wait(0.08)
    assert beat.lost is False
    assert client.renew_claim.call_count >= 2


def test_the_batch_loop_stops_after_a_lost_lease(monkeypatch):
    calls: list = []

    def run_batch(spec, client, release_id, works, **kwargs):
        calls.append(len(works))
        return {"claim_id": f"c{len(calls)}", "decided": len(works),
                "outcomes": {RESOLVED: len(works)}, "verdicts": len(works),
                "lease_lost": len(calls) == 1}

    monkeypatch.setattr(tier_mod, "_run_batch", run_batch)
    monkeypatch.setattr(tier_mod, "extract_works",
                        lambda *a, **k: [_work(i) for i in range(4)])
    report = tier_mod.run_extract_tier(None, MagicMock(), "rel-1", run=True,
                                       batch_size=2)
    assert calls == [2], "a second batch was claimed after the lease was lost"
    assert report["stopped"] == "claim lease lost"


def test_the_batch_loop_ends_although_no_work_settled(monkeypatch):
    """Every work ends `target_pending`, which the checkpoint does not subtract. The
    loop must terminate on its own memory of what it judged, not on the checkpoint."""
    calls: list = []

    def run_batch(spec, client, release_id, works, **kwargs):
        calls.append([w.work_id for w in works])
        return {"claim_id": f"c{len(calls)}", "decided": len(works),
                "outcomes": {TARGET_PENDING: len(works)}, "verdicts": len(works)}

    def works_for(*a, attempted=None, limit=None, **k):
        # A checkpoint that subtracts nothing, which is what an all-unsettled run has.
        remaining = [_work(i) for i in range(4) if i not in (attempted or set())]
        return remaining if limit is None else remaining[:limit]

    monkeypatch.setattr(tier_mod, "_run_batch", run_batch)
    monkeypatch.setattr(tier_mod, "extract_works", works_for)
    report = tier_mod.run_extract_tier(None, MagicMock(), "rel-1", run=True,
                                       batch_size=2)
    assert calls == [[0, 1], [2, 3]]
    assert report["decided"] == 4


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


def test_the_dry_run_claims_nothing_and_prices_by_rung(monkeypatch, capsys):
    monkeypatch.setattr(tier_mod, "extract_works", lambda *a, **k: [_work(1)])
    client = MagicMock()
    report = tier_mod.run_extract_tier(None, client, "rel-1", run=False)
    assert report["dry_run"] is True
    assert client.claim.call_count == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "OpenAlex" in out and "credit(s)" in out


def test_the_estimate_reports_openalex_in_credits_not_dollars():
    """OpenAlex bills a daily credit budget that resets at midnight UTC, so a dollar
    figure would answer a question nobody is asked at the point of spending."""
    est = tier_mod.estimate([_work(i) for i in range(100)])
    assert est["oa_credits"] > 0
    assert est["rows"] == 100
    assert set(est["by_rung"]) == set(tier_mod.EXTRACT_RUNG_REACH)
    assert sum(d["rows"] for d in est["by_rung"].values()) == pytest.approx(100, abs=1)


def test_the_measured_rung_reach_sums_to_one():
    """The shares are a distribution over where a row stops, so they must partition
    the rows. Measured off data/extracted.csv; re-measured from run reports."""
    assert sum(tier_mod.EXTRACT_RUNG_REACH.values()) == pytest.approx(1.0, abs=0.001)


def test_validation_verdicts_do_not_settle_the_live_worklist():
    """A shadow run must leave its works claimable live — that IS the promotion path."""
    from unittest.mock import MagicMock, patch
    import extract.tier as tier_mod

    live_claim = {"id": "c-live", "meta": {"mode": "live",
                                          "generation": tier_mod.extract_generation()}}
    val_claim = {"id": "c-val", "meta": {"mode": "validation",
                                         "generation": tier_mod.extract_generation()}}
    rows = [
        {"claim_id": "c-live", "work_id": 1, "verdict": "resolved",
         "created_at": "2026-08-06T00:00:00Z", "id": "v1"},
        {"claim_id": "c-val", "work_id": 2, "verdict": "resolved",
         "created_at": "2026-08-06T00:00:00Z", "id": "v2"},
    ]
    client = MagicMock()
    client.claims.return_value = [live_claim, val_claim]
    client.verdicts.return_value = rows

    assert tier_mod.settled_work_ids(client, "live") == {1}
    assert tier_mod.settled_work_ids(client, "validation") == {2}
