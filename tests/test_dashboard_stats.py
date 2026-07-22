"""
Tests for the dashboard aggregations added for the stage-by-stage stats redesign:
the Stage-2 rule-exit classifier, the Stage-3 replication/reproduction outcome
split, and the Supabase per-field validator agreement logic.

These are all branch-heavy derivations over data the pipeline never labels
explicitly, so they are the parts most likely to drift silently.
"""
import pandas as pd
import pytest

from shared import supabase_client as supa
from shared.dashboard_cache import _compute_extracted_stats, classify_rule_exit


# ── Stage 2: rule exit recovered from filter_evidence ────────────────────────

@pytest.mark.parametrize("evidence,expected", [
    ("exclusion:dna replication",                      "r1_exclusion"),
    ("no replication phrase detected",                 "r2_no_phrase"),
    ("phrase:direct replication; no author-year cite", "r3_no_cite"),
    ("phrase:we replicated; no same-sentence cite",    "r4_no_same_sentence"),
    ("phrase:replication of; cite:(Smith, 2011)",      "r5_pass"),
    ("",                                               "unknown"),
    (None,                                             "unknown"),
])
def test_classify_rule_exit(evidence, expected):
    assert classify_rule_exit(evidence) == expected


def test_rule_exit_survives_llm_evidence_prepend():
    """run_filter prepends the rule evidence to the LLM verdict — the marker must
    still be recoverable, otherwise every LLM-touched row falls into 'unknown'."""
    assert classify_rule_exit(
        "phrase:we replicate; no author-year cite | llm:replicates Smith (2011)"
    ) == "r3_no_cite"


# ── Stage 3: outcome split by record type ────────────────────────────────────

def test_outcome_split_by_type():
    df = pd.DataFrame([
        {"type": "replication",  "outcome": "success",  "year_r": "2020"},
        {"type": "replication",  "outcome": "failure",  "year_r": "2020"},
        {"type": "Reproduction", "outcome": "computationally successful, robust", "year_r": "2021"},
        {"type": "reproduction", "outcome": "cannot_be_determined", "year_r": ""},
    ])
    stats = _compute_extracted_stats(df)

    assert stats["by_type"] == {"replication": 2, "reproduction": 2}
    # Reproduction outcomes must not leak into the replication distribution.
    assert stats["by_outcome_replication"] == {"success": 1, "failure": 1}
    assert stats["by_outcome_reproduction"] == {
        "computationally successful, robust": 1, "cannot_be_determined": 1,
    }
    assert stats["by_year"] == {"2020": 2, "2021": 1}


def test_year_counts_drop_junk():
    df = pd.DataFrame([{"year_r": y, "outcome": "success", "type": "replication"}
                       for y in ["2020", "2020", "n/a", "", "20xx", "2019.0"]])
    # "2019.0" truncates to a valid 2019; the rest are dropped.
    assert _compute_extracted_stats(df)["by_year"] == {"2020": 2, "2019": 1}


# ── Stage 4: validator agreement ─────────────────────────────────────────────

def _judge(type_c="correct", orig_c="correct", out_c="correct", **corrections):
    return {"type_check": type_c, "original_check": orig_c, "outcome_check": out_c,
            **corrections}


def _analytics(rows, monkeypatch):
    monkeypatch.setattr(supa, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(supa, "_get", lambda table, params=None: rows)
    supa._CACHE.clear()
    return supa.get_validation_analytics()


def test_agreement_both_kept_and_both_changed(monkeypatch):
    rows = [
        # both humans keep the outcome
        {"record_id": "a", "outcome": "success",
         "validator_1": _judge(), "validator_2": _judge()},
        # both change it to the SAME replacement
        {"record_id": "b", "outcome": "success",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(out_c="incorrect", corrected_outcome="Mixed")},
        # both change it to DIFFERENT replacements
        {"record_id": "c", "outcome": "success",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(out_c="incorrect", corrected_outcome="failure")},
    ]
    out = _analytics(rows, monkeypatch)["agreement"]["outcome"]

    assert out["n"] == 3
    assert out["both_kept"] == 1
    assert out["both_changed"] == 2
    assert out["both_changed_same"] == 1     # case-insensitive match
    assert out["both_changed_differ"] == 1


def test_agreement_split_vote_uses_llm_as_the_tiebreak_signal(monkeypatch):
    rows = [
        # v1 changed, v2 kept, LLM says incorrect → LLM sided with the changer
        {"record_id": "a", "outcome": "success",
         "validator_1": _judge(out_c="incorrect"), "validator_2": _judge(),
         "llm_validator": _judge(out_c="incorrect")},
        # v1 kept, v2 changed, LLM says correct → LLM sided with the keeper
        {"record_id": "b", "outcome": "success",
         "validator_1": _judge(), "validator_2": _judge(out_c="incorrect"),
         "llm_validator": _judge(out_c="correct")},
        # split with no LLM slot at all
        {"record_id": "c", "outcome": "success",
         "validator_1": _judge(out_c="incorrect"), "validator_2": _judge()},
    ]
    out = _analytics(rows, monkeypatch)["agreement"]["outcome"]

    assert out["one_changed"] == 3
    assert out["llm_with_changer"] == 1
    assert out["llm_with_keeper"] == 1
    assert out["llm_absent"] == 1


def test_agreement_ignores_records_missing_a_human_slot(monkeypatch):
    rows = [
        {"record_id": "a", "outcome": "success", "validator_1": _judge()},
        {"record_id": "b", "outcome": "success"},
        # a filled slot with no verdict on this field must not count either
        {"record_id": "c", "outcome": "success",
         "validator_1": _judge(out_c=None), "validator_2": _judge()},
    ]
    result = _analytics(rows, monkeypatch)

    assert result["agreement"]["outcome"]["n"] == 0
    assert result["both_humans"] == 1        # record c has both slots filled
    assert result["at_least_one_vote"] == 2  # records a and c
    assert result["slots_filled"]["0"] == 1


def test_final_vs_pipeline_counts_changes_per_field(monkeypatch):
    rows = [
        {"record_id": "a", "type": "replication", "outcome": "success", "doi_o": "10.1/x",
         "final_type": "replication", "final_outcome": "mixed", "final_doi_o": "10.1/X"},
        {"record_id": "b", "type": "replication", "outcome": "success", "doi_o": "10.1/y",
         "final_type": "reproduction", "final_outcome": "success"},
    ]
    fvp = _analytics(rows, monkeypatch)["final_vs_pipeline"]

    assert fvp["type"] == {"n": 2, "kept": 1, "changed": 1}
    assert fvp["outcome"] == {"n": 2, "kept": 1, "changed": 1}
    # Only record a was finalised on the DOI, and the change is casing only.
    assert fvp["original"] == {"n": 1, "kept": 1, "changed": 0}


def test_resolution_tracks_what_the_reviewer_finally_did(monkeypatch):
    rows = [
        # split vote; reviewer kept the pipeline value → sided with the keeper
        {"record_id": "a", "outcome": "success", "validation_status": "validated",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(), "final_outcome": "success"},
        # split vote; reviewer took the changer's replacement
        {"record_id": "b", "outcome": "success", "validation_status": "validated",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(), "final_outcome": "mixed"},
        # split vote; reviewer picked something neither proposed
        {"record_id": "c", "outcome": "success", "validation_status": "validated",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(), "final_outcome": "failure"},
        # split vote, record rejected outright
        {"record_id": "d", "outcome": "success", "validation_status": "rejected",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge()},
        # split vote still awaiting the reviewer
        {"record_id": "e", "outcome": "success", "validation_status": "consensus_reached",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge()},
    ]
    res = _analytics(rows, monkeypatch)["agreement"]["outcome"]["resolution"]["one_changed"]

    assert res["reviewer_kept_pipeline"]   == 1
    assert res["reviewer_took_correction"] == 1
    assert res["reviewer_took_other"]      == 1
    assert res["excluded"]                 == 1
    assert res["pending"]                  == 1


def test_resolution_treats_not_validation_as_excluded(monkeypatch):
    rows = [{"record_id": "a", "outcome": "success", "validation_status": "validated",
             "validator_1": _judge(), "validator_2": _judge(),
             "final_type": "not_validation", "final_outcome": "success"}]
    res = _analytics(rows, monkeypatch)["agreement"]["outcome"]["resolution"]["both_kept"]

    assert res["excluded"] == 1
    assert res["reviewer_kept_pipeline"] == 0


# ── Stage 1 phrase yield ─────────────────────────────────────────────────────

def test_phrase_yield_attributes_cursor_files(tmp_path, monkeypatch):
    import json as _json

    import search.openalex_search as oas

    monkeypatch.setattr(oas, "OA_CACHE_DIR", tmp_path)
    phrase = oas.SEARCH_PHRASES[0]
    for years, n in [((2020, 2020), 7), ((2021, 2021), 5)]:
        key = oas._job_key(phrase, *years)
        (tmp_path / f"{key}.cursor.json").write_text(
            _json.dumps({"total_fetched": n, "completed": True}), encoding="utf-8")
    (tmp_path / "deadbeef.cursor.json").write_text('{"total_fetched": 99}', encoding="utf-8")

    out = oas.phrase_yield()
    row = next(r for r in out["rows"] if r["phrase"] == phrase)

    assert row["fetched"] == 12          # summed across both year jobs
    assert row["jobs"] == 2
    assert out["total_fetched"] == 12    # the unmatched file is not counted
    assert out["unattributed_files"] == 1


def test_search_phrases_endpoint_survives_a_missing_cache(tmp_path, monkeypatch):
    """cache/ is gitignored, so a deployed instance has no cursor files — the
    endpoint must fall back to the phrase yield persisted in stats.json."""
    import search.openalex_search as oas
    import validate.routes.dashboard as dash

    monkeypatch.setattr(oas, "OA_CACHE_DIR", tmp_path)   # empty: live scan yields 0
    monkeypatch.setattr(dash, "load_stats", lambda: {"candidates": {"by_phrase": {
        "rows": [{"phrase": "replication of", "fetched": 42, "jobs": 1, "source": "phrase"}],
        "total_fetched": 42, "unattributed_files": 0,
    }}})

    from validate.app import create_app
    client = create_app().test_client()
    data = client.get("/api/dashboard/search-phrases").get_json()

    assert data["_source"] == "stats_json"
    assert data["total_fetched"] == 42


# ── Set-aside CSVs ───────────────────────────────────────────────────────────

def test_set_registry_files_resolve(tmp_path):
    """Every registered set must name a real file under data/ — a typo would show
    an empty tab rather than an error."""
    from shared.config import DATA_DIR
    from validate.routes.dashboard import SET_FILES

    assert SET_FILES, "registry is empty"
    for key, spec in SET_FILES.items():
        assert {"title", "file", "why", "action"} <= set(spec), f"{key} missing keys"
        assert (DATA_DIR / spec["file"]).suffix == ".csv"


def test_set_reader_drops_phantom_unnamed_columns(tmp_path, monkeypatch):
    """Hand-maintained CSVs carry trailing commas; pandas turns them into empty
    'Unnamed: N' columns that must not be reported as real fields."""
    import validate.routes.dashboard as dash

    csv = tmp_path / "phantom.csv"
    csv.write_text("doi_r,note,,,\n10.1/a,hello,,,\n", encoding="utf-8-sig")
    monkeypatch.setitem(dash.SET_FILES, "_t", {"title": "T", "file": "phantom.csv",
                                               "why": "w", "action": "a"})
    monkeypatch.setattr(dash, "DATA_DIR", tmp_path)

    df = dash._read_set("_t")
    assert list(df.columns) == ["doi_r", "note"]


def test_validator_slot_prefixes_match_the_writer():
    """csv_to_db writes these slot names; the drilldown flattening must agree or
    the per-validator columns silently render blank."""
    from extract.csv_to_db import _VALIDATOR_SLOTS
    assert set(supa._SLOT_PREFIX) == set(_VALIDATOR_SLOTS)
