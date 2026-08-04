"""
csv_to_db.py — Import resolved rows from extracted.csv into the Supabase validation database.

Only rows where filter_status is 'replication' or 'reproduction' AND link_method is not
'target_pending' or 'api_error' are imported. These are the rows ready for validation.

For each imported row this script creates:
  - 1 row in 'unvalidated'      (the record, validation_status = 'unvalidated')
  - 1 row in 'record_metadata'  (supplementary extraction data)
  - 3 rows in 'validation_queue' (one slot each for human_1, human_2, llm)

Safe to re-run: rows already in the database are detected by pair_id and skipped.

REQUIRES A SCHEMA CHANGE IN THE VALIDATION REPO BEFORE IT WILL RUN. `unvalidated`
needs `title_r` and `title_o` columns. `study_r` / `study_o` are study identifiers —
which study inside the paper is meant — and were carrying paper titles instead, so
one column name meant a study number in FLoRA's codebook and a title in the DB, and
the real study numbers had nowhere to go. This script now sends titles as titles and
`study_r` / `study_o` as the numbers extracted.csv holds. Until those two columns exist, every
insert fails with a PostgREST "column does not exist" error; nothing is written and
nothing is corrupted.

Usage:
    python -m extract.csv_to_db --input data/extracted.csv [--release-id <id>]

Engine lineage is LINKED, not copied. `extracted.csv` carries none of
ENGINE_EXPORT_COLS, so this script writes `record_metadata.work_id` (the int64
OpenAlex id) and everything the engine knows about the row — pile, winning rule,
release — is recovered by joining that against the routing store. `--release-id`
optionally stamps the handoff's release; see docs/supabase-schema.md.

Required environment variables:
    SUPABASE_URL         — https://<project>.supabase.co
    SUPABASE_SERVICE_KEY — service-role key (not anon key)
"""
import argparse
import os
import uuid
from pathlib import Path

import pandas as pd
from supabase import create_client, Client

# Imported for its side effect: shared/config.py is the one place that loads .env and
# .env.defaults, in that precedence. A bare load_dotenv() here would read .env only
# and so miss SUPABASE_URL, which now lives in the committed .env.defaults.
import shared.config  # noqa: F401

# Columns shown in the UI for all main tables
_DISPLAY_COLS = {
    "doi_r", "title_r", "year_r", "url_r", "ref_r", "abstract_r",
    "doi_o", "title_o", "study_o", "year_o", "ref_o", "type",
    "outcome", "outcome_phrase", "out_quote_source",
}

# Resolved link methods — rows with these methods are ready for validation.
# Sourced from schema so the granular rule-based methods (citation_context_match,
# same_author_year_title_overlap, single_candidate_after_requery, title_pattern_match,
# grobid_ref_match) plus llm_cited_candidates/llm_fulltext stay in sync. Legacy rows migrated
# to author_year_match_legacy are still resolved, so they import too.
# no_original_found: LLM ran but concluded no identifiable original exists — excluded.
from shared.schema import RESOLVED_LINK_METHODS as _SCHEMA_RESOLVED_METHODS
# Same skip list Stage 3 uses, so extraction and validation agree.
from shared.flora_skip import default_flora_skip_dois
from shared.utils import bare_work_id, clean_doi
# Lineage (#146 M5): a pushed row carries the engine identity it was routed under,
# so reconciliation keys on work_id rather than on a DOI string.
from filter.engine.workids import work_id as _openalex_work_id

_RESOLVED_METHODS = _SCHEMA_RESOLVED_METHODS | {"author_year_match_legacy"}
_RESOLVED_STATUSES = {"replication", "reproduction"}

# Validator slots created per record. SOURCE OF TRUTH is the validation DB's CHECK
# constraint (flora-validation/db_schema.sql): validator_slot IN ('human_1','human_2',
# 'llm'). These must stay identical to that constraint and to supabase_client._SLOT_PREFIX
# — #50(a). validator_id is intentionally NOT set here: it is populated by the
# flora-validation app when a validator claims/acts on a slot (null at import time).
# Cohen's-kappa / IRR over these judgements is computed in flora-validation, not here.
_VALIDATOR_SLOTS = ("human_1", "human_2", "llm")


# doi_o_verification values for which the DOI is trustworthy enough to link directly.
_TRUSTED_DOI_VERIFICATION = {"verified", "corrected"}


def _derive_url_o(row) -> str:
    """Build the original-study URL a validator will click.

    A doi.org link is only emitted for a DOI whose metadata was actually confirmed.
    Two failure modes this avoids:
      * a mismatched DOI resolves to a REAL but WRONG paper — a confident link to
        the wrong original is worse than no link, because it looks authoritative;
      * originals with no registered DOI (preprints, old papers, book chapters)
        previously produced an empty cell, leaving the validator nothing to click.
    Without a trustworthy DOI, the canonical OpenAlex work URL is used when the row
    carries an oa_work_id_o — it names one specific record, unlike the title search
    below it, which is only a lookup that happens to be resolvable.
    """
    if isinstance(row, str):          # tolerate the old str signature
        row = {"doi_o": row, "doi_o_verification": "verified"}
    doi_o = str(row.get("doi_o", "") or "").strip()
    verification = str(row.get("doi_o_verification", "") or "").strip().lower()
    title_o = str(row.get("title_o", "") or "").strip()

    if doi_o and verification in _TRUSTED_DOI_VERIFICATION:
        return f"https://doi.org/{doi_o}"
    work_id = bare_work_id(str(row.get("oa_work_id_o", "") or ""))
    if work_id:
        return f"https://openalex.org/{work_id}"
    if title_o:
        from urllib.parse import quote_plus
        return f"https://openalex.org/works?search={quote_plus(title_o)}"
    # No trustworthy DOI and no title — nothing honest to point at.
    return f"https://doi.org/{doi_o}" if (doi_o and verification == "") else ""


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
        # study_r / study_o are STUDY IDENTIFIERS — which study inside the paper is
        # meant — never titles. They used to carry title_r / title_o, which made one
        # column name mean a study number in FLoRA's codebook and a title in the DB,
        # and left the actual study numbers with nowhere to go.
        #
        # Both now come from extracted.csv: the target prompt reports which study of
        # the ORIGINAL is targeted and which study of the REPLICATION re-tests it. The
        # titles move to title_r / title_o, which the validation DB needs columns for
        # — see issue #103. No new DB column is needed for study_r: the column already
        # exists and only its value changes.
        "study_r":          _s(row.get("study_r")),
        "title_r":          _s(row.get("title_r")),
        "year_r":           _s(row.get("year_r")),
        "url_r":            _s(row.get("url_r")),
        "ref_r":            _s(row.get("ref_r")),
        "abstract_r":       _s(row.get("abstract_r")),
        "doi_o":            _s(row.get("doi_o")),
        "study_o":          _s(row.get("study_o")),
        "title_o":          _s(row.get("title_o")),
        "year_o":           _s(row.get("year_o")),
        "url_o":            _derive_url_o(row),
        "ref_o":            _s(row.get("ref_o")),
        "type":             _s(row.get("type")),
        "outcome":          _s(row.get("outcome")),
        "outcome_quote":    _s(row.get("outcome_phrase")),
        "out_quote_source": _s(row.get("out_quote_source")),
        # Reproduction axes — empty on a replication row. `outcome` above carries the
        # two joined, but a validator judging one axis needs the quote for that axis,
        # not one quote asked to justify both.
        "outcome_computation":            _s(row.get("outcome_computation")),
        "outcome_computational_quote":    _s(row.get("outcome_computational_quote")),
        "out_quote_computational_source": _s(row.get("out_quote_computational_source")),
        "outcome_robustness":             _s(row.get("outcome_robustness")),
        "outcome_robustness_quote":       _s(row.get("outcome_robustness_quote")),
        "out_quote_robust_source":        _s(row.get("out_quote_robust_source")),
        "validation_status": "unvalidated",
    }


def _work_id_or_none(val) -> "int | None":
    """The int64 OpenAlex id of an `openalex_id_r` cell, or None if it has none.

    Absent or unparseable is null, never an error: rows predating the engine (and
    rows whose openalex id was never resolved) still import — they simply carry no
    lineage, which is exactly what a null work_id says.
    """
    try:
        return _openalex_work_id(_s(val))
    except ValueError:
        return None


def _build_metadata_row(record_id: str, row: pd.Series,
                        release_id: "str | None" = None) -> dict:
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
        # Full-text provenance: which acquisition tier supplied the document and
        # which parser produced the text. Blank when the row read no document.
        "pdf_source":                 _s(row.get("pdf_source")),
        "parse_method":               _s(row.get("parse_method")),
        "screen_categories":          _s(row.get("screen_categories")),
        "outcome_confidence":         _s(row.get("outcome_confidence")),
        "authors_r":                  _s(row.get("authors_r")),
        "authors_o":                  _s(row.get("authors_o")),
        "journal_r":                  _s(row.get("journal_r")),
        "openalex_id_r":              _s(row.get("openalex_id_r")),
        # Lineage (#146 M5). `work_id` is the durable half and the one
        # reconciliation actually keys on: it is derived here from
        # `openalex_id_r`, a column every extracted row carries.
        #
        # `release_id` is NOT read from the row. `EXTRACTED_COLS` is
        # `["pair_id"] + FILTERED_COLS + EXTRACT_ADDED_COLS` and deliberately
        # excludes `ENGINE_EXPORT_COLS`, so `extracted.csv` has no `release_id`
        # column and never will — the engine's provenance is linked, not copied
        # forward stage by stage. Reading `row.get("release_id")` here therefore
        # only ever produced None while looking like it read data.
        #
        # The release is a property of the HANDOFF, not of an individual row:
        # every row in one Stage 3 run came through one `filter.engine handoff`,
        # whose `data/filtered.csv.manifest.json` names its `release_id`. Pass it
        # with `--release-id` to stamp it; without it the column is null and the
        # release is still recoverable after the fact by joining
        # `record_metadata.work_id` against the engine's routing state — see
        # docs/supabase-schema.md.
        "work_id":                    _work_id_or_none(row.get("openalex_id_r")),
        "release_id":                 release_id or None,
        "oa_work_id_r":               _s(row.get("oa_work_id_r")),
        "oa_work_id_o":               _s(row.get("oa_work_id_o")),
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
               skip_flora: bool = True,
               release_id: "str | None" = None) -> None:
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
        metadata_row    = _build_metadata_row(record_id, row, release_id)
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
    parser.add_argument(
        "--release-id", default=None,
        help="Routing release these rows were handed off under, stamped into "
             "record_metadata.release_id. Read it from the `release_id` field of "
             "data/filtered.csv.manifest.json (written by `filter.engine handoff`). "
             "Omit for rows that did not come through the engine; work_id lineage "
             "is written either way.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        raise FileNotFoundError(f"Input file not found: {args.input}")

    run_import(args.input, dry_run=args.dry_run, audit_report=args.audit_report,
               skip_flora=args.skip_flora, release_id=args.release_id)
