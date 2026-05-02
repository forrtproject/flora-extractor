
# Stage 4 — Validate

**Input:** `data/extracted.csv` (loaded via `validate/import_csv.py`)
**Output:** `data/validated.csv`
**Run:**

```bash
python -m validate.import_csv
python -m validate.app
```

**URL:** `http://localhost:5001`

---

## What This Stage Does

A Flask web application that lets human reviewers check and vote on the extraction results from Stage 3. Reviewers see the extracted original study and outcome for each replication paper, vote to confirm or reject the extraction, and leave notes. Validated results are exported to `data/validated.csv` for entry into the FLoRA database.

---

## App Architecture

```
validate/
├── app.py              Flask entry point, blueprint registration, startup data load
├── import_csv.py       One-time: loads extracted.csv into SQLite
├── models.py           SQLAlchemy: Replication, Vote tables
├── state.py            In-memory shared state (DataFrames, locks)
├── routes/
│   ├── dashboard.py    GET  /dashboard
│   ├── review.py       GET  /validate    POST /vote
│   ├── flora.py        GET  /flora       GET  /api/flora
│   ├── input.py        GET  /input       POST /input/generate
│   ├── batch.py        SSE batch pipeline runner
│   ├── multi_originals.py  GET /multi-originals
│   ├── export.py       GET  /export      POST /api/export/download
│   └── disambiguation.py   GET /disambiguation
└── templates/
    ├── base.html       Shared nav + layout
    ├── dashboard.html
    ├── validate.html   Main voting UI
    ├── flora.html      FLoRA master list
    ├── set_name.html   Reviewer name prompt
    ├── input.html
    └── export.html
```

---

## Navigation Order

The app navbar renders tabs in this order:

1. **Dashboard** — stats overview
2. **Validate** — voting queue (primary workflow)
3. **FLoRA** — master list of all records with FLoRA status
4. **Input** — data generation / batch pipeline runner
5. **Single DOI** — run pipeline on a single DOI manually

---

## Dashboard (`/dashboard`)

Summary stats for the current dataset:

- Total papers loaded
- Breakdown by `filter_status` (replication / reproduction / false_positive)
- Breakdown by `original_match_type` (single_original / multiple_match / multiple_original)
- Breakdown by `outcome` (success / failure / mixed / uninformative / pending)
- Breakdown by `validation_status` (confirmed / rejected / pending / needs_review)
- Vote counts and completion percentage

Stats are served by `GET /api/dashboard/stats` as JSON and rendered client-side.

---

## Validate Tab (`/validate`) — Minimal UI

The primary reviewer workflow. Designed to show only what is needed for a confident vote. Additional detail is hidden behind a "Full Log" toggle.

### Card Layout

Each paper displays as a card with two panels:

**Left panel — Paper summary:**

| Field             | Source column                                              |
| ----------------- | ---------------------------------------------------------- |
| Replication title | `title_r`                                                  |
| Replication year  | `year_r`                                                   |
| Authors           | `authors_r`                                                |
| Abstract          | `abstract_r` (truncated to 300 chars, expandable)          |
| Original study    | `title_o` (`year_o`, `authors_o`)                          |
| Proposed outcome  | `outcome` — colour-coded pill (green / red / orange / grey)|
| Outcome phrase    | `outcome_phrase` (supporting quote)                        |

**Right panel — Voting:**

- **Confirm** button — marks the extraction as correct
- **Reject** button — marks the extraction as wrong
- **Needs Review** button — flags for a second look
- **Comment box** — free text, optional, saved with the vote

Vote is submitted via `POST /vote`. Duplicate votes from the same reviewer session update rather than stack.

### Full Log Toggle

A `Full Log ▸` button at the bottom of each card expands a details section showing:

| Section             | Content                                                                  |
| ------------------- | ------------------------------------------------------------------------ |
| Pipeline log        | `link_method`, `link_evidence`, `link_confidence`                        |
| Outcome log         | `out_quote_source`, `outcome_confidence`                                 |
| PDF cache           | PDF source tier used, direct link to cached PDF if available             |
| LLM prompt          | Full prompt text sent to the LLM                                         |
| LLM response        | Raw JSON response from the model                                         |
| OpenAlex candidates | All candidates considered before resolution                              |
| GROBID sections     | Extracted abstract / intro / methods / references from PDF               |
| Filter trace        | `filter_status`, `filter_method`, `filter_evidence`, `filter_confidence` |
| Match type trace    | `original_match_type`, `original_match_confidence`                       |

This full log is loaded lazily (`GET /api/validate/log?doi_r=...`) to keep the initial page fast.

### Queue Behaviour

- Papers are served in order of `validation_status = pending` first, then `needs_review`
- Multi-original papers (n_originals > 1) are grouped: all rows for a given `doi_r` appear together
- Confirmed and rejected papers are hidden from the default queue; a toggle shows them for correction

---

## SQLite Schema (`validate/models.py`)

### `replications` table

Maps directly to `EXTRACTED_COLS`. One row per extraction result (one row per original for multi-original papers).

Key columns for voting logic:

- `doi_r` — primary key component (with `original_rank`)
- `original_rank` — primary key component (for multi-original grouping)
- `validation_status` — `pending` / `confirmed` / `rejected` / `needs_review`
- `flora_status` — FLoRA database entry status (imported from source CSV)

### `votes` table

| Column           | Type | Description                           |
| ---------------- | ---- | ------------------------------------- |
| `id`             | int  | Auto-increment PK                     |
| `replication_id` | int  | FK → replications.id                  |
| `reviewer_id`    | str  | Session cookie or name                |
| `vote`           | str  | `confirm` / `reject` / `needs_review` |
| `comment`        | str  | Free text                             |
| `created_at`     | str  | UTC time of vote                      |

**Validation logic:**

- `confirmed` = majority of votes are `confirm` (≥ 2 votes required)
- `rejected` = majority of votes are `reject` (≥ 2 votes required)
- `needs_review` = tied or flagged by any reviewer
- Single vote sets `validation_status = needs_review` until a second vote agrees

---

## Input Tab (`/input`)

Data generation and batch pipeline runner. Two modes:

**Mode 1 — Load CSV:** Upload an `extracted.csv` directly into the SQLite database.

**Mode 2 — Batch run:** Run Stage 3 extraction on a set of DOIs from `filtered.csv`. Uses server-sent events (SSE) to stream progress. Powered by `extract/pipeline_runner.py`.

---

## Export (`/export`)

Three formats available:

| Format        | Contents                                                               |
| ------------- | ---------------------------------------------------------------------- |
| CSV           | Full `validated.csv` — all `VALIDATED_COLS` plus `flora_status`        |
| Excel (.xlsx) | Same as CSV, formatted                                                 |
| Minimal CSV   | 5 columns: `doi_r`, `title_r`, `doi_o`, `title_o`, `validation_status` |

Only `confirmed` rows are exported by default. A toggle includes `needs_review` rows.

---

## Output Schema — `validated.csv`

All columns from `extracted.csv`, plus:

| Column               | Type | Description                                        |
| -------------------- | ---- | -------------------------------------------------- |
| `validation_status`  | str  | confirmed / rejected / pending / needs_review      |
| `vote_count`         | int  | Total votes received                               |
| `confirm_votes`      | int  | Confirm votes                                      |
| `reject_votes`       | int  | Reject votes                                       |
| `validator_notes`    | str  | Aggregated reviewer comments                       |
| `flora_status`       | str  | FLoRA database entry status (from source CSV)      |
| `validated_doi_o`    | str  | Reviewer-corrected original DOI (blank = accepted) |
| `validated_outcome`  | str  | Reviewer-corrected outcome (blank = accepted)      |

---

## Files

| File                                 | Status      | Description                                                  |
| ------------------------------------ | ----------- | ------------------------------------------------------------ |
| `validate/app.py`                    | Implemented | Flask entry point — registers blueprints, loads startup data |
| `validate/import_csv.py`             | Implemented | Load extracted.csv into SQLite (run once)                    |
| `validate/models.py`                 | Implemented | SQLAlchemy: Replication + Vote tables                        |
| `validate/state.py`                  | Ported      | In-memory DataFrames + threading locks                       |
| `validate/routes/review.py`          | Implemented | GET /validate, POST /vote, GET /api/validate/log             |
| `validate/routes/dashboard.py`       | Implemented | GET /dashboard, GET /api/dashboard/stats                     |
| `validate/routes/export.py`          | Implemented | GET /export, POST /api/export/download                       |
| `validate/routes/flora.py`           | Implemented | GET /flora, GET /api/flora master list                       |
| `validate/routes/input.py`           | Ported      | GET /input, POST /input/generate                             |
| `validate/routes/batch.py`           | Ported      | SSE batch runner                                             |
| `validate/routes/multi_originals.py` | Ported      | GET /multi-originals pipeline UI                             |
| `validate/routes/disambiguation.py`  | Ported      | GET /disambiguation single-DOI runner                        |

---

## What Needs to Be Implemented

- [ ] Add `validated_doi_o` and `validated_outcome` to `Vote` model and `POST /vote` handler
- [ ] `GET /api/validate/log` — lazy full log endpoint

---

## Rules

- The Validate tab must show minimal info by default — abstract, original study, outcome, and voting only
- Full Log content is loaded lazily; never include LLM prompts or PDF paths in the initial page render
- Reviewer identity is set by session cookie — no login required
- A second vote from the same reviewer on the same paper updates their existing vote
- `confirmed` requires ≥ 2 votes with a confirm majority
- All writes to the `vote` table must also update `validation_status` on the `replication` table
- Export includes only `confirmed` rows by default — `needs_review` is opt-in
- `validated.csv` encoding: `utf-8-sig` (BOM, Excel-compatible)
