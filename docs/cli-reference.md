# CLI Reference

All commands are run from the project root with `python -m <module>`.

---

## Stage 1 — Search

```bash
# Run full search (appends new results to candidates.csv)
python -m search.run_search

# Limit to specific year range
python -m search.run_search --from-year 2020 --to-year 2024

# Rebuild candidates index (if CSV was modified outside the pipeline)
python -m search.run_search --rebuild-index

# Dry run — show what would be fetched without writing
python -m search.run_search --dry-run
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
# Gap analysis (compare extracted.csv vs FLoRA entry sheet)
python -m analysis.gap_analysis

# Filter rule analysis
python -m analysis.rule_analysis

# APA reference resolver
python -m analysis.apa_resolver
```

**Outputs:** CSV files in `analysis/` (gitignored by default)

---

## Tools

```bash
# Recalibrate outcome values in extracted.csv
python tools/recalibrate_outcomes.py

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
|-------|-------------|
| `/` | Redirects to `/dashboard` |
| `/dashboard` | Pipeline + Validation monitoring dashboard |
| `/search` | Stage 1 candidates view |
| `/filter` | Stage 2 filtered papers view |
| `/extract` | Stage 3 extraction results |
| `/extract-test` | Stage 3 test sandbox |
| `/target-pending` | Papers needing manual original DOI |
| `/api/dashboard/csv-stats` | Pipeline stats JSON (column-only CSV reads) |
| `/api/dashboard/supabase-stats` | Supabase validation KPIs |
| `/api/dashboard/supabase-outcomes` | Outcome distribution from validated table |
| `/api/dashboard/supabase-corrections` | Per-field correction frequency |
| `/api/dashboard/supabase-drilldown` | Paginated incorrect-DOI table |
| `/set-name` | Set reviewer name (stored in session) |
