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
    """Fetch pair_ids already in record_metadata so we can skip them."""
    response = client.table("record_metadata").select("pair_id").execute()
    return {r["pair_id"] for r in (response.data or []) if r.get("pair_id")}


def run_import(csv_path: Path, dry_run: bool = False) -> None:
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
    skipped_fp = (df["filter_status"] == "false_positive").sum()
    skipped_pending = (~resolved_mask & ~(df["filter_status"] == "false_positive")).sum()

    skipped_no_orig = (df["link_method"] == "no_original_found").sum()

    print(f"  Total rows:         {len(df)}")
    print(f"  Resolved (import):  {len(resolved)}")
    print(f"  false_positive:     {skipped_fp}  (skipped — not replications)")
    print(f"  no_original_found:  {skipped_no_orig}  (skipped — LLM found no identifiable original)")
    print(f"  target_pending / api_error / other: {skipped_pending - skipped_no_orig}  (skipped — not yet resolved)")

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

        # Insert in dependency order: unvalidated first (FK parent), then children
        client.table("unvalidated").insert(unvalidated_row).execute()
        client.table("record_metadata").insert(metadata_row).execute()
        client.table("validation_queue").insert(queue_rows).execute()

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
    args = parser.parse_args()

    if not args.input.exists():
        raise FileNotFoundError(f"Input file not found: {args.input}")

    run_import(args.input, dry_run=args.dry_run)
