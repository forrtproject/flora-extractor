"""
backfill_gap_rows.py — Add old-pipeline TP gap rows directly to candidates.csv.

The new pipeline's OpenAlex/S2 search (2011–2026) missed some confirmed
replications that were in all_replications.csv.  This script identifies those
rows, maps their columns to the candidates.csv schema, and appends them via
the same dedup-safe merge used by run_search.py.

Safety guarantees (same as run_search):
  - Uses the persistent candidates_index to skip rows already present — safe
    to run multiple times or after a partial run.
  - Abstract enrichment (CrossRef / S2 fallback) runs automatically inside
    _merge_into_candidates_csv so every row arrives with the best available
    abstract.
  - Rows are tagged source="backfill_old_pipeline" so they're traceable.

Usage:
    python -m search.backfill_gap_rows                          # dry-run
    python -m search.backfill_gap_rows --apply                  # write to candidates.csv
    python -m search.backfill_gap_rows --from-year 2011 --to-year 2026 --apply
    python -m search.backfill_gap_rows --all-years --apply      # include pre-2011 rows too
"""

import argparse
import sys

import pandas as pd

from shared.config import DATA_DIR, log
from shared.utils import clean_doi
from shared.schema import CANDIDATES_COLS
from search.run_search import (
    _merge_into_candidates_csv,
    _load_or_build_candidates_index,
)

_SOURCE_TAG = "backfill_old_pipeline"


# ── Column mapping ────────────────────────────────────────────────────────────

def _to_candidates_row(row: pd.Series) -> dict:
    """Map an all_replications row to candidates.csv schema."""
    doi = clean_doi(str(row.get("doi_r", "") or ""))
    url = str(row.get("url_r", "") or "").strip()

    # url_r in all_replications is either:
    #   "https://doi.org/10.xxx/yyy"  (doi row)  — keep as url_r
    #   "https://openalex.org/W..."   (oa-id row) — also becomes openalex_id_r
    oa_id = url if url.startswith("https://openalex.org/") else ""

    # If no separate landing-page URL, derive it from the DOI
    if not url and doi:
        url = f"https://doi.org/{doi}"

    return {
        "doi_r":         doi,
        "title_r":       str(row.get("study_r", "") or "").strip(),
        "abstract_r":    str(row.get("abstract_r", "") or "").strip(),
        "year_r":        str(row.get("year_r", "") or "").strip(),
        "authors_r":     "",          # not stored in all_replications
        "journal_r":     "",          # not stored in all_replications
        "url_r":         url,
        "openalex_id_r": oa_id,
        "source":        _SOURCE_TAG,
        "ref_r":         str(row.get("ref_r", "") or "").strip(),
    }


# ── Loader ────────────────────────────────────────────────────────────────────

def load_gap_rows(from_year: int, to_year: int, all_years: bool) -> pd.DataFrame:
    """Load confirmed TP rows from all_replications.csv that are NOT already in
    candidates.csv, optionally filtered to a year range.

    Returns a DataFrame ready for _merge_into_candidates_csv.
    """
    ar_path = DATA_DIR / "all_replications.csv"
    if not ar_path.exists():
        log.error("data/all_replications.csv not found")
        sys.exit(1)

    log.info("Loading all_replications.csv …")
    ar = pd.read_csv(ar_path, dtype=str)
    ar["doi_r"]  = ar["doi_r"].fillna("").str.strip().str.lower()
    ar["url_r"]  = ar["url_r"].fillna("").str.strip()
    ar["year_r"] = ar["year_r"].fillna("").str.strip()
    ar["type"]   = ar["type"].fillna("")

    # TP rows only
    tp = ar[ar["type"].isin(["replication", "reproduction"])].copy()
    log.info("Total TP rows in all_replications.csv: %d", len(tp))

    # Year filter
    if not all_years:
        valid_years = {str(y) for y in range(from_year, to_year + 1)}
        tp = tp[tp["year_r"].isin(valid_years)]
        log.info("After year filter (%d–%d): %d rows", from_year, to_year, len(tp))

    # Check which rows aren't already in candidates.csv
    cand_path = DATA_DIR / "candidates.csv"
    if cand_path.exists():
        log.info("Loading candidates index to detect already-present rows …")
        index = _load_or_build_candidates_index(cand_path)
        log.info("Index has %d keys", len(index))
    else:
        index = set()
        log.warning("candidates.csv not found — will create it")

    def _already_present(row: pd.Series) -> bool:
        doi = clean_doi(str(row.get("doi_r", "") or ""))
        url = str(row.get("url_r", "") or "").strip()
        title = str(row.get("study_r", "") or "").lower().strip()
        oa_id = url if url.startswith("https://openalex.org/") else ""
        candidates = []
        if oa_id:
            candidates.append(f"oa:{oa_id}")
        if doi:
            candidates.append(doi)
        if url:
            candidates.append(f"url:{url}")
        if title:
            candidates.append(f"title:{title}")
        return any(k in index for k in candidates)

    already = tp.apply(_already_present, axis=1)
    missing = tp[~already].copy()
    present = tp[already].copy()

    log.info(
        "Already in candidates: %d  |  Missing (will backfill): %d",
        len(present), len(missing),
    )

    # Breakdown by identifier type
    doi_missing = missing[missing["doi_r"] != ""]
    url_missing = missing[missing["doi_r"] == ""]
    log.info("  Missing with DOI: %d", len(doi_missing))
    log.info("  Missing with OA-ID only: %d", len(url_missing))

    # Map to candidates schema
    rows = [_to_candidates_row(r) for _, r in missing.iterrows()]
    if not rows:
        return pd.DataFrame(columns=CANDIDATES_COLS)

    df = pd.DataFrame(rows, columns=CANDIDATES_COLS)
    df = df.fillna("")
    return df


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Write to candidates.csv (default: dry-run only)")
    parser.add_argument("--from-year", type=int, default=2011,
                        help="Start year inclusive (default: 2011)")
    parser.add_argument("--to-year", type=int, default=2026,
                        help="End year inclusive (default: 2026)")
    parser.add_argument("--all-years", action="store_true",
                        help="Include rows from all years, not just from-year to to-year")
    args = parser.parse_args()

    year_desc = "all years" if args.all_years else f"{args.from_year}–{args.to_year}"
    log.info("=" * 60)
    log.info("Backfill gap rows — %s", year_desc)
    log.info("Mode: %s", "APPLY (will write)" if args.apply else "DRY RUN (read-only)")
    log.info("=" * 60)

    gap_df = load_gap_rows(args.from_year, args.to_year, args.all_years)

    if gap_df.empty:
        log.info("No new rows to add — candidates.csv is already complete for this range.")
        return

    log.info("Rows to add: %d", len(gap_df))

    # Preview sample
    log.info("Sample rows:")
    for _, r in gap_df.head(5).iterrows():
        log.info("  [%s] %s (%s)  doi=%s",
                 r["source"], r["title_r"][:60], r["year_r"], r["doi_r"] or r["openalex_id_r"] or "—")

    if not args.apply:
        log.info("")
        log.info("DRY RUN — nothing written. Re-run with --apply to add these rows.")
        log.info("Command: python -m search.backfill_gap_rows --apply")
        return

    out_path = DATA_DIR / "candidates.csv"
    log.info("Writing to %s …", out_path)
    _merge_into_candidates_csv(gap_df, out_path)
    log.info("Done. Backfilled rows are tagged source='%s'.", _SOURCE_TAG)
    log.info("Next: re-run Stage 2 filter on candidates.csv to classify the new rows.")


if __name__ == "__main__":
    main()
