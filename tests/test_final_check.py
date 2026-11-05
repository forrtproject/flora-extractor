"""The final AI check: what the export ships, and what the headless call may return."""

import json
import subprocess
from unittest.mock import patch

import pytest

from extract import export as ex
from extract import final_check as fc


def _row(work: int = 1, pair: str = "p1", **fields) -> dict:
    return {"pair_id": pair, "oa_work_id_r": f"W{work}", "doi_r": f"10.1/r{work}",
            "doi_o": "10.1/o", "title_o": "An original finding", "authors_o": "Smith, A.",
            "year_o": "2001", "type": "replication", "paper_type": "replication",
            "link_method": "llm_cited_candidates", "outcome": "successful", "outcome_phrase": "It held.",
            "title_r": "A replication", "abstract_r": "We replicated it.", **fields}


def _report(*rows: dict) -> dict:
    return {"main": list(rows), "aside": {}, "duplicate_pairs": []}


def _entry(rows: list[dict], status: str = "review", decision: str = "") -> dict:
    return {"work": ex.work_label(rows[0]), "fingerprint": ex.work_fingerprint(rows),
            "status": status, "decision": decision}


def _held(report: dict) -> list[str]:
    return [r["pair_id"] for r in report["aside"].get("final_check_held.csv", [])]


def test_prompt_version_is_the_evaluated_one():
    """Moving the check must not move this: the pilot's codings are keyed on it."""
    assert fc.PROMPT_VERSION == "58aa27a6c868"


def test_an_unchanged_work_ships_without_an_entry():
    row = _row()
    report = _report(dict(row))
    counts = ex.apply_final_check(report, {"p1": row}, {})
    assert [r["pair_id"] for r in report["main"]] == ["p1"]
    assert counts["unchanged"] == 1 and counts["changed"] == 0


def test_a_change_the_check_cannot_see_does_not_reopen_the_work():
    row = _row()
    report = _report({**row, "link_evidence": "search_confirm: clearly_target"})
    assert ex.apply_final_check(report, {"p1": row}, {})["changed"] == 0


def test_a_new_work_with_no_entry_is_held_out_of_the_file():
    report = _report(_row())
    ex.apply_final_check(report, {}, {})
    assert report["main"] == [] and _held(report) == ["p1"]


def test_a_released_entry_ships_and_a_review_entry_does_not():
    row = _row()
    released = _report(dict(row))
    ex.apply_final_check(released, {}, {"W1": _entry([row], "released")})
    assert [r["pair_id"] for r in released["main"]] == ["p1"]

    review = _report(dict(row))
    ex.apply_final_check(review, {}, {"W1": _entry([row], "review")})
    assert review["main"] == [] and _held(review) == ["p1"]


def test_a_ship_decision_clears_a_review_and_a_hold_overrides_a_release():
    row = _row()
    shipped = _report(dict(row))
    ex.apply_final_check(shipped, {}, {"W1": _entry([row], "review", "ship")})
    assert [r["pair_id"] for r in shipped["main"]] == ["p1"]

    held = _report(dict(row))
    ex.apply_final_check(held, {}, {"W1": _entry([row], "released", "hold")})
    assert held["main"] == [] and _held(held) == ["p1"]


def test_a_later_change_voids_the_decision():
    decided = _entry([_row()], "review", "ship")
    report = _report(_row(outcome="failed"))
    counts = ex.apply_final_check(report, {}, {"W1": decided})
    assert report["main"] == [] and counts["decisions_voided"] == 1


def test_a_changed_sibling_reopens_the_whole_work():
    a, b = _row(pair="a", doi_o="10.1/a"), _row(pair="b", doi_o="10.1/b")
    report = _report(dict(a), {**b, "outcome": "failed"})
    ex.apply_final_check(report, {"a": a, "b": b}, {})
    assert _held(report) == ["a", "b"]


def test_a_held_work_that_shipped_keeps_its_committed_rows_and_retires_nothing():
    """The blocking case: setting the new rows aside alone would retire the old pair
    from the validation queue while a review is merely pending."""
    committed = _row()
    report = _report(_row(outcome="failed"), _row(work=2, pair="p2"))
    previous = {"p1": committed, "p2": _row(work=2, pair="p2")}
    counts = ex.apply_final_check(report, previous, {})

    assert [(r["pair_id"], r["outcome"]) for r in report["main"]] == [
        ("p1", "successful"), ("p2", "successful")]
    assert report["aside"]["final_check_held.csv"][0]["outcome"] == "failed"
    assert counts["held_shipped"] == 1
    assert ex.retirements(previous, report, baseline="HEAD")[0] == []


def test_a_held_work_whose_original_changed_does_not_retire_the_old_pair():
    committed = _row()
    report = _report(_row(pair="new", doi_o="10.1/other"))
    ex.apply_final_check(report, {"p1": committed}, {})
    assert [r["pair_id"] for r in report["main"]] == ["p1"]
    assert ex.retirements({"p1": committed}, report, baseline="HEAD")[0] == []


def test_a_rejected_work_is_set_aside_and_its_old_pair_retires():
    committed = _row()
    changed = _row(outcome="failed")
    report = _report(dict(changed))
    ex.apply_final_check(report, {"p1": committed}, {"W1": _entry([changed], "review", "reject")})
    assert report["main"] == []
    assert [r["pair_id"] for r in report["aside"]["final_check_rejected.csv"]] == ["p1"]
    entries, _ = ex.retirements({"p1": committed}, report, baseline="HEAD")
    assert [(e["pair_id"], e["reason"]) for e in entries] == [("p1", "set_aside")]


def test_an_unshipped_row_is_not_the_checks_business():
    row = _row(link_method="target_pending")
    report = _report(row)
    assert ex.apply_final_check(report, {}, {})["changed"] == 0
    assert report["main"] == [row]


def test_the_check_file_round_trips_and_refuses_an_unknown_decision(tmp_path):
    path = tmp_path / ex.FINAL_CHECK_NAME
    ex._write_final_check(path, {"W1": {"work": "W1", "decision": " Ship ",
                                        "pipeline": "two\nlines"}})
    entries = ex.read_final_check(path)
    assert entries["W1"]["decision"] == "ship" and entries["W1"]["pipeline"] == "two\nlines"

    ex._write_final_check(path, {"W1": {"work": "W1", "decision": "yes"}})
    with pytest.raises(SystemExit, match="ship, hold, reject"):
        ex.read_final_check(path)


def test_the_gate_reaches_the_file_through_export_main(tmp_path, monkeypatch):
    """Without an entry the rendered row is held; --skip-final-check ships it."""
    row = _row()
    monkeypatch.setattr(ex, "render", lambda *a, **k: {
        **_report(dict(row)), "works": 1, "rows": 1, "endings": {},
        "already_in_flora": 0, "superseded_generation": 0, "not_admitted": 0,
        "screen_discarded": 0, "screen_deferred": 0, "dropped": {},
        "validated_skip": (set(), set())})
    monkeypatch.setattr(ex, "ClaimsClient", lambda *a, **k: None)
    monkeypatch.setattr(ex, "open_pending_runs", lambda *a, **k: [])
    out = tmp_path / "extracted-test.csv"
    assert ex.main(["--all-releases", "--out", str(out)]) == 0
    assert "p1" not in out.read_text(encoding="utf-8-sig")
    assert "p1" in (tmp_path / "extracted-test-set-aside" / "final_check_held.csv").read_text(
        encoding="utf-8-sig")
    assert ex.main(["--all-releases", "--skip-final-check", "--out", str(out)]) == 0
    assert "p1" in out.read_text(encoding="utf-8-sig")


# ── The step's own logic ─────────────────────────────────────────────────────

def _coding(outcome: str = "successful", confident: bool = True) -> dict:
    return {"record_type": "replication", "study_status": "completed", "reasoning": "r",
            "targets": [{"authors": "Smith, A.", "year": "2001", "title": "An original finding",
                         "doi": "10.1/o", "identified_by": "citation", "evidence_quote": "",
                         "outcome": outcome, "outcome_quote": "It held.",
                         "outcome_quote_section": "abstract", "outcome_confident": confident}]}


def _checked(coding: dict, row: dict | None = None, document: str = "") -> dict | None:
    with patch.object(fc, "document_text", return_value=(document, "", {})), \
            patch.object(fc, "metadata_references", return_value=[]), \
            patch.object(fc, "recode", return_value={
                "coding": coding, "model": "claude-opus-5-5", "effort": "high",
                "prompt_version": fc.PROMPT_VERSION}):
        return fc.check_work("W1", [row or _row()], "claude-opus-5-5")


def test_agreement_releases_the_work_and_clears_the_gate():
    row = _row()
    entry = _checked(_coding())
    assert entry["status"] == "released" and entry["reason"] == ""
    report = _report(dict(row))
    ex.apply_final_check(report, {}, {"W1": entry})
    assert [r["pair_id"] for r in report["main"]] == ["p1"]


@pytest.mark.parametrize("coding, reason", [
    (_coding("failed"), "outcome"),
    (_coding(confident=False), "not confident"),
    ({**_coding(), "targets": []}, "originals: 1 vs 0"),
    ({**_coding(), "record_type": "not_a_replication"}, "type"),
    ({**_coding(), "study_status": "prospective"}, "prospective"),
])
def test_each_disagreement_goes_to_review_and_says_why(coding, reason):
    entry = _checked(coding)
    assert entry["status"] == "review" and reason in entry["reason"]


def test_a_work_whose_document_is_not_on_this_machine_is_not_coded():
    assert _checked(_coding(), _row(parse_method="grobid")) is None


def test_the_worklist_skips_an_answered_work_and_reopens_on_a_prompt_edit():
    rows = [_row()]
    answered = {**_entry(rows), "prompt_version": fc.PROMPT_VERSION}
    assert fc.worklist({"W1": rows}, {"W1": answered}) == {}
    assert fc.worklist({"W1": rows}, {"W1": {**answered, "prompt_version": "old"}}) == {"W1": rows}
    assert fc.worklist({"W1": rows}, {}) == {"W1": rows}


def _codex_output(*items: dict) -> str:
    events = [{"type": "item.completed", "item": item} for item in items]
    events.append({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}})
    return "\n".join(json.dumps(e) for e in events)


def _run_codex(stdout: str, tmp_path, monkeypatch, returncode: int = 0) -> dict:
    (tmp_path / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
    with patch.object(fc.subprocess, "run", return_value=done) as run:
        result = fc._call_codex("prompt", "gpt-6.1-sol")
    assert run.call_args.kwargs["env"]["CODEX_HOME"] != str(tmp_path)
    return result


def test_a_codex_coding_is_read_from_its_last_message(tmp_path, monkeypatch):
    out = _codex_output({"type": "reasoning", "text": "hm"},
                        {"type": "agent_message", "text": json.dumps(_coding())})
    result = _run_codex(out, tmp_path, monkeypatch)
    assert result["coding"]["record_type"] == "replication" and result["input_tokens"] == 5


def test_a_codex_coding_that_used_a_tool_is_refused(tmp_path, monkeypatch):
    out = _codex_output({"type": "command_execution", "command": "cat data/extracted.csv"},
                        {"type": "agent_message", "text": json.dumps(_coding())})
    with pytest.raises(RuntimeError, match="not a blind coding"):
        _run_codex(out, tmp_path, monkeypatch)


def test_openai_schema_requires_every_property_without_moving_the_prompt_version():
    version = fc.PROMPT_VERSION
    strict = fc._strict(fc.SCHEMA)
    item = strict["properties"]["targets"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    assert "outcome_robustness" not in fc.SCHEMA["properties"]["targets"]["items"]["required"]
    assert fc.PROMPT_VERSION == version


def test_three_failures_in_a_row_stop_the_run_and_save_what_it_has(tmp_path):
    todo = {f"W{i}": [_row(work=i, pair=f"p{i}")] for i in range(1, 6)}
    with patch.object(fc, "check_work", side_effect=RuntimeError("limit reached")):
        counts = fc.run_check(todo, {}, tmp_path / "final_check.csv", "claude-opus-5-5", 1)
    assert counts["failed"] == 3
    assert (tmp_path / "final_check.csv").exists()


def test_a_subscription_call_carries_no_api_key(tmp_path, monkeypatch):
    """A key in the child's environment could bill the API instead of the subscription."""
    for var in fc._API_KEY_VARS:
        monkeypatch.setenv(var, "secret")
    (tmp_path / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    codex = subprocess.CompletedProcess([], 0, stdout=_codex_output(
        {"type": "agent_message", "text": json.dumps(_coding())}), stderr="")
    claude = subprocess.CompletedProcess([], 0, stdout=json.dumps(
        {"structured_output": _coding()}), stderr="")
    with patch.object(fc.subprocess, "run", side_effect=[codex, claude]) as run:
        fc._call_codex("prompt", "gpt-6.1-sol")
        fc._call_claude("prompt", "claude-opus-5-5")
    for call in run.call_args_list:
        assert not set(fc._API_KEY_VARS) & set(call.kwargs["env"])


def test_a_codex_tool_call_that_never_completes_is_refused(tmp_path, monkeypatch):
    started = json.dumps({"type": "item.started", "item": {"type": "command_execution"}})
    out = started + "\n" + _codex_output(
        {"type": "agent_message", "text": json.dumps(_coding())})
    with pytest.raises(RuntimeError, match="not a blind coding"):
        _run_codex(out, tmp_path, monkeypatch)


def test_the_check_renders_with_the_flags_the_export_will_use(monkeypatch, tmp_path):
    """Otherwise rows the export ships under the flag are held and never on the worklist."""
    seen = {}

    def _render(*a, **k):
        seen.update(k)
        return _report(_row())

    monkeypatch.setattr(fc, "admitted_work_ids", lambda *a, **k: ("r" * 12, set(), {}))
    monkeypatch.setattr(fc, "ClaimsClient", lambda *a, **k: None)
    monkeypatch.setattr(fc, "render", _render)
    out = tmp_path / "extracted-test.csv"
    assert fc.main(["--include-unconfirmed-search", "--current-generation-only",
                    "--out", str(out)]) == 0
    assert seen["include_unconfirmed_search"] and seen["current_generation_only"]


@pytest.mark.parametrize("fields, skipped", [
    ({"pdf_source": "unpaywall"}, True), ({"parse_method": "grobid"}, True),
    ({"pdf_source": "none", "parse_method": "none"}, False), ({}, False)])
def test_only_a_row_that_acquired_a_document_needs_one(fields, skipped):
    assert (_checked(_coding(), _row(**fields)) is None) is skipped
