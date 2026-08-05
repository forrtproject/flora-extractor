"""Tests for the optional cheap pre-screen (shared/prescreen.py, issue #130).

One test per seam. The tier can only lose papers, so every test here is really the
same question: does this row survive when anything at all goes wrong?
"""
from unittest.mock import patch

import pandas as pd
import pytest

import extract.sanity_check as sc
from shared import prescreen as ps
from shared.schema import EXTRACTED_COLS


# ── the deterministic override ───────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("The purpose of this prospective, double-blinded, multirater, systematic "
     "replication study was to validate the protocol.", True),
    ("We replicated and extended the counterdispositional findings of Fleeson (2002).", True),
    ("We reproduce the results of Doe (2015) using the original data.", True),
    ("A direct replication of Smith et al.", True),
    ("This study examines the effect of market concentration on firm entry.", False),
])
def test_hard_signal_catches_papers_that_state_their_design(text, expected):
    """The Suiter case: an abstract that says "systematic replication study" was
    discarded by both cheap models. A phrase that explicit must never depend on them."""
    assert bool(ps.hard_signal("", text)) is expected


def test_curated_and_short_rows_are_never_pre_screened():
    long = "y" * 400
    assert ps.prescreen_bypass("t", long, "i4r").startswith("curated:")
    assert ps.prescreen_bypass("t", "too short") == "short_text"
    assert ps.prescreen_bypass("t", long, "openalex") == ""


# ── the gate ─────────────────────────────────────────────────────────────────

def _votes(*answers):
    """Patch each voter's raw call to return the given answers in order."""
    replies = [({"maybe_replication": a}, "openrouter", "") if a else (None, "openrouter", "boom")
               for a in answers]
    return patch.object(ps, "call_model", side_effect=replies)


@pytest.mark.parametrize("answers,verdict,calls", [
    (("no", "no"), "discard", 2),
    (("yes", "no"), "proceed", 1),      # voter 2 is never asked once the row is safe
    (("no", "yes"), "proceed", 2),
    ((None, "no"), "proceed", 1),       # a provider failure is not a "no"
    (("no", None), "proceed", 2),
])
def test_only_two_explicit_noes_discard(answers, verdict, calls, tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with _votes(*answers) as call:
        out = ps.prescreen("10.1/x", "Title", "y" * 400)
    assert out["verdict"] == verdict
    assert call.call_count == calls


def test_an_unreadable_label_is_not_a_discard(tmp_path, monkeypatch):
    """A model that answers "maybe", or anything outside the two legal values, has not
    said the paper is out of scope — collapsing that to "no" would be a silent loss."""
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "call_model",
                      return_value=({"maybe_replication": "maybe"}, "openrouter", "")):
        assert ps.prescreen("10.1/x", "T", "y" * 400)["verdict"] == "proceed"


def test_a_non_answer_is_never_cached(tmp_path, monkeypatch):
    """A 503 is "ask again", not "this paper is out of scope"."""
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "call_model", side_effect=[(None, "openrouter", "503"),
                                                 ({"maybe_replication": "no"}, "openrouter", "")]):
        ps.prescreen("10.1/x", "T", "y" * 400)
    with patch.object(ps, "call_model", side_effect=[({"maybe_replication": "no"}, "openrouter", ""),
                                                 ({"maybe_replication": "no"}, "openrouter", "")]) as call:
        assert ps.prescreen("10.1/x", "T", "y" * 400)["verdict"] == "discard"
    assert call.call_count == 2   # the failed vote was re-asked, the good one was not


# ── the row it writes ────────────────────────────────────────────────────────

def test_a_discard_is_quarantined_separately_from_the_validated_screen(tmp_path, monkeypatch):
    """not_a_replication.csv means the validated pair settled the paper. A pre-screen
    discard writes the same outcome but is a weaker instrument, so any precision
    computed over that file must not have to disentangle the two."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"
    df = pd.DataFrame([
        {"doi_r": "10.1/pre", "outcome": "not_a_replication", "openalex_id_r": "W1",
         "link_method": "prescreen_discard"},
        {"doi_r": "10.1/nar", "outcome": "not_a_replication", "openalex_id_r": "W2",
         "link_method": "not_a_replication"},
    ])
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    df[EXTRACTED_COLS].to_csv(ex, index=False, encoding="utf-8-sig")

    moved = sc.run_sanity_check(ex, move=True, deep=False)["moved"]

    assert moved["prescreen_discard"] == 1
    assert moved["not_a_replication"] == 1
    pre = pd.read_csv(tmp_path / "prescreen_discard.csv", dtype=str, keep_default_na=False)
    assert set(pre["doi_r"]) == {"10.1/pre"}


def test_stage3_never_runs_the_cheap_tier(tmp_path, monkeypatch):
    """Which rows get the cheap tier is a Stage 2 routing decision (the screen_cheap
    pile). Stage 3 must not re-apply it: a row that reached the front door was routed
    past it, and re-gating here would override the rule book on exactly the rows it
    sent to the expensive tier."""
    import extract.run_extract as rx

    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    row = pd.Series({"doi_r": "10.1/x", "title_r": "Market entry",
                     "abstract_r": "We estimate the effect of concentration. " * 8,
                     "filter_status": "replication", "source": "openalex"})
    screen = {"resolution_method": "llm_refscreen_failed", "screen_verdict": "",
              "votes": [], "categories": [], "llm_error": "boom",
              "llm_reasoning": "", "llm_model": "m"}
    with patch.object(ps, "call_model") as call, \
         patch.object(rx, "classify_replication", return_value=screen):
        out = rx._process_row(row, "10.1/x", no_llm=False, no_pdf=True,
                              no_reproductions=False, resolved_only=False,
                              recalibrate_outcomes=False)

    call.assert_not_called()
    assert all(r["link_method"] != "prescreen_discard" for r in out)


def test_one_model_configured_twice_is_refused():
    """Both voters on one model would hit one cache key: the second call replays the
    first's "no" and a single answer becomes a terminal discard the row reports as two
    voters agreeing."""
    with patch.object(ps, "PRESCREEN_MODEL_1", "a/b"), \
         patch.object(ps, "PRESCREEN_MODEL_2", "a/b"):
        with pytest.raises(RuntimeError, match="different models"):
            ps.prescreen_voters()


@pytest.mark.parametrize("broken", [
    ["not", "a", "dict"],       # a transport that returns a list
    "a bare string",
    None,
])
def test_a_malformed_answer_proceeds_instead_of_crashing(broken, tmp_path, monkeypatch):
    """The tier is optional; a transport returning something unexpected must cost this
    row its saving, never the run and never the paper."""
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "call_model", return_value=(broken, "openrouter", "")):
        assert ps.prescreen("10.1/x", "T", "y" * 400)["verdict"] == "proceed"


def test_a_corrupt_cache_entry_proceeds(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "read_cache", return_value=["junk"]), \
         patch.object(ps, "call_model", return_value=(None, "openrouter", "down")):
        assert ps.prescreen("10.1/x", "T", "y" * 400)["verdict"] == "proceed"


def test_a_reopened_paper_is_purged_from_its_old_discard_file(tmp_path, monkeypatch):
    """--rescreen reopens a discard; the paper comes back target_pending and is filed in
    target_pending.csv. The old key must not stay in prescreen_discard.csv, or the next
    ordinary resume reads it as settled and strands the paper permanently."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)

    def write(path, rows):
        df = pd.DataFrame(rows)
        for c in EXTRACTED_COLS:
            if c not in df.columns:
                df[c] = ""
        df[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")

    write(tmp_path / "prescreen_discard.csv",
          [{"doi_r": "10.1/reopened", "openalex_id_r": "W9",
            "link_method": "prescreen_discard", "outcome": "not_a_replication"}])
    ex = tmp_path / "extracted.csv"
    write(ex, [{"doi_r": "10.1/reopened", "openalex_id_r": "W9",
                "link_method": "target_pending", "outcome": "pending"}])

    sc.run_sanity_check(ex, move=True, deep=False)

    left = pd.read_csv(tmp_path / "prescreen_discard.csv", dtype=str, keep_default_na=False)
    assert left.empty, "the reopened paper is still recorded as a pre-screen discard"
    pending = pd.read_csv(tmp_path / "target_pending.csv", dtype=str, keep_default_na=False)
    assert set(pending["doi_r"]) == {"10.1/reopened"}


def test_purging_leaves_untouched_set_aside_rows_alone(tmp_path, monkeypatch):
    """not_a_replication.csv also holds the non_article buckets, whose rows carry any
    link_method. Being in a file is not evidence of belonging there, so a paper this
    pass did not re-file must survive whatever its verdict looks like."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)

    def write(path, rows):
        df = pd.DataFrame(rows)
        for c in EXTRACTED_COLS:
            if c not in df.columns:
                df[c] = ""
        df[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")

    # a non_article row: filed under not_a_replication.csv but carrying a link method
    # that has nothing to do with the screen
    write(tmp_path / "not_a_replication.csv",
          [{"doi_r": "10.6084/m9.figshare.1", "openalex_id_r": "W1",
            "link_method": "llm_fulltext", "outcome": "success"}])
    ex = tmp_path / "extracted.csv"
    write(ex, [{"doi_r": "10.1/other", "openalex_id_r": "W2",
                "link_method": "target_pending", "outcome": "pending"}])

    sc.run_sanity_check(ex, move=True, deep=False)

    kept = pd.read_csv(tmp_path / "not_a_replication.csv", dtype=str, keep_default_na=False)
    assert set(kept["doi_r"]) == {"10.6084/m9.figshare.1"}


def test_rescreen_can_reopen_a_prescreen_discard():
    """Turning the flag off does not reopen discards — a resume treats a set-aside row
    as settled. --rescreen is the only way back, so the value must be in its set."""
    from extract.run_extract import SCREEN_SET_ASIDE_FILES, SCREEN_SET_ASIDE_METHODS
    assert "prescreen_discard" in SCREEN_SET_ASIDE_METHODS
    assert "prescreen_discard.csv" in SCREEN_SET_ASIDE_FILES
