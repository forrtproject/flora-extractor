# Dashboard Guide

## Overview

The monitoring dashboard is at `http://localhost:5001/dashboard` (run `python -m validate.app`).

It has two tabs: **Pipeline** and **Validation**.

---

## Pipeline Tab

Shows stats from the local CSV files.

### Pipeline Progress KPIs

| KPI | Source | Description |
|-----|--------|-------------|
| Candidates | `candidates.csv` | Total papers discovered in Stage 1 |
| Filtered | `filtered.csv` | Papers surviving Stage 2 filter |
| Extracted | `extracted.csv` | Papers processed by Stage 3 |
| Target Pending | `extracted.csv` | Papers where `link_method = target_pending` — original DOI must be supplied manually |

### Filter Results

Breakdown of `filter_status` values in `filtered.csv`:
- **Replications** — direct replications
- **Reproductions** — close reproductions
- **Needs review** — uncertain cases passed to LLM
- **False positives** — excluded

### Match Types

How Stage 3 classified each paper:
- **Single original** — paper targets exactly one original study
- **Multiple match** — multiple candidate originals, Stage 3 resolved to one
- **Multiple original** — paper targets multiple independent originals (expands to N rows)

### Link Method

How the original study DOI was resolved:
- **Author/year match** — rule-based, no LLM
- **LLM fulltext** — resolved from full PDF text
- **LLM abstract** — resolved from abstract only
- **No original found** — pipeline could not identify an original
- **Target pending** — needs manual input
- **API error** — failed after retries

### LLM Model Used

Which model was used for DOI resolution:
- **Gemini** — via Google AI Studio
- **GPT** — via OpenAI
- **Qwen** — via OpenRouter
- **None** — rule-based resolution (no LLM)

### Outcome Distribution

Donut chart + breakdown for `outcome` column in `extracted.csv`. See [csv-schema.md](csv-schema.md) for outcome definitions.

### Extract Test Section

Same stats but for `extracted-test.csv` (the test sandbox). Rows promoted to production via the **Promote** button in the Extract Test tab or via CLI.

### Analysis Section

Links to the analysis scripts and their output files. Run these to understand gaps between what was extracted and what's in the FLoRA entry sheet.

---

## Validation Tab

Pulls live data from Supabase. Requires `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in `.env`.

If Supabase is not configured, the tab shows a configuration notice.

### Validation KPIs

| KPI | Description |
|-----|-------------|
| Total Records | All records in the `unvalidated` Supabase table |
| Validated | Records with `validation_status = validated` |
| Unvalidated | Records not yet reviewed |
| In Progress | Currently being reviewed |
| Need Review | Flagged for additional attention |
| Judgements | Total completed validator assignments |
| Validators | Number of unique active validators |
| Agreement | % of queue assignments completed (validated / total) |

A progress bar shows overall validation completion.

### Correction Frequency

Bar chart showing how often each field was flagged incorrect across all validated records:
- **Type** — `replication` vs `reproduction` classification
- **Original DOI** — the linked original study was wrong
- **Outcome** — `success` / `failure` / `mixed` etc. classification

### Validated Outcomes

Donut chart of outcome distribution in the `validated` Supabase table.

### Incorrect DOI Drilldown

Paginated table of records where at least one validator flagged a correction.

**Filters:**
- **Outcome** — filter to a specific outcome value
- **Field** — filter to a specific field (type / original / outcome)

**Expanding a row** shows per-validator checks:
- `✓` = correct
- `✗` = incorrect
- Validator 1, Validator 2, and LLM validation columns

---

## Theme Toggle

Click the **🌙 Dark / ☀️ Light** button in the nav bar to switch themes. Preference is saved in `localStorage` and persists across page loads.

---

## Refreshing

Click **↺ Refresh** to reload stats without a full page refresh. The Pipeline tab reads CSVs directly (column-only scan, minimal memory). The Validation tab fetches from Supabase with a 5-minute cache.

---

## Performance Notes

- **CSV stats** use pandas column-only reads (`usecols=lambda c: c in (...)`) — loading only the columns needed. Even a 1M-row CSV takes < 2 seconds.
- **Index files** (`cache/candidates_index.txt`, `cache/filtered_index.txt`) let Stage 1/2 avoid loading the full CSVs on every run.
- **Supabase responses** are cached in-process for 5 minutes. Force a refresh by restarting the app or waiting for the TTL.
