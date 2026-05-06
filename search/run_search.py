"""
Stage 1 search orchestrator.

Each run fetches new candidates from all sources, merges them into the
existing ``data/candidates.csv`` (creating it if absent), and deduplicates
on ``openalex_id_r`` — so the file grows across runs rather than being
overwritten.

OpenAlex phrase jobs are individually resumable: interrupted runs pick up
from the last saved cursor rather than restarting.

Usage
-----
    python -m search.run_search                          # all years, unlimited
    python -m search.run_search --from-year 2020         # 2020 onwards
    python -m search.run_search --to-year 2023           # up to 2023
    python -m search.run_search --from-year 2020 --to-year 2023
    python -m search.run_search --max-per-phrase 200     # 1 page per phrase (quick test)
    python -m search.run_search --reset-cursors          # wipe OpenAlex cursors and start fresh
"""

import argparse
import glob
from typing import Optional

import pandas as pd

from shared.config import DATA_DIR, OA_CACHE_DIR, log
from shared.schema import CANDIDATES_COLS
from search.openalex_search import fetch_openalex_candidates
from search.semantic_scholar_search import fetch_semantic_scholar_candidates
from search.external_lists import fetch_i4r, fetch_replication_network
from search.deduplicate import deduplicate_candidates
from search.engine_source import fetch_engine_candidates, is_engine_enabled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_openalex_cursors() -> None:
    """Delete all saved OpenAlex cursor files so every phrase restarts from page 1."""
    cursor_files = list(OA_CACHE_DIR.glob("*.cursor.json"))
    if not cursor_files:
        log.info("No cursor files found — nothing to reset.")
        return
    for p in cursor_files:
        p.unlink()
    log.info(
        "Deleted %d cursor file(s) — OpenAlex phrases will restart from the beginning.",
        len(cursor_files),
    )


def _merge_into_candidates_csv(
    new_df: pd.DataFrame,
    out_path: "Path",
    dedup_col: str = "openalex_id_r",
) -> pd.DataFrame:
    """
    Read existing candidates.csv (if any), append *new_df*, deduplicate, write back.

    Deduplication prefers the NEW row for any duplicate key so updated
    metadata from a re-fetch takes precedence over older data.
    """
    if out_path.exists():
        existing = pd.read_csv(out_path, encoding="utf-8-sig", low_memory=False)
        log.info("Existing candidates.csv: %d rows", len(existing))
        # Concat new first so it wins on dedup (.keep='first')
        combined = pd.concat([new_df, existing], ignore_index=True)
    else:
        combined = new_df.copy()

    before = len(combined)
    # Deduplicate: drop rows whose dedup_col matches a later duplicate
    combined = combined.drop_duplicates(subset=[dedup_col], keep="first")
    # Also deduplicate rows with no id at all via doi_r
    no_id_mask = combined[dedup_col].isna() | (combined[dedup_col] == "")
    doi_dupes = combined[no_id_mask].duplicated(subset=["doi_r"], keep="first")
    combined = combined[~(no_id_mask & doi_dupes)]

    log.info(
        "Merged: %d → %d rows after dedup (added %d new)",
        before,
        len(combined),
        len(combined) - (before - len(new_df)),
    )

    combined.to_csv(out_path, index=False, encoding="utf-8-sig")
    return combined


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_search(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    max_records_per_phrase: Optional[int] = None,
) -> pd.DataFrame:
    """
    Run all Stage 1 discovery sources and merge results into candidates.csv.

    Parameters
    ----------
    from_year, to_year : int, optional
        Year range (inclusive).  Passed to all sources that support it.
        Note: year range is part of the OpenAlex cursor job identity — using
        a different range starts a new independent set of cursor files.
    max_records_per_phrase : int, optional
        Limit new OpenAlex rows per phrase per run (cursor is saved so the
        next run continues from where this one stopped).  ``None`` = unlimited.
    """
    yr_label = f"{from_year or 'any'}–{to_year or 'any'}"
    log.info("Stage 1 starting  (years: %s)", yr_label)

    frames: list[pd.DataFrame] = []

    if is_engine_enabled():
        log.info("Stage 1: fetching engine candidates (FLORA_USE_ENGINE=1)...")
        frames.append(fetch_engine_candidates(year_from=from_year, year_to=to_year))
    else:
        log.info("Stage 1: fetching OpenAlex candidates...")
        frames.append(
            fetch_openalex_candidates(
                from_year=from_year,
                to_year=to_year,
                max_records_per_phrase=max_records_per_phrase,
            )
        )

    log.info("Stage 1: fetching Semantic Scholar candidates...")
    frames.append(
        fetch_semantic_scholar_candidates(from_year=from_year, to_year=to_year)
    )

    log.info("Stage 1: fetching Replication Network sheet...")
    frames.append(fetch_replication_network(from_year=from_year, to_year=to_year))

    log.info("Stage 1: fetching I4R list...")
    frames.append(fetch_i4r(from_year=from_year, to_year=to_year))

    combined = (
        pd.concat(frames, ignore_index=True)
        if any(not f.empty for f in frames)
        else pd.DataFrame(columns=CANDIDATES_COLS)
    )

    # Deduplicate within this batch first (e.g. same paper in OA + S2)
    new_batch = deduplicate_candidates(combined)
    log.info("New batch (deduped): %d candidates", len(new_batch))

    # Merge into the persistent candidates.csv
    out_path = DATA_DIR / "candidates.csv"
    result = _merge_into_candidates_csv(new_batch, out_path)

    log.info("Stage 1 complete: %d total candidates in %s", len(result), out_path)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Stage 1 candidate search across all sources."
    )
    parser.add_argument(
        "--from-year",
        type=int,
        default=None,
        metavar="YYYY",
        help="Earliest publication year to include (inclusive).",
    )
    parser.add_argument(
        "--to-year",
        type=int,
        default=None,
        metavar="YYYY",
        help="Latest publication year to include (inclusive).",
    )
    parser.add_argument(
        "--max-per-phrase",
        type=int,
        default=None,
        metavar="N",
        help="Limit OpenAlex rows per phrase per run (cursor is saved; "
        "next run continues from this point). Omit for unlimited.",
    )
    parser.add_argument(
        "--reset-cursors",
        action="store_true",
        help="Delete all saved OpenAlex cursor files and start fresh.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.reset_cursors:
        _reset_openalex_cursors()

    run_search(
        from_year=args.from_year,
        to_year=args.to_year,
        max_records_per_phrase=args.max_per_phrase,
    )
