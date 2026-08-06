"""
audit_dois.py — Retroactive DOI verification for the extracted rows.

Checks every row's doi_o against the metadata it points to (CrossRef/OpenAlex) and
proposes corrections for hallucinated or missing DOIs. Dry-run by default.

**It reads the exported CSV and writes the stored verdicts.** The rows it audits are
read from `data/extracted.csv`, which is the readable form of what the extract tier
concluded; a correction goes back to where that row is rendered FROM — a new result
verdict superseding the old one (`extract/tier.py:supersede_targets`). Editing the
CSV would have been undone by the next `python -m extract.export`, which rebuilds
every row from the stored payload.

So `--apply` is a three-step move, and the third step is the operator's:

    python -m extract.audit_dois            # dry-run report over data/extracted.csv
    python -m extract.audit_dois --apply    # claim, correct, supersede
    python -m extract.export                # render the corrected CSV

This is the ONLY tool that re-verifies a row whose verification already settled: a
row's `doi_o_verification` is decided once, inside the tier's judge, and stored
(each re-verification costs up to three OpenAlex free-text searches at 10× a filter
query, and the row already holds the answer). The audit's job is the retroactive
pass — after a threshold change, a doi_verify fix, or a spot check.

    python -m extract.audit_dois --doi 10.x/y          # single row
    python -m extract.audit_dois --status api_error    # only rows whose verification
                                                       #   failed last time (repeatable)
    python -m extract.audit_dois --mode validation     # the sandbox render + verdicts
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

# The CSV each mode's export writes, and therefore the file each mode's report reads.
_MODE_PATHS  = {"live": DATA_DIR / "extracted.csv",
                "validation": DATA_DIR / "extracted-test.csv"}
_REPORT_PATH = DATA_DIR / "doi_audit_report.csv"

# The same set run_extract._verify_row skips, from shared/schema.py: the audit must
# leave alone exactly the rows the pipeline leaves alone, or it re-verifies (and
# overwrites the `skipped` verification of) a prescreen_discard the pipeline stopped
# spending on.
_SKIP_LINK_METHODS = VERIFICATION_SKIP_LINK_METHODS


def _patches(original: pd.DataFrame, updated: pd.DataFrame) -> dict[str, dict]:
    """`pair_id → {column: corrected value}` for every row the audit changed.

    The pair id from the row AS READ, not as corrected: it is the identity the stored
    target carries, and correcting a doi_o changes the pair id itself — which is one
    of the columns in the patch.

    A row with no pair id cannot be located in the verdicts and is reported rather
    than guessed at; a duplicate pair id is the same problem twice and `audit_extracted`
    already flags it as a blocker.
    """
    cols = list(original.columns)
    out: dict[str, dict] = {}
    for pair, before, after in zip(original.get("pair_id", pd.Series(dtype=str)),
                                   original[cols].values, updated[cols].values):
        diff = {c: str(a) for c, b, a in zip(cols, before, after) if str(b) != str(a)}
        pair = str(pair or "").strip()
        if diff and pair:
            out.setdefault(pair, {}).update(diff)
    return out


def apply_corrections(patches: dict[str, dict], *, mode: str = "live") -> dict:
    """Write the audit's corrections into the stored verdicts. Returns the report.

    A thin wrapper over `extract/tier.py:supersede_targets` — it exists so this
    module's one write path names its own batch, and so the state authority is
    imported only when something is actually being written.
    """
    from filter.engine.claims import ClaimsClient, ClaimsNotConfigured
    from extract.tier import supersede_targets

    try:
        client = ClaimsClient()
    except ClaimsNotConfigured as exc:
        raise SystemExit(f"{exc}. The rows this corrects live in the state authority, "
                         "so there is nothing to correct without it.")
    return supersede_targets(client, patches, batch_label="audit-dois", mode=mode)


def audit_file(csv_path: Path,
               apply: bool = False,
               report_path: "Path | None" = None,
               only_doi: "str | None" = None,
               only_status: "list[str] | None" = None,
               mode: str = "live") -> dict:
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
    # What the file said when the audit read it. The corrections are derived by
    # diffing this against the audited frame, keyed by pair id, and written to the
    # verdicts the file is rendered from — see _patches / apply_corrections.
    original = df.copy()

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
            # Exactly what run_extract._verify_row does with a mismatch, and for the
            # same reason: the DOI is registered but describes a DIFFERENT paper, and
            # a completed search found nothing better. Keeping it sends validators to
            # the wrong original. The audit used to leave it in place, so a row it had
            # discredited reached unresolved_doi_mismatch.csv still carrying the DOI —
            # the two tools disagreeing about the same verdict. The title/author/year
            # claim stays, so the row can still be reviewed.
            df.at[idx, "doi_o"]          = ""
            df.at[idx, "bibtex_ref_o"]   = ""
            df.at[idx, "oa_work_id_o"]   = ""
            df.at[idx, "pair_id"]        = make_pair_id(clean_doi(str(row["doi_r"])), "")
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

    counts["_patches"] = _patches(original, df)
    if apply:
        written = apply_corrections(counts.pop("_patches"), mode=mode)
        counts["_applied"] = written
        log.info("superseded %d result row(s) over %d work(s); claim %s",
                 written["rows"], written["works"], str(written["claim"])[:12])
        if written["unmatched"]:
            log.warning("%d corrected row(s) had no stored verdict to correct — "
                        "their pair_ids are not in the live payloads: %s",
                        len(written["unmatched"]), ", ".join(written["unmatched"][:5]))

    return dict(counts)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Audit doi_o values in the exported rows; --apply corrects the "
                    "verdicts they are rendered from.")
    ap.add_argument("--apply", action="store_true",
                    help="claim the affected works and write a corrected, superseding "
                         "result verdict for each (default: dry-run). Render the "
                         "corrected CSV afterwards with python -m extract.export")
    ap.add_argument("--doi", help="audit a single row by doi_r")
    ap.add_argument("--status", action="append", metavar="VALUE",
                    help=("only rows whose current doi_o_verification is VALUE "
                          "(repeatable; '' means never verified). --status api_error "
                          "re-asks the rows whose verification could not be completed"))
    ap.add_argument("--mode", choices=("live", "validation"), default="live",
                    help="which export to audit, and whose verdicts --apply corrects: "
                         "live (data/extracted.csv) or validation "
                         "(data/extracted-test.csv).")
    args = ap.parse_args()

    path = _MODE_PATHS[args.mode]
    summary = audit_file(path, apply=args.apply, only_doi=args.doi,
                         only_status=args.status, mode=args.mode)
    applied = summary.pop("_applied", None)
    patches = summary.pop("_patches", {})

    print(f"\nDOI audit of {path.name}{' (APPLIED)' if args.apply else ' (dry-run)'}:")
    for status, n in sorted(summary.items()):
        print(f"  {status:<12} {n}")
    print(f"\nReport: {_REPORT_PATH}")
    if applied is not None:
        print(f"  {applied['rows']} row(s) over {applied['works']} work(s) superseded")
        print("  Render the corrected CSV with: python -m extract.export"
              + ("" if args.mode == "live" else
                 f" --mode {args.mode} --out {path}"))
    else:
        print(f"Dry-run only — {len(patches)} row(s) would be corrected; "
              "rerun with --apply.")


if __name__ == "__main__":
    main()
