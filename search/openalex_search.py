"""
openalex_search.py — Query OpenAlex API for replication/reproduction papers.

Public API:
    fetch_openalex_candidates() → pd.DataFrame  (CANDIDATES_COLS schema)
"""
import time
from typing import Optional

import pandas as pd
import requests

from shared.config import OPENALEX_RATE_SEC, RESEARCHER_EMAIL, log
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi, cache_key

# TODO: implement OpenAlex search for replication keywords
# See misc/openalex_api_example.py for API usage examples.

REPLICATION_KEYWORDS = [
    "replication",
    "direct replication",
    "close replication",
    "conceptual replication",
    "reproduction study",
]


def fetch_openalex_candidates() -> pd.DataFrame:
    """
    Search OpenAlex for papers matching replication keywords.
    Returns a DataFrame with CANDIDATES_COLS schema.
    """
    raise NotImplementedError("fetch_openalex_candidates is not yet implemented")
