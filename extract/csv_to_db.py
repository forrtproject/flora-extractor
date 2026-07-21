"""
csv_to_db.py — Import resolved rows from extracted.csv into the Supabase validation database.

Only rows where filter_status is 'replication' or 'reproduction' AND link_method is not
'target_pending' or 'api_error' are imported. These are the rows ready for validation.

For each imported row this script creates:
  - 1 row in 'unvalidated'      (the record, validation_status = 'unvalidated')
  - 1 row in 'record_metadata'  (supplementary extraction data)
  - 3 rows in 'validation_queue' (one slot each for human_1, human_2, llm)

Safe to re-run: rows already in the database are detected by pair_id and skipped.

Usage:
    python -m extract.csv_to_db --input data/extracted.csv

Required environment variables:
    SUPABASE_URL         — https://<project>.supabase.co
    SUPABASE_SERVICE_KEY — service-role key (not anon key)
"""
import argparse
import os
import uuid
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# Columns shown in the UI for all main tables
_DISPLAY_COLS = {
    "doi_r", "title_r", "year_r", "url_r", "ref_r", "abstract_r",
    "doi_o", "title_o", "year_o", "ref_o", "type",
    "outcome", "outcome_phrase", "out_quote_source",
}

# Resolved link methods — rows with these methods are ready for validation.
# Sourced from schema so the granular rule-based methods (citation_context_match,
# same_author_year_title_overlap, single_candidate_after_requery, title_pattern_match,
# grobid_ref_match) plus llm_abstract/llm_fulltext stay in sync. Legacy rows migrated
# to author_year_match_legacy are still resolved, so they import too.
# no_original_found: LLM ran but concluded no identifiable original exists — excluded.
from shared.schema import RESOLVED_LINK_METHODS as _SCHEMA_RESOLVED_METHODS
# Same skip list Stage 3 uses, so extraction and validation agree.
from shared.flora_skip import default_flora_skip_dois
from shared.utils import clean_doi

_RESOLVED_METHODS = _SCHEMA_RESOLVED_METHODS | {"author_year_match_legacy"}
_RESOLVED_STATUSES = {"replication", "reproduction"}

# Validator slots created per record
_VALIDATOR_SLOTS = ("human_1", "human_2", "llm")


def _derive_url_o(doi_o: str) -> str:
    doi_o = str(doi_o or "").strip()
    return f"https://doi.org/{doi_o}" if doi_o else ""


def _s(val) -> str:
    """Coerce to stripped string; treat NaN/None as empty string."""
    if val is None or (isinstance(val, float) and val != val):
        return ""
    return str(val).strip()


def _int_or_none(val) -> "int | None":
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _build_unvalidated_row(record_id: str, row: pd.Series) -> dict:
    return {
        "record_id":        record_id,
        "doi_r":            _s(row.get("doi_r")),
        "study_r":          _s(row.get("title_r")),
        "year_r":           _s(row.get("year_r")),
        "url_r":            _s(row.get("url_r")),
        "ref_r":            _s(row.get("ref_r")),
        "abstract_r":       _s(row.get("abstract_r")),
        "doi_o":            _s(row.get("doi_o")),
        "study_o":          _s(row.get("title_o")),
        "year_o":           _s(row.get("year_o")),
        "url_o":            _derive_url_o(row.get("doi_o")),
        "ref_o":            _s(row.get("ref_o")),
        "type":             _s(row.get("type")),
        "outcome":          _s(row.get("outcome")),
        "outcome_quote":    _s(row.get("outcome_phrase")),
        "out_quote_source": _s(row.get("out_quote_source")),
        "validation_status": "unvalidated",
    }


def _build_metadata_row(record_id: str, row: pd.Series) -> dict:
    return {
        "record_id":                  record_id,
        "pair_id":                    _s(row.get("pair_id")),
        "filter_status":              _s(row.get("filter_status")),
        "filter_method":              _s(row.get("filter_method")),
        "filter_evidence":            _s(row.get("filter_evidence")),
        "filter_confidence":          _s(row.get("filter_confidence")),
        "original_match_type":        _s(row.get("original_match_type")),
        "original_match_confidence":  _s(row.get("original_match_confidence")),
        "link_method":                _s(row.get("link_method")),
        "link_evidence":              _s(row.get("link_evidence")),
        "link_confidence":            _s(row.get("link_confidence")),
        "link_llm_model":             _s(row.get("link_llm_model")),
        "outcome_confidence":         _s(row.get("outcome_confidence")),
        "authors_r":                  _s(row.get("authors_r")),
        "authors_o":                  _s(row.get("authors_o")),
        "journal_r":                  _s(row.get("journal_r")),
        "openalex_id_r":              _s(row.get("openalex_id_r")),
        "source":                     _s(row.get("source")),
        "original_rank":              _int_or_none(row.get("original_rank")),
        "n_originals":                _int_or_none(row.get("n_originals")),
    }


def _build_queue_rows(record_id: str) -> list[dict]:
    return [
        {
            "record_id":      record_id,
            "validator_slot": slot,
            "is_shown":       False,
            "is_validated":   False,
        }
        for slot in _VALIDATOR_SLOTS
    ]


def _load_existing_pair_ids(client: Client) -> set[str]:
    """Fetch pair_ids already in record_metadata so we can skip them.

    supabase-py caps a single select at 1000 rows, so we page through with
    .range() until a short page comes back. Without this, re-imports against a
    DB with >1000 records would miss existing pair_ids and re-insert duplicates.
    """
    page_size = 1000
    pair_ids: set[str] = set()
    start = 0
    while True:
        response = (
            client.table("record_metadata")
            .select("pair_id")
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = response.data or []
        pair_ids.update(r["pair_id"] for r in batch if r.get("pair_id"))
        if len(batch) < page_size:
            break
        start += page_size
    return pair_ids


def run_import(csv_path: Path, dry_run: bool = False,
               audit_report: "Path | None" = None,
               skip_flora: bool = True) -> None:
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        raise EnvironmentError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in environment or .env"
        )

    print(f"Reading {csv_path} …")
    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")

    # Filter to resolved rows only
    resolved_mask = (
        df["filter_status"].isin(_RESOLVED_STATUSES) &
        df["link_method"].isin(_RESOLVED_METHODS)
    )
    resolved = df[resolved_mask].copy()

    # Optional pre-validation audit gate: drop rows whose pair_id carries a
    # BLOCKER finding in the report. Default behaviour (no --audit-report) is
    # unchanged.
    skipped_audit = 0
    if audit_report is not None:
        from extract.audit_extracted import blocked_pair_ids
        blocked = blocked_pair_ids(audit_report)
        if blocked:
            audit_mask = resolved["pair_id"].map(
                lambda v: str(v or "").strip() in blocked)
            skipped_audit = int(audit_mask.sum())
            resolved = resolved[~audit_mask].copy()

    # FLoRA gate: never send a replication FLoRA already has back to validators.
    # The extraction-time skip only guards NEW rows; rows already sitting in
    # extracted.csv (e.g. 10.1037/per0000041) would otherwise still be imported.
    # Same skip list as Stage 3 — see shared/flora_skip.py.
    skipped_flora = 0
    if skip_flora:
        in_flora = default_flora_skip_dois()
        if in_flora:
            flora_mask = resolved["doi_r"].map(
                lambda v: clean_doi(str(v or "")) in in_flora)
            skipped_flora = int(flora_mask.sum())
            resolved = resolved[~flora_mask].copy()

    # Bucket every skipped row into exactly one category via disjoint masks, so counts
    # always sum to len(df) and cannot go negative. A false_positive row that also has
    # link_method == 'no_original_found' belongs to false_positive only.
    fp_mask = ~resolved_mask & (df["filter_status"] == "false_positive")
    no_orig_mask = ~resolved_mask & ~fp_mask & (df["link_method"] == "no_original_found")
    other_pending_mask = ~resolved_mask & ~fp_mask & ~no_orig_mask

    skipped_fp = fp_mask.sum()
    skipped_no_orig = no_orig_mask.sum()
    skipped_other = other_pending_mask.sum()

    print(f"  Total rows:         {len(df)}")
    print(f"  Resolved (import):  {len(resolved)}")
    print(f"  false_positive:     {skipped_fp}  (skipped — not replications)")
    print(f"  no_original_found:  {skipped_no_orig}  (skipped — LLM found no identifiable original)")
    print(f"  target_pending / api_error / other: {skipped_other}  (skipped — not yet resolved)")
    if skip_flora:
        print(f"  already in FLoRA:   {skipped_flora}  (skipped — FLoRA already has this replication)")
    if audit_report is not None:
        print(f"  audit BLOCKER:      {skipped_audit}  (skipped — flagged by pre-validation audit)")

    if resolved.empty:
        print("Nothing to import.")
        return

    if dry_run:
        print("[dry-run] Would import the following rows:")
        print(resolved[["doi_r", "doi_o", "filter_status", "link_method"]].to_string())
        return

    client: Client = create_client(supabase_url, supabase_key)

    existing_pair_ids = _load_existing_pair_ids(client)
    print(f"  Already in DB:      {len(existing_pair_ids)} pair_ids — will skip")

    inserted = 0
    skipped_dup = 0

    for _, row in resolved.iterrows():
        pair_id = _s(row.get("pair_id"))
        if pair_id and pair_id in existing_pair_ids:
            skipped_dup += 1
            continue

        record_id = str(uuid.uuid4())

        unvalidated_row = _build_unvalidated_row(record_id, row)
        metadata_row    = _build_metadata_row(record_id, row)
        queue_rows      = _build_queue_rows(record_id)

        # These three inserts are not atomic. record_metadata is the dedup anchor
        # (_load_existing_pair_ids skips any pair_id already there), so it must be
        # written LAST: if a run dies partway, an orphaned unvalidated/queue row is
        # harmless and gets completed on re-run, whereas a record_metadata row without
        # its siblings would make dedup skip the pair forever, leaving it incomplete.
        client.table("unvalidated").insert(unvalidated_row).execute()
        client.table("validation_queue").insert(queue_rows).execute()
        client.table("record_metadata").insert(metadata_row).execute()

        inserted += 1
        if inserted % 10 == 0:
            print(f"  … imported {inserted} records")

    print(f"\nDone. Inserted: {inserted}  |  Skipped (already in DB): {skipped_dup}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import extracted.csv into Supabase validation DB")
    parser.add_argument(
        "--input", type=Path, default=Path("data/extracted.csv"),
        help="Path to extracted.csv (default: data/extracted.csv)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be imported without touching the database.",
    )
    parser.add_argument(
        "--audit-report", type=Path, default=None,
        help="Path to a pre_validation_audit.csv; rows whose pair_id has a "
             "BLOCKER finding are skipped. Omit to disable the gate.",
    )
    parser.add_argument(
        "--skip-flora", action=argparse.BooleanOptionalAction, default=True,
        help="Skip rows whose doi_r is already in FLoRA (validated entry-sheet rows "
             "+ every row in flora.csv). ON by default; pass --no-skip-flora to "
             "import them anyway.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        raise FileNotFoundError(f"Input file not found: {args.input}")

    run_import(args.input, dry_run=args.dry_run, audit_report=args.audit_report,
               skip_flora=args.skip_flora)
