"""
Stage 3 I/O contracts: the CSV writer, the resume/skip decision, the chunked row
filters, the outcome writer's reproduction axes, and the reference builders.

These are the seams every run passes through on every row but that the behavioural
tests reach only indirectly — a column-order regression or a quoting failure in
_append_row corrupts extracted.csv for every consumer downstream, and nothing else
asserts against it. All external calls are mocked.
"""
import io

import pandas as pd
import pytest
from unittest.mock import patch

import extract.run_extract as run_extract
from extract.run_extract import _apply_filters, _apply_outcome, _append_row, build_bibtex
from shared.schema import EXTRACTED_COLS


# ── _append_row: the only writer of extracted.csv ────────────────────────────

class TestAppendRow:
    """Every row is streamed straight to disk, so the header written by the first
    row is the contract every later append has to match."""

    def _write(self, path, rows: list[dict]) -> pd.DataFrame:
        with patch.object(run_extract, "verify_and_correct",
                          side_effect=lambda doi, *a, **k: {
                              "doi_o": doi, "doi_o_verification": "skipped",
                              "evidence_note": ""}), \
             patch.object(run_extract, "_oa_by_doi", return_value=None):
            for i, row in enumerate(rows):
                _append_row(path, dict(row), first=(i == 0))
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")

    def test_columns_are_written_in_schema_order(self, tmp_path):
        out = tmp_path / "extracted.csv"
        df = self._write(out, [
            {"doi_r": "10.1/a", "title_r": "First", "link_method": "llm_references"},
            {"doi_r": "10.1/b", "title_r": "Second", "link_method": "llm_fulltext"},
        ])
        assert list(df.columns) == list(EXTRACTED_COLS)
        assert list(df["doi_r"]) == ["10.1/a", "10.1/b"]
        # The append must not repeat the header as a data row.
        assert "doi_r" not in set(df["doi_r"])

    def test_commas_and_newlines_round_trip(self, tmp_path):
        """Quotes for evidence and reference strings are the norm, not the exception:
        an APA reference carries commas and a parsed quote carries newlines."""
        evidence = 'Smith, J., & Jones, A. (2010).\nSecond line, with a comma.'
        out = tmp_path / "extracted.csv"
        df = self._write(out, [
            {"doi_r": "10.1/a", "link_evidence": evidence, "title_r": 'A "quoted" title'},
            {"doi_r": "10.1/b", "outcome_phrase": "we failed, and then, again\nnext line"},
        ])
        assert len(df) == 2
        # \r is normalised away on write; \n inside a quoted field survives.
        assert df.iloc[0]["link_evidence"] == evidence
        assert df.iloc[0]["title_r"] == 'A "quoted" title'
        assert df.iloc[1]["outcome_phrase"] == "we failed, and then, again\nnext line"


# ── _should_skip: what a resumed or test-mode run does not re-process ─────────

class TestShouldSkip:
    _ROW = pd.Series({"doi_r": "10.1/a", "filter_status": "replication"})

    def _skip(self, *, resolved=None, resolved_main=None, targets=(), row=None,
              validated=None):
        return run_extract._should_skip(
            row if row is not None else self._ROW, "10.1/a", "10.1/a",
            flora_skip=set(), validated_skip=validated or (set(), set()),
            resolved_rows=resolved or {},
            resolved_main=resolved_main or {}, doi_r_targets=set(targets),
            only_reproductions=False)

    def test_a_fresh_row_is_processed(self):
        assert self._skip() is None

    def test_a_row_already_in_the_output_is_skipped(self):
        assert self._skip(resolved={"10.1/a": [{}]}) == "resumed"

    def test_extracted_test_skips_dois_already_in_production(self):
        """--extracted-test loads extracted.csv purely to avoid re-paying for rows
        the production run already resolved."""
        assert self._skip(resolved_main={"10.1/a": [{}]}) == "already in extracted.csv"

    def test_an_explicit_doi_r_target_overrides_that_skip(self):
        """--doi-r names the row on purpose, so a previous production answer must not
        silently swallow the re-run."""
        assert self._skip(resolved_main={"10.1/a": [{}]}, targets=["10.1/a"]) is None

    def test_flora_and_false_positive_still_win(self):
        """--doi-r overrides only the extracted.csv skip: a row already in FLoRA, and
        a Stage 2 false positive, are still not extracted."""
        assert run_extract._should_skip(
            self._ROW, "10.1/a", "10.1/a", flora_skip={"10.1/a"},
            validated_skip=(set(), set()), resolved_rows={},
            resolved_main={}, doi_r_targets={"10.1/a"}, only_reproductions=False) == "flora"
        fp = pd.Series({"doi_r": "10.1/a", "filter_status": "false_positive"})
        assert self._skip(row=fp) == "false_positive"


# ── _apply_filters: the per-chunk row predicates ─────────────────────────────

_FILTER_HEADER = ("doi_r,title_r,abstract_r,year_r,authors_r,journal_r,url_r,"
                  "openalex_id_r,source,filter_status,filter_method,filter_evidence,"
                  "filter_confidence\n")


def _filtered_csv(rows: list[tuple[str, str, str, str]]) -> str:
    """rows: (doi_r, year_r, source, abstract_r)."""
    return _FILTER_HEADER + "".join(
        f"{doi},Paper,{abstract},{year},Smith,J. Psych,,W1,{source},"
        f"replication,rule_based,direct replication,high\n"
        for doi, year, source, abstract in rows)


class TestApplyFilters:
    _CHUNK = pd.read_csv(io.StringIO(_filtered_csv([
        ("10.1/a", "2018", "openalex", "Abstract"),
        ("10.1/b", "2020", "bob_reed", "Abstract"),
        ("10.1/c", "2022", "openalex", "Abstract"),
        ("10.1/d", "",     "openalex", "Abstract"),   # unparseable year
    ])), dtype=str).fillna("")

    @pytest.mark.parametrize("kwargs,kept", [
        ({"from_year": 2020},                 ["10.1/b", "10.1/c"]),
        ({"to_year": 2020},                   ["10.1/a", "10.1/b"]),
        ({"from_year": 2019, "to_year": 2021}, ["10.1/b"]),
        ({"source": "openalex"},              ["10.1/a", "10.1/c", "10.1/d"]),
        ({"source": "OpenAlex"},              ["10.1/a", "10.1/c", "10.1/d"]),
        ({"from_year": 2019, "source": "openalex"}, ["10.1/c"]),
    ])
    def test_year_and_source_predicates(self, kwargs, kept):
        """A row with no parseable year is dropped by any year filter — a year bound
        cannot be satisfied by a year that is not there."""
        out = _apply_filters(self._CHUNK.copy(), **kwargs)
        assert list(out["doi_r"]) == kept

    def test_the_filters_hold_across_chunk_boundaries(self, tmp_path, monkeypatch):
        """filtered.csv is read in chunks, so a filter that ran per chunk must select
        the same rows a whole-file filter would."""
        monkeypatch.setattr(run_extract, "_CHUNK_ROWS", 2)
        path = tmp_path / "filtered.csv"
        path.write_text(_filtered_csv([
            ("10.1/a", "2018", "openalex", "Abstract"),
            ("10.1/b", "2020", "bob_reed", "Abstract"),
            ("10.1/c", "2022", "openalex", "Abstract"),
            ("10.1/d", "2023", "openalex", "Abstract"),
            ("10.1/e", "2024", "openalex", ""),        # no abstract — yielded last
        ]), encoding="utf-8-sig")
        rows = list(run_extract._iter_filtered_rows(path, from_year=2020,
                                                    source="openalex"))
        assert [r["doi_r"] for r in rows] == ["10.1/c", "10.1/d", "10.1/e"]


# ── _apply_outcome on the reproduction grid ──────────────────────────────────

def test_apply_outcome_writes_the_grid_verdict_and_both_axes():
    """The derived join and the two axes it was derived from travel together — a row
    carrying the joined outcome without its axes cannot be checked against the paper."""
    outcome = {
        "outcome": "computational issues, robustness challenges",
        "outcome_confidence": "high", "outcome_phrase": "", "out_quote_source": "",
        "outcome_reasoning": "both axes settled", "llm_model": "gemini-outcome",
        "outcome_computation": "computational issues",
        "outcome_computational_quote": "The coefficient differed.",
        "out_quote_computational_source": "abstract",
        "outcome_robustness": "robustness challenges",
        "outcome_robustness_quote": "Two specifications reversed the sign.",
        "out_quote_robust_source": "abstract",
    }
    row = _apply_outcome({"type": "reproduction"}, outcome)
    assert row["outcome"] == "computational issues, robustness challenges"
    for col in run_extract._OUTCOME_AXIS_COLS:
        assert row[col] == outcome[col], col
    assert row["outcome_llm_model"] == "gemini-outcome"


def test_apply_outcome_blanks_the_axes_on_a_replication():
    """A replication has no axes, and a stale value from a previous coding would read
    as a reproduction verdict on a replication row."""
    row = _apply_outcome(
        {"type": "replication", "outcome_computation": "computational issues"},
        {"outcome": "success", "outcome_confidence": "high"})
    assert row["outcome"] == "success"
    for col in run_extract._OUTCOME_AXIS_COLS:
        assert row[col] == "", col


# ── ref_o / bibtex_ref_o: the human-readable citation of the original ────────

class TestReferenceBuilders:
    _META = {"authors": ["Smith, J.", "Jones, A."], "year": 2010,
             "title": "The Original Study", "journal": "J. Psych",
             "volume": "51", "issue": "6", "first_page": "1173",
             "last_page": "1182", "doi": "10.1037/orig"}

    def test_build_bibtex_for_a_journal_article(self):
        entry = build_bibtex(self._META["authors"], "2010", self._META["title"],
                             journal="J. Psych", volume="51", issue="6",
                             first_page="1173", last_page="1182", doi="10.1037/orig")
        assert entry.startswith("@article{Smith_2010, ")
        assert entry.endswith("}")
        assert "title={The Original Study}" in entry
        assert "author={Smith, J. and Jones, A.}" in entry
        assert "journal={J. Psych}" in entry
        assert "volume={51}" in entry and "number={6}" in entry
        assert "pages={1173--1182}" in entry
        assert "doi={10.1037/orig}" in entry
        assert "url={https://doi.org/10.1037/orig}" in entry

    def test_build_bibtex_without_a_journal_is_misc(self):
        entry = build_bibtex([], "", "A Working Paper")
        assert entry.startswith("@misc{Unknown, ")
        assert "title={A Working Paper}" in entry
        assert "journal=" not in entry and "year=" not in entry

    def test_build_ref_o_from_doi_metadata(self):
        with patch.object(run_extract, "_oa_full_meta", return_value=self._META):
            ref, authors, bibtex = run_extract._build_ref_o("10.1037/orig")
        assert ref == ("Smith, J., & Jones, A. (2010). The Original Study. "
                       "J. Psych, 51(6), 1173–1182. https://doi.org/10.1037/orig")
        assert authors == "Smith, J.; Jones, A."
        assert bibtex.startswith("@article{Smith_2010, ")

    def test_build_ref_o_falls_back_to_surname_and_year(self):
        """No metadata anywhere: the row still needs something a reviewer can read,
        and an empty bibtex rather than a fabricated one."""
        with patch.object(run_extract, "_oa_full_meta", return_value=None), \
             patch.object(run_extract, "_search_crossref_by_title", return_value=None), \
             patch.object(run_extract, "_search_openalex_by_title", return_value=None):
            ref, authors, bibtex = run_extract._build_ref_o(
                "", fallback_author="John Smith", fallback_year="1935",
                title_o="An Unindexed Book")
        assert ref == "Smith · 1935"
        assert authors == "Smith"
        assert bibtex == ""
