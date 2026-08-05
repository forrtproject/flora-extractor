"""Stage 3's row loop with more than one row in flight (EXTRACT_WORKERS).

Two properties the pool must not break, whatever order the rows finish in: every
row is written exactly once, under one header; and a run that stops on a budget
exhaustion still writes the rows that were already being worked on when it stopped.
_process_row is faked — what is under test is the loop around it, not a row.
"""
import threading

import pandas as pd
import pytest
from unittest.mock import patch

import extract.run_extract as rex
from shared.schema import EXTRACTED_COLS, FILTERED_COLS, SCREEN_COLS
from tests.conftest import SCREEN_PROCEED, screen_cells
from shared.token_usage import TokenBudgetExhausted


def _filtered_csv(tmp_path, dois: list[str]):
    rows = []
    for i, doi in enumerate(dois):
        row = {col: "" for col in list(FILTERED_COLS) + SCREEN_COLS}
        row.update({"doi_r": doi, "title_r": f"Title {i}", "abstract_r": "abstract",
                    "filter_status": "replication", "year_r": "2020"})
        # Stage 2's screen verdict: the input contract Stage 3 refuses a file without.
        row.update(screen_cells(SCREEN_PROCEED))
        rows.append(row)
    pd.DataFrame(rows).to_csv(tmp_path / "filtered.csv", index=False,
                              encoding="utf-8-sig")


def _result_row(doi_r: str, rank: int = 1, n: int = 1) -> dict:
    row = {col: "" for col in EXTRACTED_COLS}
    row.update({"doi_r": doi_r, "link_method": "llm_fulltext", "doi_o": "10.9/o",
                "original_rank": rank, "n_originals": n,
                "filter_status": "replication"})
    return row


def _run(tmp_path, fake_process_row, workers: int, **kwargs):
    """run_extract over the filtered.csv in tmp_path, with the row work faked."""
    out = tmp_path / "extracted.csv"
    with patch.object(rex, "DATA_DIR", tmp_path), \
         patch.object(rex, "EXTRACT_WORKERS", workers), \
         patch.object(rex, "_process_row", fake_process_row), \
         patch.object(rex, "_check_screen_providers"), \
         patch.object(rex, "verify_and_correct",
                      side_effect=lambda doi, *a, **k: {
                          "doi_o": doi, "doi_o_verification": "skipped",
                          "evidence_note": ""}), \
         patch.object(rex, "_oa_by_doi", return_value=None):
        rex.run_extract(out_path=out, **kwargs)
    return out


@pytest.mark.parametrize("workers", [1, 4])
def test_every_row_is_written_exactly_once(tmp_path, workers):
    """The sequential path and the pool must produce the same file — one header, one
    copy of every row a paper produced, and a paper's rows next to each other."""
    dois = [f"10.1/{i:02d}" for i in range(12)]
    _filtered_csv(tmp_path, dois)

    def fake(row, doi_r, **kwargs):
        # Two originals for every third paper, so the test also covers the multi-row
        # write that has to stay contiguous.
        n = 2 if int(doi_r[-2:]) % 3 == 0 else 1
        return [_result_row(doi_r, rank, n) for rank in range(1, n + 1)]

    out = _run(tmp_path, fake, workers)
    df = pd.read_csv(out, dtype=str, encoding="utf-8-sig").fillna("")

    assert list(df.columns) == list(EXTRACTED_COLS)
    assert "doi_r" not in set(df["doi_r"])          # the header is written once
    counts = df["doi_r"].value_counts().to_dict()
    assert counts == {doi: (2 if int(doi[-2:]) % 3 == 0 else 1) for doi in dois}
    # Contiguous: the run of rows for a paper is unbroken, wherever it landed.
    assert len(list(df["doi_r"].ne(df["doi_r"].shift()).cumsum().unique())) == len(dois)


def test_rows_really_do_overlap(tmp_path):
    """The barrier only releases when all four rows are inside _process_row at once,
    so a loop that had quietly gone back to processing one row at a time fails here
    rather than passing the correctness tests above."""
    dois = [f"10.2/{i}" for i in range(4)]
    _filtered_csv(tmp_path, dois)
    barrier = threading.Barrier(4, timeout=10)

    def fake(row, doi_r, **kwargs):
        barrier.wait()
        return [_result_row(doi_r)]

    out = _run(tmp_path, fake, workers=4)
    assert len(pd.read_csv(out, dtype=str, encoding="utf-8-sig")) == 4


def test_a_budget_stop_drains_the_pool_and_keeps_what_was_in_flight(tmp_path):
    """A worker's TokenBudgetExhausted stops submission, but the row that was already
    being worked on still lands, and the exception reaches the caller's handler."""
    dois = ["10.1/slow", "10.1/boom"] + [f"10.1/tail{i}" for i in range(8)]
    _filtered_csv(tmp_path, dois)

    in_flight = threading.Event()
    released  = threading.Event()
    seen: list[str] = []

    def fake(row, doi_r, **kwargs):
        seen.append(doi_r)
        if doi_r == "10.1/slow":
            in_flight.set()
            released.wait(5)
            return [_result_row(doi_r)]
        if doi_r == "10.1/boom":
            in_flight.wait(5)      # fail while the slow row is genuinely in flight
            released.set()         # …and never leave it waiting on a run that ended
            raise TokenBudgetExhausted("budget gone")
        return [_result_row(doi_r)]

    with pytest.raises(TokenBudgetExhausted):
        _run(tmp_path, fake, workers=2)

    df = pd.read_csv(tmp_path / "extracted.csv", dtype=str,
                     encoding="utf-8-sig").fillna("")
    assert "10.1/slow" in set(df["doi_r"])
    # Submission stopped: the loop never got past the first handful of rows.
    assert not [doi for doi in seen if doi.startswith("10.1/tail")][4:]
    assert not set(df["doi_r"]) - set(seen)
