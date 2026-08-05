"""
audit_dois.py — Retroactive DOI verification for extracted.csv.

Checks every row's doi_o against the metadata it points to (CrossRef/OpenAlex)
and proposes corrections for hallucinated or missing DOIs. Dry-run by default;
--apply writes corrections (doi_o, pair_id, doi_o_verification, link_evidence,
link_confidence) back into the CSV.

This is the ONLY tool that re-verifies a row whose verification already settled: a
resumed run_extract carries such rows forward untouched (each re-verification costs
up to three OpenAlex free-text searches at 10× a filter query, and the row already
holds the answer). It re-verifies unsettled rows itself, so the audit's job here is
the retroactive pass — after a threshold change, a doi_verify fix, or a spot check.

Usage:
    python -m extract.audit_dois                  # dry-run report, every row
    python -m extract.audit_dois --apply          # write corrections
    python -m extract.audit_dois --doi 10.x/y     # single row
    python -m extract.audit_dois --status api_error   # only rows whose verification
                                                  #   failed last time (repeatable)
    python -m extract.audit_dois --extracted-test # audit extracted-test.csv
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd

from extract.run_extract import _build_ref_o
from shared.config import DATA_DIR, log
from shared.doi_verify import keeps_no_doi, verify_and_correct
from shared.schema import VERIFICATION_SKIP_LINK_METHODS, make_pair_id
from shared.utils import clean_doi

_MAIN_PATH   = DATA_DIR / "extracted.csv"
_TEST_PATH   = DATA_DIR / "extracted-test.csv"
_REPORT_PATH = DATA_DIR / "doi_audit_report.csv"

# The same set run_extract._verify_row skips, from shared/schema.py: the audit must
# leave alone exactly the rows the pipeline leaves alone, or it re-verifies (and
# overwrites the `skipped` verification of) a prescreen_discard the pipeline stopped
# spending on.
_SKIP_LINK_METHODS = VERIFICATION_SKIP_LINK_METHODS


def audit_file(csv_path: Path,
               apply: bool = False,
               report_path: "Path | None" = None,
               only_doi: "str | None" = None,
               only_status: "list[str] | None" = None) -> dict:
    """Audit every row of *csv_path*. Returns per-status counts.

    only_status: restrict to rows whose CURRENT doi_o_verification is one of these
    (use "" for rows that have never been verified). The reason it exists is
    `--status api_error`: a row whose verification could not be completed keeps its
    doi_o and says so, and this is how those rows get asked again without paying for
    a full-file re-verification.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found")

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    if "doi_o_verification" not in df.columns:
        df["doi_o_verification"] = ""

    only = clean_doi(only_doi) if only_doi else None
    wanted = set(only_status) if only_status else None
    counts: Counter = Counter()
    report_rows: list[dict] = []

    for idx, row in df.iterrows():
        if only and clean_doi(str(row["doi_r"])) != only:
            continue
        if wanted is not None and str(row.get("doi_o_verification", "") or "").strip() not in wanted:
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
        prior_status = str(row.get("doi_o_verification", "") or "")
        status = v["doi_o_verification"]
        if keeps_no_doi(status, prior_status, str(row.get("oa_work_id_o", "") or "")):
            status = prior_status
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
            if v["doi_o"] and str(row.get("oa_work_id_o", "") or ""):
                # The id was resolved from the DOI that just turned out to be wrong.
                # This tool has no work-id refill pass, so clear it and say so — a
                # blank id is refilled by the next run_extract pass, a stale one
                # would keep pointing at a different work.
                df.at[idx, "oa_work_id_o"] = ""
                v["evidence_note"] = (f"{v['evidence_note']}; oa_work_id_o cleared "
                                      f"(resolved from the superseded DOI)").strip("; ")
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
    ap.add_argument("--status", action="append", metavar="VALUE",
                    help=("only rows whose current doi_o_verification is VALUE "
                          "(repeatable; '' means never verified). --status api_error "
                          "re-asks the rows whose verification could not be completed"))
    ap.add_argument("--extracted-test", action="store_true",
                    help="audit extracted-test.csv instead of extracted.csv")
    args = ap.parse_args()

    path = _TEST_PATH if args.extracted_test else _MAIN_PATH
    summary = audit_file(path, apply=args.apply, only_doi=args.doi,
                         only_status=args.status)

    print(f"\nDOI audit of {path.name}{' (APPLIED)' if args.apply else ' (dry-run)'}:")
    for status, n in sorted(summary.items()):
        print(f"  {status:<12} {n}")
    print(f"\nReport: {_REPORT_PATH}")
    if not args.apply:
        print("Dry-run only — rerun with --apply to write corrections.")


if __name__ == "__main__":
    main()
