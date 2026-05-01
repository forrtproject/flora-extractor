"""
run_filter.py — Stage 2 orchestrator.

Reads data/candidates.csv, applies rule-based then LLM filtering,
and writes data/filtered.csv.

Usage:
    python filter/run_filter.py
"""
import pandas as pd

from shared.config import DATA_DIR, log
from shared.schema import FILTERED_COLS
from filter.rule_filter import apply_rule_filter
from filter.llm_filter import apply_llm_filter


def run_filter() -> pd.DataFrame:
    """Run the filter pipeline and write data/filtered.csv."""
    candidates_path = DATA_DIR / "candidates.csv"
    if not candidates_path.exists():
        raise FileNotFoundError(f"candidates.csv not found at {candidates_path}. Run Stage 1 first.")

    df = pd.read_csv(candidates_path, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Stage 2: loaded %d candidates", len(df))

    df = apply_rule_filter(df)
    log.info("Stage 2: rule filter done. needs_review: %d",
             (df["filter_status"] == "needs_review").sum())

    df = apply_llm_filter(df)
    log.info("Stage 2: LLM filter done.")

    out_path = DATA_DIR / "filtered.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("Stage 2 complete: %d rows → %s", len(df), out_path)
    return df


if __name__ == "__main__":
    run_filter()
