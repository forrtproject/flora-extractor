"""Tests for the cheap discard-only tier (issue #130).

The question it asks and the bypasses that refuse to let a 3B model end a row live
in `shared/prescreen.py`; the gate over its two votes lives in the tier runner,
`filter/engine/tiers._cheap_judge`, because the tier runs in Stage 2. Both halves
are exercised here, plus the set-aside plumbing a discard still travels through.

The tier is DORMANT: all three `screen_cheap` specs ship `shadow: true`, so no live
row reaches it. These tests are what keeps promoting one spec the only step needed
to wake it.

One test per seam. The tier can only lose papers, so every test here is really the
same question: does this row survive when anything at all goes wrong?
"""
from unittest.mock import patch

import pandas as pd
import pytest

import extract.sanity_check as sc
from filter.engine import tiers
from shared import prescreen as ps
from shared.schema import EXTRACTED_COLS


def _judge(abstract: str = "y" * 400, title: str = "Title", source: str = "",
           doi: str = "10.1/x"):
    """One work through the tier's judge — the gate, over `prescreen_vote()`."""
    return tiers._cheap_judge(
        tiers.Work(1, doi, title, abstract, "screen_cheap", source))


# ── the deterministic override ───────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("The purpose of this prospective, double-blinded, multirater, systematic "
     "replication study was to validate the protocol.", True),
    ("We replicated and extended the counterdispositional findings of Fleeson (2002).", True),
    ("We reproduce the results of Doe (2015) using the original data.", True),
    ("A direct replication of Smith et al.", True),
    ("This study examines the effect of market concentration on firm entry.", False),

    # The 16 widened patterns (issue #168), derived on 7,505 FLoRA papers and reported
    # on a held-out half: evidence in analysis/prescreen_eval/OVERRIDE_EVAL.md. One
    # phrase that must trip and one near-miss that must not, for five of them.
    ("This study replicates the association in an independent cohort.", True),
    ("This virus study describes how the pathogen replicates within host cells.", False),
    ("A failed replication is reported alongside two new experiments.", True),
    ("Successful reproduction of coral colonies in captivity requires stable "
     "temperature.", False),
    ("A replication of Smith and Jones (1998) in a nationally representative sample.",
     True),
    ("Replication of viral DNA proceeds bidirectionally in infected cells.", False),
    ("The original effect did not replicate in our data.", True),
    ("Discovery and replication of loci associated with height.", True),
])
def test_hard_signal_catches_papers_that_state_their_design(text, expected):
    """The Suiter case: an abstract that says "systematic replication study" was
    discarded by both cheap models. A phrase that explicit must never depend on them.

    The near-misses are the molecular sense of the same eleven characters. They are the
    line the widened patterns had to hold: bare "replication of" (tier C in
    OVERRIDE_EVAL.md) would have caught every one of them and disabled the tier by
    regex, so it was not shipped.
    """
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
    (("no", "no"), tiers.DISCARD, 2),
    (("yes", "no"), tiers.PROCEED, 1),  # voter 2 is never asked once the row is safe
    (("no", "yes"), tiers.PROCEED, 2),
    ((None, "no"), tiers.PROCEED, 1),   # a provider failure is not a "no"
    (("no", None), tiers.PROCEED, 2),
])
def test_only_two_explicit_noes_discard(answers, verdict, calls, tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with _votes(*answers) as call:
        outcome, votes = _judge()
    assert outcome == verdict
    assert call.call_count == calls
    assert len(votes) == calls


def test_an_unreadable_label_is_not_a_discard(tmp_path, monkeypatch):
    """A model that answers "maybe", or anything outside the two legal values, has not
    said the paper is out of scope — collapsing that to "no" would be a silent loss."""
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "call_model",
                      return_value=({"maybe_replication": "maybe"}, "openrouter", "")):
        assert _judge()[0] == tiers.PROCEED


def test_a_non_answer_is_never_cached(tmp_path, monkeypatch):
    """A 503 is "ask again", not "this paper is out of scope"."""
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "call_model", side_effect=[(None, "openrouter", "503"),
                                                 ({"maybe_replication": "no"}, "openrouter", "")]):
        _judge()
    with patch.object(ps, "call_model", side_effect=[({"maybe_replication": "no"}, "openrouter", ""),
                                                 ({"maybe_replication": "no"}, "openrouter", "")]) as call:
        assert _judge()[0] == tiers.DISCARD
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

    moved = sc.run_sanity_check(ex, deep=False)["flagged"]

    assert moved["prescreen_discard"] == 1
    assert moved["not_a_replication"] == 1


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
    # A fully screened row, as Stage 2's handoff writes it.
    row["screen_verdict"] = "proceed"
    row["screen_record_type"] = "replication"
    row["screen_votes"] = "gemini=replication/confident|openai=replication/confident"
    with patch.object(ps, "call_model") as call, \
         patch.object(rx, "run_for_doi", return_value={"resolution_method":
                                                       "target_pending"}):
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
    """A transport returning something unexpected must cost this row its saving,
    never the run and never the paper."""
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "call_model", return_value=(broken, "openrouter", "")):
        assert _judge()[0] == tiers.PROCEED


def test_a_corrupt_cache_entry_proceeds(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "read_cache", return_value=["junk"]), \
         patch.object(ps, "call_model", return_value=(None, "openrouter", "down")):
        assert _judge()[0] == tiers.PROCEED


def test_a_bypassed_row_is_a_verdict_and_asks_no_model(tmp_path, monkeypatch):
    """Deciding not to ask is a decision: the bypass is recorded as this work's
    verdict, which is also its checkpoint."""
    monkeypatch.setattr(ps, "LLM_CACHE_DIR", tmp_path)
    with patch.object(ps, "call_model") as call:
        outcome, votes = _judge(abstract="A direct replication of Smith et al. " * 20)
    call.assert_not_called()
    assert outcome == tiers.PROCEED
    assert [v["model"] for v in votes] == ["prescreen_bypass"]


def test_a_prescreen_discard_is_one_of_the_screen_verdict_set_asides():
    """Historical rows carry `prescreen_discard`, and the file they are parked in is
    one of the screen's own verdicts — the set a new screening generation reopens."""
    from shared.schema import SCREEN_SET_ASIDE_FILES
    assert "prescreen_discard.csv" in SCREEN_SET_ASIDE_FILES
