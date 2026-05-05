"""
Utilities for merging search-source outputs and removing duplicate candidates.

This module applies a simple multi-pass deduplication strategy to candidate
paper records gathered from different discovery sources. It first collapses
exact DOI duplicates, then looks for likely duplicates among DOI-less rows
using fuzzy title matching, and finally removes papers that are already present
in the FLoRA entry sheet.

Public API:
    deduplicate_candidates(df) -> pd.DataFrame
"""

import pandas as pd
from rapidfuzz import fuzz

from shared.config import DATA_DIR, log
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi


# Path to the existing FLoRA entry sheet used to exclude already catalogued items.
FLORA_SHEET_PATH = DATA_DIR / "flora_entry_sheet.csv"

# Fuzzy title similarity threshold. RapidFuzz token_sort_ratio sorts words before
# comparing strings, which makes it useful when titles differ mainly in word order.
TITLE_MATCH_THRESHOLD = 90


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_flora_dois() -> set[str]:
    """Load the set of DOIs already present in the FLoRA entry sheet.

    The file is treated as optional. If it is missing, the pipeline logs a
    warning and continues without performing the cross-check.

    Returns
    -------
    set[str]
        Cleaned DOI strings found in the ``doi_r`` column of the FLoRA sheet.
        Returns an empty set if the file is missing or the expected column is absent.
    """
    if not FLORA_SHEET_PATH.exists():
        log.warning(
            "FLoRA entry sheet not found at %s — skipping cross-check",
            FLORA_SHEET_PATH,
        )
        return set()

    # Read as strings so DOI values are not mangled by type inference.
    # fillna("") makes downstream string handling simpler and more predictable.
    df = pd.read_csv(FLORA_SHEET_PATH, dtype=str, encoding="utf-8-sig").fillna("")

    if "doi_r" not in df.columns:
        # Missing column means we cannot perform a DOI-based exclusion.
        return set()

    # Clean DOI formatting so comparisons are robust to differences like
    # prefixes, casing, or surrounding whitespace.
    return {clean_doi(d) for d in df["doi_r"] if d.strip()}


def _richness(row: pd.Series) -> int:
    """Count the number of populated fields in a candidate row.

    This is used as a simple heuristic for deciding which duplicate record to
    keep: the row with more non-empty metadata is assumed to be more useful.

    Parameters
    ----------
    row
        Candidate row.

    Returns
    -------
    int
        Number of fields that are non-null and not blank-like.
    """
    # Treat None, empty strings, and the string "nan" as missing for scoring.
    return sum(1 for v in row if v is not None and str(v).strip() not in ("", "nan"))


def _best_row(group: pd.DataFrame) -> pd.Series:
    """Select the richest row from a group of presumed duplicates.

    Parameters
    ----------
    group
        Group of rows representing the same paper.

    Returns
    -------
    pd.Series
        The row with the highest richness score.
    """
    scores = group.apply(_richness, axis=1)
    return group.loc[scores.idxmax()]


# ---------------------------------------------------------------------------
# Pass 1 — exact DOI deduplication
# ---------------------------------------------------------------------------


def _dedup_by_doi(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse exact DOI duplicates, keeping the richest row per DOI.

    Rows without a DOI are left untouched and reattached after DOI-based
    deduplication is complete.

    Parameters
    ----------
    df
        Candidate records.

    Returns
    -------
    pd.DataFrame
        DataFrame with duplicate DOI-bearing rows collapsed.
    """
    df = df.copy()

    # Normalise DOI formatting before comparing so equivalent DOIs match.
    df["doi_r"] = df["doi_r"].apply(clean_doi)

    # Split into DOI-bearing and DOI-less rows because DOI deduplication only
    # applies where we have a usable identifier.
    has_doi = df["doi_r"].notna() & (df["doi_r"].str.strip() != "")
    with_doi = df[has_doi]
    without = df[~has_doi]

    if with_doi.empty:
        # Nothing to deduplicate by DOI; return the original data unchanged.
        return df

    # Group by normalised DOI and keep the row with the most complete metadata.
    # Note: pandas groupby.apply on DataFrames may emit a deprecation warning in
    # newer versions because grouping columns are included by default for now.
    deduped = (
        with_doi.groupby("doi_r", sort=False).apply(_best_row).reset_index(drop=True)
    )

    removed = len(with_doi) - len(deduped)
    if removed:
        log.info(
            "Pass 1 (DOI):   %d → %d rows  (%d duplicates removed)",
            len(with_doi),
            len(deduped),
            removed,
        )

    # Reattach rows that had no DOI; they will be handled in the title pass.
    return pd.concat([deduped, without], ignore_index=True)


# ---------------------------------------------------------------------------
# Pass 2 — fuzzy title deduplication (DOI-less rows only)
# ---------------------------------------------------------------------------


def _dedup_by_title(df: pd.DataFrame) -> pd.DataFrame:
    """Remove likely duplicates among rows that do not have a DOI.

    Title matching is restricted to DOI-less rows to avoid overriding a stronger
    identifier-based match. Similarity is computed with
    ``rapidfuzz.fuzz.token_sort_ratio``, which is relatively tolerant to word
    order changes in multi-word strings.

    Parameters
    ----------
    df
        Candidate records after DOI deduplication.

    Returns
    -------
    pd.DataFrame
        DataFrame with likely title duplicates removed among DOI-less rows.
    """
    # Keep DOI-bearing rows separate: they have already gone through the more
    # reliable exact-match deduplication pass.
    has_doi = df["doi_r"].notna() & (df["doi_r"].str.strip() != "")
    with_doi = df[has_doi]
    no_doi = df[~has_doi].reset_index(drop=True)

    if len(no_doi) < 2:
        # Fuzzy matching only makes sense when there are at least two candidates.
        return df

    # Lowercase titles for a simple case-insensitive comparison baseline.
    # Missing titles are treated as empty strings.
    titles = no_doi["title_r"].fillna("").str.lower().tolist()
    drop: set[int] = set()

    # Quadratic pairwise comparison is simple and acceptable here because the
    # DOI-less subset is expected to be modest in size. If this grows large,
    # blocking/indexing would be worth adding.
    for i in range(len(no_doi)):
        if i in drop:
            continue

        for j in range(i + 1, len(no_doi)):
            if j in drop:
                continue

            # token_sort_ratio sorts tokens before comparison, so titles that
            # differ mostly by word order can still score highly.
            if fuzz.token_sort_ratio(titles[i], titles[j]) >= TITLE_MATCH_THRESHOLD:
                ri = _richness(no_doi.iloc[i])
                rj = _richness(no_doi.iloc[j])

                # Keep the richer row and mark the weaker one for removal.
                drop.add(j if ri >= rj else i)

                # If i was dropped, stop comparing it to later rows.
                if i in drop:
                    break

    kept = no_doi.drop(index=list(drop))
    removed = len(no_doi) - len(kept)

    if removed:
        log.info(
            "Pass 2 (title): %d → %d rows  (%d duplicates removed)",
            len(no_doi),
            len(kept),
            removed,
        )

    # Recombine DOI-bearing rows with surviving DOI-less rows.
    return pd.concat([with_doi, kept], ignore_index=True)


# ---------------------------------------------------------------------------
# Pass 3 — FLoRA cross-check
# ---------------------------------------------------------------------------


def _remove_flora_dois(df: pd.DataFrame, flora_dois: set[str]) -> pd.DataFrame:
    """Remove rows whose DOI already exists in the FLoRA entry sheet.

    Parameters
    ----------
    df
        Candidate records after internal deduplication.
    flora_dois
        Set of cleaned DOI strings already present in FLoRA.

    Returns
    -------
    pd.DataFrame
        Filtered DataFrame excluding rows already catalogued in FLoRA.
    """
    if not flora_dois:
        # No known DOIs to exclude, so leave the data unchanged.
        return df

    before = len(df)

    # Re-clean each DOI before comparison so the exclusion logic is resilient to
    # formatting differences introduced upstream.
    mask = df["doi_r"].apply(
        lambda d: clean_doi(d) not in flora_dois if pd.notna(d) else True
    )

    df = df[mask].reset_index(drop=True)
    removed = before - len(df)

    if removed:
        log.info("FLoRA cross-check: removed %d already-catalogued DOIs", removed)

    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def deduplicate_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate candidate rows and remove papers already present in FLoRA.

    Deduplication order:
    1. Exact DOI match: keep the richest row per DOI.
    2. Fuzzy title match: compare DOI-less rows only.
    3. FLoRA cross-check: remove rows whose DOI is already catalogued.

    Parameters
    ----------
    df
        Combined candidate records from one or more search sources.

    Returns
    -------
    pd.DataFrame
        Deduplicated candidate records with columns ordered according to
        ``CANDIDATES_COLS``.
    """
    log.info("Deduplication starting: %d rows", len(df))

    # Reindex to the canonical schema so downstream code sees a consistent
    # column set and order, even if some sources omitted some fields.
    df = df.reindex(columns=CANDIDATES_COLS)

    # Apply passes from strongest identifier to weakest heuristic.
    df = _dedup_by_doi(df)
    df = _dedup_by_title(df)
    df = _remove_flora_dois(df, _load_flora_dois())

    log.info("Deduplication complete: %d rows remain", len(df))
    return df.reset_index(drop=True)
