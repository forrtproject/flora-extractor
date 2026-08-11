"""The already-in-the-validation-tables skip list.

The legacy `record_metadata` set is frozen and lives on disk as
`data/validated_skip.csv`; Stage 3 must never extract one of those works again.
What is worth testing is the part that decides — which spellings of an id meet,
which record can block a row at all, and what a missing file does. Nothing here
reaches Supabase.
"""

import pandas as pd

from analysis.build_validated_skip import skip_rows
from shared.flora_skip import load_validated_skip
from extract.tier import _skipped


def _skip_csv(tmp_path, rows):
    p = tmp_path / "validated_skip.csv"
    pd.DataFrame(rows, columns=["work_id", "doi", "record_id"]).to_csv(
        p, index=False, encoding="utf-8-sig")
    return p


def _should_skip(row, doi_r_clean, validated_skip):
    """The extract tier's worklist subtraction, as a boolean."""
    validated_ids, validated_dois = validated_skip
    return "validated" if _skipped(row, set(), validated_ids, validated_dois) else None


class TestLoadValidatedSkip:
    def test_work_ids_and_dois_are_read_separately(self, tmp_path):
        p = _skip_csv(tmp_path, [
            {"work_id": "11", "doi": "10.1/a", "record_id": "r1"},
            {"work_id": "",   "doi": "10.1/b", "record_id": "r2"},
            {"work_id": "12", "doi": "",       "record_id": "r3"},
            {"work_id": "",   "doi": "",       "record_id": "r4"},
        ])
        assert load_validated_skip(p) == ({11, 12}, {"10.1/a", "10.1/b"})

    def test_a_missing_file_skips_nothing_and_does_not_crash(self, tmp_path):
        assert load_validated_skip(tmp_path / "nope.csv") == (set(), set())


class TestShouldSkipValidated:
    def test_a_work_id_match_skips_the_row(self):
        """The handoff writes the URL form; the skip file holds the int64."""
        row = {"doi_r": "10.9/unseen", "openalex_id_r": "https://openalex.org/W11",
               "paper_type": "replication"}
        assert _should_skip(row, "10.9/unseen", ({11}, set())) == "validated"

    def test_a_doi_only_record_still_blocks_the_row(self):
        """31 legacy records carry no work id; most still have a DOI, and that DOI
        is enough — a row with no OpenAlex id must not slip past on that account."""
        row = {"doi_r": "10.1/a", "openalex_id_r": "", "paper_type": "replication"}
        assert _should_skip(row, "10.1/a", (set(), {"10.1/a"})) == "validated"

    def test_a_record_identifying_nothing_blocks_nothing(self):
        """An empty skip list must not swallow rows whose own ids are also empty."""
        row = {"doi_r": "", "openalex_id_r": "", "paper_type": "replication"}
        assert _should_skip(row, "", (set(), set())) is None
        blank = {"doi_r": "10.5/x", "openalex_id_r": "not-an-id",
                 "paper_type": "replication"}
        assert _should_skip(blank, "10.5/x", (set(), set())) is None

    def test_an_unrelated_row_is_processed(self):
        row = {"doi_r": "10.9/new", "openalex_id_r": "https://openalex.org/W99",
               "paper_type": "replication"}
        assert _should_skip(row, "10.9/new", ({11}, {"10.1/a"})) is None


class TestSkipRowsGenerator:
    def test_counts_separate_work_id_doi_only_and_neither(self):
        rows, counts = skip_rows(
            [{"record_id": "r1", "work_id": "https://openalex.org/W11"},
             {"record_id": "r2", "work_id": None},
             {"record_id": "r3", "work_id": ""}],
            {"r1": "10.1/a", "r2": "10.1/b"})

        assert counts == {"total": 3, "with_work_id": 1, "doi_only": 1, "neither": 1}
        assert rows[0] == {"work_id": "11", "doi": "10.1/a", "record_id": "r1"}
        assert rows[1] == {"work_id": "", "doi": "10.1/b", "record_id": "r2"}
        assert rows[2] == {"work_id": "", "doi": "", "record_id": "r3"}
