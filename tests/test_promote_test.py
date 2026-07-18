"""Tests for extract.promote_test — promote logic + the concurrent-append lock (#49).

The lock test proves promote_rows cannot clobber rows the streaming extractor appends
between promote's read and its full-file rewrite: while the shared CSV lock is held,
promote_rows must block, and once released it must preserve the appended row.
"""
import threading
import time

import pandas as pd

from shared.schema import EXTRACTED_COLS
from shared.utils import csv_lock
from extract.promote_test import promote_rows


def _row(doi: str, **kw) -> dict:
    r = {c: "" for c in EXTRACTED_COLS}
    r["doi_r"] = doi
    r.update(kw)
    return r


def _write(path, rows) -> None:
    pd.DataFrame(rows)[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")


def test_promote_appends_new_row(tmp_path):
    main = tmp_path / "extracted.csv"
    test = tmp_path / "extracted-test.csv"
    _write(main, [_row("10.1/a"), _row("10.1/b")])
    _write(test, [_row("10.1/p", link_method="citation_context_match")])

    out = promote_rows(all_rows=True, test_path=test, main_path=main)

    assert out["promoted"] == 1 and out["replaced"] == 0
    dois = set(pd.read_csv(main, dtype=str).fillna("")["doi_r"])
    assert {"10.1/a", "10.1/b", "10.1/p"} == dois


def test_promote_does_not_clobber_concurrent_append(tmp_path):
    main = tmp_path / "extracted.csv"
    test = tmp_path / "extracted-test.csv"
    _write(main, [_row("10.1/a"), _row("10.1/b")])
    _write(test, [_row("10.1/p", link_method="citation_context_match")])

    started = threading.Event()
    result: dict = {}

    def run():
        started.set()
        result["out"] = promote_rows(all_rows=True, test_path=test, main_path=main)

    with csv_lock(main):
        t = threading.Thread(target=run)
        t.start()
        started.wait(2)
        time.sleep(0.3)  # give promote time to reach the lock and block on it
        assert t.is_alive(), "promote_rows must block while the CSV lock is held"
        # Simulate the extractor appending a row while it holds the lock.
        pd.DataFrame([_row("10.1/x")])[EXTRACTED_COLS].to_csv(
            main, mode="a", index=False, header=False, encoding="utf-8-sig"
        )

    t.join(5)
    assert not t.is_alive(), "promote_rows should finish once the lock is released"

    dois = set(pd.read_csv(main, dtype=str).fillna("")["doi_r"])
    assert {"10.1/a", "10.1/b", "10.1/x", "10.1/p"} <= dois, f"appended row lost: {dois}"
    assert result["out"]["promoted"] == 1
