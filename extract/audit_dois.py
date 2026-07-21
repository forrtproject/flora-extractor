"""
audit_dois.py — Retroactive DOI verification for extracted.csv.

Checks every row's doi_o against the metadata it points to (CrossRef/OpenAlex)
and proposes corrections for hallucinated or missing DOIs. Dry-run by default;
--apply writes corrections (doi_o, pair_id, doi_o_verification, link_evidence,
link_confidence) back into the CSV.

Usage:
    python -m extract.audit_dois                  # dry-run report
    python -m extract.audit_dois --apply          # write corrections
    python -m extract.audit_dois --doi 10.x/y     # single row
    python -m extract.audit_dois --extracted-test # audit extracted-test.csv
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd

from extract.run_extract import _build_ref_o
from shared.config import DATA_DIR, log
from shared.doi_verify import verify_and_correct
from shared.schema import make_pair_id
from shared.utils import clean_doi

_MAIN_PATH   = DATA_DIR / "extracted.csv"
_TEST_PATH   = DATA_DIR / "extracted-test.csv"
_REPORT_PATH = DATA_DIR / "doi_audit_report.csv"

_SKIP_LINK_METHODS = {"target_pending", "api_error"}


def audit_file(csv_path: Path,
               apply: bool = False,
               report_path: "Path | None" = None,
               only_doi: "str | None" = None) -> dict:
    """Audit every row of *csv_path*. Returns per-status counts."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found")

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    if "doi_o_verification" not in df.columns:
        df["doi_o_verification"] = ""

    only = clean_doi(only_doi) if only_doi else None
    counts: Counter = Counter()
    report_rows: list[dict] = []

    for idx, row in df.iterrows():
        if only and clean_doi(str(row["doi_r"])) != only:
            continue

        if str(row.get("link_method", "")) in _SKIP_LINK_METHODS:
            df.at[idx, "doi_o_verification"] = "skipped"
            counts["skipped"] += 1
            continue

        old_doi = str(row.get("doi_o", "") or "")
        v = verify_and_correct(old_doi, str(row.get("title_o", "") or ""),
                               str(row.get("authors_o", "") or ""),
                               row.get("year_o", ""),
                               exclude_doi=clean_doi(str(row["doi_r"])),
                               exclude_title=str(row.get("title_r", "")
                                                 or row.get("study_r", "") or ""))
        status = v["doi_o_verification"]
        counts[status] += 1
        df.at[idx, "doi_o_verification"] = status

        if status != "verified":
            report_rows.append({
                "doi_r": row["doi_r"], "status": status,
                "old_doi_o": old_doi, "proposed_doi_o": v["doi_o"],
                "title_o": row.get("title_o", ""), "evidence": v["evidence_note"],
            })
            log.info("[%s] %s: %s → %s", row["doi_r"], status, old_doi or "—",
                     v["doi_o"] or "—")

        if v["doi_o"] != old_doi:
            df.at[idx, "doi_o"]    = v["doi_o"]
            df.at[idx, "pair_id"]  = make_pair_id(clean_doi(str(row["doi_r"])), v["doi_o"])
            new_ref, new_authors, new_bibtex = _build_ref_o(v["doi_o"],
                                                   str(row.get("authors_o", "") or ""),
                                                   str(row.get("year_o", "") or ""))
            df.at[idx, "ref_o"]        = new_ref
            df.at[idx, "authors_o"]    = new_authors
            df.at[idx, "bibtex_ref_o"] = new_bibtex
        if status == "mismatch":
            df.at[idx, "link_confidence"] = "low"
        if v["evidence_note"]:
            existing = str(row.get("link_evidence", "") or "")
            if v["evidence_note"] not in existing:
                df.at[idx, "link_evidence"] = f"{existing} | {v['evidence_note']}".strip(" |")

    rp = Path(report_path) if report_path else _REPORT_PATH
    pd.DataFrame(report_rows,
                 columns=["doi_r", "status", "old_doi_o", "proposed_doi_o",
                          "title_o", "evidence"]).to_csv(
        rp, index=False, encoding="utf-8-sig")

    if apply:
        df.to_csv(csv_path, index=False, encoding="utf-8-sig",
                  quoting=1, quotechar='"')
        log.info("Applied %d correction(s) to %s",
                 counts.get("corrected", 0), csv_path.name)

    return dict(counts)


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit/fix doi_o values in extracted.csv")
    ap.add_argument("--apply", action="store_true",
                    help="write corrections into the CSV (default: dry-run)")
    ap.add_argument("--doi", help="audit a single row by doi_r")
    ap.add_argument("--extracted-test", action="store_true",
                    help="audit extracted-test.csv instead of extracted.csv")
    args = ap.parse_args()

    path = _TEST_PATH if args.extracted_test else _MAIN_PATH
    summary = audit_file(path, apply=args.apply, only_doi=args.doi)

    print(f"\nDOI audit of {path.name}{' (APPLIED)' if args.apply else ' (dry-run)'}:")
    for status, n in sorted(summary.items()):
        print(f"  {status:<12} {n}")
    print(f"\nReport: {_REPORT_PATH}")
    if not args.apply:
        print("Dry-run only — rerun with --apply to write corrections.")


if __name__ == "__main__":
    main()
