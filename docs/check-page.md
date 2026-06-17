# Check Page

`/check` is a search-and-filter interface over any pipeline stage. It's useful for inspecting individual papers, cross-checking how the same DOI is classified at different stages, and downloading filtered subsets.

---

## Accessing it

```
http://localhost:5001/check
```

The page auto-searches on load and shows the first 25 rows of `extracted.csv`.

---

## Stage selection

Select one or more stages using the pill buttons at the top. When multiple stages are selected, the results are unioned and a **stage badge** (`CAND`, `FILT`, `EXT`, `TEST`) appears in the table so you can see how the same paper appears at each stage.

---

## Filters

All filter controls support selecting one or many values. Multi-select dropdowns show a count badge when values are selected; clicking **Clear** inside the panel resets that filter.

| Filter | Column(s) searched | Notes |
| ------ | ----------------- | ----- |
| Year range (From / To) | `year_r` | Handles float years like `2009.0` correctly |
| Type / Status | `type` (extracted) or `filter_status` (filtered) | Options change based on which stages are selected |
| Outcome | `outcome` | Extracted/test stages only |
| Link Method | `link_method` | Extracted/test stages only |
| Match Type | `original_match_type` | Extracted/test stages only |
| DOI Verified | `doi_o_verification` | Extracted/test stages only |
| Source | `source` | All stages |
| Search (text) | `doi_r`, `title_r`, `doi_o`, `title_o` | Case-insensitive substring match |

### Special boolean filters (URL params only)

These are not exposed in the UI but are used by dashboard download links:

| Param | Effect |
| ----- | ------ |
| `no_doi=1` | Only rows where `doi_r` is blank |
| `no_doi_url=1` | Only rows where both `doi_r` and `url_r` are blank |
| `no_abstract=1` | Only rows where `abstract_r` is blank |

---

## Table

The main table shows four fixed columns: `doi_r`, `title_r`, `doi_o`, `title_o`. All other columns are in the expanded view.

Click any row (or the `▶` button) to expand it. The expanded view groups columns into labelled sections (Replication Paper, Filter, Original Study, Link, DOI Verification, Outcome, Meta). Short fields are shown in a two-column grid; long-text fields (abstract, evidence, reasoning) are full-width with a collapsible "Show more" toggle. DOI values are clickable links. Enum values (outcome, filter_status, confidence, doi_o_verification, link_method, match type) render as color-coded badges.

---

## Pagination

- **Prev / Next** buttons page through results
- **Go to [N]** — type any page number and press Go or Enter to jump directly

Default page size is 25 rows; maximum is 100 (via `per_page` URL param).

---

## Downloading

**↓ Download CSV** in the filter bar downloads the current filtered results (all pages, not just the current page) to `data/dashboard/download/` and serves the file as an attachment.

Filename format: `check_{stage}_{date}.csv`. For multiple stages: `check_extracted+filtered_2026-06-16.csv`.

### API endpoint

```
GET /api/check/download?stage=extracted&outcome=failure&outcome=mixed
```

All filter params from the table above are supported. Multiple values for the same param are OR'd within that filter; filters across different params are AND'd.

### Example download URLs

```bash
# All failed extractions
/api/check/download?stage=extracted&outcome=failure

# Replications with no DOI (in filtered stage)
/api/check/download?stage=filtered&type=replication&no_doi=1

# Mismatched DOI verifications
/api/check/download?stage=extracted&doi_verified=mismatch

# Same paper across all stages
/api/check/search?stage=candidates&stage=filtered&stage=extracted&q=10.1126/science.1255484
```

---

## Backend

`validate/routes/check.py` handles both search and download. Key implementation details:

- Reads from Parquet (`data/dashboard/{stage}.parquet`) when available; falls back to chunked CSV scan (50 k rows at a time) when not.
- Year filter uses `int(float(y))` conversion to handle both `2009` and `2009.0` stored values.
- Multi-value params are parsed by `_get_list(name)` which accepts both repeated params (`?type=a&type=b`) and comma-separated values (`?type=a,b`).
- Multi-stage results are unioned with a `_stage` column inserted at position 0.
