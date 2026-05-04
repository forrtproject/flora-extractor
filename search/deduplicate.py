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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_flora_dois() -> set[str]:
    """Return the set of DOIs already in the FLoRA entry sheet."""
    if not FLORA_SHEET_PATH.exists():
        log.warning("FLoRA entry sheet not found at %s — skipping cross-check", FLORA_SHEET_PATH)
        return set()
    df = pd.read_csv(FLORA_SHEET_PATH, dtype=str, encoding="utf-8-sig").fillna("")
    if "doi_r" not in df.columns:
        return set()
    return {clean_doi(d) for d in df["doi_r"] if d.strip()}


def _richness(row: pd.Series) -> int:
    """Count non-empty fields — used to pick the best row when collapsing duplicates."""
    return sum(1 for v in row if v is not None and str(v).strip() not in ("", "nan"))


def _best_row(group: pd.DataFrame) -> pd.Series:
    """Return the richest row from a group of duplicates."""
    scores = group.apply(_richness, axis=1)
    return group.loc[scores.idxmax()]


# ---------------------------------------------------------------------------
# Pass 1 — exact DOI deduplication
# ---------------------------------------------------------------------------

def _dedup_by_doi(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["doi_r"] = df["doi_r"].apply(clean_doi)

    has_doi = df["doi_r"].notna() & (df["doi_r"].str.strip() != "")
    with_doi = df[has_doi]
    without  = df[~has_doi]

    if with_doi.empty:
        return df

    deduped = (
        with_doi.groupby("doi_r", sort=False)
        .apply(_best_row)
        .reset_index(drop=True)
    )

    removed = len(with_doi) - len(deduped)
    if removed:
        log.info("Pass 1 (DOI):   %d → %d rows  (%d duplicates removed)",
                 len(with_doi), len(deduped), removed)

    return pd.concat([deduped, without], ignore_index=True)


# ---------------------------------------------------------------------------
# Pass 2 — fuzzy title deduplication (DOI-less rows only)
# ---------------------------------------------------------------------------

def _dedup_by_title(df: pd.DataFrame) -> pd.DataFrame:
    has_doi = df["doi_r"].notna() & (df["doi_r"].str.strip() != "")
    with_doi = df[has_doi]
    no_doi   = df[~has_doi].reset_index(drop=True)

    if len(no_doi) < 2:
        return df

    titles = no_doi["title_r"].fillna("").str.lower().tolist()
    drop: set[int] = set()

    for i in range(len(no_doi)):
        if i in drop:
            continue
        for j in range(i + 1, len(no_doi)):
            if j in drop:
                continue
            if fuzz.token_sort_ratio(titles[i], titles[j]) >= TITLE_MATCH_THRESHOLD:
                ri = _richness(no_doi.iloc[i])
                rj = _richness(no_doi.iloc[j])
                drop.add(j if ri >= rj else i)
                if i in drop:
                    break  # i is dropped; no point comparing further

    kept = no_doi.drop(index=list(drop))
    removed = len(no_doi) - len(kept)
    if removed:
        log.info("Pass 2 (title): %d → %d rows  (%d duplicates removed)",
                 len(no_doi), len(kept), removed)

    return pd.concat([with_doi, kept], ignore_index=True)


# ---------------------------------------------------------------------------
# Pass 3 — FLoRA cross-check
# ---------------------------------------------------------------------------

def _remove_flora_dois(df: pd.DataFrame, flora_dois: set[str]) -> pd.DataFrame:
    if not flora_dois:
        return df
    before = len(df)
    mask = df["doi_r"].apply(lambda d: clean_doi(d) not in flora_dois if pd.notna(d) else True)
    df = df[mask].reset_index(drop=True)
    removed = before - len(df)
    if removed:
        log.info("FLoRA cross-check: removed %d already-catalogued DOIs", removed)
    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deduplicate_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge all source rows, remove duplicates, and cross-check against FLoRA.

    Deduplication order:
      1. Exact DOI match   (keep richest row per DOI)
      2. Fuzzy title match (threshold 90, DOI-less rows only)
      3. Remove DOIs already in the FLoRA entry sheet
    """
    log.info("Deduplication starting: %d rows", len(df))

    df = df.reindex(columns=CANDIDATES_COLS)   # ensure consistent column order
    df = _dedup_by_doi(df)
    df = _dedup_by_title(df)
    df = _remove_flora_dois(df, _load_flora_dois())

    log.info("Deduplication complete: %d rows remain", len(df))
    return df.reset_index(drop=True)
