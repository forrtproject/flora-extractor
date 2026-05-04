"""
Stage 1 search orchestrator.

This module runs all configured discovery sources, combines their outputs into a
single candidate table, deduplicates the combined results, and writes the final
Stage 1 output to ``data/candidates.csv``.

Usage:
    python search/run_search.py
"""

import pandas as pd

from shared.config import DATA_DIR, log
from shared.schema import CANDIDATES_COLS
from search.openalex_search import fetch_openalex_candidates
from search.semantic_scholar_search import fetch_semantic_scholar
from search.external_lists import fetch_bob_reed, fetch_i4r
from search.deduplicate import deduplicate_candidates


def run_search() -> pd.DataFrame:
    """Run all Stage 1 discovery sources and write ``data/candidates.csv``.

    The function fetches candidate records from each enabled source adapter,
    concatenates them into a single DataFrame, deduplicates the combined set,
    and saves the cleaned result to disk.

    Returns
    -------
    pd.DataFrame
        Deduplicated candidate records with columns ordered according to
        ``CANDIDATES_COLS``.
    """
    # Collect per-source DataFrames here before combining them into one table.
    frames: list[pd.DataFrame] = []

    log.info("Stage 1: fetching OpenAlex candidates...")
    frames.append(fetch_openalex_candidates())

    log.info("Stage 1: fetching Semantic Scholar candidates...")
    frames.append(fetch_semantic_scholar())

    # Optional supplementary sources. These are currently disabled.
    # log.info("Stage 1: fetching Bob Reed list...")
    # frames.append(fetch_bob_reed())

    # log.info("Stage 1: fetching I4R list...")
    # frames.append(fetch_i4r())

    # pd.concat([]) raises an error, so fall back to an empty DataFrame with the
    # canonical schema if no sources were enabled or all fetching was skipped.
    combined = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=CANDIDATES_COLS)
    )

    result = deduplicate_candidates(combined)

    out_path = DATA_DIR / "candidates.csv"

    # Write with utf-8-sig so the CSV opens more reliably in Excel while still
    # remaining valid UTF-8 for programmatic use. Passing the path directly lets
    # pandas apply the requested encoding to the output file.
    result.to_csv(out_path, index=False, encoding="utf-8-sig")

    log.info("Stage 1 complete: %d candidates → %s", len(result), out_path)
    return result


if __name__ == "__main__":
    run_search()
