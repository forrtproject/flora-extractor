# Dashboard Guide

The monitoring dashboard lives at `http://localhost:5001/dashboard` (start with `python -m validate.app`).

---

## Tabs

| Tab | Stage | Data source |
| --- | ----- | ----------- |
| Search | Stage 1 | The survivor pool (`cache/snapshot_pool/`) + `data/dashboard/stats.json` |
| Filter | Stage 2 | `data/filtered.csv` |
| Extract | Stage 3 | `data/extracted.csv` |
| Extract-Test | Stage 3 sandbox | `data/extracted-test.csv` |
| Supabase | Stage 4 | Live Supabase API |

After those five comes one **set-aside** sub-tab per quarantine file, built from
`SET_ASIDE_DESTINATIONS` in `shared/schema.py` and served by
`/api/dashboard/set-stats`, `/set-rows` and `/set-download`. The tab strip therefore
grows when a new set-aside destination is added, with no template change.

Stats are served via a 3-tier cascade (fastest to slowest):

1. `data/dashboard/stats.json` — pre-computed at end of each pipeline run
2. `data/dashboard/{stage}.parquet` — Parquet mirror written alongside stats.json
3. Full CSV scan — fallback when neither exists

See [parquet-cache.md](parquet-cache.md) for how the cache is generated and refreshed.

---

## Search Tab (Stage 1)

Stage 1's artifact is the **survivor pool**, a parquet dataset, not a CSV — so this
tab reads the pool directly and has nothing to download.

### Search — stats

| Card | What it shows | Source |
| ---- | ------------- | ------ |
| Survivors in Pool | Rows across all pool partitions | Parquet footers, read live |
| Partitions | Pool files on disk (one per snapshot partition) | Live |
| Pool on Disk | Total bytes of the pool | Live |
| No DOI | Survivors with a blank or null `doi` | `stats.json` |

**Why the gate kept the row** — counts of the three `hit_*` booleans
(`hit_token_title`, `hit_token_abstract`, `hit_concept`). A row can hit several arms,
so these sum to more than the survivor count.

**Survivors by Publication Year** — from the pool's `publication_year` column.

The first three cards are cheap (parquet footers only) and are read on every request
from whichever machine serves the dashboard. The last card and both breakdowns read
columns off every partition, which is a refresh-time cost, so they are served from
`data/dashboard/stats.json` — written by `dashboard_cache.refresh("pool")` on the
machine that holds the pool.

**When there is no pool.** The pool is several GB and gitignored, so a fresh checkout
has none. The tab then shows an explicit "Survivor pool not available here" panel
naming the directory it looked in and how to get one
(`python -m search.pool_sync --pull`), and every card reads `—`. It never renders a
zero, which would read as "the search found nothing".

### Search — docs panel

Left panel covers: what Stage 1 does, the two arms of the search gate, the CLI flags,
how to pull the pool instead of scanning, and the pool's columns. It is hand-written
copy, not generated from the code — where it disagrees with
`search/run_search.py --help` or `_POOL_SCHEMA`, those win.

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

The left panel explains what Stage 2 does and annotates the `filtered.csv` columns.

**Stale panel copy.** The text still describes the pre-#152 Stage 2 — a per-row rule
classifier with an LLM escalation, its prompt reproduced inline, and its CLI flags.
None of that exists: Stage 2 is now the declarative filter engine
(`python -m filter.engine …`), where rules route and discard and only LLM tiers admit.
Read [filter-engine.md](filter-engine.md) and [cli-reference.md](cli-reference.md)
instead, and treat the panel's Stage 2 prose as historical until it is rewritten.

The column annotations are likewise a copy: `ENGINE_EXPORTED_COLS` in
`shared/schema.py` is the actual set the handoff writes, and
[csv-schema.md](csv-schema.md) is the maintained reference.

The `false_positive` card counts rows that carry that status. Note that
`filter.engine handoff` never writes one — `false_positive` is the `discard` pile's
status and the handoff ships only the two screen piles — so on a file produced by the
current engine that card reads zero.

---

## Extract Tab (Stage 3)

### Extract — stats

- **Extracted ↓** / **Target Pending** KPI cards at the top
- **Match Types** — each row downloads that match-type subset of `extracted.csv`
- **LLM Model** — Gemini / GPT / Qwen / Other / Rule-based breakdown (display only)
- **Link Method** — each row downloads that link-method subset
- **Outcome Distribution** — donut chart; each legend entry downloads that outcome subset

### Extract — docs panel

Covers: what Stage 3 does, CLI flags, the link pipeline, the 6 PDF parse methods and
the scoring formula, and 29 of `extracted.csv`'s columns grouped into labeled
sections. (`EXTRACTED_COLS` is 52 columns, so the panel is a selection.)

**Stale panel copy, same as the Filter panel.** The link pipeline it draws is
`author_year → llm_abstract → llm_fulltext → target_pending`. Neither `author_year`
nor `llm_abstract` is a `link_method` value: `author_year_match` survives only as
`author_year_match_legacy`, and the abstract rung writes `llm_cited_candidates`. The
live ladder is title-pattern → citation/candidate rule → `llm_cited_candidates` →
`llm_references` → `llm_title_search` → `llm_fulltext`. Read
[csv-schema.md](csv-schema.md) for the real vocabulary; the panel is HTML, not code,
and nothing depends on it.

---

## Extract-Test Tab (Stage 3 sandbox)

Same layout as the Extract tab but reads `extracted-test.csv`, which is what
`python -m extract.export --mode validation --out data/extracted-test.csv` renders
from a validation-mode run's verdicts.

**There is no Promote button, and no promotion step.** The dashboard is read-only,
and a validation-mode verdict is promoted by re-running the work live — near-free,
because every LLM answer it needs is already cached:

```bash
python -m extract.tier --run --only <work id>   # live is the default mode
python -m extract.export
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

## Refreshing

Click **↺ Refresh** to reload pipeline stats without a page reload. Supabase data is cached in-process for 5 minutes.

---

## Downloadable rows

Most stat cards and stat rows in the dashboard are clickable download links. Clicking downloads a filtered CSV to `data/dashboard/download/` and serves it as a file attachment. The filename encodes the stage and filters, e.g. `check_filtered_2026-06-16.csv`.

Downloads go through `/api/check/download`, which reads from Parquet (fast) or falls back to chunked CSV. See [check-page.md](check-page.md) for all supported filter parameters.
