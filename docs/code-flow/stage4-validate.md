# Stage 4: Monitoring Web App — Code Flow

**Entry point:** `python -m validate.app` → `http://localhost:5001`

## Architecture

The web app is a **read-only monitoring dashboard**. It does not write to any pipeline CSVs or SQLite databases. Validation happens in a separate repo backed by Supabase.

```
validate/app.py
    │
    ├── create_app()
    │       register exactly three blueprints (dashboard, check, batch) —
    │           batch only when FLORA_READONLY is unset
    │       /            → redirect to the dashboard
    │       /pipeline    → 301 to the dashboard
    │       /set-name    → session-based reviewer name
    │       /pdf/<file>  → serve PDFs from PDF_CACHE_DIR (cache/pdfs/)
    │
    ├── Blueprint: dashboard_bp (routes/dashboard.py)
    │       GET /dashboard                          → dashboard.html
    │       GET /api/dashboard/csv-stats            → pipeline stats (column-only CSV reads)
    │       GET /api/dashboard/supabase-stats       → Supabase KPIs (cached 5 min)
    │       GET /api/dashboard/supabase-outcomes    → outcome distribution
    │       GET /api/dashboard/supabase-corrections → correction frequency
    │       GET /api/dashboard/supabase-analytics   → coverage + per-field agreement
    │       GET /api/dashboard/supabase-confusion   → pipeline-vs-final matrices (#72)
    │       GET /api/dashboard/supabase-drilldown   → paginated drilldown table
    │       (plus the set/analysis/download endpoints — `@dashboard_bp.route` is the list)
    │
    ├── Blueprint: check_bp (routes/check.py)
    │       GET /check                          → filter/inspect extracted rows, download subsets
    │
    └── Blueprint: batch_bp (routes/batch.py)   — skipped when FLORA_READONLY=1
            GET /batch                          → batch disambiguation for multiple-match papers
            plus /api/batch/* (stats, candidates, result, export, refresh, run, stop)
```

`validate/routes/` contains exactly three modules — `dashboard.py`, `check.py`,
`batch.py` — and all three are registered. There is no disambiguation blueprint and
no unregistered leftovers; `FLORA_READONLY=1` skips `batch` because it pulls in the
heavy PDF dependencies (pdfminer, pymupdf, playwright), which is what makes read-only
hosting possible.

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
    │           GET validation_queue where is_validated=true (check columns only)
    │           count rows with any "incorrect" check per field
    │
    ├── fetch('/api/dashboard/supabase-outcomes')
    │       supabase_client.get_validated_outcomes()
    │           GET validated table (outcome column)
    │           aggregate by outcome value
    │
    └── fetch('/api/dashboard/supabase-drilldown?page=1&...')
            supabase_client.get_drilldown_page(page, outcome_filter, check_filter)
                GET validation_queue (checks, corrections, notes) + unvalidated
                filter to rows with at least one "incorrect" field
                paginate in Python (_DRILLDOWN_PAGE_SIZE = 25)
```

## Theme toggle

Theme preference is stored in `localStorage` as `flora-theme = 'dark' | 'light'`. A `<script>` in `<head>` of `base.html` applies the theme before paint to avoid flash. The toggle button switches the `data-theme` attribute on `<html>`.

## Reviewer name

`/set-name` stores an optional reviewer name in Flask's signed session cookie
(`session["reviewer_id"]`). The current app does **not** enforce it with a
`before_request` guard — routes are reachable without setting a name (the earlier
mandatory-name guard was removed).
