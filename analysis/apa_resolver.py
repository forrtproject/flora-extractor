"""
apa_resolver.py — Resolve replications without DOI via CrossRef API + CSV fallback.

Task 6 implementation: Tiered lookup approach
1. CrossRef API: title + authors + year → DOI + metadata
2. Fallback CSV: local reference data
3. Manual: flag for manual review
"""

import pandas as pd
import requests
import time
from typing import Optional, Dict, Any, List
from pathlib import Path

from shared.config import DATA_DIR, log


def load_missing_dois() -> pd.DataFrame:
    """
    Load replications without DOI from filtered.csv + extracted.csv.

    Returns DataFrame with columns:
    - doi_r (should be empty/null/pending)
    - title_r
    - authors_r
    - year_r
    - abstract_r (optional)
    - journal_r (optional)
    - url_r (optional)
    - source_file: 'filtered' or 'extracted'
    """
    missing_list = []

    # Load filtered.csv
    filtered = pd.read_csv(DATA_DIR / "filtered.csv")
    filtered_missing = filtered[
        (filtered["doi_r"].isna()) |
        (filtered["doi_r"] == "") |
        (filtered["doi_r"].str.lower() == "pending")
    ].copy()
    filtered_missing["source_file"] = "filtered"
    missing_list.append(filtered_missing)

    # Load extracted.csv
    extracted = pd.read_csv(DATA_DIR / "extracted.csv")
    extracted_missing = extracted[
        (extracted["doi_r"].isna()) |
        (extracted["doi_r"] == "") |
        (extracted["doi_r"].str.lower() == "pending")
    ].copy()
    extracted_missing["source_file"] = "extracted"
    missing_list.append(extracted_missing)

    # Combine and deduplicate by title + authors + year
    combined = pd.concat(missing_list, ignore_index=True)

    if len(combined) > 0:
        # Deduplicate: keep first occurrence
        combined = combined.drop_duplicates(
            subset=["title_r", "authors_r", "year_r"],
            keep="first"
        )

    return combined


def query_crossref(title: str, authors: str, year: int, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """
    Query CrossRef API for a paper by title + first author + year.

    Args:
        title: paper title
        authors: author string (e.g., "Smith, B., Jones, A.")
        year: publication year
        timeout: request timeout in seconds

    Returns:
        Dict with metadata if found, None otherwise
    """
    if not title or not authors or not year:
        return None

    try:
        # Extract first author last name
        first_author = authors.split(",")[0].split("&")[0].split("and")[0].strip()

        # CrossRef query format
        query = f'{title} {first_author} {year}'

        url = "https://api.crossref.org/works"
        params = {
            "query": query,
            "rows": 5,
        }
        headers = {
            "User-Agent": "FLoRA-Extractor (mailto:research@example.com)"
        }

        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()

        data = response.json()
        items = data.get("message", {}).get("items", [])

        if not items:
            return None

        # Return best match
        best_match = items[0] if items else None

        if best_match:
            return {
                "doi": best_match.get("DOI", ""),
                "title": best_match.get("title", [""])[0] if isinstance(best_match.get("title"), list) else best_match.get("title", ""),
                "authors": best_match.get("author", []),
                "year": best_match.get("issued", {}).get("date-parts", [[None]])[0][0],
                "journal": best_match.get("container-title", [""])[0] if isinstance(best_match.get("container-title"), list) else best_match.get("container-title", ""),
                "volume": best_match.get("volume"),
                "issue": best_match.get("issue"),
                "pages": best_match.get("page"),
                "source": "crossref",
            }

        return None

    except requests.exceptions.RequestException as e:
        log.warning(f"CrossRef API error for '{title}': {e}")
        return None


def format_apa_reference(metadata: Dict[str, Any]) -> str:
    """
    Format paper metadata as APA-style reference.

    Args:
        metadata: dict with keys: authors, year, title, journal, volume, issue, pages

    Returns:
        APA-formatted reference string
    """
    if not metadata:
        return ""

    # Authors
    authors = metadata.get("authors", [])
    if isinstance(authors, list) and authors:
        if isinstance(authors[0], dict):
            # CrossRef format: [{"family": "Smith", "given": "B."}, ...]
            author_strs = []
            for a in authors[:3]:  # Limit to first 3 for brevity
                family = a.get("family", "")
                given = a.get("given", "")
                if given:
                    author_strs.append(f"{family}, {given[0]}.")
                else:
                    author_strs.append(family)
            if len(authors) > 3:
                author_strs.append("et al.")
            authors_str = ", ".join(author_strs[:-1]) + (", & " + author_strs[-1] if len(author_strs) > 1 else author_strs[0])
        else:
            # String format
            authors_str = ", ".join(str(a) for a in authors[:2])
    else:
        authors_str = str(authors) if authors else "Unknown"

    # Year
    year = metadata.get("year", "n.d.")

    # Title
    title = metadata.get("title", "")

    # Journal
    journal = metadata.get("journal", "")

    # Volume, Issue, Pages
    volume = metadata.get("volume")
    issue = metadata.get("issue")
    pages = metadata.get("pages")

    # Build APA reference
    if journal:
        # Journal article format
        apa = f"{authors_str} ({year}). {title}. {journal}"
        if volume:
            apa += f", {volume}"
        if issue:
            apa += f"({issue})"
        if pages:
            apa += f", {pages}"
        apa += "."
    else:
        # Generic format (no journal)
        apa = f"{authors_str} ({year}). {title}."

    return apa


def load_fallback_csv(csv_path: Optional[Path] = None) -> pd.DataFrame:
    """Load fallback reference CSV for manual entries."""
    if csv_path is None:
        csv_path = DATA_DIR.parent / "analysis" / "apa_reference_fallback.csv"

    if csv_path.exists() and csv_path.stat().st_size > 0:
        return pd.read_csv(csv_path)
    else:
        # Return empty DataFrame with expected columns
        return pd.DataFrame(columns=["title_r", "authors_r", "year_r", "doi_r", "apa_reference", "source"])


def fuzzy_match_csv(row: pd.Series, fallback_df: pd.DataFrame, title_threshold: float = 0.75) -> Optional[Dict[str, Any]]:
    """
    Try to match a row against fallback CSV by fuzzy title matching + year.

    Returns metadata dict if found, None otherwise.
    """
    if len(fallback_df) == 0:
        return None

    try:
        from fuzzywuzzy import fuzz
    except ImportError:
        log.warning("fuzzywuzzy not installed, skipping fuzzy matching")
        return None

    title_r = str(row.get("title_r", "")).lower()
    year_r = row.get("year_r")

    for idx, ref_row in fallback_df.iterrows():
        ref_title = str(ref_row.get("title_r", "")).lower()
        ref_year = ref_row.get("year_r")

        # Year must match exactly
        if ref_year != year_r:
            continue

        # Title similarity
        sim = fuzz.token_set_ratio(title_r, ref_title) / 100.0

        if sim >= title_threshold:
            return {
                "doi": ref_row.get("doi_r", ""),
                "title": ref_row.get("title_r", ""),
                "authors": ref_row.get("authors_r", ""),
                "year": ref_row.get("year_r"),
                "apa_reference": ref_row.get("apa_reference", ""),
                "score": sim,
                "source": "csv_fallback",
            }

    return None


def resolve_all(email: str = "research@example.com", max_rows: Optional[int] = None) -> pd.DataFrame:
    """
    Resolve all replications without DOI using tiered approach:
    1. CrossRef API
    2. Fallback CSV
    3. Manual (pending)

    Args:
        email: email for User-Agent header
        max_rows: limit number of rows to process (for testing)

    Returns:
        DataFrame with all resolutions
    """
    log.info("Loading replications without DOI...")
    missing = load_missing_dois()
    log.info(f"Found {len(missing)} rows without DOI")

    if max_rows:
        missing = missing.head(max_rows)

    # Load fallback CSV
    fallback = load_fallback_csv()
    log.info(f"Loaded {len(fallback)} fallback references")

    results = []

    for idx, row in missing.iterrows():
        if idx % 10 == 0:
            log.info(f"  Progress: {idx}/{len(missing)}")

        title = row.get("title_r", "")
        authors = row.get("authors_r", "")
        year = row.get("year_r")

        # Tier 1: CrossRef API
        metadata = query_crossref(title, authors, year)

        if metadata:
            apa = format_apa_reference(metadata)
            confidence = "high"
        else:
            # Tier 2: Fallback CSV
            metadata = fuzzy_match_csv(row, fallback)

            if metadata:
                apa = metadata.get("apa_reference", "")
                confidence = "medium"
            else:
                # Tier 3: Pending manual
                apa = f"{authors} ({year}). {title}."
                confidence = "pending_manual"
                metadata = {"source": "manual"}

        results.append({
            "doi_r_original": row.get("doi_r", ""),
            "title_r": title,
            "authors_r": authors,
            "year_r": year,
            "source_file": row.get("source_file", ""),
            "doi_resolved": metadata.get("doi", "") if metadata else "",
            "apa_reference": apa,
            "source_method": metadata.get("source", "") if metadata else "manual",
            "confidence": confidence,
        })

        # Rate limit: 1 request per second
        time.sleep(0.1)  # Use 0.1s to be faster

    log.info(f"Resolution complete: {len(results)} rows processed")

    return pd.DataFrame(results)


def run_apa_resolution(email: str = "research@example.com", max_rows: Optional[int] = None) -> str:
    """
    Run full APA resolution pipeline and save outputs.

    Args:
        email: email for User-Agent header
        max_rows: limit number of rows (for testing)

    Returns:
        Path to output CSV
    """
    output_dir = DATA_DIR.parent / "analysis"
    output_dir.mkdir(exist_ok=True)

    log.info("Running APA Reference Resolver (Task 6)...")

    results = resolve_all(email=email, max_rows=max_rows)

    output_path = output_dir / "missing_dois_resolved.csv"
    results.to_csv(output_path, index=False, encoding="utf-8-sig")

    log.info(f"Resolution complete: {output_path}")

    # Summary
    summary_lines = [
        "# APA Reference Resolution Report\n",
        f"Total processed: {len(results)}",
        f"CrossRef matches (high): {(results['confidence'] == 'high').sum()}",
        f"CSV matches (medium): {(results['confidence'] == 'medium').sum()}",
        f"Pending manual (low): {(results['confidence'] == 'pending_manual').sum()}",
        "",
        "Confidence distribution:",
        str(results['confidence'].value_counts()),
    ]

    report_path = output_dir / "apa_resolver_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))

    log.info(f"Report: {report_path}")

    return str(output_path)
