# Supabase Schema

The validation repo uses Supabase (PostgreSQL) as its backend. This document describes the four tables used, and the columns that the monitoring dashboard reads.

**Who writes them.** This repo does not. Stage 3's output is `data/extracted.csv`;
the import of resolved rows into `unvalidated`, `record_metadata` and
`validation_queue` runs from the validation repo —
[`forrtproject/flora-validation/csv_to_db.py`](https://github.com/forrtproject/flora-validation/blob/main/csv_to_db.py)
(confirmed by @Rohan-Tondlekar in
[#172](https://github.com/forrtproject/flora-extractor/issues/172)). It reads
`data/extracted.csv` directly, connects with psycopg2, wraps the whole run in one
transaction and inserts `ON CONFLICT (pair_id) DO NOTHING`, so it is atomic and
idempotent. This repo only *reads* the tables, through `shared/supabase_client.py`,
for the Stage 4 dashboard.

An older importer lived here as `extract/csv_to_db.py`. It was never run against the
current schema and is parked on the `wip/csv-to-db` branch, with `WIP.md` recording
the non-atomic three-insert defect it carried. Its `_build_unvalidated_row()` /
`_build_queue_rows()` / `_build_metadata_row()` are referenced below only as written
descriptions of the payload shape — the live column list is the validation repo's.

## Tables

### `unvalidated`

All records pushed for validation. The importer must keep its payload in step with
the table: PostgREST rejects the whole insert when the payload names a column the
table does not have, so a new column needs an `alter table` first. The payload shape
is described by `_build_unvalidated_row()` on the `wip/csv-to-db` branch. The fields
the dashboard reads back are the `select` strings in `shared/supabase_client.py`. The
ones worth naming here:

| Column | Type | Description |
|--------|------|-------------|
| `record_id` | text | Primary key; the page order key for every paged read |
| `doi_r` / `doi_o` | text | Replication and original DOIs |
| `outcome` | text | Extracted outcome (`success`, `failure`, `mixed`, …) |
| `type` | text | `replication` or `reproduction` |
| `link_method` | text | How the original was found |
| `validation_status` | text | `unvalidated` \| `validated` \| `need_review` \| `validation_inprogress` |

### `validation_queue`

Individual validator assignments, their completion status, and the judgements
themselves. **The import writes only the skeleton** — one row per validator slot with
exactly four fields (`_build_queue_rows()` on the `wip/csv-to-db` branch) —

| Column | Type | Description |
|--------|------|-------------|
| `record_id` | text | The record being validated |
| `validator_slot` | text | One of `human_1`, `human_2`, `llm`; a DB CHECK constraint enforces the same three, and `_SLOT_PREFIX` in `shared/supabase_client.py` maps them to the `validated` table's column prefixes |
| `is_shown` | bool | Written `false`; the validation app flips it |
| `is_validated` | bool | Written `false`; the validation app flips it |

Every other column on the table — `validator_id`, `validator_name`, `type_check`,
`original_check`, `outcome_check`, `corrected_doi_o`, `corrected_outcome`,
`corrected_type`, `validator_notes` — is owned and filled by the external validation
app. This repo only *reads* them; the read side is `shared/supabase_client.py`
(`get_correction_frequency()`, `get_validation_analytics()`, `get_drilldown_page()`),
whose `select` strings are the authoritative list of what the dashboard needs.

### `validated`

Completed validation records with per-validator and LLM check results.

| Column | Type | Description |
|--------|------|-------------|
| `doi_r` | text | Replication DOI |
| `doi_o` | text | Original study DOI (as extracted) |
| `outcome` | text | Extracted outcome |
| `type` | text | Study type |
| `val1_type_check` | text | Validator 1 check on type: `correct` \| `incorrect` |
| `val1_original_check` | text | Validator 1 check on original DOI |
| `val1_outcome_check` | text | Validator 1 check on outcome |
| `val1_notes` | text | Validator 1 free-text notes |
| `val2_type_check` | text | Validator 2 check on type |
| `val2_original_check` | text | Validator 2 check on original DOI |
| `val2_outcome_check` | text | Validator 2 check on outcome |
| `val2_notes` | text | Validator 2 free-text notes |
| `llm_val_type_check` | text | LLM validation check on type |
| `llm_val_original_check` | text | LLM validation check on original DOI |
| `llm_val_outcome_check` | text | LLM validation check on outcome |

### `record_metadata`

Per-record metadata. Not currently used by the monitoring dashboard — the filter,
link and outcome method / confidence / model fields, the author, journal and OpenAlex
id fields, and the original rank and count (`_build_metadata_row` on the
`wip/csv-to-db` branch). A column added to the payload needs a matching
`alter table record_metadata add column …` before the next import: PostgREST
rejects the whole insert when the payload names a column the table does not have.

**Not sent by the live import.** The parked payload builder sends these; the live
`flora-validation/csv_to_db.py` does not, and the columns have to exist before an
import that sends them runs (tracked in forrtproject/flora-validation#3):

| Column | Type | Sent since | Description |
|--------|------|-----------|-------------|
| `screen_categories` | text | — | `\|`-joined union of the front-door voters' category labels; see [`csv-schema.md`](csv-schema.md) |
| `pdf_source` | text | forrtproject/flora-extractor#124 | Acquisition tier that supplied the full text (`arxiv`, `unpaywall_pdf`, `openalex_xml`, …); blank when the row read no document |
| `parse_method` | text | forrtproject/flora-extractor#124 | Parser whose result was sent to the LLM (`pdfminer`, `grobid`, `openalex_xml`, …); blank when nothing was parsed |

**Engine lineage: `work_id` and `release_id`.** Added by
`db/migrations/0002_validation_lineage.sql` (run in the Supabase SQL editor after
0001), both nullable because every row imported before the filter engine existed
has neither. **The live import does not populate them yet** — it sends
`openalex_id_r` as text and no release — so every record it writes carries null
lineage and is invisible to `filter/engine/supersede.py`. Wiring them up is part of
issue #172.

| Column | Type | Description |
|--------|------|-------------|
| `work_id` | bigint | The alias-resolved int64 OpenAlex id of the replication, derived from `openalex_id_r` via `filter/engine/workids.work_id()`. Null when the row carries no parseable OpenAlex id |
| `release_id` | text | The routing release the row was handed off under. Null unless the import was given `--release-id` |

`work_id` is the load-bearing one. **Routing provenance is linked, not copied**:
`extracted.csv` carries none of `ENGINE_EXPORT_COLS` (`route_rule`,
`matched_rules`, `pending_reason`, `release_id`, …) because `EXTRACTED_COLS`
excludes them by design, so an importer cannot read a release id off a row and must
not pretend to. What it writes instead is the identity everything else can be
joined on:

- **Which pile, rule and release a validated record came from** — join
  `record_metadata.work_id` against the engine's `routing` table
  (`filter/engine/store.py`, keyed `PRIMARY KEY (release_id, work_id)`), which
  holds `pile`, `pending_reason`, `rule_id`, `precedence` and `matched_rules` per
  release. The store is a disposable local DuckDB cache; it rebuilds from pool +
  spec bundle, so the lineage survives losing it.
- **What was spent on the work** — `engine_claim_items.work_id` →
  `engine_claims.release_id`/`tier`, and `engine_verdicts.work_id` for the tier
  evidence. These are in Postgres and are permanent.
- **Whether an upstream change has invalidated a sent record** —
  `filter/engine/supersede.py` reads exactly this join and writes
  `engine_supersessions` rows naming the affected `record_id`s. It never mutates
  `unvalidated`, `validated` or `validation_queue`.

`release_id` is a property of the **handoff**, not of a row: every row of one
Stage 3 run came through one `filter.engine handoff`, and that command's
`data/filtered.csv.manifest.json` names its `release_id`. The import should take it
as one argument for the whole run and stamp every row with it.

Omitting it costs nothing that cannot be recovered — the `work_id` join above
still answers every routing question — it only means the record does not record
which release it was sent under without consulting the store.

The reproduction axis columns (`outcome_computation`, `outcome_computational_quote`,
`out_quote_computational_source`, `outcome_robustness`, `outcome_robustness_quote`,
`out_quote_robust_source`) are the same kind of pending entry on `unvalidated`.

## Dashboard endpoints

Registered by the `dashboard` blueprint (`validate/routes/dashboard.py`); each one is
a thin wrapper over the correspondingly named function in `shared/supabase_client.py`,
where the `_get("<table>", …)` calls are the source of truth for what is read:

| Endpoint | Tables read | Description |
|----------|-------------|-------------|
| `GET /api/dashboard/supabase-stats` | `unvalidated`, `validation_queue` | KPIs: total, status counts, completion rate (a progress metric, **not** inter-rater agreement) |
| `GET /api/dashboard/supabase-corrections` | `validation_queue` | Per-field correction frequency, over completed judgements |
| `GET /api/dashboard/supabase-outcomes` | `validated` | Outcome distribution |
| `GET /api/dashboard/supabase-analytics` | `unvalidated` | Coverage and per-field validator agreement |
| `GET /api/dashboard/supabase-confusion` | `unvalidated` | Pipeline-vs-final confusion matrices (#72) |
| `GET /api/dashboard/supabase-drilldown` | `validation_queue`, `unvalidated` | Paginated incorrect-DOI table |

All endpoints cache responses for 5 minutes (`CACHE_TTL = 300` in `shared/supabase_client.py`).

## Configuration

Set in `.env`:

```bash
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key
```

If `SUPABASE_URL` is empty, all endpoints return `{"error": "supabase_not_configured"}` and the Validation tab shows a configuration notice.
