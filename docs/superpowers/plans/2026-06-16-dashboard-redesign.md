# Dashboard Redesign — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the multi-tab Flask nav with a two-page app: a Dashboard with 6 sub-tabs (40/60 docs/stats split) and a Check page with filter+search over any pipeline CSV.

**Architecture:** Phase 1 reads CSVs directly (no Parquet). New routes live in `validate/routes/check.py`. The dashboard sub-tabs are rendered client-side with hash-based persistence. Old blueprints are unregistered from `app.py` but their files are kept.

**Tech Stack:** Flask, pandas (chunked CSV reads), vanilla JS (fetch + DOM), existing CSS custom properties.

**Spec:** `docs/superpowers/specs/2026-06-16-dashboard-redesign-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `validate/templates/base.html` | Reduce nav to Dashboard + Check |
| Modify | `validate/app.py` | Unregister old blueprints; register `check_bp` |
| Modify | `validate/routes/dashboard.py` | Add `search-stats` + `download` endpoints; extend csv-stats |
| Rewrite | `validate/templates/dashboard.html` | 6 sub-tabs with 40/60 split, docs panels, stats panels |
| Create | `validate/routes/check.py` | `/check`, `/api/check/search`, `/api/check/download` |
| Create | `validate/templates/check.html` | Filter bar, results table, row expansion, pagination |
| Modify | `tests/test_validate.py` | Tests for new routes and endpoints |

---

## Task 1: Nav simplification + blueprint cleanup

**Files:**
- Modify: `validate/templates/base.html`
- Modify: `validate/app.py`
- Modify: `tests/test_validate.py`

- [ ] **Step 1.1: Write failing tests for nav changes**

Add to `tests/test_validate.py`:

```python
def test_check_route_accessible(client):
    """New /check page must exist."""
    rv = client.get("/check")
    assert rv.status_code == 200

def test_dashboard_still_works(client):
    rv = client.get("/dashboard")
    assert rv.status_code == 200
```

- [ ] **Step 1.2: Run to verify they fail**

```
pytest tests/test_validate.py::test_check_route_accessible -v
```
Expected: FAIL — `/check` returns 404 (route not yet registered).

- [ ] **Step 1.3: Update nav in `base.html`**

Replace the `<nav>` content (lines 19–44) with:

```html
<nav>
  <span class="brand">FLoRA</span>
  <a href="{{ url_for('dashboard.dashboard_page') }}"
     class="{{ 'active' if active_page == 'dashboard' else '' }}">Dashboard</a>
  <a href="{{ url_for('check.check_page') }}"
     class="{{ 'active' if active_page == 'check' else '' }}">Check</a>

  <button class="theme-toggle" id="themeToggle" title="Toggle light / dark mode" aria-label="Toggle theme">
    <span class="icon" id="themeIcon">🌙</span>
    <span id="themeLabel">Dark</span>
  </button>

  {% if session.reviewer_id %}
  <span class="nav-reviewer">
    {{ session.reviewer_id }}
    <a href="/set-name">change</a>
  </span>
  {% endif %}
</nav>
```

- [ ] **Step 1.4: Update `validate/app.py` — unregister old blueprints, add check_bp**

Replace the blueprint section in `create_app()`:

```python
def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "flora-extractor-dev"

    if test_config:
        app.config.update(test_config)

    from validate.routes.batch import batch_bp
    from validate.routes.multi_originals import multi_orig_bp
    from validate.routes.dashboard import dashboard_bp
    from validate.routes.disambiguation import disambiguation_bp
    from validate.routes.check import check_bp

    app.register_blueprint(batch_bp)
    app.register_blueprint(multi_orig_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(disambiguation_bp)
    app.register_blueprint(check_bp)

    @app.route("/pdf/<path:filename>")
    def serve_pdf(filename: str):
        return send_from_directory(str(PDF_CACHE_DIR), filename)

    @app.route("/set-name", methods=["GET", "POST"])
    def set_name():
        from flask import render_template, request, session
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if name:
                session["reviewer_id"] = name
            next_url = request.args.get("next") or url_for("dashboard.dashboard_page")
            return redirect(next_url)
        return render_template("set_name.html")

    @app.route("/")
    def index():
        return redirect(url_for("dashboard.dashboard_page"))

    return app
```

Note: `check_bp` will be created in Task 9. For now create a stub so `app.py` imports without error.

- [ ] **Step 1.5: Create stub `validate/routes/check.py`**

```python
from flask import Blueprint, render_template

check_bp = Blueprint("check", __name__)

@check_bp.route("/check")
def check_page():
    return render_template("check.html", active_page="check")
```

- [ ] **Step 1.6: Create stub `validate/templates/check.html`**

```html
{% extends "base.html" %}
{% block title %}FLoRA — Check{% endblock %}
{% block content %}
<div style="padding:40px;text-align:center;color:var(--text-muted);">Check tab coming soon.</div>
{% endblock %}
```

- [ ] **Step 1.7: Run tests**

```
pytest tests/test_validate.py::test_check_route_accessible tests/test_validate.py::test_dashboard_still_works -v
```
Expected: both PASS.

- [ ] **Step 1.8: Commit**

```
git add validate/templates/base.html validate/app.py validate/routes/check.py validate/templates/check.html tests/test_validate.py
git commit -m "feat: reduce nav to Dashboard+Check; unregister old blueprints"
```

---

## Task 2: Dashboard sub-tab shell + CSS

**Files:**
- Rewrite: `validate/templates/dashboard.html`

This task builds the full tab skeleton (6 tabs, 40/60 grid, loading placeholders). No content yet — that comes in Tasks 4–7.

- [ ] **Step 2.1: Write failing test**

Add to `tests/test_validate.py`:

```python
def test_dashboard_has_subtabs(client):
    rv = client.get("/dashboard")
    html = rv.data.decode()
    assert 'data-tab="search"' in html
    assert 'data-tab="filter"' in html
    assert 'data-tab="extract"' in html
    assert 'data-tab="supabase"' in html
```

- [ ] **Step 2.2: Run to verify it fails**

```
pytest tests/test_validate.py::test_dashboard_has_subtabs -v
```
Expected: FAIL — current dashboard.html has no sub-tab buttons.

- [ ] **Step 2.3: Rewrite `validate/templates/dashboard.html`**

Replace the entire file with the following. The docs panels have placeholder text for now (filled in Tasks 4–7):

```html
{% extends "base.html" %}
{% block title %}FLoRA — Dashboard{% endblock %}

{% block head %}
<style>
/* ── Sub-tab nav ─────────────────────────────────────────────────────────────── */
.sub-tabs {
  display: flex; gap: 0; background: var(--surface);
  border-bottom: 2px solid var(--border); padding: 0 16px;
}
.sub-tab {
  padding: 10px 18px; font-size: 12px; font-weight: 600;
  border: none; background: none; cursor: pointer;
  color: var(--text-muted); border-bottom: 2px solid transparent;
  margin-bottom: -2px; transition: color .15s;
}
.sub-tab:hover { color: var(--text); }
.sub-tab.active { color: var(--accent); border-bottom-color: var(--accent); }

/* ── Tab panels ──────────────────────────────────────────────────────────────── */
.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* ── 40/60 split ─────────────────────────────────────────────────────────────── */
.tab-split {
  display: grid; grid-template-columns: 2fr 3fr; gap: 20px;
  padding: 20px 16px; max-width: 1300px; margin: 0 auto;
}
@media (max-width: 900px) { .tab-split { grid-template-columns: 1fr; } }

/* ── Docs panel ──────────────────────────────────────────────────────────────── */
.docs-panel {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 18px 20px; font-size: 12px; line-height: 1.7;
  overflow-y: auto; max-height: calc(100vh - 120px);
}
.docs-panel h2 {
  font-size: 13px; font-weight: 700; color: var(--text);
  margin: 0 0 12px; padding-bottom: 8px; border-bottom: 1px solid var(--border);
}
.docs-panel h3 {
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  color: var(--text-faint); letter-spacing: .5px; margin: 16px 0 6px;
}
.docs-panel p { color: var(--text-muted); margin-bottom: 8px; }
.docs-panel code {
  font-family: monospace; font-size: 11px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 3px; padding: 1px 5px; color: var(--text);
}
.docs-panel table { width: 100%; border-collapse: collapse; margin-top: 6px; }
.docs-panel th {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  color: var(--text-faint); text-align: left; padding: 4px 8px;
  border-bottom: 1px solid var(--border); background: var(--surface-2);
}
.docs-panel td {
  padding: 5px 8px; border-bottom: 1px solid var(--border);
  color: var(--text-muted); vertical-align: top;
}
.docs-panel td:first-child { font-family: monospace; font-size: 11px; color: var(--text); white-space: nowrap; }
.docs-panel .flow-step {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 6px 0; border-bottom: 1px solid var(--border);
}
.docs-panel .flow-step:last-child { border-bottom: none; }
.docs-panel .flow-num {
  min-width: 20px; height: 20px; border-radius: 50%;
  background: var(--accent); color: #fff;
  font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center;
}
.docs-panel .kw-pill {
  display: inline-block; background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 4px; padding: 1px 7px; font-size: 10px; font-family: monospace;
  color: var(--text-muted); margin: 2px 2px 2px 0;
}

/* ── Stats panel ─────────────────────────────────────────────────────────────── */
.stats-panel { display: flex; flex-direction: column; gap: 14px; }

.kpi-row { display: flex; flex-wrap: wrap; gap: 10px; }
.kpi-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 16px; text-align: center; min-width: 110px; flex: 1;
}
a.kpi-card {
  text-decoration: none; color: inherit; transition: border-color .15s, box-shadow .15s; cursor: pointer;
}
a.kpi-card:hover { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-ring); }
.kpi-num { font-size: 24px; font-weight: 800; line-height: 1.1; color: var(--text); }
.kpi-label { font-size: 10px; font-weight: 600; color: var(--text-muted); margin-top: 3px; }

.stat-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px;
}
.stat-card h3 {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  color: var(--text-faint); letter-spacing: .5px; margin: 0 0 10px;
}
.stat-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 5px 0; border-bottom: 1px solid var(--border); font-size: 12px; color: var(--text);
}
.stat-row:last-child { border-bottom: none; }
a.stat-row { text-decoration: none; transition: background .1s; }
a.stat-row:hover { background: var(--row-hover); }
.stat-val { font-weight: 700; }
.stat-pct { font-size: 10px; color: var(--text-faint); margin-left: 4px; }

.loading-msg { color: var(--text-faint); font-size: 12px; font-style: italic; padding: 20px 0; }
.null-note { color: var(--text-faint); font-size: 11px; font-style: italic; }

/* Supabase notice */
.supa-notice {
  background: var(--surface-2); border: 1px dashed var(--border-2); border-radius: 8px;
  padding: 20px; text-align: center; color: var(--text-muted); font-size: 12px;
}
</style>
{% endblock %}

{% block content %}
<!-- Sub-tab navigation -->
<div class="sub-tabs" role="tablist">
  <button class="sub-tab active" data-tab="search"       role="tab">Search</button>
  <button class="sub-tab"        data-tab="filter"       role="tab">Filter</button>
  <button class="sub-tab"        data-tab="extract"      role="tab">Extract</button>
  <button class="sub-tab"        data-tab="extract-test" role="tab">Extract-Test</button>
  <button class="sub-tab"        data-tab="supabase"     role="tab">Supabase</button>
  <button class="sub-tab"        data-tab="old-pipeline" role="tab">Old Pipeline</button>
</div>

<!-- ── Search tab ──────────────────────────────────────────────────────────────── -->
<div id="tab-search" class="tab-panel active">
  <div class="tab-split">
    <div class="docs-panel" id="docs-search"><!-- filled in Task 4 --></div>
    <div class="stats-panel" id="stats-search"><p class="loading-msg">Loading…</p></div>
  </div>
</div>

<!-- ── Filter tab ─────────────────────────────────────────────────────────────── -->
<div id="tab-filter" class="tab-panel">
  <div class="tab-split">
    <div class="docs-panel" id="docs-filter"><!-- filled in Task 5 --></div>
    <div class="stats-panel" id="stats-filter"><p class="loading-msg">Loading…</p></div>
  </div>
</div>

<!-- ── Extract tab ────────────────────────────────────────────────────────────── -->
<div id="tab-extract" class="tab-panel">
  <div class="tab-split">
    <div class="docs-panel" id="docs-extract"><!-- filled in Task 6 --></div>
    <div class="stats-panel" id="stats-extract"><p class="loading-msg">Loading…</p></div>
  </div>
</div>

<!-- ── Extract-Test tab ───────────────────────────────────────────────────────── -->
<div id="tab-extract-test" class="tab-panel">
  <div class="tab-split">
    <div class="docs-panel" id="docs-extract-test"><!-- filled in Task 6 --></div>
    <div class="stats-panel" id="stats-extract-test"><p class="loading-msg">Loading…</p></div>
  </div>
</div>

<!-- ── Supabase tab ───────────────────────────────────────────────────────────── -->
<div id="tab-supabase" class="tab-panel">
  <div class="tab-split">
    <div class="docs-panel" id="docs-supabase"><!-- filled in Task 7 --></div>
    <div class="stats-panel" id="stats-supabase"><p class="loading-msg">Loading…</p></div>
  </div>
</div>

<!-- ── Old Pipeline tab ──────────────────────────────────────────────────────── -->
<div id="tab-old-pipeline" class="tab-panel">
  <div class="tab-split">
    <div class="docs-panel" id="docs-old-pipeline"><!-- filled in Task 7 --></div>
    <div class="stats-panel" id="stats-old-pipeline"><p class="loading-msg">Loading…</p></div>
  </div>
</div>
{% endblock %}

{% block scripts %}
<script>
// ── Tab switching ─────────────────────────────────────────────────────────────
const tabLoaded = {};

function activateTab(tabId) {
  document.querySelectorAll('.sub-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  const btn = document.querySelector(`.sub-tab[data-tab="${tabId}"]`);
  const panel = document.getElementById('tab-' + tabId);
  if (!btn || !panel) return;
  btn.classList.add('active');
  panel.classList.add('active');
  history.replaceState(null, '', '#' + tabId);
  if (!tabLoaded[tabId]) {
    tabLoaded[tabId] = true;
    loadStats(tabId);
  }
}

document.querySelectorAll('.sub-tab').forEach(btn => {
  btn.addEventListener('click', () => activateTab(btn.dataset.tab));
});

// Restore from hash on load
const hash = location.hash.slice(1);
const validTabs = ['search','filter','extract','extract-test','supabase','old-pipeline'];
activateTab(validTabs.includes(hash) ? hash : 'search');

// ── Stats loaders (stubs — replaced in Tasks 3–7) ────────────────────────────
function loadStats(tab) {
  // Each task adds a case here
  if (tab === 'search')       loadSearchStats();
  else if (tab === 'filter')  loadFilterStats();
  else if (tab === 'extract') loadExtractStats();
  else if (tab === 'extract-test') loadExtractTestStats();
  else if (tab === 'supabase')     loadSupabaseStats();
  else if (tab === 'old-pipeline') loadOldPipelineStats();
}

function loadSearchStats()      { /* Task 4 */ }
function loadFilterStats()      { /* Task 5 */ }
function loadExtractStats()     { /* Task 6 */ }
function loadExtractTestStats() { /* Task 6 */ }
function loadSupabaseStats()    { /* Task 7 */ }
function loadOldPipelineStats() { /* Task 7 */ }

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(n) {
  if (n === null || n === undefined) return '—';
  return Number(n).toLocaleString();
}

function pct(n, total) {
  if (!total || n === null) return '';
  return `<span class="stat-pct">${((n / total) * 100).toFixed(1)}%</span>`;
}
</script>
{% endblock %}
```

- [ ] **Step 2.4: Run tests**

```
pytest tests/test_validate.py::test_dashboard_has_subtabs tests/test_validate.py::test_dashboard_still_works -v
```
Expected: both PASS.

- [ ] **Step 2.5: Commit**

```
git add validate/templates/dashboard.html tests/test_validate.py
git commit -m "feat: dashboard sub-tab shell with 40/60 split layout"
```

---

## Task 3: Search stats endpoint

**Files:**
- Modify: `validate/routes/dashboard.py`
- Modify: `tests/test_validate.py`

- [ ] **Step 3.1: Write failing test**

Add to `tests/test_validate.py`:

```python
def test_search_stats_returns_null_when_no_file(client):
    """Returns null total when candidates.csv doesn't exist in test env."""
    rv = client.get("/api/dashboard/search-stats")
    assert rv.status_code == 200
    data = rv.get_json()
    assert "candidates_total" in data
    assert data["candidates_total"] is None  # no CSV in test env
```

- [ ] **Step 3.2: Run to verify it fails**

```
pytest tests/test_validate.py::test_search_stats_returns_null_when_no_file -v
```
Expected: FAIL — route doesn't exist yet.

- [ ] **Step 3.3: Add `api_search_stats` to `validate/routes/dashboard.py`**

Add this after the existing `api_csv_stats` function:

```python
@dashboard_bp.route("/api/dashboard/search-stats")
def api_search_stats():
    """Read candidates.csv in 50k-row chunks — file can be 1M+ rows."""
    path = DATA_DIR / "candidates.csv"
    if not path.exists():
        return jsonify({
            "candidates_total": None,
            "candidates_no_doi": None,
            "candidates_no_doi_or_url": None,
            "candidates_no_abstract": None,
            "candidates_by_source": {},
        })

    total = no_doi = no_doi_or_url = no_abstract = 0
    by_source: dict = {}

    try:
        for chunk in pd.read_csv(
            path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
            chunksize=50_000,
            usecols=lambda c: c in ("doi_r", "url_r", "abstract_r", "source"),
        ):
            chunk = chunk.fillna("")
            total += len(chunk)

            doi_empty = chunk["doi_r"].str.strip() == "" if "doi_r" in chunk.columns else pd.Series([True] * len(chunk))
            url_empty = chunk["url_r"].str.strip() == "" if "url_r" in chunk.columns else pd.Series([True] * len(chunk))
            abs_empty = chunk["abstract_r"].str.strip() == "" if "abstract_r" in chunk.columns else pd.Series([True] * len(chunk))

            no_doi          += int(doi_empty.sum())
            no_doi_or_url   += int((doi_empty & url_empty).sum())
            no_abstract     += int(abs_empty.sum())

            if "source" in chunk.columns:
                for src, cnt in chunk["source"].value_counts().items():
                    by_source[src] = by_source.get(src, 0) + int(cnt)

        return jsonify({
            "candidates_total":        total,
            "candidates_no_doi":       no_doi,
            "candidates_no_doi_or_url": no_doi_or_url,
            "candidates_no_abstract":  no_abstract,
            "candidates_by_source":    by_source,
        })
    except Exception as exc:
        return jsonify({"candidates_total": None, "error": str(exc)})
```

- [ ] **Step 3.4: Run tests**

```
pytest tests/test_validate.py::test_search_stats_returns_null_when_no_file -v
```
Expected: PASS.

- [ ] **Step 3.5: Commit**

```
git add validate/routes/dashboard.py tests/test_validate.py
git commit -m "feat: add /api/dashboard/search-stats endpoint"
```

---

## Task 4: Search tab — docs panel + stats wiring

**Files:**
- Modify: `validate/templates/dashboard.html`

- [ ] **Step 4.1: Fill in the Search docs panel**

Replace `<!-- filled in Task 4 -->` inside `<div class="docs-panel" id="docs-search">` with:

```html
<h2>Stage 1 — Search</h2>
<p>Discovers candidate replication papers by querying <strong>OpenAlex</strong> and <strong>Semantic Scholar</strong> for 23 exact phrases matched against title + abstract.</p>

<h3>Keywords (23)</h3>
<p>
  <span class="kw-pill">replication of</span>
  <span class="kw-pill">direct replication</span>
  <span class="kw-pill">close replication</span>
  <span class="kw-pill">conceptual replication</span>
  <span class="kw-pill">replication study</span>
  <span class="kw-pill">reproduction study</span>
  <span class="kw-pill">we replicated</span>
  <span class="kw-pill">attempts to replicate</span>
  <span class="kw-pill">registered replication report</span>
  <span class="kw-pill">pre-registered replication</span>
  <span class="kw-pill">failed to replicate</span>
  <span class="kw-pill">did not replicate</span>
  <span class="kw-pill">we replicate</span>
  <span class="kw-pill">replicating the findings</span>
  <span class="kw-pill">could not reproduce</span>
  <span class="kw-pill">successfully replicated</span>
  <span class="kw-pill">reproducibility of</span>
  <span class="kw-pill">replication and extension</span>
  <span class="kw-pill">replicability of</span>
  <span class="kw-pill">attempt to replicate</span>
  <span class="kw-pill">failure to replicate</span>
  <span class="kw-pill">non-replication</span>
  <span class="kw-pill">reproducibility study</span>
  <span class="kw-pill">reproduce the findings</span>
</p>

<h3>Code flow</h3>
<div class="flow-step"><div class="flow-num">1</div><div><strong>Cache harvest</strong> — scan <code>cache/openalex/</code> and <code>cache/s2/</code> for previously downloaded pages; merge rows without re-fetching.</div></div>
<div class="flow-step"><div class="flow-num">2</div><div><strong>Live fetch</strong> — each phrase × year is an independent resumable job with cursor/offset persistence. Crashes are safe.</div></div>
<div class="flow-step"><div class="flow-num">3</div><div><strong>Deduplication</strong> (5 passes): Drop figshare/PeerJ-review DOIs → collapse versioned preprints → exact DOI dedup (keep richest row) → fuzzy title match on DOI-less rows (RapidFuzz ≥ 90) → FLoRA cross-check against <code>flora_entry_sheet.csv</code> and <code>flora.csv</code>.</div></div>
<div class="flow-step"><div class="flow-num">4</div><div><strong>Merge</strong> — check each row against <code>cache/candidates_index.txt</code>; append only new rows; update index incrementally. <em>Why:</em> candidates.csv can be 1M+ rows — the index (flat text, ~1s to load) avoids loading the full CSV.</div></div>

<h3>CLI commands</h3>
<table>
  <tr><th>Command</th><th>Description</th></tr>
  <tr><td>--from-year YYYY</td><td>Earliest publication year to include</td></tr>
  <tr><td>--to-year YYYY</td><td>Latest publication year to include</td></tr>
  <tr><td>--max-per-phrase N</td><td>Cap rows per phrase per run; checkpoint saved, next run continues</td></tr>
  <tr><td>--auto-advance</td><td>Process ONE phrase/year job per call; state in <code>cache/search_state.json</code></td></tr>
  <tr><td>--source SOURCE</td><td>Restrict to openalex, semantic_scholar, or engine (repeatable)</td></tr>
  <tr><td>--reset-cursors</td><td>Delete all cursor/offset files; restart from scratch</td></tr>
  <tr><td>--rebuild-index</td><td>Rebuild <code>candidates_index.txt</code> from CSV then exit</td></tr>
  <tr><td>--harvest-only</td><td>Scan all cached pages into CSV then exit</td></tr>
  <tr><td>--no-harvest</td><td>Skip per-cycle cache harvest in --auto-advance mode</td></tr>
</table>

<h3>candidates.csv columns</h3>
<table>
  <tr><th>Column</th><th>Meaning</th><th>Example</th></tr>
  <tr><td>doi_r</td><td>DOI of the replication paper</td><td>10.1177/0956797615</td></tr>
  <tr><td>title_r</td><td>Title</td><td>Many Labs Replication of…</td></tr>
  <tr><td>abstract_r</td><td>Abstract text</td><td>We attempted to replicate…</td></tr>
  <tr><td>year_r</td><td>Publication year</td><td>2015</td></tr>
  <tr><td>authors_r</td><td>Semicolon-separated authors</td><td>Klein, R.A.; Ratliff, K.A.</td></tr>
  <tr><td>journal_r</td><td>Journal / venue</td><td>Psychological Science</td></tr>
  <tr><td>url_r</td><td>Open-access URL</td><td>https://osf.io/…</td></tr>
  <tr><td>openalex_id_r</td><td>OpenAlex work ID</td><td>https://openalex.org/W2…</td></tr>
  <tr><td>source</td><td>Which source found this row</td><td>openalex</td></tr>
  <tr><td>ref_r</td><td>Short reference string</td><td>Klein · 2015 · Psych Sci</td></tr>
</table>
```

- [ ] **Step 4.2: Wire the Search stats panel**

Replace the stub `function loadSearchStats() { /* Task 4 */ }` in the `<script>` block with:

```javascript
function loadSearchStats() {
  fetch('/api/dashboard/search-stats')
    .then(r => r.json())
    .then(d => {
      const el = document.getElementById('stats-search');
      if (d.candidates_total === null) {
        el.innerHTML = '<p class="null-note">candidates.csv not found — run Stage 1 first.</p>';
        return;
      }
      const total = d.candidates_total;
      const src = d.candidates_by_source || {};
      const srcRows = Object.entries(src)
        .sort((a, b) => b[1] - a[1])
        .map(([s, n]) =>
          `<a class="stat-row" href="/api/dashboard/download?stage=candidates&col=source&val=${encodeURIComponent(s)}">
            <span>${s}</span><span class="stat-val">${fmt(n)} ${pct(n, total)}</span>
          </a>`
        ).join('');

      el.innerHTML = `
        <div class="kpi-row">
          <a class="kpi-card" href="/api/dashboard/download?stage=candidates&col=source&val=*">
            <div class="kpi-num">${fmt(total)}</div>
            <div class="kpi-label">TOTAL CANDIDATES</div>
          </a>
          <a class="kpi-card" href="/api/dashboard/download?stage=candidates&col=doi_r&val=">
            <div class="kpi-num">${fmt(d.candidates_no_doi)}</div>
            <div class="kpi-label">NO DOI</div>
          </a>
          <a class="kpi-card" href="/api/dashboard/download?stage=candidates&col=doi_r&val=">
            <div class="kpi-num">${fmt(d.candidates_no_doi_or_url)}</div>
            <div class="kpi-label">NO DOI OR URL</div>
          </a>
          <a class="kpi-card" href="/api/dashboard/download?stage=candidates&col=abstract_r&val=">
            <div class="kpi-num">${fmt(d.candidates_no_abstract)}</div>
            <div class="kpi-label">NO ABSTRACT</div>
          </a>
        </div>
        <div class="stat-card">
          <h3>By source</h3>
          ${srcRows || '<p class="null-note">No source data.</p>'}
        </div>`;
    })
    .catch(() => {
      document.getElementById('stats-search').innerHTML = '<p class="null-note">Failed to load stats.</p>';
    });
}
```

Note: The "NO DOI" and "NO DOI OR URL" KPI cards link to the download endpoint with `val=` (empty string). The download endpoint in Task 8 handles filtering `doi_r == ""`.

- [ ] **Step 4.3: Verify in browser**

Start app: `python -m validate.app`  
Open `http://localhost:5001/dashboard` — Search tab should show docs on left, stats (or null note) on right.

- [ ] **Step 4.4: Commit**

```
git add validate/templates/dashboard.html
git commit -m "feat: search tab docs panel + stats wiring"
```

---

## Task 5: Filter tab — extend csv-stats + fill Filter tab

**Files:**
- Modify: `validate/routes/dashboard.py`
- Modify: `validate/templates/dashboard.html`
- Modify: `tests/test_validate.py`

- [ ] **Step 5.1: Write failing test**

Add to `tests/test_validate.py`:

```python
def test_csv_stats_has_filter_breakdown(client):
    rv = client.get("/api/dashboard/csv-stats")
    assert rv.status_code == 200
    data = rv.get_json()
    # These keys must exist (value may be 0 or None if no CSV)
    assert "filter_method_rule_based" in data
    assert "filter_method_llm" in data
    assert "filter_method_both" in data
    assert "filter_conf_high" in data
    assert "filter_repro_no_doi" in data
```

- [ ] **Step 5.2: Run to verify it fails**

```
pytest tests/test_validate.py::test_csv_stats_has_filter_breakdown -v
```
Expected: FAIL.

- [ ] **Step 5.3: Extend `api_csv_stats` in `validate/routes/dashboard.py`**

In the filtered CSV section of `api_csv_stats` (after the existing `filter_replication` / `filter_reproduction` counts), add:

```python
        # ── Extended filter breakdown ──────────────────────────────────────────
        if filt_path.exists():
            try:
                fdf2 = pd.read_csv(
                    filt_path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
                    usecols=lambda c: c in (
                        "doi_r", "url_r", "abstract_r",
                        "filter_status", "filter_method", "filter_confidence",
                    ),
                ).fillna("")

                # By method
                mc = fdf2["filter_method"].value_counts().to_dict() if "filter_method" in fdf2.columns else {}
                stats["filter_method_rule_based"] = int(mc.get("rule_based", 0))
                stats["filter_method_llm"]        = int(mc.get("llm",        0))
                stats["filter_method_both"]       = int(mc.get("both",       0))

                # By confidence
                cc = fdf2["filter_confidence"].value_counts().to_dict() if "filter_confidence" in fdf2.columns else {}
                stats["filter_conf_high"]   = int(cc.get("high",   0))
                stats["filter_conf_medium"] = int(cc.get("medium", 0))
                stats["filter_conf_low"]    = int(cc.get("low",    0))

                # Among replications + reproductions only
                if "filter_status" in fdf2.columns:
                    repro_mask = fdf2["filter_status"].isin(["replication", "reproduction"])
                    sub = fdf2[repro_mask]
                else:
                    sub = pd.DataFrame()

                if not sub.empty:
                    doi_e = sub["doi_r"].str.strip() == "" if "doi_r" in sub.columns else pd.Series([True] * len(sub))
                    url_e = sub["url_r"].str.strip() == "" if "url_r" in sub.columns else pd.Series([True] * len(sub))
                    abs_e = sub["abstract_r"].str.strip() == "" if "abstract_r" in sub.columns else pd.Series([True] * len(sub))
                    stats["filter_repro_no_doi"]        = int(doi_e.sum())
                    stats["filter_repro_no_doi_or_url"] = int((doi_e & url_e).sum())
                    stats["filter_repro_no_abstract"]   = int(abs_e.sum())
                else:
                    stats["filter_repro_no_doi"] = stats["filter_repro_no_doi_or_url"] = stats["filter_repro_no_abstract"] = 0

            except Exception:
                for k in ("filter_method_rule_based","filter_method_llm","filter_method_both",
                          "filter_conf_high","filter_conf_medium","filter_conf_low",
                          "filter_repro_no_doi","filter_repro_no_doi_or_url","filter_repro_no_abstract"):
                    stats.setdefault(k, 0)
        else:
            for k in ("filter_method_rule_based","filter_method_llm","filter_method_both",
                      "filter_conf_high","filter_conf_medium","filter_conf_low",
                      "filter_repro_no_doi","filter_repro_no_doi_or_url","filter_repro_no_abstract"):
                stats[k] = 0
```

Place this block *after* the existing filtered CSV block (after the `except Exception:` that sets `filtered_count = None`).

- [ ] **Step 5.4: Run tests**

```
pytest tests/test_validate.py::test_csv_stats_has_filter_breakdown -v
```
Expected: PASS.

- [ ] **Step 5.5: Fill Filter docs panel in `dashboard.html`**

Replace `<!-- filled in Task 5 -->` inside `<div class="docs-panel" id="docs-filter">`:

```html
<h2>Stage 2 — Filter</h2>
<p>Reads <code>candidates.csv</code> in 50k-row chunks, classifies each row using a two-pass rule+LLM approach. Results stream to <code>filtered.csv</code> one row at a time. Resumes via <code>cache/filtered_index.txt</code>.</p>

<h3>Code flow</h3>
<div class="flow-step"><div class="flow-num">1</div><div>Load / build <code>filtered_index.txt</code> — skip already-processed rows.</div></div>
<div class="flow-step"><div class="flow-num">2</div><div><strong>Rule filter</strong> (title + abstract): exclusion patterns → <code>false_positive</code>; no replication phrase → <code>false_positive</code>; phrase + author–year cite → <code>replication</code> or <code>reproduction</code>; phrase, no cite → <code>needs_review</code>.</div></div>
<div class="flow-step"><div class="flow-num">3</div><div><strong>LLM uplift</strong> (only <code>needs_review</code> rows): OpenAI primary → Gemini fallback. Cached by hash(title + abstract). Sets <code>filter_method = both</code>.</div></div>

<h3>Rule logic</h3>
<p><strong>Exclusion patterns → false_positive (high):</strong> DNA replication, replication fork/origin/stress/timing, code/data/virus/cell replication.</p>
<p><strong>Reproduction vs replication:</strong> if only reproduction-flavoured phrases fire ("re-analysis", "same original dataset") → <code>reproduction</code>.</p>

<h3>LLM prompt (needs_review rows only)</h3>
<p>System: <em>"You are an expert in scientific replication and reproducibility."</em><br>
Classifies into: <strong>replication</strong> (new data, tests original finding) · <strong>reproduction</strong> (same original data) · <strong>false_positive</strong> (meta-analyses, methodology papers, biological replication).<br>
Returns JSON: <code>{filter_status, filter_confidence, filter_evidence}</code>.<br>
Model: <code>FILTER_OPENAI_MODEL</code> (default gpt-5.4-mini) → Gemini fallback.</p>

<h3>CLI commands</h3>
<table>
  <tr><th>Command</th><th>Description</th></tr>
  <tr><td>--limit N</td><td>Stop after N new rows</td></tr>
  <tr><td>--offset N</td><td>Skip first N unprocessed rows</td></tr>
  <tr><td>--from-year YYYY</td><td>Only rows with year_r ≥ YYYY</td></tr>
  <tr><td>--to-year YYYY</td><td>Only rows with year_r ≤ YYYY</td></tr>
  <tr><td>--source SOURCE</td><td>Only rows from this source</td></tr>
  <tr><td>--rebuild-index</td><td>Rebuild <code>filtered_index.txt</code> then exit</td></tr>
</table>

<h3>filtered.csv added columns</h3>
<table>
  <tr><th>Column</th><th>Meaning</th><th>Example</th></tr>
  <tr><td>filter_status</td><td>Classification result</td><td>replication</td></tr>
  <tr><td>filter_method</td><td>How classified</td><td>rule_based · llm · both</td></tr>
  <tr><td>filter_evidence</td><td>Triggering phrase or LLM quote</td><td>phrase:"failed to replicate"; cite:Smith (2009)</td></tr>
  <tr><td>filter_confidence</td><td>Certainty level</td><td>high · medium · low</td></tr>
</table>
```

- [ ] **Step 5.6: Wire the Filter stats panel**

Replace `function loadFilterStats() { /* Task 5 */ }` with:

```javascript
function loadFilterStats() {
  fetch('/api/dashboard/csv-stats')
    .then(r => r.json())
    .then(d => {
      const el = document.getElementById('stats-filter');
      if (d.filtered_count === null) {
        el.innerHTML = '<p class="null-note">filtered.csv not found — run Stage 2 first.</p>';
        return;
      }
      const total = d.filtered_count;
      const dl = (col, val) => `/api/dashboard/download?stage=filtered&col=${col}&val=${encodeURIComponent(val)}`;

      el.innerHTML = `
        <div class="kpi-row">
          <a class="kpi-card" href="${dl('filter_status','*')}">
            <div class="kpi-num">${fmt(total)}</div><div class="kpi-label">TOTAL FILTERED</div>
          </a>
          <a class="kpi-card" href="${dl('filter_status','replication')}">
            <div class="kpi-num">${fmt(d.filter_replication)}</div><div class="kpi-label">REPLICATION</div>
          </a>
          <a class="kpi-card" href="${dl('filter_status','reproduction')}">
            <div class="kpi-num">${fmt(d.filter_reproduction)}</div><div class="kpi-label">REPRODUCTION</div>
          </a>
          <a class="kpi-card" href="${dl('filter_status','needs_review')}">
            <div class="kpi-num">${fmt(d.filter_needs_review)}</div><div class="kpi-label">NEEDS REVIEW</div>
          </a>
          <a class="kpi-card" href="${dl('filter_status','false_positive')}">
            <div class="kpi-num">${fmt(d.filter_false_positive)}</div><div class="kpi-label">FALSE POSITIVE</div>
          </a>
        </div>
        <div class="stat-card">
          <h3>Among replications + reproductions</h3>
          <div class="stat-row"><span>No DOI</span><span class="stat-val">${fmt(d.filter_repro_no_doi)} ${pct(d.filter_repro_no_doi, d.filter_replication + d.filter_reproduction)}</span></div>
          <div class="stat-row"><span>No DOI or URL</span><span class="stat-val">${fmt(d.filter_repro_no_doi_or_url)}</span></div>
          <div class="stat-row"><span>No abstract</span><span class="stat-val">${fmt(d.filter_repro_no_abstract)}</span></div>
        </div>
        <div class="stat-card">
          <h3>By method</h3>
          <a class="stat-row" href="${dl('filter_method','rule_based')}"><span>Rule-based</span><span class="stat-val">${fmt(d.filter_method_rule_based)} ${pct(d.filter_method_rule_based, total)}</span></a>
          <a class="stat-row" href="${dl('filter_method','llm')}"><span>LLM only</span><span class="stat-val">${fmt(d.filter_method_llm)} ${pct(d.filter_method_llm, total)}</span></a>
          <a class="stat-row" href="${dl('filter_method','both')}"><span>Both</span><span class="stat-val">${fmt(d.filter_method_both)} ${pct(d.filter_method_both, total)}</span></a>
        </div>
        <div class="stat-card">
          <h3>By confidence</h3>
          <a class="stat-row" href="${dl('filter_confidence','high')}"><span>High</span><span class="stat-val">${fmt(d.filter_conf_high)} ${pct(d.filter_conf_high, total)}</span></a>
          <a class="stat-row" href="${dl('filter_confidence','medium')}"><span>Medium</span><span class="stat-val">${fmt(d.filter_conf_medium)} ${pct(d.filter_conf_medium, total)}</span></a>
          <a class="stat-row" href="${dl('filter_confidence','low')}"><span>Low</span><span class="stat-val">${fmt(d.filter_conf_low)} ${pct(d.filter_conf_low, total)}</span></a>
        </div>`;
    });
}
```

- [ ] **Step 5.7: Commit**

```
git add validate/routes/dashboard.py validate/templates/dashboard.html tests/test_validate.py
git commit -m "feat: filter tab docs + stats; extend csv-stats with filter breakdown"
```

---

## Task 6: Extract + Extract-Test tabs

**Files:**
- Modify: `validate/templates/dashboard.html`

- [ ] **Step 6.1: Fill Extract docs panel**

Replace `<!-- filled in Task 6 -->` in `<div id="docs-extract">`:

```html
<h2>Stage 3 — Extract</h2>
<p>For each replication/reproduction in <code>filtered.csv</code>: identifies the original study, extracts the outcome, verifies the original's DOI, streams results to <code>extracted.csv</code>.</p>

<h3>Code flow</h3>
<div class="flow-step"><div class="flow-num">1</div><div>Skip <code>false_positive</code> rows unchanged.</div></div>
<div class="flow-step"><div class="flow-num">2</div><div>Classify <code>original_match_type</code> (single vs. multiple originals).</div></div>
<div class="flow-step"><div class="flow-num">3</div><div><strong>Single original linking</strong> (link_original.py): A) rule-based citation scoring (author+year+journal+title Jaccard, resolve if ≥ 4.0 with gap ≥ 2.0) → B) title pattern regexes ("A Replication of X") → C) LLM fallback (OpenRouter/Qwen → Gemini → OpenAI).</div></div>
<div class="flow-step"><div class="flow-num">4</div><div><strong>Outcome extraction</strong> (code_outcome.py): keyword scan (failure → mixed → success → descriptive) on title / abstract / fulltext[:3000]; LLM pass when keyword fails.</div></div>
<div class="flow-step"><div class="flow-num">5</div><div><strong>DOI verification</strong> (doi_verify.py): CrossRef/OpenAlex metadata check on doi_o; 3-tier re-resolution on mismatch.</div></div>
<div class="flow-step"><div class="flow-num">6</div><div>Append row to <code>extracted.csv</code> (or <code>extracted-test.csv</code> with <code>--extracted-test</code>).</div></div>

<h3>Outcome keyword patterns</h3>
<p><strong>Failure (checked first):</strong> "failed to replicate", "did not replicate", "no support for the original", "null result", "no significant effect"…</p>
<p><strong>Mixed:</strong> "partially replicated", "mixed results", "some but not all", "smaller effect"…</p>
<p><strong>Success:</strong> "successfully replicated", "confirmed the findings", "consistent with the original", bare "replicated"…</p>
<p><strong>Descriptive:</strong> "adapted the method", "in a different context", "not intended to test"…</p>

<h3>CLI commands</h3>
<table>
  <tr><th>Command</th><th>Description</th></tr>
  <tr><td>--resume</td><td>Carry forward resolved rows; re-run only target_pending</td></tr>
  <tr><td>--extracted-test</td><td>Write to extracted-test.csv instead</td></tr>
  <tr><td>--doi-r DOIS</td><td>Process only specific DOI(s)</td></tr>
  <tr><td>--from-year / --to-year</td><td>Year range filter</td></tr>
  <tr><td>--limit N</td><td>Process first N non-false-positive rows</td></tr>
  <tr><td>--no-llm</td><td>Skip all LLM calls (rule-based only)</td></tr>
  <tr><td>--no-pdf</td><td>Skip PDF download; abstract-only</td></tr>
  <tr><td>--no-multiple-originals</td><td>Write multiple_original rows as target_pending</td></tr>
  <tr><td>--no-reproductions</td><td>Skip reproduction rows</td></tr>
  <tr><td>--skip-flora-validated</td><td>Skip DOIs already validated in FLoRA entry sheet</td></tr>
  <tr><td>--resolved-only</td><td>Only write fully resolved rows</td></tr>
  <tr><td>--predicted-outcome</td><td>Pre-filter by keyword-predicted outcome</td></tr>
  <tr><td>--source SOURCE</td><td>Only rows from this source</td></tr>
  <tr><td>--match-type-only</td><td>Classify match type only → match_type_only.csv</td></tr>
  <tr><td>--outcome-only</td><td>Classify outcome only → outcome_only.csv</td></tr>
</table>

<h3>extracted.csv added columns</h3>
<table>
  <tr><th>Column</th><th>Meaning</th></tr>
  <tr><td>original_match_type</td><td>single_original · multiple_match · multiple_original</td></tr>
  <tr><td>doi_o / title_o / year_o / authors_o</td><td>Identified original study metadata</td></tr>
  <tr><td>link_method</td><td>author_year_match · llm_abstract · llm_fulltext · target_pending · api_error</td></tr>
  <tr><td>link_evidence</td><td>Evidence string from resolution step</td></tr>
  <tr><td>link_confidence</td><td>high · medium · low</td></tr>
  <tr><td>doi_o_verification</td><td>verified · corrected · mismatch · no_doi · not_found · no_metadata · api_error · skipped</td></tr>
  <tr><td>outcome</td><td>success · failure · mixed · uninformative · descriptive · pending · api_error</td></tr>
  <tr><td>outcome_phrase</td><td>Verbatim quote that triggered classification</td></tr>
  <tr><td>type</td><td>replication · reproduction</td></tr>
</table>
```

- [ ] **Step 6.2: Wire Extract stats panel**

Replace `function loadExtractStats() { /* Task 6 */ }` with:

```javascript
function loadExtractStats() {
  fetch('/api/dashboard/csv-stats')
    .then(r => r.json())
    .then(d => renderExtractStats('stats-extract', d, ''));
}

function loadExtractTestStats() {
  fetch('/api/dashboard/csv-stats')
    .then(r => r.json())
    .then(d => renderExtractStats('stats-extract-test', d, 'test_'));
}

function renderExtractStats(elId, d, prefix) {
  const el = document.getElementById(elId);
  const stage = prefix === 'test_' ? 'extracted-test' : 'extracted';
  const total = d[`${prefix}extracted_count`];
  if (total === null) {
    el.innerHTML = `<p class="null-note">${stage}.csv not found.</p>`;
    return;
  }
  const dl = (col, val) => `/api/dashboard/download?stage=${stage}&col=${col}&val=${encodeURIComponent(val)}`;
  const outcomes = ['success','failure','mixed','uninformative','descriptive','pending','api_error','cannot_be_determined'];
  const outcomeRows = outcomes.map(o =>
    `<a class="stat-row" href="${dl('outcome', o)}"><span>${o}</span><span class="stat-val">${fmt(d[`${prefix}outcome_${o}`])} ${pct(d[`${prefix}outcome_${o}`], total)}</span></a>`
  ).join('');
  const methods = ['author_year_match','llm_abstract','llm_fulltext','no_original_found','target_pending','api_error'];
  const methodRows = methods.map(m =>
    `<a class="stat-row" href="${dl('link_method', m)}"><span>${m}</span><span class="stat-val">${fmt(d[`${prefix}method_${m}`])} ${pct(d[`${prefix}method_${m}`], total)}</span></a>`
  ).join('');
  const verif = ['verified','corrected','mismatch','no_doi','not_found','no_metadata','api_error','skipped'];
  const verifRows = verif.map(v =>
    `<a class="stat-row" href="${dl('doi_o_verification', v)}"><span>${v}</span><span class="stat-val">${fmt(d[`${prefix}verif_${v}`] ?? 0)}</span></a>`
  ).join('');

  el.innerHTML = `
    <div class="kpi-row">
      <a class="kpi-card" href="${dl('outcome','*')}">
        <div class="kpi-num">${fmt(total)}</div><div class="kpi-label">TOTAL</div>
      </a>
      <a class="kpi-card" href="${dl('link_method','target_pending')}">
        <div class="kpi-num">${fmt(d[prefix+'target_pending_count'])}</div><div class="kpi-label">TARGET PENDING</div>
      </a>
    </div>
    <div class="stat-card"><h3>Outcomes</h3>${outcomeRows}</div>
    <div class="stat-card"><h3>Link method</h3>${methodRows}</div>
    <div class="stat-card">
      <h3>Match type</h3>
      <div class="stat-row"><span>Single original</span><span class="stat-val">${fmt(d[prefix+'match_single'])}</span></div>
      <div class="stat-row"><span>Multiple match</span><span class="stat-val">${fmt(d[prefix+'match_multiple_match'])}</span></div>
      <div class="stat-row"><span>Multiple original</span><span class="stat-val">${fmt(d[prefix+'match_multiple_original'])}</span></div>
    </div>
    <div class="stat-card">
      <h3>LLM model family</h3>
      <div class="stat-row"><span>Gemini</span><span class="stat-val">${fmt(d[prefix+'model_gemini'])}</span></div>
      <div class="stat-row"><span>GPT</span><span class="stat-val">${fmt(d[prefix+'model_gpt'])}</span></div>
      <div class="stat-row"><span>Qwen</span><span class="stat-val">${fmt(d[prefix+'model_qwen'])}</span></div>
      <div class="stat-row"><span>None / other</span><span class="stat-val">${fmt(d[prefix+'model_none'])}</span></div>
    </div>`;
}
```

- [ ] **Step 6.3: Fill Extract-Test docs panel**

Replace `<!-- filled in Task 6 -->` in `<div id="docs-extract-test">`:

```html
<h2>Stage 3 — Extract-Test (Sandbox)</h2>
<p>Identical to Extract but writes to <code>extracted-test.csv</code>. Use this to test new pipeline runs safely before promoting rows to production.</p>

<h3>Workflow</h3>
<div class="flow-step"><div class="flow-num">1</div><div>Run extraction with <code>--extracted-test</code>: rows go to <code>data/extracted-test.csv</code>.</div></div>
<div class="flow-step"><div class="flow-num">2</div><div>Review results here or in the web app's Extract-Test tab.</div></div>
<div class="flow-step"><div class="flow-num">3</div><div>Promote satisfied rows to production with <code>promote_test</code>.</div></div>

<h3>promote_test CLI</h3>
<table>
  <tr><th>Command</th><th>Description</th></tr>
  <tr><td>--all</td><td>Promote all rows from extracted-test.csv</td></tr>
  <tr><td>--doi 10.x/y</td><td>Promote a single row by doi_r</td></tr>
  <tr><td>--dry-run</td><td>Preview what would be promoted without writing</td></tr>
  <tr><td>--force</td><td>Overwrite existing extracted.csv rows</td></tr>
</table>
<p><strong>Run:</strong> <code>python -m extract.promote_test --all</code></p>

<h3>Note on DOI skipping</h3>
<p>When running <code>--extracted-test</code>, already-resolved DOIs in <code>extracted.csv</code> are skipped automatically. Only genuinely new candidates are processed.</p>
```

- [ ] **Step 6.4: Commit**

```
git add validate/templates/dashboard.html
git commit -m "feat: extract + extract-test tabs docs and stats panels"
```

---

## Task 7: Supabase + Old Pipeline tabs

**Files:**
- Modify: `validate/templates/dashboard.html`

- [ ] **Step 7.1: Fill Supabase docs panel**

Replace `<!-- filled in Task 7 -->` in `<div id="docs-supabase">`:

```html
<h2>Supabase — Validation</h2>
<p>Validation KPIs from the Supabase-backed validation database. Reviewers vote to confirm or reject each extracted row. Corrections to <code>doi_o</code> and <code>outcome</code> are tracked separately.</p>

<h3>Validation statuses</h3>
<table>
  <tr><th>Status</th><th>Meaning</th></tr>
  <tr><td>confirmed</td><td>Reviewer agreed the extraction is correct</td></tr>
  <tr><td>rejected</td><td>Reviewer flagged the extraction as wrong</td></tr>
  <tr><td>pending</td><td>Not yet reviewed</td></tr>
  <tr><td>needs_review</td><td>Flagged for further discussion</td></tr>
</table>

<h3>validated.csv columns</h3>
<table>
  <tr><th>Column</th><th>Meaning</th></tr>
  <tr><td>validation_status</td><td>confirmed · rejected · pending · needs_review</td></tr>
  <tr><td>vote_count</td><td>Total votes received</td></tr>
  <tr><td>confirm_votes / reject_votes</td><td>Breakdown of votes</td></tr>
  <tr><td>validated_doi_o</td><td>Reviewer-corrected original DOI (blank = accepted unchanged)</td></tr>
  <tr><td>validated_outcome</td><td>Reviewer-corrected outcome (blank = accepted unchanged)</td></tr>
  <tr><td>validator_notes</td><td>Free-text reviewer notes</td></tr>
</table>
```

- [ ] **Step 7.2: Wire Supabase stats panel**

Replace `function loadSupabaseStats() { /* Task 7 */ }` with:

```javascript
function loadSupabaseStats() {
  const el = document.getElementById('stats-supabase');
  Promise.all([
    fetch('/api/dashboard/supabase-stats').then(r => r.json()),
    fetch('/api/dashboard/supabase-outcomes').then(r => r.json()),
    fetch('/api/dashboard/supabase-corrections').then(r => r.json()),
  ]).then(([stats, outcomes, corrections]) => {
    if (stats.error || !stats.total_validated) {
      el.innerHTML = '<div class="supa-notice">Supabase not configured or no data yet.</div>';
      return;
    }
    const total = stats.total_validated || 0;
    const outcomeRows = Object.entries(outcomes)
      .map(([o, n]) => `<div class="stat-row"><span>${o}</span><span class="stat-val">${fmt(n)} ${pct(n, total)}</span></div>`)
      .join('');
    const corrRows = Object.entries(corrections)
      .map(([field, n]) => `<div class="stat-row"><span>${field}</span><span class="stat-val">${fmt(n)}</span></div>`)
      .join('');
    el.innerHTML = `
      <div class="kpi-row">
        <div class="kpi-card"><div class="kpi-num">${fmt(total)}</div><div class="kpi-label">VALIDATED</div></div>
        <div class="kpi-card"><div class="kpi-num">${fmt(stats.confirmed)}</div><div class="kpi-label">CONFIRMED</div></div>
        <div class="kpi-card"><div class="kpi-num">${fmt(stats.rejected)}</div><div class="kpi-label">REJECTED</div></div>
        <div class="kpi-card"><div class="kpi-num">${fmt(stats.pending)}</div><div class="kpi-label">PENDING</div></div>
      </div>
      <div class="stat-card"><h3>Outcome distribution</h3>${outcomeRows}</div>
      <div class="stat-card"><h3>Correction frequency</h3>${corrRows || '<p class="null-note">No corrections yet.</p>'}</div>`;
  }).catch(() => {
    el.innerHTML = '<div class="supa-notice">Could not reach Supabase.</div>';
  });
}
```

- [ ] **Step 7.3: Fill Old Pipeline docs panel**

Replace `<!-- filled in Task 7 -->` in `<div id="docs-old-pipeline">`:

```html
<h2>Old Pipeline — Analysis</h2>
<p>Legacy analysis output generated by the analysis scripts in <code>analysis/</code>. These files are generated separately and not updated by the main pipeline runs.</p>
<h3>Files</h3>
<table>
  <tr><th>File</th><th>Contents</th></tr>
  <tr><td>analysis/gap_summary.md</td><td>DOI / URL / fuzzy gap counts; filter misclassifications</td></tr>
  <tr><td>analysis/extraction_audit.md</td><td>Total extracted, missing DOIs, API errors, link method breakdown</td></tr>
  <tr><td>analysis/rule_improvement_opportunities.csv</td><td>Candidates that could be caught by new rules</td></tr>
  <tr><td>analysis/gap_analysis_doi_matched.csv</td><td>FLoRA rows not yet in extracted.csv, matched by DOI</td></tr>
  <tr><td>analysis/gap_analysis_url_matched.csv</td><td>FLoRA rows matched by URL</td></tr>
</table>
```

- [ ] **Step 7.4: Wire Old Pipeline stats panel**

Replace `function loadOldPipelineStats() { /* Task 7 */ }` with:

```javascript
function loadOldPipelineStats() {
  fetch('/api/dashboard/analysis-stats')
    .then(r => r.json())
    .then(d => {
      const el = document.getElementById('stats-old-pipeline');
      const gap = d.gap_summary || {};
      const audit = d.audit || {};
      el.innerHTML = `
        <div class="stat-card">
          <h3>Gap summary ${gap.generated ? '— ' + gap.generated : ''}</h3>
          <div class="stat-row"><span>DOI-matched gaps</span><span class="stat-val">${fmt(gap.doi_gaps)}</span></div>
          <div class="stat-row"><span>URL-matched gaps</span><span class="stat-val">${fmt(gap.url_gaps)}</span></div>
          <div class="stat-row"><span>Fuzzy-matched gaps</span><span class="stat-val">${fmt(gap.fuzzy_gaps)}</span></div>
          <div class="stat-row"><span>Total gaps</span><span class="stat-val">${fmt(gap.total_gaps)}</span></div>
          <div class="stat-row"><span>Filter misclassifications</span><span class="stat-val">${fmt(gap.filter_misclassifications)}</span></div>
        </div>
        <div class="stat-card">
          <h3>Extraction audit ${audit.generated ? '— ' + audit.generated : ''}</h3>
          <div class="stat-row"><span>Total extracted</span><span class="stat-val">${fmt(audit.total_extracted)}</span></div>
          <div class="stat-row"><span>Missing DOI</span><span class="stat-val">${fmt(audit.missing_doi)}</span></div>
          <div class="stat-row"><span>API errors</span><span class="stat-val">${fmt(audit.api_errors)}</span></div>
          <div class="stat-row"><span>Target pending</span><span class="stat-val">${fmt(audit.target_pending)}</span></div>
        </div>`;
    });
}
```

- [ ] **Step 7.5: Commit**

```
git add validate/templates/dashboard.html
git commit -m "feat: supabase + old-pipeline tabs docs and stats panels"
```

---

## Task 8: Download endpoint + clickable stat cards

**Files:**
- Modify: `validate/routes/dashboard.py`
- Modify: `tests/test_validate.py`

- [ ] **Step 8.1: Write failing tests**

Add to `tests/test_validate.py`:

```python
def test_download_invalid_stage(client):
    rv = client.get("/api/dashboard/download?stage=bad&col=x&val=y")
    assert rv.status_code == 400

def test_download_missing_csv(client):
    rv = client.get("/api/dashboard/download?stage=filtered&col=filter_status&val=replication")
    assert rv.status_code == 404
```

- [ ] **Step 8.2: Run to verify they fail**

```
pytest tests/test_validate.py::test_download_invalid_stage tests/test_validate.py::test_download_missing_csv -v
```
Expected: both FAIL.

- [ ] **Step 8.3: Add imports to `validate/routes/dashboard.py`**

At the top of the file add these imports (if not already present):

```python
import datetime
import re

from flask import Blueprint, jsonify, render_template, request, send_file
```

- [ ] **Step 8.4: Add `api_download` to `validate/routes/dashboard.py`**

Add after `api_search_stats`:

```python
@dashboard_bp.route("/api/dashboard/download")
def api_download():
    """Stream a filtered subset of a pipeline CSV as a file download.

    Query params:
      stage — candidates | filtered | extracted | extracted-test
      col   — column name to filter on (ignored when val == '*')
      val   — column value to match; '*' downloads the entire CSV
    """
    stage = request.args.get("stage", "").strip()
    col   = request.args.get("col",   "").strip()
    val   = request.args.get("val",   "").strip()

    path_map = {
        "candidates":    DATA_DIR / "candidates.csv",
        "filtered":      DATA_DIR / "filtered.csv",
        "extracted":     DATA_DIR / "extracted.csv",
        "extracted-test": DATA_DIR / "extracted-test.csv",
    }
    if stage not in path_map:
        return jsonify({"error": "invalid stage"}), 400

    csv_path = path_map[stage]
    if not csv_path.exists():
        return jsonify({"error": "file not found"}), 404

    download_dir = DATA_DIR / "dashboard" / "download"
    download_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.date.today().isoformat()
    if val == "*":
        filename = f"{stage}_all_{date_str}.csv"
        out_path = download_dir / filename
        # Stream entire file — copy in chunks to avoid OOM
        with open(csv_path, "rb") as src, open(out_path, "wb") as dst:
            while chunk := src.read(1 << 20):  # 1 MB chunks
                dst.write(chunk)
    else:
        safe_val = re.sub(r"[^\w\-]", "_", val)[:40]
        filename  = f"{stage}_{col}_{safe_val}_{date_str}.csv"
        out_path  = download_dir / filename
        chunks = []
        for chunk in pd.read_csv(
            csv_path, encoding="utf-8-sig", dtype=str,
            chunksize=50_000, on_bad_lines="skip",
        ):
            chunk = chunk.fillna("")
            if col in chunk.columns:
                chunks.append(chunk[chunk[col] == val])
        result = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
        result.to_csv(out_path, index=False, encoding="utf-8-sig")

    return send_file(str(out_path), as_attachment=True, download_name=filename,
                     mimetype="text/csv")
```

- [ ] **Step 8.5: Run tests**

```
pytest tests/test_validate.py::test_download_invalid_stage tests/test_validate.py::test_download_missing_csv -v
```
Expected: both PASS.

- [ ] **Step 8.6: Commit**

```
git add validate/routes/dashboard.py tests/test_validate.py
git commit -m "feat: /api/dashboard/download endpoint for filtered CSV export"
```

---

## Task 9: Check route + search/download endpoints

**Files:**
- Modify: `validate/routes/check.py`
- Modify: `tests/test_validate.py`

- [ ] **Step 9.1: Write failing tests**

Add to `tests/test_validate.py`:

```python
def test_check_search_invalid_stage(client):
    rv = client.get("/api/check/search?stage=bad")
    assert rv.status_code == 400

def test_check_search_missing_csv(client):
    rv = client.get("/api/check/search?stage=extracted")
    data = rv.get_json()
    assert data["total"] == 0
    assert data["rows"] == []

def test_check_download_invalid_stage(client):
    rv = client.get("/api/check/download?stage=bad")
    assert rv.status_code == 400
```

- [ ] **Step 9.2: Run to verify they fail**

```
pytest tests/test_validate.py::test_check_search_invalid_stage tests/test_validate.py::test_check_search_missing_csv tests/test_validate.py::test_check_download_invalid_stage -v
```
Expected: all FAIL.

- [ ] **Step 9.3: Replace `validate/routes/check.py` with full implementation**

```python
"""
check.py — Check tab: filter + search over any pipeline CSV.

Routes:
  GET /check                  → check page
  GET /api/check/search       → filtered/paginated rows as JSON
  GET /api/check/download     → filtered rows as CSV attachment
"""
import datetime
import re
from pathlib import Path

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import DATA_DIR

check_bp = Blueprint("check", __name__)

_STAGES = {
    "candidates":     DATA_DIR / "candidates.csv",
    "filtered":       DATA_DIR / "filtered.csv",
    "extracted":      DATA_DIR / "extracted.csv",
    "extracted-test": DATA_DIR / "extracted-test.csv",
}

# Per-stage column names for the "type" filter param
_TYPE_COL = {
    "candidates":     None,
    "filtered":       "filter_status",
    "extracted":      "type",
    "extracted-test": "type",
}


def _apply_filters(chunk: pd.DataFrame, stage: str, params: dict) -> pd.DataFrame:
    """Apply all active filter params to one chunk. Returns filtered chunk."""
    year_from = params.get("year_from", "")
    year_to   = params.get("year_to",   "")
    outcome   = params.get("outcome",   "")
    link_method  = params.get("link_method",  "")
    match_type   = params.get("match_type",   "")
    doi_verified = params.get("doi_verified", "")
    source       = params.get("source",       "")
    type_val     = params.get("type_val",     "")
    q            = params.get("q",            "")

    if year_from and "year_r" in chunk.columns:
        chunk = chunk[chunk["year_r"].apply(
            lambda y: y.isdigit() and int(y) >= int(year_from)
        )]
    if year_to and "year_r" in chunk.columns:
        chunk = chunk[chunk["year_r"].apply(
            lambda y: y.isdigit() and int(y) <= int(year_to)
        )]

    type_col = _TYPE_COL.get(stage)
    if type_val and type_col and type_col in chunk.columns:
        chunk = chunk[chunk[type_col] == type_val]

    for col, val in [
        ("outcome",            outcome),
        ("link_method",        link_method),
        ("original_match_type", match_type),
        ("doi_o_verification", doi_verified),
        ("source",             source),
    ]:
        if val and col in chunk.columns:
            chunk = chunk[chunk[col] == val]

    if q:
        mask = pd.Series(False, index=chunk.index)
        if "doi_r" in chunk.columns:
            mask |= chunk["doi_r"].str.lower().str.contains(q, na=False)
        if "title_r" in chunk.columns:
            mask |= chunk["title_r"].str.lower().str.contains(q, na=False)
        chunk = chunk[mask]

    return chunk


def _read_filtered(stage: str, params: dict) -> pd.DataFrame:
    """Read the stage CSV in chunks, applying all filters. Returns combined DataFrame."""
    path = _STAGES[stage]
    if not path.exists():
        return pd.DataFrame()

    chunks = []
    for chunk in pd.read_csv(
        path, encoding="utf-8-sig", dtype=str,
        chunksize=50_000, on_bad_lines="skip",
    ):
        chunk = chunk.fillna("")
        filtered = _apply_filters(chunk, stage, params)
        if not filtered.empty:
            chunks.append(filtered)

    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


def _extract_params() -> dict:
    return {
        "year_from":   request.args.get("year_from", "").strip(),
        "year_to":     request.args.get("year_to",   "").strip(),
        "type_val":    request.args.get("type",       "").strip(),
        "outcome":     request.args.get("outcome",    "").strip(),
        "link_method": request.args.get("link_method","").strip(),
        "match_type":  request.args.get("match_type", "").strip(),
        "doi_verified":request.args.get("doi_verified","").strip(),
        "source":      request.args.get("source",     "").strip(),
        "q":           request.args.get("q",           "").strip().lower(),
    }


@check_bp.route("/check")
def check_page():
    return render_template("check.html", active_page="check")


@check_bp.route("/api/check/search")
def api_check_search():
    stage = request.args.get("stage", "extracted").strip()
    if stage not in _STAGES:
        return jsonify({"error": "invalid stage"}), 400

    page     = max(1, int(request.args.get("page",     1)))
    per_page = min(100, max(10, int(request.args.get("per_page", 25))))

    df = _read_filtered(stage, _extract_params())
    total  = len(df)
    pages  = max(1, (total + per_page - 1) // per_page) if total else 1
    page   = min(page, pages)
    start  = (page - 1) * per_page
    rows   = df.iloc[start : start + per_page].to_dict("records") if not df.empty else []

    return jsonify({"total": total, "pages": pages, "page": page, "rows": rows})


@check_bp.route("/api/check/download")
def api_check_download():
    stage = request.args.get("stage", "extracted").strip()
    if stage not in _STAGES:
        return jsonify({"error": "invalid stage"}), 400

    df = _read_filtered(stage, _extract_params())

    download_dir = DATA_DIR / "dashboard" / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    filename = f"check_{stage}_{date_str}.csv"
    out_path = download_dir / filename

    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return send_file(str(out_path), as_attachment=True,
                     download_name=filename, mimetype="text/csv")
```

- [ ] **Step 9.4: Run tests**

```
pytest tests/test_validate.py::test_check_search_invalid_stage tests/test_validate.py::test_check_search_missing_csv tests/test_validate.py::test_check_download_invalid_stage -v
```
Expected: all PASS.

- [ ] **Step 9.5: Commit**

```
git add validate/routes/check.py tests/test_validate.py
git commit -m "feat: check route with /api/check/search and /api/check/download"
```

---

## Task 10: Check template

**Files:**
- Modify: `validate/templates/check.html`

- [ ] **Step 10.1: Write failing test**

Add to `tests/test_validate.py`:

```python
def test_check_page_has_filter_bar(client):
    rv = client.get("/check")
    html = rv.data.decode()
    assert 'id="stage-select"' in html
    assert 'id="search-input"' in html
    assert 'id="results-table"' in html
```

- [ ] **Step 10.2: Run to verify it fails**

```
pytest tests/test_validate.py::test_check_page_has_filter_bar -v
```
Expected: FAIL — stub template has none of those elements.

- [ ] **Step 10.3: Replace `validate/templates/check.html` with full implementation**

```html
{% extends "base.html" %}
{% block title %}FLoRA — Check{% endblock %}

{% block head %}
<style>
.check-page { max-width: 1300px; margin: 0 auto; padding: 16px; }

/* ── Filter bar ──────────────────────────────────────────────────────────────── */
.filter-bar {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px; margin-bottom: 14px;
}
.filter-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 10px; }
.filter-row:last-child { margin-bottom: 0; }
.filter-group { display: flex; flex-direction: column; gap: 4px; }
.filter-group label { font-size: 10px; font-weight: 700; text-transform: uppercase; color: var(--text-faint); }
.filter-group select, .filter-group input {
  padding: 5px 8px; border: 1px solid var(--border); border-radius: 5px;
  background: var(--surface-2); color: var(--text); font-size: 12px;
}
.filter-group select { min-width: 140px; }
.filter-group input  { min-width: 240px; }
.filter-group.grow   { flex: 1; }
.filter-group.grow input { width: 100%; }

.btn-primary {
  padding: 6px 16px; background: var(--accent); color: #fff;
  border: none; border-radius: 5px; font-size: 12px; font-weight: 600;
  cursor: pointer; transition: background .15s; white-space: nowrap;
}
.btn-primary:hover { background: var(--accent-hover); }
.btn-outline {
  padding: 6px 14px; background: none; border: 1px solid var(--border);
  color: var(--text-muted); border-radius: 5px; font-size: 12px;
  cursor: pointer; transition: border-color .15s, color .15s;
}
.btn-outline:hover { border-color: var(--accent); color: var(--accent); }

/* ── Results header ──────────────────────────────────────────────────────────── */
.results-header {
  display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
  flex-wrap: wrap;
}
.result-count { font-size: 13px; font-weight: 700; color: var(--text); }
.result-count span { color: var(--text-muted); font-weight: 400; }

/* ── Results table ───────────────────────────────────────────────────────────── */
.results-wrap {
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  overflow: hidden;
}
#results-table { width: 100%; border-collapse: collapse; }
#results-table th {
  text-align: left; padding: 8px 12px; font-size: 10px; font-weight: 700;
  text-transform: uppercase; color: var(--text-faint); letter-spacing: .4px;
  background: var(--table-head); border-bottom: 1px solid var(--border);
}
#results-table td { padding: 7px 12px; border-bottom: 1px solid var(--border); font-size: 12px; vertical-align: top; }
#results-table tr.data-row { cursor: pointer; transition: background .1s; }
#results-table tr.data-row:hover { background: var(--row-hover); }
#results-table tr.data-row:last-child td { border-bottom: none; }
#results-table tr.expand-row td {
  background: var(--row-expand); font-size: 11px; color: var(--text-muted);
  padding: 10px 12px; border-bottom: 1px solid var(--border);
}
.doi-cell { font-family: monospace; font-size: 11px; color: var(--text-muted); }
.title-cell { max-width: 300px; }
.badge {
  display: inline-block; border-radius: 4px; padding: 1px 7px;
  font-size: 10px; font-weight: 700; text-transform: lowercase;
}
.badge-success    { background: #dcfce7; color: #166534; }
.badge-failure    { background: #fee2e2; color: #991b1b; }
.badge-mixed      { background: #fef9c3; color: #854d0e; }
.badge-descriptive{ background: #e0f2fe; color: #0369a1; }
.badge-verified   { background: #dcfce7; color: #166534; }
.badge-corrected  { background: #fef9c3; color: #854d0e; }
.badge-mismatch   { background: #fee2e2; color: #991b1b; }
.badge-default    { background: var(--surface-2); color: var(--text-muted); }

.expand-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 6px 16px; }
.expand-item { display: flex; flex-direction: column; }
.expand-key { font-size: 10px; font-weight: 700; text-transform: uppercase; color: var(--text-faint); }
.expand-val { font-size: 11px; color: var(--text); word-break: break-all; }

/* ── Pagination ──────────────────────────────────────────────────────────────── */
.pagination { display: flex; gap: 4px; align-items: center; padding: 10px 12px; justify-content: center; }
.page-btn {
  padding: 4px 10px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text-muted); border-radius: 4px; font-size: 11px; cursor: pointer;
}
.page-btn:hover, .page-btn.active { border-color: var(--accent); color: var(--accent); }
.page-btn:disabled { opacity: .4; cursor: default; }

.null-note { color: var(--text-faint); font-size: 12px; font-style: italic; padding: 20px; text-align: center; }
</style>
{% endblock %}

{% block content %}
<div class="check-page">

  <!-- Filter bar -->
  <div class="filter-bar">
    <div class="filter-row">
      <div class="filter-group">
        <label>Stage</label>
        <select id="stage-select" onchange="onStageChange()">
          <option value="extracted">extracted</option>
          <option value="extracted-test">extracted-test</option>
          <option value="filtered">filtered</option>
          <option value="candidates">candidates</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Year from</label>
        <input type="number" id="year-from" placeholder="2011" min="1950" max="2030" style="width:90px">
      </div>
      <div class="filter-group">
        <label>Year to</label>
        <input type="number" id="year-to" placeholder="2026" min="1950" max="2030" style="width:90px">
      </div>
      <div class="filter-group" id="type-group">
        <label>Type</label>
        <select id="type-select">
          <option value="">All</option>
          <option value="replication">Replication</option>
          <option value="reproduction">Reproduction</option>
        </select>
      </div>
      <div class="filter-group" id="outcome-group">
        <label>Outcome</label>
        <select id="outcome-select">
          <option value="">All</option>
          <option value="success">Success</option>
          <option value="failure">Failure</option>
          <option value="mixed">Mixed</option>
          <option value="descriptive">Descriptive</option>
          <option value="cannot_be_determined">Cannot determine</option>
          <option value="uninformative">Uninformative</option>
          <option value="pending">Pending</option>
          <option value="api_error">API error</option>
        </select>
      </div>
      <div class="filter-group" id="link-group">
        <label>Link method</label>
        <select id="link-select">
          <option value="">All</option>
          <option value="author_year_match">Author-year match</option>
          <option value="llm_abstract">LLM abstract</option>
          <option value="llm_fulltext">LLM fulltext</option>
          <option value="target_pending">Target pending</option>
          <option value="no_original_found">No original found</option>
          <option value="api_error">API error</option>
        </select>
      </div>
      <div class="filter-group" id="doi-verif-group">
        <label>DOI verified</label>
        <select id="doi-verif-select">
          <option value="">All</option>
          <option value="verified">Verified</option>
          <option value="corrected">Corrected</option>
          <option value="mismatch">Mismatch</option>
          <option value="no_doi">No DOI</option>
          <option value="not_found">Not found</option>
          <option value="skipped">Skipped</option>
          <option value="api_error">API error</option>
        </select>
      </div>
      <div class="filter-group" id="match-type-group">
        <label>Original match</label>
        <select id="match-type-select">
          <option value="">All</option>
          <option value="single_original">Single original</option>
          <option value="multiple_match">Multiple match</option>
          <option value="multiple_original">Multiple original</option>
        </select>
      </div>
    </div>
    <div class="filter-row">
      <div class="filter-group grow">
        <label>Search by DOI or title</label>
        <input type="text" id="search-input" placeholder="Type to search…" onkeydown="if(event.key==='Enter')doSearch()">
      </div>
      <button class="btn-primary" onclick="doSearch()">Search</button>
      <button class="btn-outline" onclick="clearFilters()">Clear</button>
    </div>
  </div>

  <!-- Results header -->
  <div class="results-header" id="results-header" style="display:none">
    <div class="result-count" id="result-count"></div>
    <a id="download-btn" class="btn-outline" href="#" onclick="downloadResults(event)">⬇ Download</a>
  </div>

  <!-- Results table -->
  <div class="results-wrap" id="results-wrap" style="display:none">
    <table id="results-table">
      <thead id="table-head"></thead>
      <tbody id="table-body"></tbody>
    </table>
    <div class="pagination" id="pagination"></div>
  </div>

  <div id="empty-msg" class="null-note" style="display:none">No results found.</div>
  <div id="loading-msg" class="null-note" style="display:none">Searching…</div>

</div>
{% endblock %}

{% block scripts %}
<script>
let currentPage = 1;
let currentTotal = 0;
let currentPages = 0;

// Show/hide filters by stage
function onStageChange() {
  const stage = document.getElementById('stage-select').value;
  const extracted = ['extracted','extracted-test'].includes(stage);
  const filtered  = stage === 'filtered';
  toggle('outcome-group',    extracted);
  toggle('link-group',       extracted);
  toggle('doi-verif-group',  extracted);
  toggle('match-type-group', extracted);
  toggle('type-group',       extracted || filtered);
}
function toggle(id, show) {
  document.getElementById(id).style.display = show ? '' : 'none';
}

function clearFilters() {
  ['year-from','year-to','search-input'].forEach(id => document.getElementById(id).value = '');
  ['stage-select','type-select','outcome-select','link-select','doi-verif-select','match-type-select']
    .forEach(id => { document.getElementById(id).selectedIndex = 0; });
  document.getElementById('results-wrap').style.display = 'none';
  document.getElementById('results-header').style.display = 'none';
  document.getElementById('empty-msg').style.display = 'none';
  onStageChange();
}

function buildParams(page) {
  const p = new URLSearchParams();
  p.set('stage',       document.getElementById('stage-select').value);
  p.set('page',        page);
  p.set('per_page',    25);
  const yearFrom  = document.getElementById('year-from').value;
  const yearTo    = document.getElementById('year-to').value;
  const typeVal   = document.getElementById('type-select').value;
  const outcome   = document.getElementById('outcome-select').value;
  const link      = document.getElementById('link-select').value;
  const doiVerif  = document.getElementById('doi-verif-select').value;
  const matchType = document.getElementById('match-type-select').value;
  const q         = document.getElementById('search-input').value.trim();
  if (yearFrom)  p.set('year_from',    yearFrom);
  if (yearTo)    p.set('year_to',      yearTo);
  if (typeVal)   p.set('type',         typeVal);
  if (outcome)   p.set('outcome',      outcome);
  if (link)      p.set('link_method',  link);
  if (doiVerif)  p.set('doi_verified', doiVerif);
  if (matchType) p.set('match_type',   matchType);
  if (q)         p.set('q',            q);
  return p;
}

function doSearch(page) {
  page = page || 1;
  currentPage = page;
  document.getElementById('loading-msg').style.display = 'block';
  document.getElementById('results-wrap').style.display = 'none';
  document.getElementById('results-header').style.display = 'none';
  document.getElementById('empty-msg').style.display = 'none';

  fetch('/api/check/search?' + buildParams(page))
    .then(r => r.json())
    .then(data => {
      document.getElementById('loading-msg').style.display = 'none';
      currentTotal = data.total;
      currentPages = data.pages;

      if (!data.total) {
        document.getElementById('empty-msg').style.display = 'block';
        return;
      }
      renderResults(data);
    })
    .catch(() => {
      document.getElementById('loading-msg').style.display = 'none';
      document.getElementById('empty-msg').textContent = 'Search failed.';
      document.getElementById('empty-msg').style.display = 'block';
    });
}

const OUTCOME_BADGE = {
  success:'badge-success', failure:'badge-failure', mixed:'badge-mixed',
  descriptive:'badge-descriptive', corrected:'badge-corrected',
  mismatch:'badge-mismatch', verified:'badge-verified',
};
function badge(val) {
  if (!val) return '<span class="badge badge-default">—</span>';
  const cls = OUTCOME_BADGE[val] || 'badge-default';
  return `<span class="badge ${cls}">${val}</span>`;
}

const COMMON_COLS = ['doi_r','title_r','year_r','outcome','link_method','doi_o_verification'];

function renderResults(data) {
  // Header
  document.getElementById('results-header').style.display = 'flex';
  document.getElementById('result-count').innerHTML =
    `<strong>${data.total.toLocaleString()}</strong> <span>result${data.total === 1 ? '' : 's'}</span>`;

  // Table head
  document.getElementById('table-head').innerHTML =
    `<tr>${COMMON_COLS.map(c => `<th>${c}</th>`).join('')}</tr>`;

  // Table body
  const tbody = document.getElementById('table-body');
  tbody.innerHTML = '';
  data.rows.forEach((row, i) => {
    const tr = document.createElement('tr');
    tr.className = 'data-row';
    tr.dataset.idx = i;
    tr.innerHTML = `
      <td class="doi-cell">${row.doi_r || '—'}</td>
      <td class="title-cell">${(row.title_r || '—').slice(0, 80)}${(row.title_r||'').length > 80 ? '…' : ''}</td>
      <td>${row.year_r || '—'}</td>
      <td>${badge(row.outcome)}</td>
      <td><span class="badge badge-default">${row.link_method || '—'}</span></td>
      <td>${badge(row.doi_o_verification)}</td>`;
    tr.addEventListener('click', () => toggleExpand(tr, row, i));
    tbody.appendChild(tr);
  });

  document.getElementById('results-wrap').style.display = 'block';
  renderPagination(data.page, data.pages);
}

function toggleExpand(tr, row, idx) {
  const existing = document.getElementById('expand-' + idx);
  if (existing) { existing.remove(); return; }
  const expandTr = document.createElement('tr');
  expandTr.className = 'expand-row';
  expandTr.id = 'expand-' + idx;
  const allKeys = Object.keys(row).filter(k => !COMMON_COLS.includes(k));
  const items = allKeys
    .filter(k => row[k] !== '' && row[k] !== null)
    .map(k => `<div class="expand-item"><div class="expand-key">${k}</div><div class="expand-val">${row[k]}</div></div>`)
    .join('');
  expandTr.innerHTML = `<td colspan="${COMMON_COLS.length}"><div class="expand-grid">${items || 'No additional fields.'}</div></td>`;
  tr.after(expandTr);
}

function renderPagination(page, pages) {
  const el = document.getElementById('pagination');
  if (pages <= 1) { el.innerHTML = ''; return; }
  const btns = [];
  btns.push(`<button class="page-btn" onclick="doSearch(${page - 1})" ${page <= 1 ? 'disabled' : ''}>← Prev</button>`);
  const lo = Math.max(1, page - 2), hi = Math.min(pages, page + 2);
  if (lo > 1) btns.push(`<button class="page-btn" onclick="doSearch(1)">1</button>${lo > 2 ? '<span>…</span>' : ''}`);
  for (let p = lo; p <= hi; p++)
    btns.push(`<button class="page-btn${p === page ? ' active' : ''}" onclick="doSearch(${p})">${p}</button>`);
  if (hi < pages) btns.push(`${hi < pages - 1 ? '<span>…</span>' : ''}<button class="page-btn" onclick="doSearch(${pages})">${pages}</button>`);
  btns.push(`<button class="page-btn" onclick="doSearch(${page + 1})" ${page >= pages ? 'disabled' : ''}>Next →</button>`);
  el.innerHTML = btns.join('');
}

function downloadResults(e) {
  e.preventDefault();
  window.location.href = '/api/check/download?' + buildParams(1);
}

// Init
onStageChange();
</script>
{% endblock %}
```

- [ ] **Step 10.4: Run all tests**

```
pytest tests/test_validate.py -v
```
Expected: all PASS.

- [ ] **Step 10.5: Smoke test in browser**

Start app: `python -m validate.app`

1. Open `http://localhost:5001/dashboard` — verify all 6 sub-tabs switch correctly, hash updates in URL, stats load (or show null note).
2. Click a stat card on Filter tab — verify browser downloads a CSV.
3. Open `http://localhost:5001/check` — verify filter bar shows, stage dropdown filters visible controls, search returns rows from any CSV that exists.
4. Click a result row — verify it expands to show all fields.
5. Click "Download" — verify a CSV file is downloaded.

- [ ] **Step 10.6: Final commit**

```
git add validate/templates/check.html tests/test_validate.py
git commit -m "feat: check page template with filter bar, results table, row expansion, pagination"
```

---

## Self-review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| Nav: Dashboard + Check only | Task 1 |
| 6 dashboard sub-tabs, client-side, hash persistence | Task 2 |
| 40/60 docs/stats split per tab | Task 2 |
| Search docs panel (keywords, flow, CLI, columns) | Task 4 |
| Search stats (total, no_doi, no_doi_or_url, no_abstract, by_source) | Task 3 + 4 |
| Filter docs panel (flow, rule logic, LLM prompt, CLI, columns) | Task 5 |
| Filter stats (total, by_status, by_method, by_confidence, repro subset) | Task 5 |
| Extract docs panel (flow, outcome keywords, CLI, columns) | Task 6 |
| Extract stats (reuse csv-stats extract_ keys) | Task 6 |
| Extract-Test docs + stats | Task 6 |
| Supabase docs + stats | Task 7 |
| Old Pipeline docs + stats | Task 7 |
| Clickable stat cards → download | Task 8 |
| `/api/dashboard/download` endpoint | Task 8 |
| `data/dashboard/download/` directory | Task 8 |
| Old blueprints unregistered from app.py | Task 1 |
| Check tab: filter bar (stage, year, type, outcome, link_method, match_type, doi_verified, source, q) | Task 10 |
| Check tab: expandable results table | Task 10 |
| Check tab: pagination (25/page) | Task 10 |
| Check tab: download button | Task 10 |
| `/api/check/search` endpoint | Task 9 |
| `/api/check/download` endpoint | Task 9 |

**All spec requirements covered. No gaps found.**
