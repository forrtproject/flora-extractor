"""
external_lists.py — Scrapers for Bob Reed list, I4R list, and SCORE CSV.

Public API:
    fetch_bob_reed() → pd.DataFrame   (CANDIDATES_COLS schema)
    fetch_i4r()      → pd.DataFrame   (CANDIDATES_COLS schema)
    load_score_csv(path) → pd.DataFrame  (CANDIDATES_COLS schema)
"""
import pandas as pd
import requests

from shared.config import log
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi

# TODO: implement scrapers for each external source.
# Bob Reed list:  https://replicationnetwork.com/replication-studies/
# I4R list:       https://i4replication.org/reports/


def fetch_bob_reed() -> pd.DataFrame:
    """Scrape Bob Reed's Replication Network list."""
    raise NotImplementedError("fetch_bob_reed is not yet implemented")


def fetch_i4r() -> pd.DataFrame:
    """Scrape the Institute for Replication (I4R) reports list."""
    raise NotImplementedError("fetch_i4r is not yet implemented")


def load_score_csv(path: str) -> pd.DataFrame:
    """Load a SCORE CSV file and map to CANDIDATES_COLS schema."""
    raise NotImplementedError("load_score_csv is not yet implemented")
