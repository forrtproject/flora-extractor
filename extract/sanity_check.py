"""
sanity_check.py — Post-extraction integrity pass over extracted.csv.

Runs automatically at the end of every `python -m extract.run_extract` (on normal
completion AND on Ctrl-C, via the __main__ finally block), and standalone:

    python -m extract.sanity_check                       # extracted.csv
    python -m extract.sanity_check --input data/extracted-test.csv
    python -m extract.sanity_check --no-move             # report only, move nothing

One automatic mutation: rows with outcome == "not_a_replication" are moved out to
data/not_a_replication.csv (they are false positives that survived the phrase gate,
not resolved replications). Everything else is REPORTED, not changed — some flags are
genuine data characteristics (a real original with no registered DOI) rather than
errors, so the fix is a human/`audit_dois` decision.

The "is doi_o real / does it point to the right article" question is already answered
per row during extraction and stored in `doi_o_verification` (verify_and_correct in
shared/doi_verify.py). This pass reads that column rather than re-hitting the network;
to re-verify/fix flagged DOIs, run `python -m extract.audit_dois --apply`.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR
from shared.schema import EXTRACTED_COLS
from shared.utils import bare_work_id, clean_doi, csv_lock

NOT_A_REP_PATH = DATA_DIR / "not_a_replication.csv"

# doi_o_verification values that mean "could not confirm this DOI is the real original":
#   not_found / no_metadata → DOI unregistered or unfetchable → possibly hallucinated
#   mismatch                → DOI points to a demonstrably different paper
#   api_error               → verification never completed (e.g. rate-limited) → re-run
_UNVERIFIED = {"not_found", "no_metadata", "mismatch", "api_error"}


def _dedup_key(r) -> str:
    """Stable identity for a row. Uses openalex_id_r before doi_r so DOI-less rows
    (whose pair_id all collapse to md5('|')) are not merged into one."""
    oa = bare_work_id(str(r.get("openalex_id_r", "")))
    if oa:
        return f"oa:{oa}"
    doi = clean_doi(str(r.get("doi_r", "")))
    return doi if doi else "t:" + str(r.get("title_r", "")).strip().lower()


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    return df[EXTRACTED_COLS].fillna("")


def _move_not_a_replication(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Move not_a_replication rows to NOT_A_REP_PATH. Returns (kept_df, n_moved)."""
    mask = df["outcome"] == "not_a_replication"
    move = df[mask].copy()
    if move.empty:
        return df, 0

    existing = _norm(pd.read_csv(NOT_A_REP_PATH, dtype=str, keep_default_na=False)) \
        if NOT_A_REP_PATH.exists() else pd.DataFrame(columns=EXTRACTED_COLS)
    # Free r-side work id from the Stage-1 URL where blank (no API call).
    for frame in (existing, move):
        blank = frame["oa_work_id_r"].str.strip() == ""
        frame.loc[blank, "oa_work_id_r"] = frame.loc[blank, "openalex_id_r"].map(bare_work_id)

    combined = pd.concat([existing, _norm(move)], ignore_index=True)
    combined["_k"] = combined.apply(_dedup_key, axis=1)
    combined = combined.drop_duplicates(subset="_k", keep="last").drop(columns="_k")
    combined.to_csv(NOT_A_REP_PATH, index=False, encoding="utf-8-sig")
    return df[~mask], len(move)


def run_sanity_check(path: "str | Path" = None, move: bool = True) -> dict:
    """Move not_a_replication rows out, report integrity flags. Returns a summary dict."""
    path = Path(path) if path else DATA_DIR / "extracted.csv"
    if not path.exists():
        print(f"[sanity] {path} not found — nothing to check.")
        return {}

    df = _norm(pd.read_csv(path, dtype=str, keep_default_na=False))
    n_before = len(df)

    moved = 0
    if move:
        df, moved = _move_not_a_replication(df)
        if moved:
            with csv_lock(path):
                df.to_csv(path, index=False, encoding="utf-8-sig")

    doi_o = df["doi_o"].map(clean_doi)
    doi_r = df["doi_r"].map(clean_doi)
    yo = pd.to_numeric(df["year_o"], errors="coerce")
    yr = pd.to_numeric(df["year_r"], errors="coerce")

    self_link = df[(doi_o != "") & (doi_o == doi_r)]
    chronology = df[(yo > yr) & yo.notna() & yr.notna()]
    unverified = df[df["doi_o_verification"].isin(_UNVERIFIED)]
    dup_pair = df[(df["pair_id"].str.strip() != "") & df["pair_id"].duplicated(keep=False)
                  & (doi_r != "")]
    blank_doi_r = df[doi_r == ""]

    ver = df["doi_o_verification"].value_counts().to_dict()
    summary = {
        "rows_before": n_before, "rows_after": len(df), "not_a_replication_moved": moved,
        "self_links": len(self_link), "chronology_errors": len(chronology),
        "unverified_doi_o": len(unverified), "duplicate_pair_ids": len(dup_pair),
        "blank_doi_r": len(blank_doi_r), "verification_mix": ver,
    }

    print("\n" + "=" * 70)
    print("EXTRACTED.CSV SANITY CHECK")
    print("=" * 70)
    print(f"  not_a_replication moved -> {NOT_A_REP_PATH.name}: {moved}")
    print(f"  rows now in {path.name}: {len(df)}")
    print("  -- flags (reported, not changed) --")
    print(f"  self-links (doi_o == doi_r):        {len(self_link)}")
    print(f"  chronology errors (year_o > year_r):{len(chronology)}")
    print(f"  unverified/possibly-hallucinated doi_o: {len(unverified)} "
          f"(not_found/no_metadata/mismatch/api_error)")
    print(f"  duplicate pair_ids:                 {len(dup_pair)}")
    print(f"  blank doi_r:                        {len(blank_doi_r)}")
    print(f"  doi_o_verification: {ver}")
    if len(unverified) or len(chronology) or len(self_link):
        print("  -> re-verify/fix flagged DOIs: python -m extract.audit_dois --apply")
    print("=" * 70 + "\n")
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sanity-check extracted.csv.")
    p.add_argument("--input", type=Path, default=None,
                   help="CSV to check (default: data/extracted.csv).")
    p.add_argument("--no-move", action="store_true",
                   help="Report only; do not move not_a_replication rows.")
    a = p.parse_args()
    run_sanity_check(a.input, move=not a.no_move)
