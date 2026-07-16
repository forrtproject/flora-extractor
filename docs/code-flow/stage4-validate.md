# Stage 4: Monitoring Web App — Code Flow

**Entry point:** `python -m validate.app` → `http://localhost:5001`

## Architecture

The web app is a **read-only monitoring dashboard**. It does not write to any pipeline CSVs or SQLite databases. Validation happens in a separate repo backed by Supabase.

```
validate/app.py
    │
    ├── create_app()
    │       register the 5 blueprints below (dashboard, check, batch,
    │           multi_originals, disambiguation) — nothing else
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
    ├── Blueprint: check_bp (routes/check.py)
    │       GET /check                          → filter/inspect extracted rows, download subsets
    │
    ├── Blueprint: batch_bp (routes/batch.py)
    │       GET /batch                          → batch disambiguation for multiple-match papers
    │
    ├── Blueprint: multi_orig_bp (routes/multi_originals.py)
    │       GET /multi-originals                → multi-original paper review
    │
    └── Blueprint: disambiguation_bp (routes/disambiguation.py)
            manual disambiguation UI
```

> **Orphaned / legacy blueprints.** `routes/extract_view.py`, `routes/search_view.py`,
> `routes/filter_view.py`, `routes/pipeline.py`, `routes/target_pending.py`, and
> `routes/input.py` still exist under `validate/routes/` but are **NOT registered** in
> `app.py` — they are leftovers from an earlier tabbed design (`/extract`, `/search`,
> `/filter`, etc. are not served by the running app). Do not assume their routes are live.

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

## Reviewer name

`/set-name` stores an optional reviewer name in Flask's signed session cookie
(`session["reviewer_id"]`). The current app does **not** enforce it with a
`before_request` guard — routes are reachable without setting a name (the earlier
mandatory-name guard was removed).
