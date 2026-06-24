# Supabase Schema

The validation repo uses Supabase (PostgreSQL) as its backend. This document describes the four tables used, and the columns that the monitoring dashboard reads.

## Tables

### `unvalidated`

All records pushed for validation. The FLoRA extractor pipeline writes `extracted.csv`; the validation repo imports rows here.

| Column | Type | Description |
|--------|------|-------------|
| `doi_r` | text | Replication paper DOI |
| `doi_o` | text | Original study DOI |
| `title_r` | text | Replication paper title |
| `title_o` | text | Original study title |
| `outcome` | text | Extracted outcome (`success`, `failure`, `mixed`, etc.) |
| `type` | text | `replication` or `reproduction` |
| `link_method` | text | How the original was found |
| `validation_status` | text | `unvalidated` \| `validated` \| `need_review` \| `validation_inprogress` |

### `validation_queue`

Individual validator assignments and completion status.

| Column | Type | Description |
|--------|------|-------------|
| `doi_r` | text | Replication DOI being validated |
| `validator_id` | text | Validator identifier |
| `is_validated` | bool | Whether this assignment has been completed |
| `assigned_at` | timestamp | When assigned |
| `completed_at` | timestamp | When completed (null if pending) |

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

Per-record metadata. Not currently used by the monitoring dashboard.

## Dashboard endpoints

The monitoring dashboard reads these tables via three API endpoints:

| Endpoint | Tables read | Description |
|----------|-------------|-------------|
| `GET /api/dashboard/supabase-stats` | `unvalidated`, `validation_queue` | KPIs: total, progress, validators, agreement |
| `GET /api/dashboard/supabase-corrections` | `validated` | Per-field correction frequency |
| `GET /api/dashboard/supabase-outcomes` | `validated` | Outcome distribution |
| `GET /api/dashboard/supabase-drilldown` | `validated` | Paginated incorrect-DOI table |

All endpoints cache responses for 5 minutes (`CACHE_TTL = 300` in `shared/supabase_client.py`).

## Configuration

Set in `.env`:

```bash
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key
```

If `SUPABASE_URL` is empty, all endpoints return `{"error": "supabase_not_configured"}` and the Validation tab shows a configuration notice.
