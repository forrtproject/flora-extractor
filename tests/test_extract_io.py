"""
Stage 3 I/O contracts: the CSV writer, the resume/skip decision, the chunked row
filters, the outcome writer's reproduction axes, and the reference builders.

These are the seams every run passes through on every row but that the behavioural
tests reach only indirectly — a column-order regression or a quoting failure in
the finalise+write pair corrupts extracted.csv for every consumer downstream, and nothing else
asserts against it. All external calls are mocked.
"""
import io

import pandas as pd
import pytest
from unittest.mock import patch

import extract.run_extract as run_extract
from extract.run_extract import _apply_filters, _apply_outcome, build_bibtex
from shared.schema import EXTRACTED_COLS, FILTERED_COLS, SCREEN_COLS


# ── _finalise_row + _write_row: the only writer of extracted.csv ─────────────

class TestWriteRow:
    """Every row is streamed straight to disk, so the header written by the first
    row is the contract every later append has to match."""

    def _write(self, path, rows: list[dict]) -> pd.DataFrame:
        with patch.object(run_extract, "verify_and_correct",
                          side_effect=lambda doi, *a, **k: {
                              "doi_o": doi, "doi_o_verification": "skipped",
                              "evidence_note": ""}), \
             patch.object(run_extract, "_oa_by_doi", return_value=None):
            run_extract._write_header(path)
            for row in rows:
                run_extract._write_row(path, run_extract._finalise_row(dict(row)))
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


# ── the resume write: what a carried-forward row does NOT pay for again ──────

class TestResumeDoesNotReverify:
    """A resume re-wrote every resolved row through _finalise_row, so it re-ran
    verify_and_correct — up to three OpenAlex free-text searches at 10x a filter
    query — on every restart, to re-derive an answer the row already carried."""

    def _resume(self, tmp_path, rows: list[dict]):
        """Resume over an extracted.csv of *rows* with an empty filtered.csv."""
        out = tmp_path / "extracted.csv"
        pd.DataFrame([{**{c: "" for c in EXTRACTED_COLS}, **r} for r in rows]).to_csv(
            out, index=False, encoding="utf-8-sig")
        pd.DataFrame(columns=list(FILTERED_COLS) + SCREEN_COLS).to_csv(
            tmp_path / "filtered.csv", index=False, encoding="utf-8-sig")
        with patch.object(run_extract, "DATA_DIR", tmp_path), \
             patch.object(run_extract, "_check_screen_providers"), \
             patch.object(run_extract, "_oa_by_doi", return_value=None), \
             patch.object(run_extract, "verify_and_correct",
                          side_effect=lambda doi, *a, **k: {
                              "doi_o": doi, "doi_o_verification": "verified",
                              "evidence_note": ""}) as vc:
            run_extract.run_extract(out_path=out)
        return vc, pd.read_csv(out, dtype=str, encoding="utf-8-sig").fillna("")

    _SETTLED = {"doi_r": "10.1/a", "doi_o": "10.9/a", "link_method": "llm_fulltext",
                "doi_o_verification": "verified"}
    _FAILED  = {"doi_r": "10.1/b", "doi_o": "10.9/b", "link_method": "llm_fulltext",
                "doi_o_verification": "api_error"}

    def test_a_settled_row_is_carried_forward_unverified(self, tmp_path):
        vc, df = self._resume(tmp_path, [self._SETTLED])
        vc.assert_not_called()
        assert list(df["doi_o"]) == ["10.9/a"]
        assert list(df["doi_o_verification"]) == ["verified"]

    def test_an_api_error_row_is_verified_again(self, tmp_path):
        """The point of the api_error status: the registries were unreachable, so the
        row holds no answer to trust and the next run asks again."""
        vc, df = self._resume(tmp_path, [self._SETTLED, self._FAILED])
        assert [c.args[0] for c in vc.call_args_list] == ["10.9/b"]
        assert set(df["doi_o_verification"]) == {"verified"}

    def test_a_row_that_was_never_verified_is_verified(self, tmp_path):
        """A blank column — a row from before verification reached it — is not a
        settled answer either."""
        vc, _ = self._resume(tmp_path, [{**self._SETTLED, "doi_o_verification": ""}])
        assert vc.call_count == 1


class TestResumeNeverLosesRowsOnDisk:
    """The output is truncated to its header before the carried rows are written, so
    anything that can raise between the two costs the run rows it already had."""

    _resume  = TestResumeDoesNotReverify._resume
    _SETTLED = TestResumeDoesNotReverify._SETTLED

    def test_a_legacy_float_year_row_survives_the_resume(self, tmp_path):
        """#140's assertion belongs to rows this run BUILDS. A carried row comes from
        a CSV an older checkout wrote, where "2018.0" legitimately lives."""
        _, df = self._resume(tmp_path, [{**self._SETTLED, "year_r": "2018.0",
                                         "year_o": "2009.0"}])
        assert list(df["doi_r"]) == ["10.1/a"]
        assert (df.iloc[0]["year_r"], df.iloc[0]["year_o"]) == ("2018", "2009")

    def test_a_row_that_cannot_be_finalised_is_written_anyway(self, tmp_path):
        """A quota exhaustion or an outage inside the work-id fill must not empty the
        file: the row goes back as it stood on disk, with a warning."""
        out = tmp_path / "extracted.csv"
        rows = [self._SETTLED, {**self._SETTLED, "doi_r": "10.1/b"}]
        pd.DataFrame([{**{c: "" for c in EXTRACTED_COLS}, **r} for r in rows]).to_csv(
            out, index=False, encoding="utf-8-sig")
        pd.DataFrame(columns=list(FILTERED_COLS) + SCREEN_COLS).to_csv(
            tmp_path / "filtered.csv", index=False, encoding="utf-8-sig")
        with patch.object(run_extract, "DATA_DIR", tmp_path), \
             patch.object(run_extract, "_check_screen_providers"), \
             patch.object(run_extract, "_finalise_carried_row",
                          side_effect=RuntimeError("OpenAlex quota exhausted")):
            run_extract.run_extract(out_path=out)
        df = pd.read_csv(out, dtype=str, encoding="utf-8-sig").fillna("")
        assert sorted(df["doi_r"]) == ["10.1/a", "10.1/b"]

    _PENDING = {"doi_r": "10.1/p", "link_method": "target_pending"}

    def _prepare(self, tmp_path, rows: list[dict]):
        """extracted.csv of *rows*, an empty filtered.csv beside it."""
        out = tmp_path / "extracted.csv"
        pd.DataFrame([{**{c: "" for c in EXTRACTED_COLS}, **r} for r in rows]).to_csv(
            out, index=False, encoding="utf-8-sig")
        pd.DataFrame(columns=list(FILTERED_COLS) + SCREEN_COLS).to_csv(
            tmp_path / "filtered.csv", index=False, encoding="utf-8-sig")
        return out

    def test_an_interrupt_in_the_prologue_keeps_the_pending_rows(self, tmp_path):
        """The prologue re-verifies unsettled rows against OpenAlex, so it can run for
        minutes — and it used to sit outside the try/finally that writes the pending
        rows back. A Ctrl-C there left the file truncated to its header plus whatever
        the carry loop had managed: every target_pending row deleted."""
        out = self._prepare(tmp_path, [self._SETTLED, self._PENDING])
        with patch.object(run_extract, "DATA_DIR", tmp_path), \
             patch.object(run_extract, "_check_screen_providers"), \
             patch.object(run_extract, "_finalise_carried_row",
                          side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                run_extract.run_extract(out_path=out)
        df = pd.read_csv(out, dtype=str, encoding="utf-8-sig").fillna("")
        assert sorted(df["doi_r"]) == ["10.1/a", "10.1/p"]

    def test_an_interrupt_inside_the_write_does_not_duplicate_the_row(self, tmp_path):
        """The counter can lag the flush by one row. Restoring from the counter then
        writes a row that is already on disk — a duplicate every later resume keeps."""
        out = self._prepare(tmp_path, [self._SETTLED, {**self._SETTLED, "doi_r": "10.1/b"}])
        real_write = run_extract._write_row
        calls = []

        def _write_then_interrupt(path, row):
            real_write(path, row)
            calls.append(row)
            if len(calls) == 1:
                raise KeyboardInterrupt   # flushed, but before the loop counted it

        with patch.object(run_extract, "DATA_DIR", tmp_path), \
             patch.object(run_extract, "_check_screen_providers"), \
             patch.object(run_extract, "_oa_by_doi", return_value=None), \
             patch.object(run_extract, "_write_row", side_effect=_write_then_interrupt):
            with pytest.raises(KeyboardInterrupt):
                run_extract.run_extract(out_path=out)
        df = pd.read_csv(out, dtype=str, encoding="utf-8-sig").fillna("")
        assert sorted(df["doi_r"]) == ["10.1/a", "10.1/b"]

    def test_a_row_appended_between_the_read_and_the_truncation_is_not_lost(self, tmp_path):
        """The read and the truncation are one locked operation. Unlocked, a row a
        concurrent run appended in between was wiped by the truncation and appeared in
        neither the resolved nor the pending set — nothing could put it back, because
        nothing had seen it."""
        import threading

        out = self._prepare(tmp_path, [self._SETTLED])
        appended = {**{c: "" for c in EXTRACTED_COLS}, "doi_r": "10.1/new",
                    "doi_o": "10.9/n", "link_method": "llm_fulltext",
                    "doi_o_verification": "verified"}
        real_load = run_extract._load_extracted_rows
        writers: list[threading.Thread] = []

        def _load_then_let_a_writer_in(path, **kw):
            state = real_load(path, **kw)
            # A concurrent Stage 3 append, attempted at the worst possible moment:
            # after this run has read the file and before it truncates it. It takes
            # the same csv_lock, so it can only land once the truncation is done.
            writer = threading.Thread(target=run_extract._write_row, args=(out, appended))
            writer.start()
            writers.append(writer)
            writer.join(timeout=1.0)
            return state

        with patch.object(run_extract, "DATA_DIR", tmp_path), \
             patch.object(run_extract, "_check_screen_providers"), \
             patch.object(run_extract, "_oa_by_doi", return_value=None), \
             patch.object(run_extract, "_load_extracted_rows",
                          side_effect=_load_then_let_a_writer_in):
            run_extract.run_extract(out_path=out)
        for writer in writers:
            writer.join(timeout=10)
        df = pd.read_csv(out, dtype=str, encoding="utf-8-sig").fillna("")
        assert sorted(df["doi_r"]) == ["10.1/a", "10.1/new"]

    def test_a_pending_row_written_back_has_its_year_normalised(self, tmp_path):
        """The write-back streams rows straight off disk, so a legacy "2018.0" went
        back out as it came in."""
        out = self._prepare(tmp_path, [{**self._PENDING, "year_r": "2018.0"}])
        with patch.object(run_extract, "DATA_DIR", tmp_path), \
             patch.object(run_extract, "_check_screen_providers"):
            run_extract.run_extract(out_path=out)
        df = pd.read_csv(out, dtype=str, encoding="utf-8-sig").fillna("")
        assert list(df["doi_r"]) == ["10.1/p"]
        assert df.iloc[0]["year_r"] == "2018"


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
