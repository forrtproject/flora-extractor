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
    ("rule:phrase-with-cite; phrase:direct replication", "engine_route"),
    ("rule:dataset-type",                              "engine_route"),
    ("",                                               "unknown"),
    (None,                                             "unknown"),
])
def test_classify_rule_exit(evidence, expected):
    assert classify_rule_exit(evidence) == expected


def test_rule_exit_survives_llm_evidence_prepend():
    """The retired per-row Stage 2 prepended its rule evidence to the LLM verdict.
    Rows written that way are still on disk, so the marker must stay recoverable —
    otherwise every LLM-touched historical row falls into 'unknown'."""
    assert classify_rule_exit(
        "phrase:we replicate; no author-year cite | llm:replicates Smith (2011)"
    ) == "r3_no_cite"


# ── Stage 3: outcome split by record type ────────────────────────────────────

def test_outcome_split_by_type():
    df = pd.DataFrame([
        {"type": "replication",  "outcome": "successful",  "year_r": "2020"},
        {"type": "replication",  "outcome": "failed",  "year_r": "2020"},
        {"type": "Reproduction", "outcome": "computationally successful, robust", "year_r": "2021"},
        {"type": "reproduction", "outcome": "cannot_be_determined", "year_r": ""},
    ])
    stats = _compute_extracted_stats(df)

    assert stats["by_type"] == {"replication": 2, "reproduction": 2}
    # Reproduction outcomes must not leak into the replication distribution.
    assert stats["by_outcome_replication"] == {"successful": 1, "failed": 1}
    assert stats["by_outcome_reproduction"] == {
        "computationally successful, robust": 1, "cannot_be_determined": 1,
    }
    assert stats["by_year"] == {"2020": 2, "2021": 1}


def test_year_counts_drop_junk():
    df = pd.DataFrame([{"year_r": y, "outcome": "successful", "type": "replication"}
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
        {"record_id": "a", "outcome": "successful",
         "validator_1": _judge(), "validator_2": _judge()},
        # both change it to the SAME replacement
        {"record_id": "b", "outcome": "successful",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(out_c="incorrect", corrected_outcome="Mixed")},
        # both change it to DIFFERENT replacements
        {"record_id": "c", "outcome": "successful",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(out_c="incorrect", corrected_outcome="failed")},
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
        {"record_id": "a", "outcome": "successful",
         "validator_1": _judge(out_c="incorrect"), "validator_2": _judge(),
         "llm_validator": _judge(out_c="incorrect")},
        # v1 kept, v2 changed, LLM says correct → LLM sided with the keeper
        {"record_id": "b", "outcome": "successful",
         "validator_1": _judge(), "validator_2": _judge(out_c="incorrect"),
         "llm_validator": _judge(out_c="correct")},
        # split with no LLM slot at all
        {"record_id": "c", "outcome": "successful",
         "validator_1": _judge(out_c="incorrect"), "validator_2": _judge()},
    ]
    out = _analytics(rows, monkeypatch)["agreement"]["outcome"]

    assert out["one_changed"] == 3
    assert out["llm_with_changer"] == 1
    assert out["llm_with_keeper"] == 1
    assert out["llm_absent"] == 1


def test_agreement_ignores_records_missing_a_human_slot(monkeypatch):
    rows = [
        {"record_id": "a", "outcome": "successful", "validator_1": _judge()},
        {"record_id": "b", "outcome": "successful"},
        # a filled slot with no verdict on this field must not count either
        {"record_id": "c", "outcome": "successful",
         "validator_1": _judge(out_c=None), "validator_2": _judge()},
    ]
    result = _analytics(rows, monkeypatch)

    assert result["agreement"]["outcome"]["n"] == 0
    assert result["both_humans"] == 1        # record c has both slots filled
    assert result["at_least_one_vote"] == 2  # records a and c
    assert result["slots_filled"]["0"] == 1


def test_final_vs_pipeline_counts_changes_per_field(monkeypatch):
    rows = [
        {"record_id": "a", "type": "replication", "outcome": "successful", "doi_o": "10.1/x",
         "final_type": "replication", "final_outcome": "mixed", "final_doi_o": "10.1/X"},
        {"record_id": "b", "type": "replication", "outcome": "successful", "doi_o": "10.1/y",
         "final_type": "reproduction", "final_outcome": "successful"},
    ]
    fvp = _analytics(rows, monkeypatch)["final_vs_pipeline"]

    assert fvp["type"] == {"n": 2, "kept": 1, "changed": 1}
    assert fvp["outcome"] == {"n": 2, "kept": 1, "changed": 1}
    # Only record a was finalised on the DOI, and the change is casing only.
    assert fvp["original"] == {"n": 1, "kept": 1, "changed": 0}


def test_resolution_tracks_what_the_reviewer_finally_did(monkeypatch):
    rows = [
        # split vote; reviewer kept the pipeline value → sided with the keeper
        {"record_id": "a", "outcome": "successful", "validation_status": "validated",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(), "final_outcome": "successful"},
        # split vote; reviewer took the changer's replacement
        {"record_id": "b", "outcome": "successful", "validation_status": "validated",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(), "final_outcome": "mixed"},
        # split vote; reviewer picked something neither proposed
        {"record_id": "c", "outcome": "successful", "validation_status": "validated",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge(), "final_outcome": "failed"},
        # split vote, record rejected outright
        {"record_id": "d", "outcome": "successful", "validation_status": "rejected",
         "validator_1": _judge(out_c="incorrect", corrected_outcome="mixed"),
         "validator_2": _judge()},
        # split vote still awaiting the reviewer
        {"record_id": "e", "outcome": "successful", "validation_status": "consensus_reached",
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
    rows = [{"record_id": "a", "outcome": "successful", "validation_status": "validated",
             "validator_1": _judge(), "validator_2": _judge(),
             "final_type": "not_validation", "final_outcome": "successful"}]
    res = _analytics(rows, monkeypatch)["agreement"]["outcome"]["resolution"]["both_kept"]

    assert res["excluded"] == 1
    assert res["reviewer_kept_pipeline"] == 0


# ── Stage 1: the survivor pool ───────────────────────────────────────────────

def _write_pool(pool_dir, parts):
    """A minimal survivor pool: the _POOL_SCHEMA columns the stats read."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    pool_dir.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([("id", pa.string()), ("doi", pa.string()),
                        ("publication_year", pa.int32()),
                        ("hit_token_title", pa.bool_()),
                        ("hit_token_abstract", pa.bool_()),
                        ("hit_concept", pa.bool_())])
    for i, rows in enumerate(parts):
        pq.write_table(pa.Table.from_pylist(rows, schema=schema),
                       pool_dir / f"part-2016-0{i}.parquet")


def _clear_pool_memo():
    import shared.dashboard_cache as dc
    dc._totals_memo = None


def test_pool_stats_count_rows_years_and_gate_arms(tmp_path):
    """The pool replaces candidates.csv as Stage 1's artifact: totals come from the
    parquet footers, and the three hit_* booleans are why each row was kept."""
    from shared.dashboard_cache import compute_pool_stats

    _write_pool(tmp_path / "pool", [
        [{"id": "W1", "doi": "10.1/a", "publication_year": 2015,
          "hit_token_title": True, "hit_token_abstract": False, "hit_concept": True},
         {"id": "W2", "doi": "", "publication_year": 2016,
          "hit_token_title": False, "hit_token_abstract": True, "hit_concept": True}],
        [{"id": "W3", "doi": None, "publication_year": 1899,
          "hit_token_title": False, "hit_token_abstract": True, "hit_concept": False}],
    ])
    _clear_pool_memo()
    stats = compute_pool_stats(tmp_path / "pool")

    assert stats["total"] == 3 and stats["files"] == 2
    assert stats["no_doi"] == 2, "blank and null DOIs both count"
    assert stats["by_year"] == {"2015": 1, "2016": 1, "1899": 1}
    # A row can hit several arms, so these deliberately sum to more than 3.
    assert stats["gate_hits"] == {"title": 1, "abstract": 2, "concept": 2}


def test_pool_absent_is_none_not_an_error(tmp_path):
    """The pool is gigabytes and gitignored — a checkout without one is normal, and
    every pool read must degrade to 'not available here' rather than raise."""
    from shared.dashboard_cache import compute_pool_stats, compute_stage_stats, pool_totals

    _clear_pool_memo()
    assert pool_totals(tmp_path / "nope") is None
    _clear_pool_memo()
    assert compute_pool_stats(tmp_path / "nope") is None
    _clear_pool_memo()
    assert compute_stage_stats("pool") is None or isinstance(compute_stage_stats("pool"), dict)


def test_csv_stats_endpoint_reports_pool_absence(tmp_path, monkeypatch):
    """With neither a local pool nor pool figures in stats.json, the Stage 1 panel
    must say so — not render a zero that reads as 'the search found nothing'."""
    import validate.routes.dashboard as dash

    monkeypatch.setattr(dash, "load_stats", lambda: {})
    monkeypatch.setattr(dash, "pool_totals", lambda: None)
    monkeypatch.setattr(dash, "_STAGE_FILES", {})
    from validate.app import create_app
    data = create_app().test_client().get("/api/dashboard/csv-stats").get_json()

    assert data["pool_present"] is False
    assert data["pool_source"] == "none"
    assert data["pool_count"] is None


# ── Set-aside CSVs ───────────────────────────────────────────────────────────

def test_set_aside_tabs_come_from_the_shared_destination_map():
    """The dashboard's tabs are built from shared/schema.py's SET_ASIDE_DESTINATIONS,
    not from a hand-kept copy — the copy had drifted, listing a file sanity_check never
    writes and omitting four live destinations."""
    import validate.routes.dashboard as dash
    from shared.schema import SET_ASIDE_DESTINATIONS

    tabbed = {spec["file"] for spec in dash.SET_FILES.values()}
    assert set(SET_ASIDE_DESTINATIONS.values()) <= tabbed
    # Everything else with a tab is declared as a non-set-aside view, once.
    extra = tabbed - set(SET_ASIDE_DESTINATIONS.values())
    assert extra == {spec["file"] for spec in dash._OTHER_SETS.values()}
    # Every tab is renderable: the template reads all four fields.
    for key, spec in dash.SET_FILES.items():
        assert {"title", "file", "why", "action"} <= set(spec), key


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


def test_validator_slot_prefixes_match_the_validation_tables():
    """The importer in the validation repo writes these three slot names; the
    drilldown flattening must agree or the per-validator columns silently render
    blank. The set is a cross-repo contract, so it is pinned literally here."""
    assert set(supa._SLOT_PREFIX) == {"human_1", "human_2", "llm"}
