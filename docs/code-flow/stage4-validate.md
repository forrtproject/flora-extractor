# Stage 4: Monitoring Web App — Code Flow

**Entry point:** `python -m validate.app` → `http://localhost:5001`

## Architecture

The web app is a **read-only monitoring dashboard**. It does not write to any pipeline CSVs or SQLite databases. Validation happens in a separate repo backed by Supabase.

```
validate/app.py
    │
    ├── create_app()
    │       register all blueprints
    │       before_request: session guard for reviewer name
    │       /set-name: session-based reviewer name
    │       /pdf: serve PDFs from cache/
    │
    ├── Blueprint: dashboard_bp (routes/dashboard.py)
    │       GET /dashboard                      → dashboard.html
    │       GET /api/dashboard/csv-stats        → pipeline stats (column-only CSV reads)
    │       GET /api/dashboard/supabase-stats   → Supabase KPIs (cached 5 min)
    │       GET /api/dashboard/supabase-outcomes → outcome distribution
    │       GET /api/dashboard/supabase-corrections → correction frequency
    │       GET /api/dashboard/supabase-drilldown   → paginated drilldown table
    │
    ├── Blueprint: extract_view_bp (routes/extract_view.py)
    │       GET /extract                        → Extract tab
    │       GET /api/extract/list               → paginated extracted.csv rows
    │       GET /api/extract/detail             → single row + PDF availability
    │       GET /api/extract/promote            → promote test row to production
    │
    ├── Blueprint: extract_test_view_bp
    │       GET /extract-test                   → Extract Test tab
    │       (same API routes under /extract-test)
    │
    ├── Blueprint: search_view_bp (routes/search_view.py)
    │       GET /search                         → Search tab (candidates.csv)
    │
    ├── Blueprint: filter_view_bp (routes/filter_view.py)
    │       GET /filter                         → Filter tab (filtered.csv)
    │
    ├── Blueprint: pipeline_bp (routes/pipeline.py)
    │       GET /api/pipeline/list              → paginated pipeline list
    │       GET /api/pipeline/detail            → single row detail
    │
    ├── Blueprint: batch_bp (routes/batch.py)
    │       Batch disambiguation for multiple-match papers
    │
    ├── Blueprint: multi_orig_bp (routes/multi_originals.py)
    │       Multi-original paper review
    │
    ├── Blueprint: disambiguation_bp (routes/disambiguation.py)
    │       Manual disambiguation UI
    │
    ├── Blueprint: target_pending_bp (routes/target_pending.py)
    │       Papers needing manual original DOI
    │
    └── Blueprint: input_bp (routes/input.py)
            Generate and download pipeline input CSVs
```

## Supabase integration flow

```
dashboard.html (JavaScript)
    │
    ├── fetch('/api/dashboard/supabase-stats')
    │       dashboard.py → supabase_client.get_validation_stats()
    │           _cached("validation_stats", _fetch)
    │               if cache hit (< 5 min): return cached
    │               else: GET unvalidated + validation_queue tables
    │                     compute KPIs: total, validated, in_progress, etc.
    │
    ├── fetch('/api/dashboard/supabase-corrections')
    │       supabase_client.get_correction_frequency()
    │           GET validated table (check columns only)
    │           count rows with any "incorrect" check per field
    │
    ├── fetch('/api/dashboard/supabase-outcomes')
    │       supabase_client.get_validated_outcomes()
    │           GET validated table (outcome column)
    │           aggregate by outcome value
    │
    └── fetch('/api/dashboard/supabase-drilldown?page=1&...')
            supabase_client.get_drilldown_page(page, outcome_filter, check_filter)
                GET validated table (all check + notes columns)
                filter to rows with at least one "incorrect" field
                paginate in Python (_DRILLDOWN_PAGE_SIZE = 25)
```

## Theme toggle

Theme preference is stored in `localStorage` as `flora-theme = 'dark' | 'light'`. A `<script>` in `<head>` of `base.html` applies the theme before paint to avoid flash. The toggle button switches the `data-theme` attribute on `<html>`.

## Session guard

All non-API, non-static routes require `session.reviewer_id`. Unauthenticated requests redirect to `/set-name?next=<url>`. The reviewer name is stored in Flask's signed session cookie.
