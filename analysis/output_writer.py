"""
output_writer.py — CSV and markdown output generators.

Handles:
  - Writing gap analysis CSVs (with utf-8-sig BOM for Excel)
  - Building narrative markdown report with findings and recommendations
"""

import pandas as pd
from pathlib import Path
from typing import Optional

from shared.config import log


def write_gap_csv(
    df: pd.DataFrame,
    output_path: Path,
    match_method: str,
) -> None:
    """
    Write gap analysis DataFrame to CSV with Excel-friendly encoding.

    Args:
        df: DataFrame to write
        output_path: Path to output file
        match_method: "doi", "url", or "fuzzy_title" (for context)
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(df) == 0:
        # Write empty file with headers
        pd.DataFrame().to_csv(output_path, index=False, encoding="utf-8-sig")
        log.info(f"  (empty) {output_path.name}")
    else:
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        log.info(f"  {output_path.name}: {len(df)} rows")


def write_report_markdown(
    output_path: Path,
    gaps_doi: pd.DataFrame,
    gaps_url: pd.DataFrame,
    gaps_fuzzy: pd.DataFrame,
    misclassifications: pd.DataFrame,
    source_stats: pd.DataFrame,
    rule_stats: pd.DataFrame,
    archaeology_findings: dict,
) -> None:
    """
    Write comprehensive markdown report with all findings.

    Args:
        output_path: Path to output file
        gaps_*: Gap DataFrames from analyses 1a
        misclassifications: DataFrame from analysis 1b
        source_stats: DataFrame from analysis 1d
        rule_stats: DataFrame from analysis 1e
        archaeology_findings: Dict from analysis 1c
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []

    lines.append("# Task 2: Overlap Analysis Report\n")
    lines.append(f"Generated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Executive Summary
    lines.append("## Executive Summary\n")

    total_refs = len(gaps_doi) + len(gaps_url) + len(gaps_fuzzy)
    total_misclass = len(misclassifications)

    lines.append(f"- **Gap Analysis Results** (known replications absent from candidates.csv):")
    lines.append(f"  - Gaps with DOI: {len(gaps_doi)}")
    lines.append(f"  - Gaps URL-only (no DOI): {len(gaps_url)}")
    lines.append(f"  - Fuzzy-matched (found via title, not a gap): {len(gaps_fuzzy)}")
    lines.append(f"  - Total genuine gaps: {total_refs}")
    lines.append(f"- **Filter misclassifications**: {total_misclass} rows")
    if len(source_stats) > 0:
        lines.append(
            f"- **Underrepresented sources**: {(source_stats.get('underrepresented', '') == 'YES').sum() if isinstance(source_stats, pd.DataFrame) else 'unknown'} sources\n"
        )
    else:
        lines.append("")

    # Recall Gap Details
    lines.append("## Recall Gaps (Analysis 1a)\n")
    lines.append(
        f"Known replications in all_replications.csv absent from candidates.csv: {total_refs}\n"
    )

    if len(gaps_doi) > 0:
        lines.append(f"### Gaps with DOI ({len(gaps_doi)} rows)")
        try:
            lines.append(gaps_doi[["doi_r", "study_r", "year_r"]].head(10).to_markdown())
        except:
            lines.append(f"{len(gaps_doi)} rows with DOI-matched gaps")
        lines.append("\n")

    if len(gaps_url) > 0:
        lines.append(f"### Gaps URL-only / no DOI ({len(gaps_url)} rows)")
        try:
            lines.append(gaps_url[["url_r", "study_r", "year_r"]].head(10).to_markdown())
        except:
            lines.append(f"{len(gaps_url)} rows with URL-matched gaps")
        lines.append("\n")

    if len(gaps_fuzzy) > 0:
        lines.append(f"### Fuzzy-matched gaps ({len(gaps_fuzzy)} rows)")
        try:
            lines.append(
                gaps_fuzzy[["study_r", "year_r", "match_confidence"]].head(10).to_markdown()
            )
        except:
            lines.append(f"{len(gaps_fuzzy)} rows with fuzzy-matched gaps")
        lines.append("\n")

    # Filter Gap Details
    lines.append("## Filter Gaps (Analysis 1b)\n")
    lines.append(f"Replications discovered but wrongly filtered: {total_misclass}\n")

    if len(misclassifications) > 0:
        try:
            lines.append(
                misclassifications[["doi_r", "title_r", "filter_status", "filter_evidence"]]
                .head(20)
                .to_markdown()
            )
        except:
            lines.append(f"{len(misclassifications)} rows misclassified")
        lines.append("\n")

    # Source Contribution
    if len(source_stats) > 0:
        lines.append("## Source Contribution (Analysis 1d)\n")
        try:
            lines.append(source_stats.to_markdown(index=False))
        except:
            pass
        lines.append("\n")

    # Filter Rule Breakdown
    if len(rule_stats) > 0:
        lines.append("## Filter Rule Breakdown (Analysis 1e)\n")
        try:
            lines.append(rule_stats.to_markdown(index=False))
        except:
            pass
        lines.append("\n")

    # Archaeology
    if archaeology_findings and archaeology_findings.get("status") != "skipped":
        lines.append("## Older Pipeline Archaeology (Analysis 1c)\n")
        if archaeology_findings.get("status") == "partial":
            lines.append(archaeology_findings.get("message", ""))
            lines.append("\nRecommended files to review:\n")
            for f in archaeology_findings.get("recommended_files", []):
                lines.append(f"- {f}\n")
        lines.append("\n")

    # Recommendations
    lines.append("## Recommendations\n")
    lines.append(
        "1. **Investigate recall gaps**: Why are known replications missing? Check search keywords and sources.\n"
    )
    lines.append(
        "2. **Review misclassifications**: Correct filter rules to avoid losing valid replications.\n"
    )
    lines.append("3. **Augment underrepresented sources**: Adjust search strategy for weak sources.\n")
    lines.append(
        "4. **Older pipeline archaeology**: Manually document differences in search strategy.\n"
    )

    # Write to file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"  Report: {output_path.name}")
