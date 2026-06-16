# Dashboard Redesign — Design Spec
**Date:** 2026-06-16  
**Status:** Approved for implementation

---

## Overview

Replace the current multi-tab Flask web app with a focused two-page experience:

- **Dashboard** — 6 sub-tabs (Search, Filter, Extract, Extract-Test, Supabase, Old Pipeline), each split 40/60 left=static docs / right=live stats. Stat cards are clickable and download a filtered CSV subset.
- **Check** — A filter + search interface over any pipeline CSV, with an expandable results table and a download button.

All other top-nav tabs (Search, Filter, Extract, Extract-Test, Target Pending) are removed from the nav. Their blueprint files are kept on disk but unregistered from `app.py`.

---

## Phase 1 — UI + CSV-based reads

Everything in Phase 1 reads CSVs directly (or reuses existing `/api/dashboard/csv-stats`). Parquet is Phase 2.

### 1. Navigation

`validate/templates/base.html` top nav changes to:

```
FLoRA  |  Dashboard  |  Check  |  [theme toggle]
```

Removed from nav: Search, Filter, Extract, Extract-Test, Target Pending.  
Removed from `app.py` blueprint registration: `search_view_bp`, `filter_view_bp`, `extract_view_bp`, `extract_test_view_bp`, `target_pending_bp`, `input_bp`, `pipeline_bp`.  
Their route files and templates are retained but not mounted.

### 2. Dashboard page — sub-tab structure

`GET /dashboard` renders `dashboard.html`.  
Six sub-tabs rendered client-side (no page reload):

| Tab label   | Data source                    |
|-------------|--------------------------------|
| Search      | `/api/dashboard/search-stats`  |
| Filter      | `/api/dashboard/csv-stats`     |
| Extract     | `/api/dashboard/csv-stats`     |
| Extract-Test| `/api/dashboard/csv-stats`     |
| Supabase    | `/api/dashboard/supabase-stats` + corrections + outcomes |
| Old Pipeline| `/api/dashboard/analysis-stats`|

Active tab is preserved in the URL hash (`#search`, `#filter`, etc.) so page refresh restores position.

### 3. Per-tab layout — 40/60 split

Every tab renders a two-column layout:

```
┌─────────────────────┬──────────────────────────────┐
│   DOCS (40%)        │   STATS (60%)                │
│   Static HTML       │   Fetched from API, rendered  │
│   • What it does    │   as KPI cards + breakdown    │
│   • Code flow       │   tables. Cards are anchor    │
│   • CLI commands    │   links → download endpoint.  │
│   • CSV columns     │                               │
└─────────────────────┴──────────────────────────────┘
```

Docs content is hardcoded HTML in the template — it changes when code changes, not at runtime.

### 4. Docs content per tab

#### Search
- **What it does:** Discovers candidate replication papers by querying OpenAlex and Semantic Scholar for 23 exact phrases matched against title + abstract.
- **Keywords (23):** "replication of", "direct replication", "close replication", "conceptual replication", "replication study", "reproduction study", "we replicated", "attempts to replicate", "registered replication report", "pre-registered replication", "failed to replicate", "did not replicate", "we replicate", "replicating the findings", "could not reproduce", "successfully replicated", "reproducibility of", "replication and extension", "replicability of", "attempt to replicate", "failure to replicate", "non-replication", "reproducibility study", "reproduce the findings"
- **Code flow:**
  1. Cache harvest — scan `cache/openalex/` and `cache/s2/` for previously downloaded pages; merge rows into `candidates.csv` without re-fetching
  2. Live fetch — each phrase × year is an independent resumable job with cursor/offset persistence; crashes are safe
  3. Batch deduplication (5 passes):
     - Pass 0a: Drop figshare DOIs (`10.6084/`) and PeerJ peer-review DOIs (`/reviews/`)
     - Pass 0b: Versioned preprints — collapse `_v1`/`_v2` to highest version or canonical DOI
     - Pass 1: Exact DOI dedup — keep richest row (most populated fields) per DOI
     - Pass 2: Fuzzy title match (`RapidFuzz token_sort_ratio ≥ 90`) on DOI-less rows only
     - Pass 3: FLoRA cross-check — remove DOIs already in `data/flora_entry_sheet.csv` or `data/flora.csv`
  4. Merge — check each row against `cache/candidates_index.txt`; append only new rows; update index incrementally
- **Why the index:** `candidates.csv` grows to 1M+ rows. The index (flat text file, ~1s to load) answers "already seen?" via any identifier (`oa:<id>`, DOI, `url:<url>`, `title:<lowercased>`) without loading the CSV.
- **CLI commands table:**

| Command | Description |
|---------|-------------|
| `--from-year YYYY` | Earliest publication year to include |
| `--to-year YYYY` | Latest publication year to include |
| `--max-per-phrase N` | Cap rows fetched per phrase per run; checkpoint saved, next run continues |
| `--auto-advance` | Process ONE phrase/year job per call; state in `cache/search_state.json` |
| `--source SOURCE` | Restrict to `openalex`, `semantic_scholar`, or `engine` (repeatable) |
| `--reset-cursors` | Delete all OpenAlex cursor + S2 offset files; restart from scratch |
| `--rebuild-index` | Rebuild `candidates_index.txt` from `candidates.csv` then exit |
| `--harvest-only` | Scan all cached pages into `candidates.csv` then exit |
| `--no-harvest` | Skip per-cycle cache harvest in `--auto-advance` mode |

- **`candidates.csv` columns:**

| Column | What it represents | Example |
|--------|--------------------|---------|
| `doi_r` | DOI of the replication paper | `10.1177/0956797615` |
| `title_r` | Title of the replication paper | `Many Labs Replication of...` |
| `abstract_r` | Abstract text | `We attempted to replicate...` |
| `year_r` | Publication year | `2015` |
| `authors_r` | Semicolon-separated author names | `Klein, R.A.; Ratliff, K.A.` |
| `journal_r` | Journal/venue name | `Psychological Science` |
| `url_r` | Open-access URL | `https://osf.io/...` |
| `openalex_id_r` | OpenAlex work ID | `https://openalex.org/W2...` |
| `source` | Which source found this row | `openalex` |
| `ref_r` | Short reference string | `Klein · 2015 · Psych Sci` |

---

#### Filter
- **What it does:** Reads `candidates.csv` in 50k-row chunks, classifies each row as `replication | reproduction | false_positive | needs_review` using a two-pass rule+LLM approach. Results stream to `filtered.csv` one row at a time. Resumes via `cache/filtered_index.txt`.
- **Code flow:**
  1. Load/build `filtered_index.txt`; skip already-processed rows
  2. Per row: run rule classifier on `title_r + abstract_r`
  3. If `needs_review`: call LLM (OpenAI primary → Gemini fallback); set `filter_method = both`
- **Rule logic:**
  1. Exclusion patterns → `false_positive` (high confidence): non-scholarly contexts — DNA replication, replication fork/origin/stress/timing, code/data replication, virus/cell/organism replication
  2. No replication phrase in text → `false_positive` (high)
  3. Phrase found + author–year citation `(Author, YYYY)` → `replication` or `reproduction` (high)
  4. Phrase found, no author–year citation → `needs_review` (medium)
  - **Reproduction vs replication:** if only reproduction-flavoured phrases fire ("re-analysis", "computational reproducibility", "same original dataset") → `reproduction`
- **LLM prompt (needs_review rows only):**  
  System: *"You are an expert in scientific replication and reproducibility."*  
  Defines three classes: **replication** (new data, tests original finding), **reproduction** (same original data, computational check), **false_positive** (meta-analyses, methodology papers, biological replication, casual mentions). Returns JSON: `{filter_status, filter_confidence, filter_evidence}`.  
  Primary: `FILTER_OPENAI_MODEL` (default `gpt-5.4-mini`). Fallback: Gemini. Cached by `hash(title + abstract)`.
- **CLI commands table:**

| Command | Description |
|---------|-------------|
| `--limit N` | Stop after N new rows |
| `--offset N` | Skip first N unprocessed rows |
| `--from-year YYYY` | Only rows with year_r ≥ YYYY |
| `--to-year YYYY` | Only rows with year_r ≤ YYYY |
| `--source SOURCE` | Only rows from this source |
| `--rebuild-index` | Rebuild `filtered_index.txt` from `filtered.csv` then exit |

- **`filtered.csv` columns** (all `candidates.csv` columns plus):

| Column | What it represents | Example |
|--------|--------------------|---------|
| `filter_status` | Classification result | `replication` |
| `filter_method` | How classified | `rule_based`, `llm`, `both` |
| `filter_evidence` | Triggering phrase or LLM quote | `phrase:"failed to replicate"; cite:Smith (2009)` |
| `filter_confidence` | Certainty level | `high`, `medium`, `low` |

---

#### Extract
- **What it does:** For each replication/reproduction in `filtered.csv`: identifies which original study it targets, extracts the replication outcome, verifies the original's DOI, streams results to `extracted.csv`.
- **Code flow:**
  1. Skip `false_positive` rows (written through unchanged)
  2. Classify `original_match_type` (single vs. multiple originals)
  3. **Single original** — `link_original.py`:
     - Step A (rule-based): parse abstract for `(Author, YYYY)` citations → score OpenAlex candidates by author match (+2) + year match (+2) + journal Jaccard (+3/+1.5) + title Jaccard (+≤1); resolve if best ≥ 4.0 AND gap ≥ 2.0
     - Step B (title patterns): extract target title from replication paper's own title via regexes ("A Replication of X", "Replicating X", "Revisiting X", etc.) → CrossRef/OpenAlex lookup
     - Step C (LLM fallback): provides title, abstract, cited pattern, and OpenAlex candidate list to LLM; system: *"expert that identifies original studies from replication papers"*; model chain: OpenRouter/Qwen → Gemini → OpenAI
  4. **Outcome extraction** — `code_outcome.py`:
     - Pass 1 keyword scan (order matters: failure → mixed → success → descriptive):
       - **Failure:** "failed to replicate", "did not replicate", "no support for the original", "null result", "no significant effect", etc.
       - **Mixed:** "partially replicated", "mixed results", "some but not all", "smaller effect", "reduced magnitude", etc.
       - **Success:** "successfully replicated", "confirmed the findings", "consistent with the original", bare "replicated"
       - **Descriptive:** "adapted the method", "in a different context", "not intended to test"
     - Scanned on: title (high-confidence only) → abstract (any hit) → fulltext[:3000] (high-confidence only)
     - Pass 2 LLM (when keyword scan returns nothing): abstract + original study context → `success|failure|mixed|descriptive|cannot_be_determined`
  5. **DOI verification** — `shared/doi_verify.py`: fetch CrossRef/OpenAlex metadata for `doi_o`; compare title Jaccard (threshold 0.5); if mismatch, attempt 3-tier re-resolution; set `doi_o_verification` status
  6. Append to `extracted.csv` (or `extracted-test.csv` with `--extracted-test`)
- **CLI commands table:**

| Command | Description |
|---------|-------------|
| `--resume` | Carry forward resolved rows; re-run only `target_pending` rows |
| `--extracted-test` | Write to `extracted-test.csv` instead of `extracted.csv` |
| `--doi-r DOIS` | Process only specific DOI(s) |
| `--from-year YYYY` | Only rows with year_r ≥ YYYY |
| `--to-year YYYY` | Only rows with year_r ≤ YYYY |
| `--limit N` | Process only first N non-false-positive rows |
| `--no-llm` | Skip all LLM calls; rule-based only |
| `--no-pdf` | Skip PDF download; use abstract-only |
| `--no-multiple-originals` | Write multiple_original rows as target_pending |
| `--no-reproductions` | Skip reproduction rows |
| `--skip-flora-validated` | Skip DOIs already validated in FLoRA entry sheet |
| `--resolved-only` | Only write fully resolved rows |
| `--predicted-outcome` | Pre-filter by keyword-predicted outcome |
| `--source SOURCE` | Only rows from this source |
| `--match-type-only` | Classify match type only → `match_type_only.csv` |
| `--outcome-only` | Classify outcome only → `outcome_only.csv` |

- **`extracted.csv` columns** (all `filtered.csv` columns plus): `original_match_type`, `original_match_confidence`, `doi_o`, `title_o`, `year_o`, `authors_o`, `link_method`, `link_evidence`, `link_confidence`, `doi_o_verification`, `outcome`, `outcome_phrase`, `outcome_confidence`, `out_quote_source`, `type`, `original_rank`, `n_originals`

---

#### Extract-Test
- **What it does:** Test sandbox for new extraction runs. Identical to Extract but writes to `extracted-test.csv`. Rows can be promoted to `extracted.csv` via `promote_test` CLI or the Promote button in the web app.
- **Key CLI commands:** `--extracted-test` flag on `run_extract`; `python -m extract.promote_test --all | --doi | --dry-run | --force`
- **Docs content:** Same structure as Extract; highlight sandbox vs. production distinction; explain promote workflow.

---

#### Supabase
- **What it does:** Shows validation KPIs from the Supabase-backed validation database (confirmed/rejected/pending, correction rates, outcome distribution).
- **Docs content:** Brief explanation of what Supabase validation is, how voting works, what the `validated.csv` columns mean (`validation_status`, `vote_count`, `validated_doi_o`, `validated_outcome`).

---

#### Old Pipeline
- **What it does:** Legacy analysis output — gap analysis, extraction audit, rule improvement opportunities.
- **Docs content:** Brief note that this is legacy analysis; points to `analysis/` folder.

---

### 5. Stats content per tab

#### Search stats
New endpoint `GET /api/dashboard/search-stats`:
- Total candidates
- No DOI count
- No DOI or URL count
- No abstract count
- By source breakdown (OpenAlex, Semantic Scholar, etc.) — each row is a clickable download link
- *(Keyword breakdown deferred — would require scanning candidates.csv by `source` which is already grouped; future enhancement)*

#### Filter stats
Extend existing `csv-stats` response:
- Total filtered, breakdown by `filter_status` (replication, reproduction, false_positive, needs_review) — each clickable
- Among replications + reproductions: No DOI count, No DOI or URL count, No abstract count
- Breakdown by `filter_method` (rule_based, llm, both)
- Breakdown by `filter_confidence` (high, medium, low)

#### Extract stats
Reuse existing `csv-stats` extracted stats (total, outcomes, link methods, match types, model family, DOI verification statuses) — all cards clickable.

#### Extract-Test stats
Same cards as Extract, sourced from `extracted-test.csv` (existing `test_` prefix stats in `csv-stats`).

#### Supabase stats
Reuse existing `/api/dashboard/supabase-stats`, `supabase-outcomes`, `supabase-corrections`, `supabase-drilldown`.

#### Old Pipeline stats
Reuse existing `/api/dashboard/analysis-stats`.

---

### 6. Clickable stat cards → CSV download

New endpoint:

```
GET /api/dashboard/download
  ?stage=candidates|filtered|extracted|extracted-test
  &col=<column_name>
  &val=<column_value>
```

Reads the relevant CSV, filters to `col == val`, streams as a file attachment.  
Filename: `{stage}_{col}_{val}_{YYYY-MM-DD}.csv`  
Saved to: `data/dashboard/download/` before streaming.

Stats cards that represent a filterable subset (e.g. "Replications: 17,000") are rendered as `<a href="/api/dashboard/download?stage=filtered&col=filter_status&val=replication">` anchor tags.  
Cards representing totals (e.g. "Total candidates: 1,712,043") use `val=*` which skips the column filter and downloads the entire stage CSV.

---

### 7. Check tab

**Route:** `GET /check` → `check.html`

**Layout:**
```
┌─────────────────────────────────────────────────────────┐
│  Stage: [candidates▼]  Year: [2011]–[2026]              │
│  Type: [All▼]  Outcome: [All▼]  Link method: [All▼]     │
│  DOI verified: [All▼]  Original match: [All▼]           │
│  ─────────────────────────────────────────────────────  │
│  [Search by DOI or title...                    ] Search │
└─────────────────────────────────────────────────────────┘
142 results  [⬇ Download 142 rows]

┌──────────────────────┬────────────┬──────────────┬───────┐
│ Title / DOI          │ Outcome    │ Link method  │ DOI ✓ │
├──────────────────────┼────────────┼──────────────┼───────┤
│ Many labs replic...  │ ● success  │ llm_fulltext │ ✓     │
│  ▼ doi_o: 10.1037/.. · title_o: Effects of... · year_o: 2002 ...  │
├──────────────────────┼────────────┼──────────────┼───────┤
│ Replication of ego.. │ ● failure  │ author_year  │ ⚠     │
└──────────────────────┴────────────┴──────────────┴───────┘
← 1 2 3 … 6 →
```

**Filter fields by stage:**

| Stage | Available filters |
|-------|------------------|
| candidates | year, source |
| filtered | year, source, filter_status, filter_method, filter_confidence |
| extracted / extracted-test | year, type, outcome, link_method, original_match_type, doi_o_verification, source |

**Endpoint:** `GET /api/check/search`

Query params: `stage`, `year_from`, `year_to`, `type`, `outcome`, `link_method`, `match_type`, `doi_verified`, `source`, `q` (DOI or title substring), `page`, `per_page` (default 25).

Returns: `{total, pages, page, rows: [...]}` — rows include all columns of the relevant CSV.

Row expansion: clicking a row reveals all fields inline (no modal). Collapse on second click.

Download: "Download N rows" button at top hits `GET /api/check/download` with the same filter params → streams filtered CSV as attachment to `data/dashboard/download/`.

---

## Phase 2 — Parquet layer (separate PR)

### Data directory structure

```
data/
  dashboard/
    candidates.parquet
    filtered.parquet
    extracted.parquet
    extracted-test.parquet
    stats.json
    download/           ← generated download files (gitignored)
```

### stats.json structure

```json
{
  "updated_at": "2026-06-16T14:32:00",
  "candidates": {
    "total": 1712043,
    "no_doi": 312041,
    "no_doi_or_url": 98203,
    "no_abstract": 421000,
    "by_source": {"openalex": 1204312, "semantic_scholar": 507731}
  },
  "filtered": { ... },
  "extracted": { ... },
  "extracted_test": { ... }
}
```

### Parquet update hooks

Each pipeline runner (`run_search.py`, `run_filter.py`, `run_extract.py`) writes to Parquet and updates `stats.json` at two points:
- On normal completion (after the main loop finishes)
- On `KeyboardInterrupt` (Ctrl-C handler) — partial progress is saved

A shared helper `shared/dashboard_cache.py` provides:
- `write_parquet(stage: str)` — reads the stage CSV and writes to `data/dashboard/{stage}.parquet`
- `update_stats(stage: str)` — recomputes counts for that stage and updates `stats.json`

### UI impact

Phase 2 is a drop-in swap:
- `/api/dashboard/search-stats` and `/api/dashboard/csv-stats` switch from `pd.read_csv` to `pd.read_parquet`
- `/api/dashboard/download` and `/api/check/download` switch to filtering Parquet instead of CSV
- `stats.json` values are used to populate KPI cards instantly (before the fetch returns) to eliminate flicker

---

## Files affected — Phase 1

| File | Change |
|------|--------|
| `validate/templates/base.html` | Reduce nav to Dashboard + Check |
| `validate/templates/dashboard.html` | Full rewrite — sub-tabs, 40/60 layout, docs panels, stats panels |
| `validate/templates/check.html` | New file — filter bar, results table |
| `validate/routes/dashboard.py` | Add `/api/dashboard/search-stats`, `/api/dashboard/download` |
| `validate/routes/check.py` | New file — `/check`, `/api/check/search`, `/api/check/download` |
| `validate/app.py` | Register `check_bp`; unregister old blueprints |

## Files affected — Phase 2

| File | Change |
|------|--------|
| `shared/dashboard_cache.py` | New — `write_parquet()`, `update_stats()` |
| `search/run_search.py` | Add completion + Ctrl-C hook |
| `filter/run_filter.py` | Add completion + Ctrl-C hook |
| `extract/run_extract.py` | Add completion + Ctrl-C hook |
| `validate/routes/dashboard.py` | Switch reads to Parquet + stats.json |
| `validate/routes/check.py` | Switch reads to Parquet |

---

## Out of scope

- Authentication / access control
- Real-time push updates (polling on tab focus is sufficient)
- Keyword-by-keyword breakdown in Search stats (needs scanning full CSV per phrase)
- Multi-column filter combinations in the download endpoint (phase 2 extension)
