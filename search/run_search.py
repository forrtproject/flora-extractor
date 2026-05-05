"""
Stage 1 search orchestrator.

This module runs all configured discovery sources, combines their outputs into a
single candidate table, deduplicates the combined results, and writes the final
Stage 1 output to ``data/candidates.csv``.

Usage:
    python -m search.run_search                        # all years
    python -m search.run_search --from-year 2020       # 2020 onwards
    python -m search.run_search --to-year 2023         # up to 2023
    python -m search.run_search --from-year 2020 --to-year 2023
"""
import argparse
from typing import Optional

import pandas as pd

from shared.config import DATA_DIR, log
from shared.schema import CANDIDATES_COLS
from search.openalex_search import fetch_openalex_candidates
from search.semantic_scholar_search import fetch_semantic_scholar_candidates
from search.external_lists import fetch_i4r
from search.deduplicate import deduplicate_candidates


def run_search(
    from_year: Optional[int] = None,
    to_year:   Optional[int] = None,
) -> pd.DataFrame:
    """Run all Stage 1 discovery sources and write ``data/candidates.csv``.

    The function fetches candidate records from each enabled source adapter,
    concatenates them into a single DataFrame, deduplicates the combined set,
    and saves the cleaned result to disk.

    Parameters
    ----------
    from_year : int, optional
        Earliest publication year (inclusive). None = no lower bound.
    to_year : int, optional
        Latest publication year (inclusive). None = no upper bound.

    Returns
    -------
    pd.DataFrame
        Deduplicated candidate records with columns ordered according to
        ``CANDIDATES_COLS``.
    """
    yr_label = f"{from_year or 'any'}–{to_year or 'any'}"
    log.info("Stage 1 starting  (years: %s)", yr_label)

    # Collect per-source DataFrames here before combining them into one table.
    frames: list[pd.DataFrame] = []

    log.info("Stage 1: fetching OpenAlex candidates...")
    frames.append(fetch_openalex_candidates(from_year=from_year, to_year=to_year))

    log.info("Stage 1: fetching Semantic Scholar candidates...")
    frames.append(fetch_semantic_scholar_candidates(from_year=from_year, to_year=to_year))

    # log.info("Stage 1: fetching Bob Reed list...")
    # frames.append(fetch_bob_reed())

    log.info("Stage 1: fetching I4R list...")
    frames.append(fetch_i4r(from_year=from_year, to_year=to_year))

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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Stage 1 candidate search across all sources."
    )
    parser.add_argument(
        "--from-year", type=int, default=None,
        metavar="YYYY",
        help="Earliest publication year to include (inclusive).",
    )
    parser.add_argument(
        "--to-year", type=int, default=None,
        metavar="YYYY",
        help="Latest publication year to include (inclusive).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_search(from_year=args.from_year, to_year=args.to_year)
