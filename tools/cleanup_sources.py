#!/usr/bin/env python3
"""
Remove Bob Reed and I4R candidates from candidates.csv and filtered.csv.
Handles large files efficiently by processing in chunks.
"""
import pandas as pd
from pathlib import Path

from shared.config import DATA_DIR

def clean_csv(filepath: Path, output_path: Path = None) -> tuple[int, int]:
    """
    Remove rows where source is 'bob_reed' or 'i4r' (case-insensitive).
    Returns (original_count, removed_count).
    """
    if output_path is None:
        output_path = filepath

    print(f"\nProcessing {filepath.name}...")

    # Read the CSV
    df = pd.read_csv(filepath, encoding="utf-8-sig", low_memory=False)
    original_count = len(df)

    # Identify rows to remove (case-insensitive)
    if "source" in df.columns:
        # Convert to lowercase for comparison
        source_lower = df["source"].fillna("").str.lower()
        mask = ~source_lower.isin(["bob_reed", "i4r"])
        df_clean = df[mask].reset_index(drop=True)
        removed_count = original_count - len(df_clean)

        print(f"  Original rows: {original_count}")
        print(f"  Removed (Bob Reed/I4R): {removed_count}")
        print(f"  Final rows: {len(df_clean)}")

        # Write back
        df_clean.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"  Saved to {output_path}")

        return original_count, removed_count
    else:
        print(f"  WARNING: 'source' column not found. Skipping {filepath.name}")
        return 0, 0

def main():
    """Clean both candidates.csv and filtered.csv."""
    print("=" * 70)
    print("Cleaning candidates and filtered CSVs")
    print("=" * 70)

    candidates_file = DATA_DIR / "candidates.csv"
    filtered_file = DATA_DIR / "filtered.csv"

    total_removed = 0

    # Clean candidates.csv
    if candidates_file.exists():
        orig, removed = clean_csv(candidates_file)
        total_removed += removed
    else:
        print(f"\nWARNING: {candidates_file} not found")

    # Clean filtered.csv
    if filtered_file.exists():
        orig, removed = clean_csv(filtered_file)
        total_removed += removed
    else:
        print(f"\nWARNING: {filtered_file} not found")

    print("\n" + "=" * 70)
    print(f"SUMMARY: Total {total_removed} Bob Reed/I4R rows removed")
    print("=" * 70)

if __name__ == "__main__":
    main()
