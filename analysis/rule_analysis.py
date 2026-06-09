"""
rule_analysis.py — Audit current extraction performance and identify improvements.

Phase 1 of Task 4: Analyze failures in extracted.csv to understand where
rule-based filtering and original linking are falling short.
"""

import pandas as pd
from typing import Dict, Any
from shared.config import DATA_DIR, log


def audit_extracted_csv() -> Dict[str, Any]:
    """
    Audit extracted.csv and return statistics about current performance.

    Returns dict with:
    - total_rows: count of rows
    - by_link_method: distribution of link_method values
    - by_link_confidence: distribution of link_confidence values
    - by_outcome: distribution of outcome values
    - missing_doi_count: count of rows with empty/pending doi_o
    - api_error_count: count of api_error link methods
    - pending_count: count of pending link methods
    """
    path = DATA_DIR / "extracted.csv"
    df = pd.read_csv(path)

    audit = {
        "total_rows": len(df),
        "by_link_method": df["link_method"].value_counts().to_dict() if "link_method" in df.columns else {},
        "by_link_confidence": df["link_confidence"].value_counts().to_dict() if "link_confidence" in df.columns else {},
        "by_outcome": df["outcome"].value_counts().to_dict() if "outcome" in df.columns else {},
        "missing_doi_count": (df["doi_o"].isna() | (df["doi_o"] == "") | (df["doi_o"] == "pending")).sum() if "doi_o" in df.columns else 0,
    }

    # Count api_error rows
    if "link_method" in df.columns:
        audit["api_error_count"] = (df["link_method"] == "api_error").sum()
    if "link_method" in df.columns:
        audit["pending_count"] = (df["link_method"] == "target_pending").sum()

    return audit


def analyze_link_method_distribution() -> pd.DataFrame:
    """Analyze distribution of link_method in extracted.csv."""
    path = DATA_DIR / "extracted.csv"
    df = pd.read_csv(path)

    if "link_method" not in df.columns:
        return pd.DataFrame()

    dist = df["link_method"].value_counts().reset_index()
    dist.columns = ["link_method", "count"]
    dist["percentage"] = (dist["count"] / len(df) * 100).round(1)

    return dist.sort_values("count", ascending=False)


def find_missing_doi_rows() -> pd.DataFrame:
    """Find all rows in extracted.csv where doi_o is missing/pending."""
    path = DATA_DIR / "extracted.csv"
    df = pd.read_csv(path)

    if "doi_o" not in df.columns:
        return pd.DataFrame()

    missing = df[
        (df["doi_o"].isna()) |
        (df["doi_o"] == "") |
        (df["doi_o"] == "pending") |
        (df["doi_o"].str.lower() == "pending")
    ]

    return missing[["doi_r", "title_r", "authors_r", "year_r", "link_method", "link_confidence"]]


def analyze_confidence_distribution() -> pd.DataFrame:
    """Analyze distribution of link_confidence values."""
    path = DATA_DIR / "extracted.csv"
    df = pd.read_csv(path)

    if "link_confidence" not in df.columns:
        return pd.DataFrame()

    dist = df["link_confidence"].value_counts().reset_index()
    dist.columns = ["link_confidence", "count"]
    dist["percentage"] = (dist["count"] / len(df) * 100).round(1)

    return dist.sort_values("count", ascending=False)


def count_by_link_method_and_confidence() -> pd.DataFrame:
    """Cross-tabulate link_method vs. link_confidence."""
    path = DATA_DIR / "extracted.csv"
    df = pd.read_csv(path)

    if "link_method" not in df.columns or "link_confidence" not in df.columns:
        return pd.DataFrame()

    crosstab = pd.crosstab(df["link_method"], df["link_confidence"], margins=True)
    return crosstab


def generate_extraction_audit_report(gap_summary_path: str = None) -> str:
    """
    Generate markdown report comparing Task 2 gaps vs. current extraction state.

    Args:
        gap_summary_path: path to Task 2 gap_summary.md (optional)

    Returns:
        Markdown-formatted report string
    """
    import datetime

    lines = []
    lines.append("# Extraction Failure Audit Report\n")
    lines.append(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Current state
    lines.append("## Current Extraction State\n")
    audit = audit_extracted_csv()
    lines.append(f"- Total extracted rows: {audit['total_rows']}")
    lines.append(f"- Missing DOI (doi_o empty/pending): {audit.get('missing_doi_count', 0)}")
    lines.append(f"- API error count: {audit.get('api_error_count', 0)}")
    lines.append(f"- Target pending count: {audit.get('pending_count', 0)}\n")

    # Link method breakdown
    lines.append("## Link Method Distribution\n")
    dist = analyze_link_method_distribution()
    if len(dist) > 0:
        lines.append(dist.to_markdown(index=False))
    lines.append("")

    # Confidence breakdown
    lines.append("## Link Confidence Distribution\n")
    conf = analyze_confidence_distribution()
    if len(conf) > 0:
        lines.append(conf.to_markdown(index=False))
    lines.append("")

    # Cross-tab
    lines.append("## Link Method vs. Confidence\n")
    cross = count_by_link_method_and_confidence()
    if len(cross) > 0:
        lines.append(cross.to_markdown())
    lines.append("")

    # Rows with missing DOI
    lines.append("## Rows with Missing DOI (Sample)\n")
    missing = find_missing_doi_rows()
    if len(missing) > 0:
        lines.append(f"Total: {len(missing)} rows")
        lines.append("")
        lines.append(missing.head(10).to_markdown(index=False))
        if len(missing) > 10:
            lines.append(f"\n... and {len(missing) - 10} more rows")
    lines.append("")

    return "\n".join(lines)


def generate_improvement_opportunities() -> pd.DataFrame:
    """
    Based on current audit, identify specific improvement opportunities.

    Returns DataFrame with columns:
    - Category: type of improvement
    - Count: number of affected rows
    - Current Status: how it's currently handled
    - Suggested Fix: what could improve it
    - Priority: High/Medium/Low
    """
    opportunities = []

    # Missing DOI issue
    missing = find_missing_doi_rows()
    if len(missing) > 0:
        opportunities.append({
            "Category": "Missing DOI (doi_o empty/pending)",
            "Count": len(missing),
            "Current Status": "Falls back to LLM or marks as pending",
            "Suggested Fix": "Add URL-based DOI lookup (CrossRef) before LLM",
            "Priority": "High",
        })

    # API errors
    audit = audit_extracted_csv()
    if audit.get("api_error_count", 0) > 0:
        opportunities.append({
            "Category": "API errors in DOI resolution",
            "Count": audit["api_error_count"],
            "Current Status": "Marked as api_error",
            "Suggested Fix": "Improve rule-based matching to reduce LLM calls",
            "Priority": "High",
        })

    # Low confidence matches
    path = DATA_DIR / "extracted.csv"
    df = pd.read_csv(path)
    low_conf = (df["link_confidence"] == "low").sum() if "link_confidence" in df.columns else 0
    if low_conf > 0:
        opportunities.append({
            "Category": "Low confidence original matches",
            "Count": low_conf,
            "Current Status": "LLM-based match with low confidence",
            "Suggested Fix": "Improve citation context scoring, title matching",
            "Priority": "Medium",
        })

    return pd.DataFrame(opportunities)


def compare_task2_gaps_with_extracted() -> pd.DataFrame:
    """
    Compare Task 2 gap findings with current extracted.csv state.

    Identifies which known gaps are in extracted.csv vs. not found.

    Returns comparison DataFrame
    """
    # Load Task 2 gaps
    gap_dir = DATA_DIR.parent / "analysis"

    gaps_doi = pd.read_csv(gap_dir / "gap_analysis_doi_matched.csv") if (gap_dir / "gap_analysis_doi_matched.csv").exists() else pd.DataFrame()

    # Load current extracted.csv
    extracted = pd.read_csv(DATA_DIR / "extracted.csv")

    comparison = []

    # Check if known gaps appear in extracted.csv
    all_gaps = gaps_doi if len(gaps_doi) > 0 else pd.DataFrame()

    if len(all_gaps) > 0:
        for idx, gap_row in all_gaps.iterrows():
            gap_doi_r = gap_row.get("doi_r", "")

            # Try to find in extracted.csv
            found = extracted[extracted["doi_r"] == gap_doi_r] if "doi_r" in extracted.columns else pd.DataFrame()

            if len(found) > 0:
                status = "Found in extracted"
                link_method = found.iloc[0].get("link_method", "unknown") if "link_method" in found.columns else "unknown"
                confidence = found.iloc[0].get("link_confidence", "unknown") if "link_confidence" in found.columns else "unknown"
            else:
                status = "NOT in extracted (gap remains)"
                link_method = "N/A"
                confidence = "N/A"

            study = gap_row.get("study_r", "")
            study_str = str(study) if pd.notna(study) else ""
            comparison.append({
                "doi_r": gap_doi_r,
                "title_r": study_str[:50] if study_str else "N/A",
                "match_status": status,
                "link_method": link_method,
                "confidence": confidence,
            })

    return pd.DataFrame(comparison)


def run_phase1_analysis() -> Dict[str, str]:
    """
    Run Phase 1 analysis: audit extraction + generate improvement opportunities.

    Returns dict with paths to generated files:
    - extraction_audit.md
    - rule_improvement_opportunities.csv
    """
    from pathlib import Path

    output_dir = DATA_DIR.parent / "analysis"
    output_dir.mkdir(exist_ok=True)

    # Generate extraction audit
    log.info("Generating extraction audit report...")
    audit_report = generate_extraction_audit_report()
    audit_path = output_dir / "extraction_audit.md"
    with open(audit_path, "w", encoding="utf-8") as f:
        f.write(audit_report)
    log.info(f"  -> {audit_path.name}")

    # Compare with Task 2 gaps
    log.info("Comparing with Task 2 gap findings...")
    comparison = compare_task2_gaps_with_extracted()
    comparison_path = output_dir / "gap_vs_extracted_comparison.csv"
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    log.info(f"  -> {comparison_path.name}")

    # Generate improvement opportunities
    log.info("Analyzing improvement opportunities...")
    opportunities = generate_improvement_opportunities()
    oppo_path = output_dir / "rule_improvement_opportunities.csv"
    opportunities.to_csv(oppo_path, index=False, encoding="utf-8-sig")
    log.info(f"  -> {oppo_path.name}")

    return {
        "extraction_audit": str(audit_path),
        "comparison": str(comparison_path),
        "opportunities": str(oppo_path),
    }
