"""
analyses.py — Five analysis functions for overlap comparison (1a-1e).

Each analysis loads data, performs comparisons, and returns structured results.
"""

import pandas as pd
from typing import Tuple

from shared.config import log
from analysis.data_loader import load_candidates, load_filtered, load_all_replications
from analysis.matching import find_best_match


def analyze_recall_gap() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Analysis 1a: Find replications in all_replications.csv not in candidates.csv.

    Compares replication studies in both datasets to identify gaps (known replications
    we failed to discover in Stage 1).

    Uses indexed lookups to avoid O(n²) complexity on large datasets.

    Returns:
        (gaps_by_doi, gaps_by_url, gaps_by_fuzzy)
        Each is a DataFrame of unmatched reference rows, separated by match method.
    """
    candidates = load_candidates()
    all_reps = load_all_replications()

    # Build indices for fast lookup
    doi_index = candidates.set_index("doi_r")
    url_index = candidates.set_index("url_r")

    gaps_by_doi = []
    gaps_by_url = []
    gaps_by_fuzzy = []

    log.info(f"Comparing {len(all_reps)} reference rows against {len(candidates)} candidates...")

    for ref_idx, ref_row in all_reps.iterrows():
        if ref_idx % 5000 == 0:
            log.info(f"  Progress: {ref_idx}/{len(all_reps)}")

        best_method = None
        best_confidence = 0.0

        # Try DOI lookup first (O(1))
        ref_doi = ref_row.get("doi_r", "")
        if ref_doi and ref_doi in doi_index.index:
            best_method = "doi"
            best_confidence = 1.0
        else:
            # Try URL lookup (O(1))
            ref_url = ref_row.get("url_r", "")
            if ref_url and ref_url in url_index.index:
                best_method = "url"
                best_confidence = 1.0
            else:
                # Try fuzzy match on year + author + title for unmatched rows
                # Only sample candidates to avoid O(n²) - match first 1000 by year/author
                ref_year = ref_row.get("year_r")
                ref_authors = str(ref_row.get("authors_r", "")).split(",")[0].strip().lower()

                candidates_same_year = candidates[candidates["year_r"] == ref_year]
                for cand_idx, cand_row in candidates_same_year.head(1000).iterrows():
                    cand_dict = {
                        "doi_r": cand_row.get("doi_r", ""),
                        "url_r": cand_row.get("url_r", ""),
                        "title_r": cand_row.get("title_r", ""),
                        "year_r": cand_row.get("year_r"),
                        "authors_r": cand_row.get("authors_r", ""),
                    }
                    ref_dict = {
                        "doi_o": ref_row.get("doi_r", ""),
                        "doi_r": ref_row.get("doi_r", ""),
                        "url_o": ref_row.get("url_r", ""),
                        "url_r": ref_row.get("url_r", ""),
                        "title_o": ref_row.get("study_r", ""),
                        "title_r": ref_row.get("study_r", ""),
                        "year_o": ref_row.get("year_r"),
                        "year_r": ref_row.get("year_r"),
                        "authors_o": ref_row.get("authors_r", ""),
                        "authors_r": ref_row.get("authors_r", ""),
                    }

                    match_result = find_best_match(cand_dict, ref_dict)
                    if match_result:
                        method, confidence = match_result
                        if confidence > best_confidence:
                            best_method = method
                            best_confidence = confidence
                        break  # Found a fuzzy match, stop searching

        # Categorize gap
        gap_row = ref_row.to_dict()
        gap_row["match_status"] = "matched" if best_method else "unmatched"
        gap_row["match_method"] = best_method
        gap_row["match_confidence"] = best_confidence if best_method else None

        if best_method == "doi":
            gaps_by_doi.append(gap_row)
        elif best_method == "url":
            gaps_by_url.append(gap_row)
        elif best_method == "fuzzy_title":
            gaps_by_fuzzy.append(gap_row)
        else:
            # Unmatched - add to DOI list for visibility
            gaps_by_doi.append(gap_row)

    log.info(f"Analysis 1a complete: {len(gaps_by_doi)} DOI, {len(gaps_by_url)} URL, {len(gaps_by_fuzzy)} Fuzzy matches")

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
