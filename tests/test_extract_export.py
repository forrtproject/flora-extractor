"""The round trip: an extracted row → a stored payload → the same row back.

This is the acceptance test for the extract tier's payload shape. The tier stores one
result row per work in `engine_verdicts` and `extract/export.py` renders
`data/extracted.csv` back out of it; if a column cannot survive that trip, the file
the validators read is not the file Stage 3 produced, and the failure is silent — a
blank column, not a crash.

So the fixture is REAL: representative rows lifted out of today's
`data/extracted.csv`, one per shape the payload has to carry (single link,
multi-original, set-aside-bound, api_error). They go through the same
`result_payload()` the judge calls and back through the same `render_payload()` the
export calls, and the assertion is field-for-field equality — plus byte equality of
the CSV at the file level, because that is what a reader actually consumes.

Every network call is mocked. Nothing here talks to anything.
"""

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from extract import export as export_mod
from extract.tier import (RESOLVED, TARGET_PENDING, TIER_EXTRACT,
                          extract_generation, render_payload, result_payload)
from shared.schema import EXTRACTED_COLS, FILTERED_COLS, SCREEN_COLS

# ── The fixture, lifted from data/extracted.csv on 2026-08-06 ────────────────
# Trimmed to the columns that carry a value; every other EXTRACTED_COLS column is
# filled with "" by `_row()` below, which is what the CSV holds for them.

_SINGLE = {
    "pair_id": "a2d85876fefa01164d090ba9d70bad7a",
    "doi_r": "10.1108/s1574-076520240000026005",
    "title_r": "Fraudulent Research Practices in Accounting: A Replication and "
               "Extension of Bailey, Hasselback, and Karcher (2001)",
    "abstract_r": "Bailey et al. (2001) queried accounting researchers concerning "
                  "admitted fraudulent research practices.",
    "year_r": "2024", "authors_r": "Charles D. Bailey",
    "journal_r": "Research on professional responsibility and ethics in accounting",
    "openalex_id_r": "https://openalex.org/W4396880944", "source": "openalex",
    "ref_r": "Bailey · 2024 · Research on professional responsibility",
    "filter_status": "replication", "filter_method": "rule_based",
    "filter_evidence": "phrase:replication and extension", "filter_confidence": "high",
    "original_match_type": "single_original", "original_match_confidence": "low",
    "oa_work_id_r": "W4396880944", "oa_work_id_o": "W2144439281",
    "doi_o": "10.1111/1467-6281.00073",
    "title_o": "Research Misconduct in Accounting Literature",
    "year_o": "2001", "authors_o": "Bailey, C. N.; Hasselback, J. R.",
    "ref_o": "Bailey, C. N. (2001). Research Misconduct in Accounting Literature.",
    "bibtex_ref_o": "@article{Bailey_2001, title={Research Misconduct}}",
    "bibtex_ref_r": "@article{CharlesDBailey_2024, title={Fraudulent Research}}",
    "link_method": "author_year_match_legacy", "link_confidence": "high",
    "doi_o_verification": "verified",
    "outcome": "cannot_be_determined",
    "outcome_phrase": "The current study replicates Bailey et al. (2001).",
    "outcome_confidence": "high", "out_quote_source": "abstract",
    "outcome_reasoning": "The abstract states the paper replicates but not the result.",
    "type": "replication", "original_rank": "1", "n_originals": "1",
}

# Two originals from one paper: the per-target adapter's shape, where the rows share
# every FILTERED_COLS value and differ in the whole o-side.
_MULTI_A = {
    "pair_id": "1111111111111111aaaaaaaaaaaaaaaa",
    "doi_r": "10.1080/13854040903307243", "title_r": "A two-target replication",
    "abstract_r": "We re-test two earlier findings.", "year_r": "2010",
    "authors_r": "A. Author", "journal_r": "J. Neuropsych",
    "openalex_id_r": "https://openalex.org/W2000000001", "source": "openalex",
    "filter_status": "replication", "filter_method": "screen",
    "filter_confidence": "high",
    "screen_categories": "direct_replication|multi_study",
    "original_match_type": "multiple_original", "original_match_confidence": "high",
    "oa_work_id_r": "W2000000001", "oa_work_id_o": "W2000000091",
    "study_r": "1", "doi_o": "10.1000/first", "title_o": "First original",
    "study_o": "2", "year_o": "1999", "authors_o": "B. Original",
    "link_method": "llm_references", "link_evidence": "the model named @orig1999",
    "link_confidence": "high", "link_llm_model": "gpt-5.4-mini",
    "doi_o_verification": "verified", "outcome": "success",
    "outcome_phrase": "We replicated the first effect.", "outcome_confidence": "high",
    "out_quote_source": "discussion", "outcome_llm_model": "gpt-5.4-mini",
    "type": "replication", "original_rank": "1", "n_originals": "2",
}
_MULTI_B = {**_MULTI_A,
            "pair_id": "2222222222222222bbbbbbbbbbbbbbbb",
            "oa_work_id_o": "W2000000092", "study_r": "2",
            "doi_o": "10.1000/second", "title_o": "Second original",
            "study_o": "1", "year_o": "2001", "authors_o": "C. Original",
            "outcome": "failure",
            "outcome_phrase": "The second effect did not replicate.",
            "original_rank": "2"}

# Bound for a set-aside file rather than for extracted.csv: `sanity_check`'s
# target_pending bucket, which the export must route out of the main CSV.
_PENDING = {
    "pair_id": "3333333333333333cccccccccccccccc",
    "doi_r": "10.1000/pending", "title_r": "A paper whose target was never found",
    "abstract_r": "Some abstract.", "year_r": "2022", "source": "openalex",
    "openalex_id_r": "https://openalex.org/W3000000003",
    "filter_status": "replication", "filter_method": "screen",
    "filter_confidence": "medium",
    "original_match_type": "single_original", "original_match_confidence": "low",
    "oa_work_id_r": "W3000000003",
    "link_method": "target_pending", "link_confidence": "low",
    "doi_o_verification": "no_doi", "outcome": "pending",
    "type": "replication", "original_rank": "1", "n_originals": "1",
}

_API_ERROR = {
    "pair_id": "4444444444444444dddddddddddddddd",
    "doi_r": "10.1000/broke", "title_r": "A paper the provider fell over on",
    "abstract_r": "Some abstract.", "year_r": "2023", "source": "openalex",
    "openalex_id_r": "https://openalex.org/W4000000004",
    "filter_status": "replication", "filter_method": "screen",
    "filter_confidence": "high",
    "original_match_type": "single_original", "original_match_confidence": "low",
    "oa_work_id_r": "W4000000004",
    "link_method": "api_error",
    "link_evidence": "extraction failed: ReadTimeout: provider timed out",
    "link_confidence": "low", "doi_o_verification": "no_doi",
    "outcome": "api_error", "type": "replication",
    "original_rank": "1", "n_originals": "1",
}


def _row(partial: dict) -> dict:
    """One EXTRACTED_COLS row: the fixture's values, blank everywhere else."""
    return {**{col: "" for col in EXTRACTED_COLS}, **partial}


def _input_row(row: dict) -> dict:
    """The handoff row the judge would have read for *row*.

    Every FILTERED_COLS + SCREEN_COLS value the CSV row carries. `filter_status` and
    `filter_method` are deliberately given their PRE-screen values where the row shows
    the screen retyped them, so the fixture exercises the case where a target entry
    has to carry a changed FILTERED_COLS value rather than inherit it.
    """
    source = {col: row.get(col, "") for col in FILTERED_COLS}
    source.update({col: "" for col in SCREEN_COLS if col not in source})
    if row.get("filter_method") == "screen":
        source["filter_status"] = "needs_review"
        source["filter_method"] = "engine:abc123def456"
        source["screen_verdict"] = "proceed"
        source["screen_record_type"] = row.get("type", "")
        source["screen_categories"] = row.get("screen_categories", "")
    return source


def _payload(rows: list[dict], observed: "dict | None" = None) -> dict:
    return result_payload(_input_row(rows[0]), rows[0]["doi_r"], rows,
                          observed or {})


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,rows", [
    ("single", [_row(_SINGLE)]),
    ("multi_original", [_row(_MULTI_A), _row(_MULTI_B)]),
    ("set_aside_bound", [_row(_PENDING)]),
    ("api_error", [_row(_API_ERROR)]),
])
def test_a_row_survives_the_payload_field_for_field(name, rows):
    """The gate. If a column cannot round-trip, the payload shape is wrong."""
    rendered = render_payload(_payload(rows))
    assert len(rendered) == len(rows), name
    for original, back in zip(rows, rendered):
        for col in EXTRACTED_COLS:
            assert str(back.get(col, "")) == str(original.get(col, "")), \
                f"{name}: column {col} did not survive the payload"


def test_the_csv_is_byte_identical_after_a_round_trip(tmp_path):
    """File level, because a CSV is what a reader actually consumes."""
    rows = [_row(_SINGLE), _row(_MULTI_A), _row(_MULTI_B)]

    direct = tmp_path / "direct.csv"
    _write(direct, rows)

    results = {
        1: {"payload": _payload([_row(_SINGLE)])},
        2: {"payload": _payload([_row(_MULTI_A), _row(_MULTI_B)])},
    }
    round_tripped = tmp_path / "round.csv"
    _write(round_tripped, export_mod.rows_from_results(results))

    assert round_tripped.read_bytes() == direct.read_bytes()


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXTRACTED_COLS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in EXTRACTED_COLS})


def test_the_input_snapshot_is_not_duplicated_onto_every_target():
    """A two-row paper stores its shared FILTERED_COLS once, not twice.

    Not a size micro-optimisation: it is what makes "the input the judge read" a
    single fact in the payload rather than N copies that could disagree.
    """
    payload = _payload([_row(_MULTI_A), _row(_MULTI_B)])
    assert payload["input"]["abstract_r"] == _MULTI_A["abstract_r"]
    for target in payload["targets"]:
        assert "abstract_r" not in target
        assert "authors_r" not in target
    # …but a FILTERED_COLS value the row CHANGED is on the target, or the screen's
    # retyping would be lost.
    assert all(t["filter_status"] == "replication" for t in payload["targets"])
    assert payload["input"]["filter_status"] == "needs_review"


def test_rendering_needs_nothing_but_the_payload(monkeypatch):
    """The self-sufficiency rule, asserted rather than described.

    Any network reach would be a bug that only shows up on a machine without the
    cache, so the two modules a render could plausibly reach through are broken here.
    """
    import shared.openalex_client as oa

    monkeypatch.setattr(oa, "_oa_get", _explode)
    monkeypatch.setattr(oa, "fetch_openalex_by_doi", _explode)
    rendered = render_payload(_payload([_row(_SINGLE)]))
    assert rendered[0]["doi_o"] == _SINGLE["doi_o"]


def _explode(*args, **kwargs):
    raise AssertionError("the export reached the network")


# ---------------------------------------------------------------------------
# Reading the verdicts back
# ---------------------------------------------------------------------------


def _verdict(work: int, *, verdict: str = RESOLVED, claim: str = "c-live",
             generation: "str | None" = None, created_at: str = "2026-08-06T10:00:00Z",
             row_id: str = "v1", rows: "list[dict] | None" = None) -> dict:
    payload = _payload(rows or [_row(_SINGLE)])
    return {"id": row_id, "claim_id": claim, "work_id": work, "tier": TIER_EXTRACT,
            "verdict": verdict, "created_at": created_at,
            "prompt_hash": extract_generation() if generation is None else generation,
            "payload": payload}


def _client(verdicts: list[dict], claims: "list[dict] | None" = None) -> MagicMock:
    client = MagicMock()
    client.verdicts.return_value = verdicts
    client.claims.return_value = claims if claims is not None else [
        {"id": "c-live", "meta": {"mode": "live"}},
        {"id": "c-val", "meta": {"mode": "validation"}}]
    return client


def test_the_latest_result_row_speaks_for_a_work():
    """Two runs are two answers about one work, and the newer one is the answer."""
    old = _verdict(11, verdict=TARGET_PENDING, created_at="2026-08-01T00:00:00Z",
                   row_id="v-old")
    new = _verdict(11, verdict=RESOLVED, created_at="2026-08-06T00:00:00Z",
                   row_id="v-new")
    results, stale = export_mod.latest_results(_client([old, new]))
    assert set(results) == {11}
    assert results[11]["id"] == "v-new"
    assert stale == 0


def test_an_evidence_row_is_never_rendered_as_a_result():
    """An evidence row says what a call answered, not what the work concluded."""
    evidence = {**_verdict(12), "verdict": "evidence", "id": "v-ev"}
    results, _ = export_mod.latest_results(_client([evidence]))
    assert results == {}


def test_a_validation_run_is_invisible_to_the_live_export():
    live = _verdict(13, claim="c-live", row_id="v-live")
    validation = _verdict(14, claim="c-val", row_id="v-val")
    results, _ = export_mod.latest_results(_client([live, validation]))
    assert set(results) == {13}
    results, _ = export_mod.latest_results(_client([live, validation]),
                                           mode="validation")
    assert set(results) == {14}


def test_a_superseded_generation_is_carried_forward_and_counted():
    """A prompt edit must not silently delete a finding from the corpus.

    The strict view exists and is one flag away; the default carries the row and says
    how many it carried.
    """
    stale_row = _verdict(15, generation="deadbeefdeadbeef", row_id="v-stale")
    results, stale = export_mod.latest_results(_client([stale_row]))
    assert set(results) == {15} and stale == 1

    results, stale = export_mod.latest_results(_client([stale_row]),
                                               current_generation_only=True)
    assert results == {} and stale == 0


def test_a_current_generation_row_beats_a_stale_one_for_the_same_work():
    stale_row = _verdict(16, generation="deadbeefdeadbeef", row_id="v-stale",
                         created_at="2026-08-09T00:00:00Z")
    fresh = _verdict(16, row_id="v-fresh", created_at="2026-08-01T00:00:00Z")
    results, stale = export_mod.latest_results(_client([stale_row, fresh]))
    assert results[16]["id"] == "v-fresh"
    assert stale == 0


# ---------------------------------------------------------------------------
# Partitioning and writing
# ---------------------------------------------------------------------------


def test_set_aside_rows_leave_the_main_csv_through_the_shared_rules():
    rows = [_row(_SINGLE), _row(_PENDING), _row(_API_ERROR)]
    main, aside = export_mod.partition(rows)
    assert [r["doi_r"] for r in main] == [_SINGLE["doi_r"]]
    assert set(aside) == {"target_pending.csv", "api_error.csv"}
    assert len(aside["target_pending.csv"]) == 1
    assert len(aside["api_error.csv"]) == 1


def test_a_resolved_method_with_no_doi_o_is_demoted_before_it_is_bucketed():
    """The malformed case: it claims a target it cannot name, so it is filed as
    pending rather than written into the validation-ready file."""
    malformed = _row({**_SINGLE, "doi_o": "", "link_method": "llm_references"})
    main, aside = export_mod.partition([malformed])
    assert main == []
    filed = aside["target_pending.csv"][0]
    assert filed["link_method"] == "target_pending"
    assert filed["link_evidence"].startswith("demoted by sanity_check")


def test_check_reports_a_difference_and_writing_removes_it(tmp_path):
    out = tmp_path / "extracted.csv"
    report = export_mod.render(_client([_verdict(21, row_id="v-21")]))

    diff = export_mod.check(report, out)
    assert diff["exists"] is False

    export_mod.write(report, out)
    diff = export_mod.check(report, out)
    assert diff["n_only_on_disk"] == 0 and diff["n_only_in_render"] == 0

    # A row nobody rendered shows up on exactly one side of the diff.
    with out.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=EXTRACTED_COLS,
                       extrasaction="ignore").writerow(_row(_API_ERROR))
    diff = export_mod.check(report, out)
    assert diff["n_only_on_disk"] == 1 and diff["n_only_in_render"] == 0


def test_the_set_asides_belong_to_the_csv_being_written(tmp_path):
    """A render to a sandbox path must not settle a paper for the production resume."""
    out = tmp_path / "extracted-test.csv"
    report = export_mod.render(_client(
        [_verdict(31, verdict=TARGET_PENDING, row_id="v-31",
                  rows=[_row(_PENDING)])]))
    export_mod.write(report, out)
    assert (tmp_path / "extracted-test-set-aside" / "target_pending.csv").exists()
    assert not (tmp_path / "target_pending.csv").exists()


# ---------------------------------------------------------------------------
# Retroactive corrections: the write path the two audit tools share
# ---------------------------------------------------------------------------


def _correcting_client(verdicts: list[dict]) -> MagicMock:
    client = _client(verdicts, claims=[
        {"id": "c-live", "release_id": "rel-1", "meta": {"mode": "live"}}])
    client.claim.return_value = "c-fix"
    client.record_verdict.return_value = "v-new"
    return client


def test_a_correction_claims_writes_a_new_result_row_and_supersedes_the_old():
    """The correction goes where the row is rendered FROM. The old row stays as
    evidence of what was believed; the new one carries the same ending and
    generation, because a corrected field is not a re-extraction."""
    from extract.tier import supersede_targets

    pair = _SINGLE["pair_id"]
    client = _correcting_client([_verdict(11, row_id="v-old")])
    report = supersede_targets(client, {pair: {"doi_o": "10.9/right"}},
                               batch_label="audit-dois")

    assert report == {"works": 1, "rows": 1, "unmatched": [], "claim": "c-fix"}
    # Claimed before anything was written, in the release the old row belongs to.
    assert client.claim.call_args[0][:2] == ("rel-1", TIER_EXTRACT)
    assert client.claim.call_args[1]["meta"]["kind"] == "correction"
    written = client.record_verdict.call_args[1]
    assert written["verdict"] == RESOLVED, "the ending is the old row's"
    assert written["prompt_hash"] == extract_generation()
    assert written["payload"]["targets"][0]["doi_o"] == "10.9/right"
    client.supersede_verdict.assert_called_once_with("v-old", "v-new")
    client.release_claim.assert_called_once_with("c-fix", "complete")


def test_a_correction_naming_no_stored_row_is_reported_not_guessed():
    """A pair id the live payloads do not hold names a row this tool cannot correct.
    Nothing is claimed and nothing is written."""
    from extract.tier import supersede_targets

    client = _correcting_client([_verdict(11, row_id="v-old")])
    report = supersede_targets(client, {"deadbeef": {"doi_o": "10.9/x"}},
                               batch_label="audit-dois")

    assert report["unmatched"] == ["deadbeef"] and report["works"] == 0
    client.claim.assert_not_called()
    client.record_verdict.assert_not_called()


def test_the_export_refreshes_the_dashboard_mirror_for_the_file_it_wrote(tmp_path,
                                                                        monkeypatch):
    """Stage 4 reads a parquet mirror, and every runner refreshes the stage it wrote.
    That used to be the CSV runner's last act; the writer is this module now."""
    import extract.export as mod

    out = tmp_path / "extracted.csv"
    monkeypatch.setattr("shared.dashboard_cache._STAGE_CSV",
                        {"extracted": out, "extracted-test": tmp_path / "other.csv"})
    called: list[str] = []
    monkeypatch.setattr("shared.dashboard_cache.refresh",
                        lambda stage: called.append(stage))

    mod._refresh_dashboard(out)
    assert called == ["extracted"]

    # A render to a path the dashboard has never heard of is not a stage it displays.
    mod._refresh_dashboard(tmp_path / "scratch.csv")
    assert called == ["extracted"]


def test_a_failing_dashboard_refresh_does_not_fail_the_export(tmp_path, monkeypatch):
    """The rows are already published; a stale mirror is not worth losing that over."""
    import extract.export as mod

    out = tmp_path / "extracted.csv"
    monkeypatch.setattr("shared.dashboard_cache._STAGE_CSV", {"extracted": out})

    def _boom(stage):
        raise RuntimeError("parquet engine missing")

    monkeypatch.setattr("shared.dashboard_cache.refresh", _boom)
    mod._refresh_dashboard(out)   # no raise
