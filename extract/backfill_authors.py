"""
backfill_authors.py — Retroactively refresh authors_o / ref_o / bibtex_ref_o.

Fetches full author lists and APA-style references from OpenAlex for every extracted
row that has a doi_o. All OpenAlex responses are cached, so re-runs are fast.

Like `extract/audit_dois.py`, it READS the exported CSV and WRITES the verdicts that
CSV is rendered from: `python -m extract.export` rebuilds every row from the stored
payload, so an edit to the file would be gone at the next render. `--apply` claims the
affected works, writes a corrected result verdict per work and supersedes the previous
one (`extract/tier.py:supersede_targets`); rendering the corrected CSV is the
operator's next step.

    python -m extract.backfill_authors                  # dry-run: print the changes
    python -m extract.backfill_authors --apply          # claim, correct, supersede
    python -m extract.export                            # render the corrected CSV
    python -m extract.backfill_authors --doi 10.xxx/y   # one doi_o only
    python -m extract.backfill_authors --mode validation
"""
from __future__ import annotations

import argparse

import pandas as pd

from shared.config import DATA_DIR, log
from shared.utils import clean_doi
from extract.run_extract import _build_ref_o

# The CSV each mode's export writes, and therefore the file each mode's run reads.
_MODE_PATHS = {"live": DATA_DIR / "extracted.csv",
               "validation": DATA_DIR / "extracted-test.csv"}


def backfill(csv_path, apply: bool = False, target_doi: str = "",
             mode: str = "live") -> dict:
    """The corrections this run would make, as `pair_id → {column: value}`.

    A row with no pair id is skipped rather than guessed at: the pair id is how a
    correction names the stored target it is about.
    """
    if not csv_path.exists():
        log.error("%s does not exist", csv_path)
        return {}

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Loaded %d rows from %s", len(df), csv_path.name)

    patches: dict[str, dict] = {}
    changes: list[tuple] = []

    for _, row in df.iterrows():
        doi_o   = clean_doi(str(row.get("doi_o",   "") or ""))
        title_o = str(row.get("title_o", "") or "").strip()
        pair_id = str(row.get("pair_id", "") or "").strip()

        # Skip rows with neither a DOI nor a title to search by
        if not doi_o and not title_o:
            continue
        # Skip the one no_original_found row (no title, nothing to look up)
        if row.get("link_method") == "no_original_found":
            continue
        if target_doi and clean_doi(target_doi) != doi_o:
            continue
        if not pair_id:
            log.warning("[%s] no pair_id — cannot name the stored row to correct", doi_o)
            continue

        old_authors = str(row.get("authors_o", "") or "")
        old_ref     = str(row.get("ref_o",     "") or "")
        fallback_author = old_authors.split(";")[0].strip() if old_authors else ""

        try:
            new_ref, new_authors, new_bibtex = _build_ref_o(
                doi_o, fallback_author,
                str(row.get("year_o", "") or ""),
                title_o,
            )
        except Exception as exc:
            log.warning("[%s] backfill failed: %s", doi_o, exc)
            continue

        if new_authors != old_authors or new_ref != old_ref:
            changes.append((new_authors, new_ref, old_authors, old_ref, doi_o))
            patches[pair_id] = {"authors_o": new_authors, "ref_o": new_ref,
                                "bibtex_ref_o": new_bibtex}

    print(f"\nBackfill: {len(changes)} rows changed out of {len(df)} total")
    for new_auth, new_ref, old_auth, old_ref, doi_o in changes[:20]:
        print(f"\n  doi_o    : {doi_o}")
        print(f"  authors_o: {old_auth!r}")
        print(f"           -> {new_auth!r}")
        print(f"  ref_o    : {old_ref!r}")
        print(f"           -> {new_ref[:120]!r}")
    if len(changes) > 20:
        print(f"\n  … and {len(changes) - 20} more rows")

    if apply and patches:
        from extract.audit_dois import apply_corrections

        written = apply_corrections(patches, mode=mode)
        print(f"\n{written['rows']} row(s) over {written['works']} work(s) superseded "
              f"(claim {str(written['claim'])[:12]})")
        if written["unmatched"]:
            print(f"  {len(written['unmatched'])} row(s) had no stored verdict to "
                  "correct — their pair_ids are not in the live payloads")
        print("Render the corrected CSV with: python -m extract.export")
        log.info("backfill_authors: superseded %d row(s)", written["rows"])
    elif not apply:
        print("\nDry-run — nothing written. Pass --apply to correct the verdicts.")
    return patches


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill authors_o / ref_o from OpenAlex, into the stored "
                    "verdicts data/extracted.csv is rendered from."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Claim the affected works and write a corrected, "
                             "superseding result verdict for each (default: dry-run).")
    parser.add_argument("--mode", choices=("live", "validation"), default="live",
                        help="Which export to read, and whose verdicts --apply "
                             "corrects.")
    parser.add_argument("--doi", type=str, default="",
                        help="Only update rows with this doi_o.")
    args = parser.parse_args()

    backfill(_MODE_PATHS[args.mode], apply=args.apply, target_doi=args.doi,
             mode=args.mode)
