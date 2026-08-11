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
from shared.prompts import prompt_version
from shared.schema import (EXTRACTED_COLS, FILTERED_COLS,
                           PROVISIONAL_LINK_METHODS, SCREEN_COLS)

_INPUT = {
    "doi_r": "10.1000/repl", "title_r": "A replication of something",
    "abstract_r": "We re-test the anchoring effect in a new sample.",
    "year_r": "2024", "authors_r": "A. Author", "journal_r": "J. Repl",
    "url_r": "", "openalex_id_r": "https://openalex.org/W99", "source": "openalex",
    "ref_r": "Author · 2024 · J. Repl",
    "paper_type": "replication", "filter_method": "engine:abc",
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
                "doi_o_verification": "verified", "outcome": "successful"})
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
    ("llm_title_search", RESOLVED),
    ("keyed_link_disputed", PROVISIONAL),
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


def test_a_payload_stored_before_the_paper_type_rename_still_renders():
    """A verdict stored under `filter_status` renders as `paper_type` (issue #93).

    Both halves: the input row, and a target — a work the screen retyped carries
    the paper type on the target, because that is where a CHANGED input value goes.
    """
    payload = {
        "kind": "result",
        "input": {"doi_r": "10.1000/legacy", "filter_status": "needs_review"},
        "targets": [{"pair_id": "p1", "doi_o": "10.1000/orig"},
                    {"pair_id": "p2", "doi_o": "10.1000/second",
                     "filter_status": "reproduction"}],
    }
    rendered = tier_mod.render_payload(payload)
    assert [row["paper_type"] for row in rendered] == ["needs_review", "reproduction"]
    assert all("filter_status" not in row for row in rendered)


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


@pytest.mark.parametrize("method", sorted(PROVISIONAL_LINK_METHODS))
def test_every_provisional_link_method_settles_the_work(method):
    """A provisional link is the answer a re-run would get, so it ends the work. The
    ending used to name llm_title_search alone, so a second provisional method was
    filed target_pending and the work reopened for ever."""
    verdict = tier_mod._verdict_for([{"link_method": method}], {})
    assert verdict == PROVISIONAL
    assert verdict not in tier_mod.UNSETTLING_VERDICTS


def test_a_sandbox_redo_never_supersedes_a_live_result_row():
    """`--mode validation --redo` re-extracts in the sandbox. Marking the work's LIVE
    row superseded would delete it from the export, which reads live rows — the one
    thing the sandbox exists not to touch."""
    generation = tier_mod.extract_generation()
    client = MagicMock()
    client.claims.return_value = [
        {"id": "c-live", "meta": {"mode": "live", "generation": generation}},
        {"id": "c-sandbox", "meta": {"mode": "validation", "generation": generation}},
    ]
    client.verdicts.return_value = [
        {"id": "v-live", "claim_id": "c-live", "work_id": 7, "verdict": RESOLVED,
         "created_at": "2026-08-01T00:00:00Z"},
        {"id": "v-sandbox", "claim_id": "c-sandbox", "work_id": 7,
         "verdict": NO_ORIGINAL_FOUND, "created_at": "2026-08-02T00:00:00Z"},
    ]
    assert tier_mod._supersedable(client, [7], "validation") == {7: "v-sandbox"}
    assert tier_mod._supersedable(client, [7], "live") == {7: "v-live"}


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
    # The ladder is deliberately absent: it changes HOW an original is found, not what
    # the pipeline asks, and its reopen is named (`--redo-status`) rather than inferred.
    assert set(inputs) == {"prompts", "models"}
    assert set(inputs["prompts"]) == {
        "build_target_outcome_prompt", "build_repro_target_outcome_prompt",
        "build_outcome_prompt", "build_repro_outcome_prompt",
        # The pooled-candidate pick decides a link, so an edit to it changes what a
        # row concludes and must reopen the works it decided.
        "build_author_year_pick_prompt",
        # The keyed-record confirm (issue #186 Shape 1) can demote a resolved row,
        # so it reopens works on the same grounds.
        "build_keyed_confirm_prompt"}
    assert set(inputs["models"]) == {"linking", "outcome", "pdf_parse"}
    # The efforts are IN the model ids, or two runs at different reasoning levels
    # would share a generation (`cache_model_id`).
    from shared.config import LINKING_EFFORT
    assert LINKING_EFFORT in inputs["models"]["linking"]
    assert len(extract_generation()) == 16


def test_the_label_rename_does_not_reopen_the_settled_works():
    """The declared equivalence (issue #171) for the FLoRA outcome-label rename.

    Both replication prompts still hash into the fingerprint at the version they had
    before the rename, so the works settled under it stay settled. The check is worth
    a test because the failure is silent in the wrong direction: a broken equivalence
    reopens 1,899 works and the next run simply re-extracts them.
    """
    for name, (after, before) in tier_mod._GENERATION_PROMPT_EQUIVALENCES.items():
        assert prompt_version(name) == after, (
            f"{name} has been edited since the rename was reviewed; either the edit "
            f"is answer-preserving and the pair here needs re-declaring against the "
            f"new version, or it is not and the entry must go")
        assert generation_inputs()["prompts"][name] == before


def test_an_undeclared_prompt_edit_still_moves_the_generation(monkeypatch):
    """The equivalence is pinned to one reviewed version, so it expires by itself."""
    monkeypatch.setattr(tier_mod, "_GENERATION_PROMPT_EQUIVALENCES",
                        {"build_outcome_prompt": ("not-the-current-version", "old")})
    assert (generation_inputs()["prompts"]["build_outcome_prompt"]
            == prompt_version("build_outcome_prompt"))


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


def test_redo_status_names_the_population_a_ladder_change_reaches():
    """The reopen a ladder bump needs: the works that ended in a named verdict, not
    every settled work. Latest row wins and the mode is honoured, exactly as the
    checkpoint reads them — a work the worklist would offer anyway needs no reopening.
    """
    from unittest.mock import MagicMock

    gen = tier_mod.extract_generation()
    claims = [{"id": "c-live", "meta": {"mode": "live", "generation": gen}},
              {"id": "c-val", "meta": {"mode": "validation", "generation": gen}}]
    rows = [
        # work 1 ended unresolved; work 2 resolved; work 3 was unresolved and is not
        # any more, so its latest row is what counts.
        {"claim_id": "c-live", "work_id": 1, "verdict": NO_ORIGINAL_FOUND,
         "created_at": "2026-08-06T00:00:00Z", "id": "v1"},
        {"claim_id": "c-live", "work_id": 2, "verdict": RESOLVED,
         "created_at": "2026-08-06T00:00:00Z", "id": "v2"},
        {"claim_id": "c-live", "work_id": 3, "verdict": NO_ORIGINAL_FOUND,
         "created_at": "2026-08-06T00:00:00Z", "id": "v3"},
        {"claim_id": "c-live", "work_id": 3, "verdict": RESOLVED,
         "created_at": "2026-08-07T00:00:00Z", "id": "v4"},
        {"claim_id": "c-val", "work_id": 9, "verdict": NO_ORIGINAL_FOUND,
         "created_at": "2026-08-06T00:00:00Z", "id": "v5"},
    ]
    client = MagicMock()
    client.claims.return_value = claims
    client.verdicts.return_value = rows

    assert tier_mod.works_with_status(client, [NO_ORIGINAL_FOUND]) == {1}
    assert tier_mod.works_with_status(client, [NO_ORIGINAL_FOUND, RESOLVED]) == {1, 2, 3}
    assert tier_mod.works_with_status(client, [NO_ORIGINAL_FOUND],
                                      mode="validation") == {9}


def test_redo_status_takes_a_link_method_where_the_verdict_is_too_coarse():
    """`unidentified_original` and `keyed_link_disputed` are both `provisional`
    endings, and the ladder changes that reach one do not reach the other — so the
    finer name has to be askable."""
    from unittest.mock import MagicMock

    gen = tier_mod.extract_generation()
    claims = [{"id": "c", "meta": {"mode": "live", "generation": gen}}]
    rows = [
        {"claim_id": "c", "work_id": 1, "verdict": PROVISIONAL, "id": "v1",
         "created_at": "2026-08-06T00:00:00Z",
         "payload": {"targets": [{"link_method": "unidentified_original"}]}},
        {"claim_id": "c", "work_id": 2, "verdict": PROVISIONAL, "id": "v2",
         "created_at": "2026-08-06T00:00:00Z",
         "payload": {"targets": [{"link_method": "keyed_link_disputed"}]}},
    ]
    client = MagicMock()
    client.claims.return_value = claims
    client.verdicts.return_value = rows

    assert tier_mod.works_with_status(client, ["unidentified_original"]) == {1}
    assert tier_mod.works_with_status(client, [PROVISIONAL]) == {1, 2}


def test_a_status_name_that_does_not_exist_is_refused():
    """A typo must not silently reopen nothing — the flag would read as "already
    fixed" when it had asked for a class that cannot exist."""
    from unittest.mock import MagicMock

    with pytest.raises(ValueError, match="not a result verdict or a link method"):
        tier_mod.works_with_status(MagicMock(), ["unidentified_originals"])


def test_one_errored_target_stops_a_multi_target_work_settling():
    """A work with one original found and another whose search never completed has
    not been answered. Settling it closes the second original for good, and `resolved`
    used to outrank `api_error` — so a two-target work lost its second target to a
    five-minute outage, permanently."""
    verdict = tier_mod._verdict_for(
        [{"link_method": "llm_references"}, {"link_method": "api_error"}], {})
    assert verdict == API_ERROR
    assert verdict in tier_mod.UNSETTLING_VERDICTS


def test_a_work_whose_targets_all_answered_still_settles():
    verdict = tier_mod._verdict_for(
        [{"link_method": "llm_references"}, {"link_method": "target_pending"}], {})
    assert verdict == RESOLVED


def test_a_fresh_target_pending_rests_and_a_stale_one_reopens():
    """Five runs of the 2026-08-09/10 campaign each re-bought ~830 unresolvable
    works' queries. A target_pending younger than EXTRACT_PENDING_RETRY_DAYS is
    subtracted from the worklist like a settled work; older, it re-offers."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fresh = {"outcome": TARGET_PENDING, "settles": False,
             "row": {"created_at": (now - timedelta(days=1)).isoformat()}}
    stale = {"outcome": TARGET_PENDING, "settles": False,
             "row": {"created_at":
                     (now - timedelta(days=tier_mod.EXTRACT_PENDING_RETRY_DAYS + 1)
                      ).isoformat()}}
    resolved = {"outcome": RESOLVED, "settles": True, "row": {"created_at": ""}}
    api_err = {"outcome": API_ERROR, "settles": False,
               "row": {"created_at": (now - timedelta(days=1)).isoformat()}}
    assert tier_mod._resting(fresh) is True
    assert tier_mod._resting(stale) is False
    assert tier_mod._resting(resolved) is False   # settled rows never need the rest
    assert tier_mod._resting(api_err) is False    # api_error retries immediately
    assert tier_mod._resting({"outcome": TARGET_PENDING, "settles": False,
                              "row": {"created_at": "not-a-date"}}) is False
