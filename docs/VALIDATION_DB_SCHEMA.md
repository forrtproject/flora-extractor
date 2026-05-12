# FLoRA Validation Database Schema

This document specifies the PostgreSQL/Supabase database used for the FLoRA
replication validation workflow. It is the authoritative reference for any agent
or developer building or maintaining the validation UI.

---

## Overview

After Stage 3 (`extract/run_extract.py`) produces `data/extracted.csv`, **resolved
rows only** (i.e. rows where `link_method` is not `target_pending` or `api_error`,
and `filter_status` is `replication` or `reproduction`) are loaded into the database
by `validate/csv_to_db.py`.

The database has four tables:

| Table | Purpose |
|---|---|
| `unvalidated` | One row per resolved (doi_r, doi_o) pair; summary of validation progress |
| `validation_queue` | Three rows per record (human_1, human_2, llm); individual validator assignments |
| `validated` | Final consensus records ready for FLoRA entry |
| `record_metadata` | Supplementary extraction data; keyed by `record_id` |

**Append-only**: data is never overwritten. Rows are inserted; corrections are
captured as new column values by validators. The `validated` table has a uniqueness
constraint on `(doi_r, study_r, doi_o, study_o)` to prevent duplicate final records.

---

## Unique Identifier

Every record in `unvalidated` gets a **random UUID4** (`record_id`) generated at
import time. This is the primary key linking all four tables.

UUID4 is preferred over an MD5 hash of `(doi_r, doi_o)` because:
- The `doi_o` may be corrected during validation — a content-derived hash would
  change, breaking all foreign key links.
- UUID4 is guaranteed unique regardless of content.

The original `pair_id` MD5 from `extracted.csv` is preserved in `record_metadata`
for provenance tracing but is not used as a primary key.

---

## Display Columns

These columns appear in all three main tables (`unvalidated`, `validation_queue`,
`validated`) and are what validators and reviewers see in the UI:

| Column | Source in extracted.csv | Notes |
|---|---|---|
| `doi_r` | `doi_r` | Replication paper DOI |
| `study_r` | `title_r` | Replication paper title |
| `year_r` | `year_r` | |
| `url_r` | `url_r` | Open-access URL for replication paper |
| `ref_r` | `ref_r` | "Surname · Year · Journal" |
| `abstract_r` | `abstract_r` | |
| `doi_o` | `doi_o` | Original study DOI (as resolved by Stage 3) |
| `study_o` | `title_o` | Original study title |
| `year_o` | `year_o` | |
| `url_o` | *(derived)* | `https://doi.org/{doi_o}` if doi_o present, else blank |
| `ref_o` | `ref_o` | "Surname · Year · Journal" |
| `type` | `type` | `replication` or `reproduction` |
| `outcome` | `outcome` | `success / failure / mixed / uninformative / descriptive` |
| `outcome_quote` | `outcome_phrase` | Supporting quote from the paper |
| `out_quote_source` | `out_quote_source` | `abstract / fulltext / title` |

> **Note on `url_o`**: This column is not produced by Stage 3. It is derived at
> import time as `https://doi.org/{doi_o}`. If `doi_o` is blank, `url_o` is blank.

---

## Table Definitions (SQL)

### `unvalidated`

One row per resolved `(doi_r, doi_o)` pair. The `validation_status` column tracks
where the record is in the workflow. Validator summary columns are updated in place
as each validator slot in `validation_queue` is completed.

```sql
CREATE TABLE unvalidated (
    record_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Display columns
    doi_r               TEXT        NOT NULL,
    study_r             TEXT,
    year_r              TEXT,
    url_r               TEXT,
    ref_r               TEXT,
    abstract_r          TEXT,
    doi_o               TEXT,
    study_o             TEXT,
    year_o              TEXT,
    url_o               TEXT,
    ref_o               TEXT,
    type                TEXT        CHECK (type IN ('replication', 'reproduction')),
    outcome             TEXT        CHECK (outcome IN (
                                        'success', 'failure', 'mixed',
                                        'uninformative', 'descriptive')),
    outcome_quote       TEXT,
    out_quote_source    TEXT,

    -- Workflow status
    validation_status   TEXT        NOT NULL DEFAULT 'unvalidated'
                                    CHECK (validation_status IN (
                                        'unvalidated', 'validation_inprogress',
                                        'validated', 'need_review')),

    -- Validator 1 summary
    val1_id             TEXT,
    val1_name           TEXT,
    val1_validated_at   TIMESTAMPTZ,
    val1_type_check     TEXT        CHECK (val1_type_check     IN ('correct', 'incorrect')),
    val1_original_check TEXT        CHECK (val1_original_check IN ('correct', 'incorrect')),
    val1_outcome_check  TEXT        CHECK (val1_outcome_check  IN ('correct', 'incorrect')),
    val1_corrected_doi_o   TEXT,
    val1_corrected_study_o TEXT,
    val1_corrected_outcome TEXT,
    val1_corrected_type    TEXT,
    val1_notes          TEXT,

    -- Validator 2 summary (same pattern as Validator 1)
    val2_id             TEXT,
    val2_name           TEXT,
    val2_validated_at   TIMESTAMPTZ,
    val2_type_check     TEXT        CHECK (val2_type_check     IN ('correct', 'incorrect')),
    val2_original_check TEXT        CHECK (val2_original_check IN ('correct', 'incorrect')),
    val2_outcome_check  TEXT        CHECK (val2_outcome_check  IN ('correct', 'incorrect')),
    val2_corrected_doi_o   TEXT,
    val2_corrected_study_o TEXT,
    val2_corrected_outcome TEXT,
    val2_corrected_type    TEXT,
    val2_notes          TEXT,

    -- LLM Validator summary
    llm_val_model           TEXT,
    llm_val_validated_at    TIMESTAMPTZ,
    llm_val_type_check      TEXT    CHECK (llm_val_type_check     IN ('correct', 'incorrect')),
    llm_val_original_check  TEXT    CHECK (llm_val_original_check IN ('correct', 'incorrect')),
    llm_val_outcome_check   TEXT    CHECK (llm_val_outcome_check  IN ('correct', 'incorrect')),
    llm_val_corrected_doi_o   TEXT,
    llm_val_corrected_study_o TEXT,
    llm_val_corrected_outcome TEXT,
    llm_val_corrected_type    TEXT,
    llm_val_notes           TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `validation_queue`

Three rows per `record_id` — one per validator slot (`human_1`, `human_2`, `llm`).
The UI uses `is_shown` to avoid assigning the same record to two validators at once,
and `is_validated` to know when a slot is complete.

```sql
CREATE TABLE validation_queue (
    queue_id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id           UUID        NOT NULL REFERENCES unvalidated(record_id),
    validator_slot      TEXT        NOT NULL
                                    CHECK (validator_slot IN ('human_1', 'human_2', 'llm')),

    -- Assignment tracking
    is_shown            BOOLEAN     NOT NULL DEFAULT FALSE,   -- TRUE = being shown to a validator
    is_validated        BOOLEAN     NOT NULL DEFAULT FALSE,   -- TRUE = validator has submitted

    -- Validator identity (filled when is_shown becomes TRUE)
    validator_id        TEXT,
    validator_name      TEXT,

    -- Core validation checks (filled when is_validated becomes TRUE)
    type_check          TEXT        CHECK (type_check     IN ('correct', 'incorrect')),
    original_check      TEXT        CHECK (original_check IN ('correct', 'incorrect')),
    outcome_check       TEXT        CHECK (outcome_check  IN ('correct', 'incorrect')),

    -- Corrections — filled only when the corresponding check = 'incorrect'
    corrected_doi_o     TEXT,
    corrected_study_o   TEXT,
    corrected_outcome   TEXT,
    corrected_type      TEXT,

    -- Extensible additional checks (JSONB so new check types can be added without schema changes)
    -- Example: {"author_check": "correct", "year_check": "incorrect"}
    additional_checks   JSONB,

    validator_notes     TEXT,
    shown_at            TIMESTAMPTZ,       -- when is_shown was set TRUE
    validated_at        TIMESTAMPTZ,       -- when is_validated was set TRUE

    UNIQUE (record_id, validator_slot)
);
```

### `validated`

Final consensus records. Populated automatically when all three validator slots for
a record are complete and there are no corrections (or corrections agree). If any
validator made a correction, the record goes to `need_review` in `unvalidated`
instead and requires manual adjudication before being inserted here.

```sql
CREATE TABLE validated (
    validated_record_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id           UUID        NOT NULL REFERENCES unvalidated(record_id),

    -- Display columns (original values from unvalidated)
    doi_r               TEXT        NOT NULL,
    study_r             TEXT,
    year_r              TEXT,
    url_r               TEXT,
    ref_r               TEXT,
    abstract_r          TEXT,
    doi_o               TEXT,
    study_o             TEXT,
    year_o              TEXT,
    url_o               TEXT,
    ref_o               TEXT,
    type                TEXT,
    outcome             TEXT,
    outcome_quote       TEXT,
    out_quote_source    TEXT,

    -- Final consensus values (same as original when all validators agreed)
    final_doi_o         TEXT,
    final_study_o       TEXT,
    final_outcome       TEXT,
    final_type          TEXT,

    validated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Prevent the same pair from being entered twice
    UNIQUE (doi_r, study_r, doi_o, study_o)
);
```

### `record_metadata`

All supplementary extraction data from `extracted.csv` that is not shown in the
main UI. Linked to `unvalidated` via `record_id`.

```sql
CREATE TABLE record_metadata (
    metadata_id             UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id               UUID    NOT NULL UNIQUE REFERENCES unvalidated(record_id),

    -- Original MD5 pair hash from extracted.csv (for provenance only)
    pair_id                 TEXT,

    -- Stage 2 filter info
    filter_status           TEXT,
    filter_method           TEXT,
    filter_evidence         TEXT,
    filter_confidence       TEXT,

    -- Stage 3 match-type info
    original_match_type     TEXT,
    original_match_confidence TEXT,

    -- Stage 3 linking info
    link_method             TEXT,
    link_evidence           TEXT,
    link_confidence         TEXT,
    link_llm_model          TEXT,

    -- Outcome detail
    outcome_confidence      TEXT,

    -- Bibliographic info
    authors_r               TEXT,
    authors_o               TEXT,
    journal_r               TEXT,
    openalex_id_r           TEXT,
    source                  TEXT,

    -- Multi-original bookkeeping
    original_rank           INTEGER,
    n_originals             INTEGER,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

## Validation Workflow

```
extracted.csv (resolved rows only)
        │
        ▼
  csv_to_db.py
        │  ├─ INSERT into unvalidated        (validation_status = 'unvalidated')
        │  ├─ INSERT into record_metadata
        │  └─ INSERT 3 rows into validation_queue
        │       (human_1, human_2, llm — all is_shown=FALSE, is_validated=FALSE)
        │
        ▼
  Validation UI assigns a record to a validator
        │  • Sets is_shown = TRUE, shown_at = NOW(), validator_id/name
        │  • Updates unvalidated.validation_status → 'validation_inprogress'
        │    (when at least one queue slot has is_shown=TRUE)
        │
        ▼
  Validator submits their review
        │  • Sets is_validated = TRUE, validated_at = NOW()
        │  • Writes type_check, original_check, outcome_check
        │  • Writes corrected_* fields if any check = 'incorrect'
        │  • Copies summary into unvalidated.val1_* / val2_* / llm_val_* columns
        │
        ▼
  After ALL THREE slots are is_validated = TRUE:
        │
        ├─ Any validator made a correction?
        │       YES → unvalidated.validation_status = 'need_review'
        │             (manual adjudication required before promoting to validated)
        │
        └─ All validators agreed (no corrections)?
                NO  → same as YES above (need_review)
                YES → unvalidated.validation_status = 'validated'
                      INSERT into validated (with UNIQUE guard on doi_r+study_r+doi_o+study_o)
```

### Validation Status Values

| Status | Meaning |
|---|---|
| `unvalidated` | Record imported; no validator has started |
| `validation_inprogress` | At least one queue slot has `is_shown = TRUE` |
| `validated` | All 3 slots complete; all agreed; copied to `validated` |
| `need_review` | All 3 slots complete but at least one validator made a correction |

### Status Transition Rules

- A record transitions to `validated` only when **all three** slots (`human_1`,
  `human_2`, `llm`) have `is_validated = TRUE`. One human + one LLM completing
  first does not trigger promotion.
- If any validator sets a `corrected_*` field, the record becomes `need_review`
  regardless of what the other validators said.
- `need_review` records are resolved by a project lead who adjudicates the
  disagreement and manually inserts the corrected row into `validated`.

---

## Import Script

Run `validate/csv_to_db.py` after `extract/run_extract.py` finishes:

```bash
python validate/csv_to_db.py --input data/extracted.csv
```

The script:
1. Reads `extracted.csv`
2. Filters to resolved rows: `filter_status IN (replication, reproduction)` AND
   `link_method NOT IN (target_pending, api_error)`
3. Generates a UUID4 `record_id` per row
4. Derives `url_o` from `doi_o`
5. Inserts into `unvalidated`, `record_metadata`, and `validation_queue` (3 slots)
6. Skips rows already present (idempotent — safe to re-run)

Required environment variables:

```bash
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=<service-role key>   # use service key for server-side inserts
```

---

## Design Decisions

- **Append-only**: no UPDATE or DELETE on existing rows. Corrections are captured
  as new column values by validators, not by modifying the original extracted values.
- **Configurable checks**: `additional_checks JSONB` in `validation_queue` allows
  new check types to be added without a schema migration.
- **LLM as tiebreaker**: The `llm` slot in `validation_queue` votes like a human.
  Because all three slots must agree before `validated` status is set, the LLM vote
  acts as a tiebreaker when the two humans disagree.
- **Uniqueness in `validated`**: The `UNIQUE (doi_r, study_r, doi_o, study_o)`
  constraint prevents a pair from being added twice even if the import script is
  re-run after partial failures.
