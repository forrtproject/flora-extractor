"""
phrase_coverage_analysis.py — Compare phrase-detection coverage between the current
filter pipeline and an expanded phrase set ported from the old R pipeline.

Motivation
----------
The current REPLICATION_PHRASES list in filter/phrase_detection.py misses ~5,400
patterns per 500k candidates that the old R pipeline's explicit_replication_claims
would have caught.  This script:

  1. Loads candidates.csv
  2. Skips rows with no abstract (counts them)
  3. Applies BOTH the current and expanded phrase sets (vectorized)
  4. Compares against filtered.csv to identify candidates currently marked
     false_positive that the expanded set would have kept
  5. Prints a full summary report and saves phrase_coverage_recovery.csv

Usage
-----
    python -m analysis.phrase_coverage_analysis
    python -m analysis.phrase_coverage_analysis --limit 200000
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Phrase sets  (stored as plain strings for vectorized str.contains use)
# ---------------------------------------------------------------------------

CURRENT_PHRASE_STRINGS: list[str] = [
    r"\breplication of\b",
    r"\bwe replicated\b",
    r"\bwe replicate\b",
    r"\breplicating the findings\b",
    r"\bdirect replication\b",
    r"\bconceptual replication\b",
    r"\bpreregistered replication\b",
    r"\bregistered replication\b",
    r"\bfailed to replicate\b",
    r"\bdid not replicate\b",
    r"\bcould not reproduce\b",
    r"\bsuccessfully replicated\b",
    r"\breproducibility of\b",
    r"\breplication and extensions?\b",
    r"\bregistered report of\b",
    r"\b(?:close|high[-\s]powered|pre[-\s]?registered|large[-\s]scale)\s+replication\b",
    r"\breplication (?:and|&) extension\b",
    r"\breproduce[ds]?\s+(?:the\s+)?(?:original\s+)?(?:findings?|effects?|results?)\b",
]

# Phrases ported from the old R pipeline's explicit_replication_claims that are
# NOT covered by CURRENT_PHRASE_STRINGS.
ADDED_PHRASE_STRINGS: list[str] = [
    r"\battempt\w*\s+to\s+replicate\b",
    r"\baim\w*\s+to\s+replicate\b",
    r"\bset\s+out\s+to\s+replicate\b",
    r"\bsuccess\w*\s+replicat\w*\b",
    r"\bwe\s+(?:conducted|performed|carried\s+out)\s+a\s+replication\b",
    r"\b(?:many-?labs?|multi-?site)\s+replication\b",
    r"\breplicat\w*\s+and\s+exten\w*\b",
    r"\breplication\s+stud(?:y|ies)\s+of\b",
    r"\bstudy\s+replicate[sd]\b",
    r"\bour\s+replication\b",
    r"\bindependent\s+replication\b",
    r"\bexact\s+replication\b",
    r"\breplication\s+attempt\b",
    r"\bcross-?(?:cultural|national|lab(?:oratory)?)\s+replication\b",
]

# Combined regex strings (single pattern, much faster than looping)
_CURRENT_COMBINED   = "(?:" + "|".join(CURRENT_PHRASE_STRINGS) + ")"
_ADDED_COMBINED     = "(?:" + "|".join(ADDED_PHRASE_STRINGS) + ")"
_EXPANDED_COMBINED  = "(?:" + "|".join(CURRENT_PHRASE_STRINGS + ADDED_PHRASE_STRINGS) + ")"

# Exclusion patterns — id + combined regex per group
EXCLUSION_PATTERNS: list[tuple[str, str]] = [
    ("BIOLOGICAL",
     r"\b(?:dna|rna|viral|virus|cell|cellular|chromosome|plasmid)\s+replication\b"),
    ("TECHNICAL_OBJECT",
     r"\b(?:replication of (?:the )?(?:apparatus|code|dataset|data|database|model|method|"
     r"pipeline|protocol|software|simulation)|(?:apparatus|code|dataset|data|database|model|"
     r"method|pipeline|protocol|software|simulation)\s+replication)\b"),
    ("TECHNICAL_VERB",
     r"\breplicat(?:e|ed|ing)\s+(?:the )?(?:apparatus|code|dataset|data|database|model|"
     r"method|pipeline|protocol|software|simulation)\b"),
    ("STRUCTURAL",
     r"\breplication\s+(?:fork|origin|stress|timing)\b"),
]
_EXCLUSION_COMBINED = "(?:" + "|".join(p for _, p in EXCLUSION_PATTERNS) + ")"


# ---------------------------------------------------------------------------
# Vectorized helpers
# ---------------------------------------------------------------------------

def _vec_contains(series: pd.Series, pattern: str) -> pd.Series:
    """Case-insensitive vectorized str.contains — NaN-safe, returns bool Series."""
    return series.str.contains(pattern, flags=re.IGNORECASE, regex=True, na=False)


def _first_added_hit(text: str) -> str:
    """Return the matched text of the first ADDED phrase that fires (for sample display)."""
    for p in ADDED_PHRASE_STRINGS:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0).lower()
    return ""


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def run_analysis(limit: Optional[int] = None) -> None:
    data_dir = Path(__file__).parent.parent / "data"
    candidates_path = data_dir / "candidates.csv"
    filtered_path   = data_dir / "filtered.csv"

    if not candidates_path.exists():
        sys.exit(f"ERROR: {candidates_path} not found.")

    # ------------------------------------------------------------------
    # Load candidates (only columns we need)
    # ------------------------------------------------------------------
    print(f"\n{'='*70}")
    print("PHRASE COVERAGE ANALYSIS")
    print(f"{'='*70}")
    print(f"\nLoading candidates.csv (limit={limit or 'none'})...")

    use_cols = ["doi_r", "title_r", "abstract_r", "year_r", "authors_r", "source"]
    chunks: list[pd.DataFrame] = []
    total_read = 0

    for chunk in pd.read_csv(
        candidates_path, dtype=str, encoding="utf-8-sig",
        chunksize=50_000, low_memory=False, usecols=use_cols,
    ):
        chunk = chunk.fillna("")
        total_read += len(chunk)
        if limit:
            taken_so_far = sum(len(c) for c in chunks)
            remaining = limit - taken_so_far
            if remaining <= 0:
                break
            chunk = chunk.iloc[:remaining]
        chunks.append(chunk)
        if limit and sum(len(c) for c in chunks) >= limit:
            break

    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    print(f"  Rows loaded: {len(df):,}")

    # ------------------------------------------------------------------
    # Skip rows with missing abstracts
    # ------------------------------------------------------------------
    has_abstract    = df["abstract_r"].str.strip().astype(bool)
    no_abstract_count = int((~has_abstract).sum())
    df = df[has_abstract].copy()

    print(f"  Skipped (no abstract): {no_abstract_count:,}  "
          f"({100 * no_abstract_count / max(total_read, 1):.1f}% of loaded rows)")
    print(f"  Rows with abstract:    {len(df):,}")

    # ------------------------------------------------------------------
    # Build combined text column (vectorized)
    # ------------------------------------------------------------------
    print("\nClassifying rows (vectorized)...")
    text = (df["title_r"] + " " + df["abstract_r"]).str.strip()

    # Exclusion pass
    is_excluded = _vec_contains(text, _EXCLUSION_COMBINED)

    # Phrase detection on non-excluded rows
    not_excl = ~is_excluded
    current_pass  = not_excl & _vec_contains(text, _CURRENT_COMBINED)
    expanded_pass = not_excl & _vec_contains(text, _EXPANDED_COMBINED)
    new_catches   = expanded_pass & ~current_pass   # added phrases only, no exclusion

    # Per-exclusion-type counts
    excl_counts: dict[str, int] = {}
    for eid, epat in EXCLUSION_PATTERNS:
        excl_counts[eid] = int(_vec_contains(text, epat).sum())

    # Per-added-phrase counts (only on the new-catch subset — small)
    added_phrase_counts: dict[str, int] = {}
    if new_catches.any():
        new_text = text[new_catches]
        for pat in ADDED_PHRASE_STRINGS:
            cnt = int(_vec_contains(new_text, pat).sum())
            if cnt:
                added_phrase_counts[pat] = cnt
        added_phrase_counts = dict(sorted(added_phrase_counts.items(), key=lambda x: -x[1]))

    print("  Done.")

    # ------------------------------------------------------------------
    # Compare against filtered.csv (only 2 columns needed)
    # ------------------------------------------------------------------
    filtered_exists = filtered_path.exists()
    fp_total = kept_total = 0
    new_catch_dois: set[str] = set()
    rescued_dois: set[str] = set()
    filtered_total = 0

    if filtered_exists:
        print("\nLoading filtered.csv (doi_r + filter_status only)...")
        filt_chunks: list[pd.DataFrame] = []
        filt_read = 0
        for fc in pd.read_csv(
            filtered_path, dtype=str, encoding="utf-8-sig",
            chunksize=50_000, low_memory=False,
            usecols=["doi_r", "filter_status"],
        ):
            fc = fc.fillna("")
            filt_read += len(fc)
            filt_chunks.append(fc)
            if limit and filt_read >= limit:
                break

        filt_df = pd.concat(filt_chunks, ignore_index=True)
        filtered_total = len(filt_df)
        print(f"  Filtered rows loaded: {filtered_total:,}")

        fp_dois   = set(filt_df.loc[filt_df["filter_status"] == "false_positive",   "doi_r"].str.strip())
        kept_dois = set(filt_df.loc[filt_df["filter_status"].isin(["replication", "reproduction"]), "doi_r"].str.strip())
        fp_total   = len(fp_dois)
        kept_total = len(kept_dois)

        new_catch_dois = set(df.loc[new_catches, "doi_r"].str.strip()) - {""}
        rescued_dois   = new_catch_dois & fp_dois

    # ------------------------------------------------------------------
    # Print report
    # ------------------------------------------------------------------
    print(f"\n{'='*70}")
    print("RESULTS")
    print(f"{'='*70}")

    print(f"\n-- Candidate pool ({'first ' + str(limit) + ' rows' if limit else 'full file'})")
    print(f"   Total rows loaded:          {total_read:,}")
    print(f"   Missing abstract (skipped): {no_abstract_count:,}  ({100*no_abstract_count/max(total_read,1):.1f}%)")
    print(f"   Rows analysed:              {len(df):,}")

    print(f"\n-- Exclusion patterns fired (over all analysed rows)")
    for eid, cnt in sorted(excl_counts.items(), key=lambda x: -x[1]):
        print(f"   {eid:<20} {cnt:>8,}")
    print(f"   {'TOTAL (any excl.)':<20} {int(is_excluded.sum()):>8,}")

    n_not_excl = int(not_excl.sum())
    print(f"\n-- Phrase detection (on {n_not_excl:,} non-excluded rows)")
    print(f"   Current phrases pass:        {int(current_pass.sum()):>8,}  "
          f"({100*current_pass.sum()/max(n_not_excl,1):.2f}%)")
    print(f"   Expanded phrases pass:       {int(expanded_pass.sum()):>8,}  "
          f"({100*expanded_pass.sum()/max(n_not_excl,1):.2f}%)")
    print(f"   NEWLY CAUGHT by expansion:   {int(new_catches.sum()):>8,}  "
          f"({100*new_catches.sum()/max(n_not_excl,1):.2f}%)")

    print(f"\n-- Which added phrases are firing (rows caught only by expansion)")
    for pat_str, cnt in added_phrase_counts.items():
        display = pat_str[:65] + "..." if len(pat_str) > 65 else pat_str
        print(f"   {cnt:>6,}  {display}")

    if filtered_exists:
        print(f"\n-- Comparison against filtered.csv")
        print(f"   Filtered rows loaded:        {filtered_total:,}")
        print(f"   Currently false_positive:    {fp_total:,}  "
              f"({100*fp_total/max(filtered_total,1):.1f}%)")
        print(f"   Currently kept (rep/repro):  {kept_total:,}  "
              f"({100*kept_total/max(filtered_total,1):.1f}%)")
        print(f"\n   New-catch rows with DOI:     {len(new_catch_dois):,}")
        print(f"   Of those currently FP:       {len(rescued_dois):,}  "
              f"<-- candidates expansion rescues from false_positive")

    # ------------------------------------------------------------------
    # Sample of newly caught rows
    # ------------------------------------------------------------------
    sample_df = df[new_catches][["doi_r", "title_r", "year_r"]].head(25).copy()
    sample_df["matched_phrase"] = (
        text[new_catches].head(25).apply(_first_added_hit).values
    )

    print(f"\n-- Sample of newly caught candidates (up to 25)")
    print(f"   (pass expanded but NOT current phrases; not excluded)")
    if sample_df.empty:
        print("   (none)")
    else:
        for _, row in sample_df.iterrows():
            doi    = (row.get("doi_r",   "") or "(no DOI)")[:32]
            title  = (row.get("title_r", "") or "(no title)")[:65]
            year   = row.get("year_r", "")
            phrase = row.get("matched_phrase", "") or ""
            safe_title = title.encode("ascii", "replace").decode("ascii")
            print(f'   [{year}] {doi:<32}  "{phrase}"')
            print(f'           {safe_title}')

    # ------------------------------------------------------------------
    # Save recovery list
    # ------------------------------------------------------------------
    out_path = Path(__file__).parent / "phrase_coverage_recovery.csv"
    recovery_df = df[new_catches][
        ["doi_r", "title_r", "abstract_r", "year_r", "authors_r", "source"]
    ].copy()
    recovery_df["matched_added_phrase"] = text[new_catches].apply(_first_added_hit).values
    recovery_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n-- Recovery list saved: analysis/phrase_coverage_recovery.csv")
    print(f"   {len(recovery_df):,} rows that the expanded phrase set would rescue")
    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phrase coverage gap analysis")
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Analyse only the first N rows of candidates.csv (default: all)",
    )
    args = parser.parse_args()
    run_analysis(limit=args.limit)
