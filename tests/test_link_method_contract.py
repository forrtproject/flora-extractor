"""
Cross-module agreement on the link_method enum.

Three modules read link_method independently: run_extract's outcome gate decides
whether a row is coded at all, csv_to_db decides whether it reaches the validation
DB, and sanity_check decides whether it stays in extracted.csv. Nothing forces them
to agree, and a link method added to shared/schema.py that one of them has never
heard of fails silently in the direction that is hardest to notice — an unresolved
row that gets an outcome coded and imported as settled.

These tests hold each consumer against LINK_METHOD_VALUES, so a new method has to
be classified by all three before the suite goes green.
"""
import sys
import types

import pandas as pd
import pytest

import extract.sanity_check as sc
from extract.run_extract import _outcome_without_coding
from shared.schema import EXTRACTED_COLS, LINK_METHOD_VALUES, RESOLVED_LINK_METHODS

# `supabase` is not a test dependency; csv_to_db imports it at module scope.
if "supabase" not in sys.modules:
    _stub = types.ModuleType("supabase")
    _stub.create_client = lambda url, key: None
    _stub.Client = object
    sys.modules["supabase"] = _stub

from extract.csv_to_db import _RESOLVED_METHODS  # noqa: E402

# The two unresolved methods sanity_check deliberately leaves in extracted.csv: they
# carry no link and no quarantine bucket, and a re-run is what fixes them.
_KEPT_UNRESOLVED = {"no_original_found", "api_error"}

# Unresolved methods sanity_check moves to a set-aside CSV.
_QUARANTINED = {"screen_disagreement", "llm_title_search", "target_pending",
                "not_a_replication", "prescreen_discard"}


def test_every_link_method_is_classified_exactly_once():
    """The three buckets must partition the enum — no value unaccounted for, none
    claimed twice."""
    buckets = [RESOLVED_LINK_METHODS, _QUARANTINED, _KEPT_UNRESOLVED]
    union = set().union(*buckets)

    assert union == LINK_METHOD_VALUES - {"author_year_match_legacy"}, (
        "link method(s) unclassified: "
        f"{(LINK_METHOD_VALUES - {'author_year_match_legacy'}) - union}")
    assert sum(len(b) for b in buckets) == len(union), "a link method is in two buckets"


def test_csv_to_db_imports_exactly_the_resolved_methods():
    """csv_to_db's filter may add the legacy alias, but must not admit any live
    unresolved method — an imported row is presented to validators as settled."""
    assert RESOLVED_LINK_METHODS <= _RESOLVED_METHODS
    assert _RESOLVED_METHODS - RESOLVED_LINK_METHODS == {"author_year_match_legacy"}
    assert not _RESOLVED_METHODS & (_QUARANTINED | _KEPT_UNRESOLVED)


@pytest.mark.parametrize("method", sorted(LINK_METHOD_VALUES))
def test_outcome_gate_agrees_with_the_resolved_set(method):
    """run_extract codes an outcome for a resolved method and refuses for every
    other value in the enum, including the legacy alias."""
    gate = _outcome_without_coding(method, {"link_evidence": "", "llm_error": ""})

    if method in RESOLVED_LINK_METHODS:
        assert gate is None, f"{method} is resolved but the gate blocked coding"
    else:
        assert gate is not None, f"{method} is unresolved but would be outcome-coded"
        assert gate["outcome"], f"{method} produced a blank placeholder outcome"


def test_sanity_check_routes_every_unresolved_method_as_its_bucket_says(tmp_path,
                                                                       monkeypatch):
    """One row per link method through the real quarantine pass: resolved and
    kept-unresolved rows survive, quarantined ones leave extracted.csv."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"

    rows = []
    for i, method in enumerate(sorted(LINK_METHOD_VALUES)):
        rows.append({
            "doi_r": f"10.1/{method}", "doi_o": f"10.2/o{i}", "year_r": "2020",
            "year_o": "2015", "openalex_id_r": f"W{i}", "pair_id": f"p{i}",
            "doi_o_verification": "verified", "link_method": method,
            # The screen's discard verdict writes both fields; sanity_check routes
            # that row on the outcome, so the fixture has to carry it too.
            "outcome": "not_a_replication" if method == "not_a_replication" else "success",
        })
    df = pd.DataFrame(rows)
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    df[EXTRACTED_COLS].to_csv(ex, index=False, encoding="utf-8-sig")

    sc.run_sanity_check(ex, move=True, deep=False)

    survivors = set(pd.read_csv(ex, dtype=str, keep_default_na=False)["link_method"])
    expected = RESOLVED_LINK_METHODS | _KEPT_UNRESOLVED | {"author_year_match_legacy"}

    assert survivors == expected, (
        f"unexpectedly quarantined: {expected - survivors}; "
        f"unexpectedly kept: {survivors - expected}")
