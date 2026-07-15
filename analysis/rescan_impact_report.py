"""
rescan_impact_report.py — Read-only impact report for the 2026-07-08 classification
accuracy fixes (same-sentence proximity gate, month/weekday stopword filter, Stage 3
narrative-citation extraction). Re-runs the already-fixed code against the EXISTING
filtered.csv/extracted.csv data and reports how many rows would be reclassified or
relinked under the new logic. No writes, no LLM calls. Stage 3 re-linking reuses each
row's already-cached OpenAlex candidate list and journal lookups — if a candidate's
journal name isn't already cached, it's treated as unknown rather than fetched fresh,
so this script makes no network calls either.

See docs/superpowers/specs/2026-07-08-classification-accuracy-fixes-design.md for the
fixes themselves.

Usage:
    python -m analysis.rescan_impact_report
    python -m analysis.rescan_impact_report --limit 5000   # smaller run for a quick look
"""
import argparse
import json
from unittest.mock import patch

import pandas as pd

from shared.config import CACHE_DIR, DATA_DIR, log
from shared.utils import cache_key, clean_doi
from filter.rule_filter import classify_row
from extract.link_original import _resolve_rule_based

_OA_CACHE_DIR = CACHE_DIR / "openalex"


def _cached_only_journal(doi: str) -> str:
    """Cache-only stand-in for link_original._fetch_journal_cached — never makes a
    network call. A journal whose cache file doesn't exist yet is treated as unknown
    for the purposes of this read-only report."""
    doi = clean_doi(doi)
    if not doi:
        return ""
    cache_path = _OA_CACHE_DIR / f"journal_{cache_key(doi)}.json"
    if not cache_path.exists():
        return ""
    try:
        return json.loads(cache_path.read_text(encoding="utf-8")).get("journal", "")
    except Exception:
        return ""


def audit_filter_gate_impact(limit: int | None = None, chunksize: int = 50_000) -> pd.DataFrame:
    """
    Re-run classify_row() (proximity gate + stopword filter already applied) against
    every currently high-confidence replication/reproduction row that was decided by
    the RULE PATH ALONE (filter_method == "rule_based") in filtered.csv. Returns rows
    whose filter_status or filter_confidence would change. No writes.

    Rows with filter_method "llm" or "both" are deliberately excluded: those were
    already reviewed by an LLM at some point, so their current status reflects a
    semantic judgment call, not a naive rule match — comparing them against a
    rule-only rerun isn't a meaningful "regression" signal (classify_row() never
    calls the LLM, so it will always disagree with an LLM-influenced verdict
    regardless of these fixes). Only "rule_based" rows were confidently decided
    with zero semantic check, which is exactly what Fix 1/2 target.
    """
    filtered_path = DATA_DIR / "filtered.csv"
    changes: list[dict] = []
    n_checked = 0

    for chunk in pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig",
                              chunksize=chunksize, low_memory=False):
        chunk = chunk.fillna("")
        eligible = chunk[
            (chunk["filter_confidence"] == "high")
            & (chunk["filter_status"].isin(["replication", "reproduction"]))
            & (chunk["filter_method"] == "rule_based")
        ]
        for _, row in eligible.iterrows():
            if limit is not None and n_checked >= limit:
                break
            row_dict = row.to_dict()
            n_checked += 1
            new = classify_row(row_dict)
            if (new["filter_status"], new["filter_confidence"]) != (
                row_dict["filter_status"], row_dict["filter_confidence"]
            ):
                changes.append({
                    "doi_r": row_dict.get("doi_r", ""),
                    "title_r": row_dict.get("title_r", "")[:100],
                    "old_filter_status": row_dict["filter_status"],
                    "old_filter_confidence": row_dict["filter_confidence"],
                    "new_filter_status": new["filter_status"],
                    "new_filter_confidence": new["filter_confidence"],
                    "new_filter_evidence": new["filter_evidence"],
                })
        if limit is not None and n_checked >= limit:
            break

    log.info("Filter gate audit: checked %d rule_based high-confidence rows, %d would change",
              n_checked, len(changes))
    return pd.DataFrame(changes)


def audit_stage3_relink_impact(limit: int | None = None) -> pd.DataFrame:
    """
    Re-run _resolve_rule_based() (narrative-citation-aware extraction already applied)
    against every extracted.csv row linked via the rule-based author_year_match path,
    reusing each row's cached OpenAlex candidate list (no new API calls). Returns rows
    whose resolved doi_o would change. Rows without a cached candidate list are
    skipped — re-checking them would require a fresh OpenAlex query.
    """
    extracted_path = DATA_DIR / "extracted.csv"
    df = pd.read_csv(extracted_path, dtype=str, encoding="utf-8-sig", low_memory=False).fillna("")
    eligible = df[df["link_method"] == "author_year_match"]

    changes: list[dict] = []
    n_checked = 0
    n_skipped_no_cache = 0

    with patch("extract.link_original._fetch_journal_cached", side_effect=_cached_only_journal):
        for _, row in eligible.iterrows():
            if limit is not None and n_checked >= limit:
                break

            doi_r = clean_doi(str(row.get("doi_r", "")))
            cache_path = _OA_CACHE_DIR / f"candidates_{cache_key(doi_r)}.json"
            if not cache_path.exists():
                n_skipped_no_cache += 1
                continue

            try:
                candidates = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                n_skipped_no_cache += 1
                continue

            try:
                year_r = int(row.get("year_r") or 0)
            except ValueError:
                year_r = 0

            n_checked += 1
            result = _resolve_rule_based(
                doi_r, row.get("abstract_r", ""), candidates, year_r, row.get("title_r", "")
            )
            new_doi_o = clean_doi(result.get("resolved_doi_o", ""))
            old_doi_o = clean_doi(str(row.get("doi_o", "")))

            if result["resolved"] and new_doi_o and new_doi_o != old_doi_o:
                changes.append({
                    "doi_r": doi_r,
                    "title_r": str(row.get("title_r", ""))[:100],
                    "old_doi_o": old_doi_o,
                    "old_title_o": row.get("title_o", ""),
                    "new_doi_o": new_doi_o,
                    "new_title_o": result.get("resolved_title_o", ""),
                    "resolution_method": result.get("resolution_method", ""),
                })

    log.info("Stage 3 relink audit: checked %d cached rows (%d skipped, no cache), %d would relink",
              n_checked, n_skipped_no_cache, len(changes))
    return pd.DataFrame(changes)


def run_report(limit: int | None = None) -> dict:
    out_dir = DATA_DIR.parent / "analysis"
    out_dir.mkdir(exist_ok=True)

    log.info("Running filter gate impact audit (reads filtered.csv in 50k-row chunks)...")
    filter_changes = audit_filter_gate_impact(limit=limit)
    filter_path = out_dir / "rescan_filter_gate_impact.csv"
    filter_changes.to_csv(filter_path, index=False, encoding="utf-8-sig")

    log.info("Running Stage 3 relink impact audit...")
    relink_changes = audit_stage3_relink_impact(limit=limit)
    relink_path = out_dir / "rescan_stage3_relink_impact.csv"
    relink_changes.to_csv(relink_path, index=False, encoding="utf-8-sig")

    summary = {
        "filter_rows_would_change": len(filter_changes),
        "filter_report": str(filter_path),
        "stage3_rows_would_relink": len(relink_changes),
        "stage3_report": str(relink_path),
    }
    log.info("=" * 60)
    log.info("RESCAN IMPACT SUMMARY")
    log.info("=" * 60)
    for k, v in summary.items():
        log.info("  %s: %s", k, v)
    log.info("=" * 60)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                         help="Check only the first N eligible rows per audit (for a quick look).")
    args = parser.parse_args()
    result = run_report(limit=args.limit)
    print("\n" + json.dumps(result, indent=2))
