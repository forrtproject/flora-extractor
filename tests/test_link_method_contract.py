"""
Cross-module agreement on the link_method enum.

Two modules read link_method independently: run_extract's outcome gate decides
whether a row is coded at all, and sanity_check decides whether it stays in
extracted.csv. Nothing forces them to agree, and a link method added to
shared/schema.py that one of them has never heard of fails silently in the
direction that is hardest to notice — an unresolved row that gets an outcome
coded and presented as settled.

These tests hold each consumer against LINK_METHOD_VALUES, so a new method has to
be classified by both before the suite goes green.
"""
import pandas as pd
import pytest

import extract.sanity_check as sc
from extract.run_extract import _outcome_without_coding
from shared.schema import (EXTRACTED_COLS, LINK_METHOD_VALUES, RESOLVED_LINK_METHODS,
                           REOPENED_SET_ASIDE_FILES, SCREEN_SET_ASIDE_FILES,
                           SET_ASIDE_DESTINATIONS, SETTLED_SET_ASIDE_FILES)

# Unresolved methods sanity_check moves to a set-aside CSV — all of them. extracted.csv
# is validation-ready rows and nothing else, so no unresolved method survives the pass.
_QUARANTINED = {"screen_disagreement", "unidentified_original",
                "keyed_link_disputed", "target_pending",
                "not_a_replication", "prescreen_discard", "no_original_found",
                "api_error"}


def test_every_link_method_is_classified_exactly_once():
    """The two buckets must partition the enum — no value unaccounted for, none
    claimed twice."""
    buckets = [RESOLVED_LINK_METHODS, _QUARANTINED]
    union = set().union(*buckets)

    assert union == LINK_METHOD_VALUES - {"author_year_match_legacy"}, (
        "link method(s) unclassified: "
        f"{(LINK_METHOD_VALUES - {'author_year_match_legacy'}) - union}")
    assert sum(len(b) for b in buckets) == len(union), "a link method is in two buckets"


def test_every_set_aside_destination_is_settled_or_reopened():
    """Resume reads the settled set-asides and redoes the reopened ones; every
    destination must be in exactly one of those two lists.

    A new set-aside file that is in neither is the bug this partition exists to catch:
    its rows are silently re-screened, re-linked and re-paid for on every resume
    (issue #136 §1, where provisional_title_search.csv and four others were unread)."""
    files = set(SET_ASIDE_DESTINATIONS.values())
    settled, reopened = set(SETTLED_SET_ASIDE_FILES), set(REOPENED_SET_ASIDE_FILES)

    assert settled | reopened == files, f"unclassified set-aside file(s): {files - settled - reopened}"
    assert not settled & reopened, f"set-aside file in both lists: {settled & reopened}"
    # The abstract-only screen verdicts are the subset a new screening generation
    # reopens; they can only be reopened if they count as settled in the first place.
    assert set(SCREEN_SET_ASIDE_FILES) <= settled


def test_sanity_check_writes_no_unclassified_destination(tmp_path, monkeypatch):
    """The export may only write files the destination map names — a rule pointing at
    a new file that schema.py has never heard of fails here."""
    monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
    ex = tmp_path / "extracted.csv"

    rows = []
    for i, method in enumerate(sorted(LINK_METHOD_VALUES)):
        rows.append({"doi_r": f"10.1/{method}", "doi_o": f"10.2/o{i}",
                     "link_method": method, "doi_o_verification": "verified",
                     "outcome": "not_a_replication" if method == "not_a_replication"
                     else "success"})
    df = pd.DataFrame(rows)
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    df[EXTRACTED_COLS].to_csv(ex, index=False, encoding="utf-8-sig")

    from extract.export import partition
    _main, aside = partition(rows)

    assert set(aside) <= set(SET_ASIDE_DESTINATIONS.values()), (
        f"set-aside file(s) unknown to shared/schema.py: "
        f"{set(aside) - set(SET_ASIDE_DESTINATIONS.values())}")


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
    """One row per link method through the export's partition: only resolved rows
    reach extracted.csv, which is the validation-ready set."""
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

    from extract.export import partition
    main, _aside = partition(rows)
    survivors = {row["link_method"] for row in main}
    expected = RESOLVED_LINK_METHODS | {"author_year_match_legacy"}

    assert survivors == expected, (
        f"unexpectedly quarantined: {expected - survivors}; "
        f"unexpectedly kept: {survivors - expected}")
