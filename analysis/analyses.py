"""
analyses.py — Five analysis functions for overlap comparison (1a-1e).

Each analysis loads data, performs comparisons, and returns structured results.
"""

import pandas as pd
from typing import Tuple

from shared.config import log
from analysis.data_loader import load_all_replications


def _build_candidate_index_sets() -> Tuple[set, set]:
    """Build DOI and URL/OA-ID index sets from candidates.csv in chunks to avoid OOM.

    The URL set includes both url_r (open-access PDF / landing page) AND
    openalex_id_r (e.g. 'https://openalex.org/W...') so that papers found by
    the old pipeline via work-ID URL still match correctly.

    Returns:
        (doi_set, url_set) — cleaned DOIs and all URL/OA-ID values in candidates.
    """
    from shared.config import DATA_DIR

    doi_set: set = set()
    url_set: set = set()
    path = DATA_DIR / "candidates.csv"
    chunks = pd.read_csv(
        path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
        usecols=lambda c: c in ("doi_r", "url_r", "openalex_id_r"), chunksize=50_000,
    )
    for chunk in chunks:
        if "doi_r" in chunk.columns:
            doi_set.update(chunk["doi_r"].dropna().str.strip().str.lower())
        if "url_r" in chunk.columns:
            url_set.update(chunk["url_r"].dropna().str.strip())
        # Also index the OpenAlex work ID so old-pipeline openalex.org/W... URLs match
        if "openalex_id_r" in chunk.columns:
            url_set.update(chunk["openalex_id_r"].dropna().str.strip())
    doi_set.discard("")
    url_set.discard("")
    log.info("Candidate index: %d DOIs, %d URL/OA-IDs", len(doi_set), len(url_set))
    return doi_set, url_set


def analyze_recall_gap() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Analysis 1a: Find replications in all_replications.csv not in candidates.csv.

    Compares the ground-truth replication set against Stage 1 candidates to find
    genuine recall gaps — papers we failed to discover.  Uses chunked index sets to
    handle candidates.csv being too large to load entirely into memory.

    Returns:
        (gaps_by_doi, gaps_by_url, gaps_by_fuzzy)
        gaps_by_doi  — unmatched rows that have a DOI identifier
        gaps_by_url  — unmatched rows with a URL but no DOI
        gaps_by_fuzzy — always empty (fuzzy match disabled for large candidates)
    """
    doi_set, url_set = _build_candidate_index_sets()
    all_reps = load_all_replications()

    gaps_by_doi: list = []
    gaps_by_url: list = []
    gaps_by_fuzzy: list = []  # kept for API compatibility; always empty

    log.info(f"Comparing {len(all_reps)} reference rows against candidate index...")

    matched = 0
    for ref_idx, ref_row in all_reps.iterrows():
        if ref_idx % 5000 == 0:
            log.info(f"  Progress: {ref_idx}/{len(all_reps)}")

        ref_doi = str(ref_row.get("doi_r") or "").strip().lower()
        ref_url = str(ref_row.get("url_r") or "").strip()

        if ref_doi and ref_doi in doi_set:
            matched += 1
            continue  # found — not a gap
        if ref_url and ref_url in url_set:
            matched += 1
            continue  # found — not a gap

        # Genuine gap: record it
        gap_row = ref_row.to_dict()
        gap_row["match_status"] = "unmatched"
        gap_row["match_method"] = None
        gap_row["match_confidence"] = None

        if ref_doi:
            gaps_by_doi.append(gap_row)
        elif ref_url:
            gaps_by_url.append(gap_row)
        else:
            gaps_by_doi.append(gap_row)  # no identifier; group with DOI gaps

    log.info(
        f"Analysis 1a complete: {matched} matched, "
        f"{len(gaps_by_doi)} gaps with DOI, {len(gaps_by_url)} gaps URL-only"
    )

    return (
        pd.DataFrame(gaps_by_doi) if gaps_by_doi else pd.DataFrame(),
        pd.DataFrame(gaps_by_url) if gaps_by_url else pd.DataFrame(),
        pd.DataFrame(gaps_by_fuzzy) if gaps_by_fuzzy else pd.DataFrame(),
    )


def analyze_filter_gap() -> pd.DataFrame:
    """
    Analysis 1b: Find replications we discovered but wrongly filtered out.

    Placeholder — will be implemented in Task 5.

    Returns:
        DataFrame of misclassified rows with filter evidence.
    """
    return pd.DataFrame()


def analyze_older_pipeline(old_pipeline_dir=None) -> dict:
    """
    Analysis 1c: Archaeology of older pipeline code.

    Placeholder — will be implemented in Task 6.

    Returns:
        Dict with findings (keywords, sources, differences, etc.)
    """
    return {}


def analyze_source_contribution() -> pd.DataFrame:
    """
    Analysis 1d: Source contribution breakdown.

    Placeholder — will be implemented in Task 7.

    Returns:
        DataFrame with source counts and contribution analysis.
    """
    return pd.DataFrame()


def analyze_filter_rules() -> pd.DataFrame:
    """
    Analysis 1e: Filter rule breakdown.

    Placeholder — will be implemented in Task 8.

    Returns:
        DataFrame with rule frequency and LLM verdict analysis.
    """
    return pd.DataFrame()
