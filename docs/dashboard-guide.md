# Dashboard Guide

The monitoring dashboard lives at `http://localhost:5001/dashboard` (start with `python -m validate.app`).

---

## Tabs

| Tab | Stage | Data source |
| --- | ----- | ----------- |
| Search | Stage 1 | `data/candidates.csv` |
| Filter | Stage 2 | `data/filtered.csv` |
| Extract | Stage 3 | `data/extracted.csv` |
| Extract-Test | Stage 3 sandbox | `data/extracted-test.csv` |
| Supabase | Stage 4 | Live Supabase API |
| Old Pipeline | Analysis | `analysis/` output files |

Stats are served via a 3-tier cascade (fastest to slowest):

1. `data/dashboard/stats.json` — pre-computed at end of each pipeline run
2. `data/dashboard/{stage}.parquet` — Parquet mirror written alongside stats.json
3. Full CSV scan — fallback when neither exists

See [parquet-cache.md](parquet-cache.md) for how the cache is generated and refreshed.

---

## Search Tab (Stage 1)

### Search — stats

| Card | What it shows | Clickable? |
| ---- | ------------- | ---------- |
| Total Candidates ↓ | Total rows in `candidates.csv` | Yes — downloads full file |
| No DOI ↓ | Rows where `doi_r` is blank | Yes — downloads subset |
| No DOI or URL ↓ | Rows where both `doi_r` and `url_r` are blank | Yes — downloads subset |
| No Abstract ↓ | Rows where `abstract_r` is blank | Yes — downloads subset |

All download cards call `/api/check/download?stage=candidates&…` and write the file to `data/dashboard/download/`.

**Source Breakdown** — count per discovery source (`openalex`, `semantic_scholar`, `engine`, …). Each row is a download link for that source's rows.

### Search — docs panel

Left panel covers: what Stage 1 does, all 3 sources, all 9 CLI flags, 5-pass deduplication logic, inclusion keywords (23 phrases), exclusion keywords applied by Stage 2, and all `candidates.csv` columns with examples.

---

## Filter Tab (Stage 2)

### Filter — stats

**Status KPI cards** (each is a download link):

| Card | `filter_status` value |
| ---- | --------------------- |
| Total Filtered ↓ | all rows |
| Replications ↓ | `replication` |
| Reproductions ↓ | `reproduction` |
| Needs Review ↓ | `needs_review` |
| False Positives ↓ | `false_positive` |

**Data Quality — Replications & Reproductions only** (each row downloads that subset):

| Row | Filter applied |
| --- | -------------- |
| No DOI ↓ | `stage=filtered&type=replication&type=reproduction&no_doi=1` |
| No DOI or URL ↓ | `stage=filtered&type=replication&type=reproduction&no_doi_url=1` |
| No abstract ↓ | `stage=filtered&type=replication&type=reproduction&no_abstract=1` |

### Filter — docs panel

Covers: how Stage 2 works, full decision logic table (rule → LLM flow), the exact LLM prompt text, all 6 CLI flags, and all 14 `filtered.csv` columns (10 inherited + 4 added) with section dividers.

---

## Extract Tab (Stage 3)

### Extract — stats

- **Extracted ↓** / **Target Pending** KPI cards at the top
- **Match Types** — each row downloads that match-type subset of `extracted.csv`
- **LLM Model** — Gemini / GPT / Qwen / Other / Rule-based breakdown (display only)
- **Link Method** — each row downloads that link-method subset
- **Outcome Distribution** — donut chart; each legend entry downloads that outcome subset

### Extract — docs panel

Covers: what Stage 3 does, CLI flags, link pipeline (author\_year → llm\_abstract → llm\_fulltext → target\_pending), 6 PDF parse methods and scoring formula, and all ~25 `extracted.csv` columns grouped into labeled sections.

---

## Extract-Test Tab (Stage 3 sandbox)

Same layout as Extract tab but reads `extracted-test.csv`. Rows here have not been promoted to production.

Promote via the **Promote** button in the web table or via CLI:

```bash
python -m extract.promote_test --all           # promote everything
python -m extract.promote_test --doi 10.xxx/y  # promote one row
python -m extract.promote_test --all --dry-run # preview
```

---

## Supabase Tab (Stage 4)

Requires `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in `.env`. Shows a configuration notice if not set up.

| KPI | Description |
| --- | ----------- |
| Total Records | All records in the validation table |
| Validated | `validation_status = validated` |
| Unvalidated | Not yet reviewed |
| Need Review | Flagged for follow-up |
| Judgements | Total validator assignments completed |
| Validators | Unique active reviewers |
| Agreement | % of queue assignments completed |

Also shows: validation progress bar, Correction Frequency bar chart (type / original DOI / outcome), Validated Outcomes donut, and a paginated Drilldown table filterable by outcome and field.

---

## Old Pipeline Tab (Analysis)

Shows gap analysis comparing `extracted.csv` against the FLoRA entry sheet. Run `python -m analysis.run_overlap_analysis` to generate the input data first.

---

## Refreshing

Click **↺ Refresh** to reload pipeline stats without a page reload. Supabase data is cached in-process for 5 minutes.

---

## Downloadable rows

Most stat cards and stat rows in the dashboard are clickable download links. Clicking downloads a filtered CSV to `data/dashboard/download/` and serves it as a file attachment. The filename encodes the stage and filters, e.g. `check_filtered_2026-06-16.csv`.

Downloads go through `/api/check/download`, which reads from Parquet (fast) or falls back to chunked CSV. See [check-page.md](check-page.md) for all supported filter parameters.
