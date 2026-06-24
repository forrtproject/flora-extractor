# CLI Reference

All commands are run from the project root with `python -m <module>`.

---

## Stage 1 — Search

```bash
# Run full search across all sources (appends new results to candidates.csv)
python -m search.run_search

# Limit to specific year range
python -m search.run_search --from-year 2020 --to-year 2024

# Auto-advance: process one (source, phrase/concept, year) job per call; repeat until exit 2
python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 200

# Harvest cached API pages into candidates.csv without making new API calls
# (run this first after any crash or cursor deletion to recover orphaned pages)
python -m search.run_search --harvest-only

# Rebuild candidates index (if CSV was modified outside the pipeline)
python -m search.run_search --rebuild-index

# Reset all cursors to start fetching from page 1 again
python -m search.run_search --reset-cursors
```

### Filtering by source

The `--source` flag restricts which discovery tracks run. It can be repeated.

| Source value | What it searches |
| --- | --- |
| `openalex` | 37 keyword phrases via `title_and_abstract.search` |
| `openalex_concept` | OpenAlex concept tags (`C12590798` Replication, `C9893847` Reproducibility) |
| `semantic_scholar` | Same 37 phrases via Semantic Scholar bulk search |
| `engine` | Internal engine source (requires `FLORA_USE_ENGINE=1`) |

```bash
# Phrase-based sources — need the auto-advance loop
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 --source openalex } until ($LASTEXITCODE -eq 2)
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 --source semantic_scholar } until ($LASTEXITCODE -eq 2)

# Concept-based source — single run or auto-advance loop (large result sets)
python -m search.run_search --source openalex_concept --from-year 2011 --to-year 2026
do { python -m search.run_search --auto-advance --source openalex_concept --from-year 2011 --to-year 2026 --max-per-phrase 10000 } until ($LASTEXITCODE -eq 2)

# Curated external lists (single fetch, no loop needed)
python -m search.run_search --source bob_reed
python -m search.run_search --source i4r
```

### Concept ID management

Concept IDs are defined in `CONCEPT_IDS` inside `search/openalex_search.py`. To look up IDs:

```bash
# Print OpenAlex concepts matching a query (live API call, then exit)
python -m search.run_search --list-concepts "replication"
python -m search.run_search --list-concepts "reproducibility"
```

Current verified IDs (as of 2026-06-23):

- `C12590798` — Replication (statistics) — ~263k works
- `C9893847` — Reproducibility — ~121k works

### Skipping the cache harvest in auto-advance

The harvest step scans all cached JSON pages and can be slow on large caches. Skip it per-call with `--no-harvest` and run it separately on a schedule:

```bash
# Run auto-advance without per-cycle harvest
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 --no-harvest } until ($LASTEXITCODE -eq 2)

# Run harvest separately (weekly, or after a crash)
python -m search.run_search --harvest-only
```

**Output:** `data/candidates.csv`

---

## Stage 2 — Filter

```bash
# Run filter on candidates.csv
python -m filter.run_filter

# Limit to specific year range
python -m filter.run_filter --from-year 2020

# Rebuild filtered index
python -m filter.run_filter --rebuild-index

# Filter using only rule-based classifier (no LLM calls)
python -m filter.run_filter --no-llm
```

**Input:** `data/candidates.csv`  
**Output:** `data/filtered.csv`

---

## Stage 3 — Extract

```bash
# Run extraction (streams to extracted.csv)
python -m extract.run_extract

# Write to test sandbox instead of production
python -m extract.run_extract --extracted-test

# Resume from last processed row
python -m extract.run_extract --resume

# Skip LLM calls (rule-based only)
python -m extract.run_extract --no-llm

# Combine flags
python -m extract.run_extract --extracted-test --resume --no-llm

# Limit to N rows
python -m extract.run_extract --limit 50
```

**Input:** `data/filtered.csv`  
**Output:** `data/extracted.csv` (or `data/extracted-test.csv` with `--extracted-test`)

### Promoting test results

```bash
# Promote all test rows to production
python -m extract.promote_test --all

# Promote a single DOI
python -m extract.promote_test --doi 10.1234/example

# Preview without writing
python -m extract.promote_test --all --dry-run

# Force overwrite (skip conflict check)
python -m extract.promote_test --all --force
```

### DOI verification audit

Retroactively verify `doi_o` values in an existing CSV. Runs automatically during extraction; use this to audit rows that predate the feature.

```bash
# Dry run: print summary + write data/doi_audit_report.csv
python -m extract.audit_dois

# Write corrections into extracted.csv
python -m extract.audit_dois --apply

# Audit a single DOI
python -m extract.audit_dois --doi 10.1234/example

# Audit extracted-test.csv instead
python -m extract.audit_dois --extracted-test
```

---

## Stage 4 — Monitoring web app

```bash
# Start the web app
python -m validate.app
# → http://localhost:5001
```

The app is read-only — it displays pipeline stats and pulls validation data from Supabase. No writes to local files.

---

## Analysis

```bash
# Overlap / recall gap analysis — compares all_replications.csv against candidates.csv
# Reports genuine gaps (papers in the reference set not found by Stage 1)
python -m analysis.run_overlap_analysis

# Rule analysis — audit filter rules and extraction link methods
python -m analysis.run_overlap_analysis  # also produces rule_improvement_opportunities.csv

# APA reference resolver
python -m analysis.apa_resolver
```

**Outputs:** CSV and Markdown files in `analysis/` (see [code-flow/analysis.md](code-flow/analysis.md) for what each file means)

Key output files:

- `analysis/gap_summary.md` — human-readable recall gap report
- `analysis/gap_analysis_doi_matched.csv` — gaps where the reference has a DOI
- `analysis/gap_analysis_url_matched.csv` — gaps where the reference has a URL but no DOI
- `analysis/rule_improvement_opportunities.csv` — ranked filter/extract improvement suggestions
- `analysis/extraction_audit.md` — link method and confidence breakdown

---

## Tools

```bash
# Recalibrate outcome values in extracted.csv
# Must be run as a module from the project root (not from inside tools/)
python -m tools.recalibrate_outcomes

# Only reprocess recently added rows (last N rows of the CSV, which are the newest appended entries)
python -m tools.recalibrate_outcomes --tail 50

# Only reprocess rows from a given publication year onward
python -m tools.recalibrate_outcomes --since-year 2022

# Force fresh LLM calls (clears cached outcomes for rows being reprocessed)
python -m tools.recalibrate_outcomes --tail 50 --clear-cache

# Preview without writing
python -m tools.recalibrate_outcomes --tail 50 --dry-run

# Process only first N uncertain rows (for testing a prompt change)
python -m tools.recalibrate_outcomes --limit 10 --dry-run

# Load a plain DOI list as pipeline input
python tools/load_doi_list.py path/to/dois.txt

# Clean up duplicate sources in candidates.csv
python tools/cleanup_sources.py
```

---

## Tests

```bash
# Run all unit tests
python -m pytest tests/

# Run specific test file
python -m pytest tests/test_extract.py -v

# Run with live API access (requires TEST_LIVE_API=1)
TEST_LIVE_API=1 python -m pytest tests/live/

# Run with coverage
python -m pytest tests/ --cov=. --cov-report=html
```

---

## Cache management

```bash
# Clear all caches
python -c "import shutil; shutil.rmtree('cache', ignore_errors=True)"

# Clear only parse cache (re-fetch PDFs and re-parse)
python -c "import shutil; shutil.rmtree('cache/parse', ignore_errors=True)"

# Clear only LLM result cache
python -c "import shutil; shutil.rmtree('cache/llm', ignore_errors=True)"
```

---

## Web app routes

| Route | Description |
| ----- | ----------- |
| `/` | Redirects to `/dashboard` |
| `/dashboard` | 6-tab monitoring dashboard — see [dashboard-guide.md](dashboard-guide.md) |
| `/check` | Search/filter/download across any stage — see [check-page.md](check-page.md) |
| `/search` | Stage 1 candidates table |
| `/filter` | Stage 2 filtered papers table |
| `/extract` | Stage 3 extraction results table |
| `/extract-test` | Stage 3 test sandbox table (with Promote button) |
| `/validate` | Stage 4 voting queue |
| `/api/dashboard/csv-stats` | Pipeline stats JSON (3-tier cascade: stats.json → Parquet → CSV) |
| `/api/dashboard/download` | Download a full stage CSV (`?stage=candidates\|filtered\|extracted\|extracted-test`) |
| `/api/check/search` | Filtered/paginated rows as JSON |
| `/api/check/download` | Filtered rows as CSV attachment |
| `/api/dashboard/supabase-stats` | Supabase validation KPIs |
| `/api/dashboard/supabase-outcomes` | Outcome distribution from validated table |
| `/api/dashboard/supabase-corrections` | Per-field correction frequency |
| `/api/dashboard/supabase-drilldown` | Paginated incorrect-DOI table |
