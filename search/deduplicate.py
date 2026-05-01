"""
deduplicate.py — Merge sources and deduplicate by DOI / fuzzy title.

Public API:
    deduplicate_candidates(df) → pd.DataFrame
"""
import pandas as pd
from rapidfuzz import fuzz

from shared.config import DATA_DIR, log
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi

FLORA_SHEET_PATH = DATA_DIR / "flora_entry_sheet.csv"
TITLE_MATCH_THRESHOLD = 90


def _load_flora_dois() -> set[str]:
    """Return the set of DOIs already in the FLoRA entry sheet."""
    if not FLORA_SHEET_PATH.exists():
        log.warning("FLoRA entry sheet not found at %s — skipping deduplication against it", FLORA_SHEET_PATH)
        return set()
    df = pd.read_csv(FLORA_SHEET_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    if "doi_r" not in df.columns:
        return set()
    return {clean_doi(d) for d in df["doi_r"] if d.strip()}


def deduplicate_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge all source rows, remove duplicates, and cross-check against FLoRA.

    Deduplication order:
      1. Exact DOI match (clean_doi)
      2. Fuzzy title match (threshold 90, using rapidfuzz)
      3. Remove DOIs already in the FLoRA entry sheet
    """
    # TODO: implement full deduplication logic
    raise NotImplementedError("deduplicate_candidates is not yet implemented")
