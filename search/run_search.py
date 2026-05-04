"""
run_search.py — Stage 1 orchestrator.

Calls all discovery sources, deduplicates, and writes data/candidates.csv.

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
    """Run all search sources and write data/candidates.csv."""
    frames = []

    log.info("Stage 1: fetching OpenAlex candidates...")
    frames.append(fetch_openalex_candidates())

    log.info("Stage 1: fetching Semantic Scholar candidates...")
    frames.append(fetch_semantic_scholar())

    # log.info("Stage 1: fetching Bob Reed list...")
    # frames.append(fetch_bob_reed())

    # log.info("Stage 1: fetching I4R list...")
    # frames.append(fetch_i4r())

    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=CANDIDATES_COLS)

    out_path = DATA_DIR / "candidates.csv"
    # result = deduplicate_candidates(combined)
    result.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("Stage 1 complete: %d candidates → %s", len(result), out_path)
    return result


if __name__ == "__main__":
    run_search()
