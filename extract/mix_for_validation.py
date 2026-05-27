"""
mix_for_validation.py — Sample extracted.csv into a validation-ready mix.

Builds a weighted sample: N% failure rows (optionally from a year range) +
(100-N)% other-outcome rows (from any year), then writes data/validation_sample.csv.

The file is append-compatible with Stage 4 (same schema as extracted.csv).

Usage:
    # Default: 75% failures (2011-2021) + 25% others (any year), up to 400 rows
    python -m extract.mix_for_validation

    # Custom split
    python -m extract.mix_for_validation --failure-pct 70 --n 500

    # Different year range for the failure pool
    python -m extract.mix_for_validation --failure-year-from 2015 --failure-year-to 2023

    # Write to a custom output path
    python -m extract.mix_for_validation --output data/batch1_sample.csv
"""

import argparse
import math
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, log
from shared.schema import EXTRACTED_COLS


def mix_for_validation(
    failure_pct: int = 75,
    failure_year_from: "int | None" = None,
    failure_year_to: "int | None" = None,
    n: "int | None" = None,
    output_path: "Path | None" = None,
    extracted_path: "Path | None" = None,
) -> pd.DataFrame:
    """
    Sample extracted.csv into a validation-ready mix.

    Parameters
    ----------
    failure_pct : int
        Target percentage of failure rows (0–100). Default 75.
    failure_year_from, failure_year_to : int, optional
        Year bounds for the failure pool. Other-outcome rows ignore year.
    n : int, optional
        Total rows in the output. Defaults to all available rows in the
        right ratio (i.e. uses the smaller of the two pools as the bottleneck).
    output_path : Path, optional
        Destination CSV. Defaults to data/validation_sample.csv.
    extracted_path : Path, optional
        Source CSV. Defaults to data/extracted.csv.

    Returns
    -------
    pd.DataFrame
        The mixed sample.
    """
    src = extracted_path or DATA_DIR / "extracted.csv"
    dst = output_path or DATA_DIR / "validation_sample.csv"

    if not src.exists():
        raise FileNotFoundError(f"extracted.csv not found at {src}. Run Stage 3 first.")

    df = pd.read_csv(src, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Loaded extracted.csv: %d rows", len(df))

    # Only fully resolved rows — exclude target_pending / api_error / no_original_found
    resolved_methods = {"author_year_match", "llm_abstract", "llm_fulltext"}
    df = df[df["link_method"].isin(resolved_methods)].copy()
    log.info("Resolved rows (link_method in author_year_match/llm_abstract/llm_fulltext): %d", len(df))

    # ── Build failure pool ────────────────────────────────────────────────────
    failure_mask = df["outcome"] == "failure"
    if failure_year_from is not None or failure_year_to is not None:
        def _yr(v: str) -> "int | None":
            try:
                return int(v)
            except (ValueError, TypeError):
                return None
        years = df["year_r"].apply(_yr)
        if failure_year_from is not None:
            failure_mask &= years.apply(lambda y: y is not None and y >= failure_year_from)
        if failure_year_to is not None:
            failure_mask &= years.apply(lambda y: y is not None and y <= failure_year_to)

    failure_pool = df[failure_mask]
    other_pool   = df[~(df["outcome"] == "failure")]  # any year, any non-failure outcome

    log.info(
        "Failure pool (outcome=failure%s): %d rows",
        f", year {failure_year_from or 'any'}–{failure_year_to or 'any'}"
        if failure_year_from or failure_year_to else "",
        len(failure_pool),
    )
    log.info("Other pool (non-failure, any year): %d rows", len(other_pool))

    other_pct = 100 - failure_pct

    # Determine how many rows to take from each pool.
    # If n is given, use it as a hard cap. Otherwise, take as many as the smaller
    # pool allows while maintaining the requested ratio.
    if n is not None:
        n_failure = min(math.floor(n * failure_pct / 100), len(failure_pool))
        n_other   = min(n - n_failure, len(other_pool))
    else:
        # Ratio-constrained: find the largest N where both pools have enough rows
        max_from_failure = len(failure_pool)
        max_from_other   = len(other_pool)
        if failure_pct == 0:
            n_failure, n_other = 0, max_from_other
        elif other_pct == 0:
            n_failure, n_other = max_from_failure, 0
        else:
            # n_failure / n_other = failure_pct / other_pct
            # constrained by both pool sizes
            n_from_fail_side  = min(max_from_failure,
                                    math.floor(max_from_other * failure_pct / other_pct))
            n_from_other_side = min(max_from_other,
                                    math.floor(max_from_failure * other_pct / failure_pct))
            if n_from_fail_side * other_pct >= n_from_other_side * failure_pct:
                n_failure = n_from_fail_side
                n_other   = math.floor(n_failure * other_pct / failure_pct)
            else:
                n_other   = n_from_other_side
                n_failure = math.floor(n_other * failure_pct / other_pct)

    log.info(
        "Sampling: %d failure + %d other = %d total (%.0f%% / %.0f%%)",
        n_failure, n_other, n_failure + n_other,
        100 * n_failure / max(n_failure + n_other, 1),
        100 * n_other   / max(n_failure + n_other, 1),
    )

    sample_failure = failure_pool.sample(n=n_failure, random_state=42) if n_failure else pd.DataFrame(columns=df.columns)
    sample_other   = other_pool.sample(n=n_other,   random_state=42) if n_other   else pd.DataFrame(columns=df.columns)

    mixed = pd.concat([sample_failure, sample_other], ignore_index=True)
    mixed = mixed.sample(frac=1, random_state=42).reset_index(drop=True)  # shuffle

    # Ensure all schema columns are present
    for col in EXTRACTED_COLS:
        if col not in mixed.columns:
            mixed[col] = ""
    mixed = mixed.reindex(columns=EXTRACTED_COLS, fill_value="")

    mixed.to_csv(dst, index=False, encoding="utf-8-sig")
    log.info("Wrote %d rows → %s", len(mixed), dst)

    # Summary
    outcome_counts = mixed["outcome"].value_counts()
    log.info("Outcome breakdown in sample:")
    for outcome, count in outcome_counts.items():
        pct = 100 * count / len(mixed)
        log.info("  %-15s %4d  (%.1f%%)", outcome, count, pct)

    return mixed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mix extracted.csv into a validation-ready sample.")
    parser.add_argument(
        "--failure-pct", type=int, default=75, metavar="N",
        help="Target %% of failure rows (0–100). Default: 75.",
    )
    parser.add_argument(
        "--failure-year-from", type=int, default=None, metavar="YYYY",
        help="Only pull failure rows from this year onwards.",
    )
    parser.add_argument(
        "--failure-year-to", type=int, default=None, metavar="YYYY",
        help="Only pull failure rows up to this year.",
    )
    parser.add_argument(
        "--n", type=int, default=None, metavar="N",
        help="Total rows in the output. Omit to use all available rows in the right ratio.",
    )
    parser.add_argument(
        "--output", type=str, default=None, metavar="PATH",
        help="Output CSV path. Default: data/validation_sample.csv.",
    )
    parser.add_argument(
        "--extracted", type=str, default=None, metavar="PATH",
        help="Source extracted.csv path. Default: data/extracted.csv.",
    )
    args = parser.parse_args()

    mix_for_validation(
        failure_pct=args.failure_pct,
        failure_year_from=args.failure_year_from,
        failure_year_to=args.failure_year_to,
        n=args.n,
        output_path=Path(args.output) if args.output else None,
        extracted_path=Path(args.extracted) if args.extracted else None,
    )
