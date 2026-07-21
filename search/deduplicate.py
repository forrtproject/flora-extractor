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

import re

import pandas as pd
from rapidfuzz import fuzz

from shared.config import DATA_DIR, log
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi


# Paths to FLoRA data files for deduplication
FLORA_SHEET_PATH = DATA_DIR / "flora_entry_sheet.csv"
FLORA_CSV_PATH = DATA_DIR / "flora.csv"

# Fuzzy title similarity threshold. RapidFuzz token_sort_ratio sorts words before
# comparing strings, which makes it useful when titles differ mainly in word order.
TITLE_MATCH_THRESHOLD = 90

# DOI prefix for figshare — a data/figure repository, never a scholarly paper.
_FIGSHARE_PREFIX = "10.6084/"

# PeerJ embeds peer reviews as citable objects with "/reviews/N" in the DOI path.
_PEER_REVIEW_RE = re.compile(r"/reviews/", re.IGNORECASE)

# Versioned preprint DOIs end with _vN (e.g. 10.31234/osf.io/abc123_v2).
_VERSIONED_RE = re.compile(r"^(.+?)_v(\d+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_flora_dois() -> set[str]:
    """Load the set of DOIs already present in FLoRA data (entry sheet + flora.csv).

    Checks both FLORA_SHEET_PATH and FLORA_CSV_PATH. The files are treated as
    optional. If missing, the pipeline logs a warning and continues without
    performing the cross-check.

    Returns
    -------
    set[str]
        Cleaned DOI strings found in the ``doi_r``, ``doi_o`` columns of FLoRA data.
        Returns an empty set if all files are missing or expected columns are absent.
    """
    flora_dois = set()

    # Check flora_entry_sheet.csv
    if FLORA_SHEET_PATH.exists():
        try:
            df = pd.read_csv(FLORA_SHEET_PATH, dtype=str, encoding="utf-8-sig").fillna("")
            if "doi_r" in df.columns:
                flora_dois.update({clean_doi(d) for d in df["doi_r"] if d.strip()})
            log.info("FLoRA entry sheet: loaded %d DOIs", len(flora_dois))
        except Exception as e:
            log.warning("FLoRA entry sheet read failed (%s) — skipping", e)
    else:
        log.warning("FLoRA entry sheet not found at %s — skipping", FLORA_SHEET_PATH)

    # Check flora.csv
    if FLORA_CSV_PATH.exists():
        try:
            df = pd.read_csv(FLORA_CSV_PATH, dtype=str, encoding="utf-8-sig").fillna("")
            before = len(flora_dois)

            # Check both doi_r (replication) and doi_o (original) columns
            if "doi_r" in df.columns:
                flora_dois.update({clean_doi(d) for d in df["doi_r"] if d.strip()})
            if "doi_o" in df.columns:
                flora_dois.update({clean_doi(d) for d in df["doi_o"] if d.strip()})

            added = len(flora_dois) - before
            log.info("flora.csv: loaded %d additional unique DOIs (total now %d)", added, len(flora_dois))
        except Exception as e:
            log.warning("flora.csv read failed (%s) — skipping", e)
    else:
        log.debug("flora.csv not found at %s — skipping", FLORA_CSV_PATH)

    return flora_dois


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
# Pass 0a — DOI-pattern exclusions (figshare, peer reviews)
# ---------------------------------------------------------------------------


def _exclude_by_doi_pattern(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows whose DOI identifies a non-paper object.

    Excluded patterns:
    - ``10.6084/`` — figshare datasets/figures; never a scholarly paper.
    - ``/reviews/`` in the DOI path — PeerJ embeds peer-review text as
      citable objects distinct from the reviewed article.
    """
    def _should_exclude(doi: str) -> bool:
        if not doi or not isinstance(doi, str):
            return False
        doi = doi.strip()
        return doi.startswith(_FIGSHARE_PREFIX) or bool(_PEER_REVIEW_RE.search(doi))

    before = len(df)
    mask = ~df["doi_r"].apply(_should_exclude)
    df = df[mask].reset_index(drop=True)
    removed = before - len(df)
    if removed:
        log.info("Pass 0a (DOI patterns): removed %d figshare/peer-review rows", removed)
    return df


# ---------------------------------------------------------------------------
# Pass 0b — versioned preprint deduplication
# ---------------------------------------------------------------------------


def _dedup_versioned_preprints(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse versioned preprint DOIs (e.g. ``_v1``, ``_v2``) to one row.

    For each group of DOIs sharing the same base (the part before ``_vN``):

    - If a non-versioned DOI with the same base already exists in the
      dataset, drop *all* versioned variants — the canonical DOI wins.
    - Otherwise keep only the row with the highest version number and drop
      all lower-version rows.

    Surviving rows pass through the regular exact-DOI deduplication pass
    so that any remaining duplicates at the same version are handled there.
    """
    has_doi = df["doi_r"].notna() & (df["doi_r"].str.strip() != "")
    versioned_mask = has_doi & df["doi_r"].apply(
        lambda d: bool(_VERSIONED_RE.match(str(d).strip()))
    )

    if not versioned_mask.any():
        return df

    all_dois: set[str] = set(df.loc[has_doi, "doi_r"].str.strip())

    drop_indices: set[int] = set()

    # Group versioned rows by their base DOI.
    base_groups: dict[str, list[tuple[int, int]]] = {}
    for idx in df.index[versioned_mask]:
        doi = str(df.at[idx, "doi_r"]).strip()
        m = _VERSIONED_RE.match(doi)
        if not m:
            continue
        base, version = m.group(1), int(m.group(2))
        base_groups.setdefault(base, []).append((idx, version))

    for base, group in base_groups.items():
        if base in all_dois:
            # Unversioned canonical DOI present — drop every versioned variant.
            drop_indices.update(idx for idx, _ in group)
        else:
            # No canonical DOI — keep only the highest version.
            max_v = max(v for _, v in group)
            drop_indices.update(idx for idx, v in group if v < max_v)

    if drop_indices:
        log.info(
            "Pass 0b (versions): removed %d lower-version / superseded preprint DOIs",
            len(drop_indices),
        )

    return df.drop(index=list(drop_indices)).reset_index(drop=True)


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
    # Score all rows at once, then use idxmax() per group — avoids the
    # np.vstack that groupby.apply() uses internally, which causes MemoryError
    # on batches of millions of rows.
    scores = with_doi.apply(_richness, axis=1)
    best_indices = scores.groupby(with_doi["doi_r"], sort=False).idxmax()
    deduped = with_doi.loc[best_indices].reset_index(drop=True)

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
    df = _exclude_by_doi_pattern(df)
    df = _dedup_versioned_preprints(df)
    df = _dedup_by_doi(df)
    df = _dedup_by_title(df)
    df = _remove_flora_dois(df, _load_flora_dois())

    log.info("Deduplication complete: %d rows remain", len(df))
    return df.reset_index(drop=True)
