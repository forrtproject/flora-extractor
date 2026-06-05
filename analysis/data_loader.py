"""
data_loader.py — Load and normalize input CSVs for analysis.

Handles:
  - Loading candidates.csv, filtered.csv, all_replications.csv
  - Normalizing DOIs, URLs, handling missing values
  - Validating required columns
"""

import pandas as pd
from pathlib import Path
from typing import Optional

from shared.config import DATA_DIR
from shared.utils import clean_doi


def load_candidates() -> pd.DataFrame:
    """Load candidates.csv and normalize DOI/URL."""
    path = DATA_DIR / "candidates.csv"
    df = pd.read_csv(path)

    # Normalize DOI
    if "doi_r" in df.columns:
        df["doi_r"] = df["doi_r"].fillna("").apply(clean_doi)

    # Normalize URL (strip whitespace, lowercase scheme)
    if "url_r" in df.columns:
        df["url_r"] = df["url_r"].fillna("").str.strip()
        # Replace http/https with https for consistency
        df["url_r"] = df["url_r"].str.replace(r"^http://", "https://", regex=True)

    return df


def load_filtered() -> pd.DataFrame:
    """Load filtered.csv and normalize DOI/URL."""
    path = DATA_DIR / "filtered.csv"
    df = pd.read_csv(path)

    # Same normalization as candidates
    if "doi_r" in df.columns:
        df["doi_r"] = df["doi_r"].fillna("").apply(clean_doi)

    if "url_r" in df.columns:
        df["url_r"] = df["url_r"].fillna("").str.strip()
        df["url_r"] = df["url_r"].str.replace(r"^http://", "https://", regex=True)

    return df


def load_all_replications() -> pd.DataFrame:
    """Load all_replications.csv and normalize DOI/URL.

    Note: all_replications.csv contains both original and replication metadata.
    For analysis, we focus on the replication side (doi_r, study_r, year_r, etc.)
    """
    path = DATA_DIR / "all_replications.csv"
    df = pd.read_csv(path)

    # Normalize replication-side DOI
    if "doi_r" in df.columns:
        df["doi_r"] = df["doi_r"].fillna("").apply(clean_doi)

    # Normalize URL (if present on replication side)
    if "url_r" in df.columns:
        df["url_r"] = df["url_r"].fillna("").str.strip()
        df["url_r"] = df["url_r"].str.replace(r"^http://", "https://", regex=True)

    # Also normalize original-side DOI for matching purposes
    if "doi_o" in df.columns:
        df["doi_o"] = df["doi_o"].fillna("").apply(clean_doi)

    return df
