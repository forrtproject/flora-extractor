"""
run_overlap_analysis.py — Orchestrator for Task 2 overlap analysis.

Compares current pipeline (candidates.csv, filtered.csv) against ground-truth
reference (all_replications.csv) to identify:
  1a. Recall gaps (known replications we didn't discover)
  1b. Filter gaps (discoveries we wrongly filtered out)
  1c. Older pipeline archaeology (why older pipeline differs)
  1d. Source contribution (which sources are underrepresented)
  1e. Filter rule breakdown (which rules reject valid replications)

Usage:
    python -m analysis.run_overlap_analysis
    python -m analysis.run_overlap_analysis --old-pipeline-dir <path>
"""

import argparse
from pathlib import Path

from shared.config import DATA_DIR, log
from analysis.analyses import (
    analyze_recall_gap,
    analyze_filter_gap,
    analyze_older_pipeline,
    analyze_source_contribution,
    analyze_filter_rules,
)
from analysis.output_writer import write_gap_csv, write_report_markdown


def main():
    parser = argparse.ArgumentParser(
        description="Run Task 2 overlap analysis."
    )
    parser.add_argument(
        "--old-pipeline-dir",
        type=Path,
        default=None,
        help="Path to older pipeline code for archaeology (optional).",
    )
    args = parser.parse_args()

    log.info("=" * 70)
    log.info("Task 2: Deep Overlap Analysis")
    log.info("=" * 70)
    log.info(f"Candidates: {DATA_DIR / 'candidates.csv'}")
    log.info(f"Filtered: {DATA_DIR / 'filtered.csv'}")
    log.info(f"Reference: {DATA_DIR / 'all_replications.csv'}")

    if args.old_pipeline_dir:
        log.info(f"Older pipeline code: {args.old_pipeline_dir}")

    output_dir = DATA_DIR.parent / "analysis"
    output_dir.mkdir(exist_ok=True)

    # Run analyses
    log.info("\nRunning Analysis 1a: Recall gap...")
    gaps_doi, gaps_url, gaps_fuzzy = analyze_recall_gap()

    log.info("Running Analysis 1b: Filter gap...")
    misclassifications = analyze_filter_gap()

    log.info("Running Analysis 1c: Older pipeline archaeology...")
    archaeology = analyze_older_pipeline(args.old_pipeline_dir)

    log.info("Running Analysis 1d: Source contribution...")
    sources = analyze_source_contribution()

    log.info("Running Analysis 1e: Filter rule breakdown...")
    rules = analyze_filter_rules()

    # Write outputs
    log.info("\nWriting CSV outputs...")
    write_gap_csv(gaps_doi, output_dir / "gap_analysis_doi_matched.csv", "doi")
    write_gap_csv(gaps_url, output_dir / "gap_analysis_url_matched.csv", "url")
    write_gap_csv(gaps_fuzzy, output_dir / "gap_analysis_fuzzy_title.csv", "fuzzy_title")
    write_gap_csv(misclassifications, output_dir / "filter_misclassifications.csv", "filter")
    write_gap_csv(sources, output_dir / "source_contribution.csv", "source")
    write_gap_csv(rules, output_dir / "filter_rules.csv", "rules")

    log.info("Writing markdown report...")
    write_report_markdown(
        output_dir / "gap_summary.md",
        gaps_doi,
        gaps_url,
        gaps_fuzzy,
        misclassifications,
        sources,
        rules,
        archaeology,
    )

    log.info("\n" + "=" * 70)
    log.info(f"✓ Analysis complete. Outputs in {output_dir}/")
    log.info("=" * 70)
    log.info("Generated files:")
    log.info("  - gap_analysis_doi_matched.csv")
    log.info("  - gap_analysis_url_matched.csv")
    log.info("  - gap_analysis_fuzzy_title.csv")
    log.info("  - filter_misclassifications.csv")
    log.info("  - source_contribution.csv")
    log.info("  - filter_rules.csv")
    log.info("  - gap_summary.md")


if __name__ == "__main__":
    main()
