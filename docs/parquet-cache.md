# Parquet Cache & Stats JSON

`shared/dashboard_cache.py` maintains two fast-read artefacts so the dashboard never needs to scan large CSVs at request time.

---

## Files produced

| File | Location | Purpose |
| ---- | -------- | ------- |
| `stats.json` | `data/dashboard/stats.json` | Pre-computed counts for all KPI cards |
| `{stage}.parquet` | `data/dashboard/{stage}.parquet` | Columnar copy of each stage CSV (snappy-compressed) |

`data/dashboard/` is created automatically on first write.

**Two CSV stages, and two whose artifact is not a CSV.** `_STAGE_CSV` in
`shared/dashboard_cache.py` holds `extracted` and `extracted-test`; those are the
only stages with a Parquet mirror. Stage 1 is the `pool` stage (`POOL_STAGE`): its
artifact is the survivor pool directory, and its stats are read off the pool's own
parquet partitions. Stage 2 is the `filtered` stage (`FILTERED_STAGE`): its artifact
is the routing store, and its stats are one DuckDB query. Neither gets a mirror.
`candidates` is not a stage any more.

---

## When files are updated

Every pipeline runner calls `refresh(stage)` in a `try/finally` block so the cache is updated even when a run is interrupted:

| Runner | Stage refreshed |
| ------ | --------------- |
| `search/run_search.py` | `pool` (with `pool_dir=`, because `--survivor-pool` may have sent the scan elsewhere) |
| `extract/export.py` | `extracted` or `extracted-test`, whichever it just wrote |

No runner refreshes `filtered`, and nothing needs to: the dashboard queries the
routing store on every load. `refresh("filtered")` writes the same figures into
`stats.json` for a reader that has the store but not the app.

`refresh(stage)` does two things in order — except for `pool` and `filtered`,
which are **stats-only**: the pool is already parquet and the routing store is
already a database, so there is no mirror to write and `refresh()` calls
`update_stats` alone. A stage name that is none of the four logs
"unknown stage — skipping" and
does nothing.

1. **`write_parquet(stage)`** — reads the stage CSV in 50 k-row chunks and writes a Parquet file via `pyarrow.parquet.ParquetWriter` (snappy compression). Writes to a `.tmp.parquet` file first and atomically renames it on success, so a partial write never corrupts the live file.

2. **`update_stats(stage)`** — reads only the columns needed for stats from the Parquet file (or CSV fallback), computes counts, and merges the result into `stats.json`. The JSON key is the stage name with hyphens replaced by underscores, e.g. `extracted_test`.

You can also call `refresh` manually from a Python shell:

```python
from shared.dashboard_cache import refresh
refresh("filtered")     # or "extracted", "extracted-test"
refresh("pool")         # reads the pool directory; writes no parquet mirror
```

---

## Stats computed per stage

### pool (Stage 1)

From `compute_pool_stats()`, which reads five narrow columns off every partition —
seconds to minutes over a full pool, so it is a refresh-time call, never a
per-request one. `pool_totals()` is the cheap half: row, file and byte counts read
from parquet footers only, memoised, safe on a web request.

| Key | Meaning |
| --- | ------- |
| row / file / byte totals | From `pool_totals()`, plus the `pool_dir` it read |
| `no_doi` | Pool rows with a blank or null `doi` |
| `by_year` | `{year: count}` from `publication_year` |
| `gate_hits` | `{title, abstract, concept: count}` — which arm of the search gate admitted the row (`hit_token_title`, `hit_token_abstract`, `hit_concept`) |

`None` when the pool is not on this machine, which is a normal state: the pool is
several GB and gitignored.

### filtered

One routing release — the newest the store holds routing for.

| Key | Meaning |
| --- | ------- |
| `available` | `False` when there is no store, no datable release, or the store is locked; `reason` then says which |
| `store` | Path to the DuckDB file the figures came from |
| `release_id` | The release these counts belong to |
| `release_created_at` | When that release was routed |
| `total` | Works routed, summed over the piles |
| `by_pile` | `{pile: count}` — `discard`, `screen_expensive`, `screen_cheap`, `needs_human`, `pending` |

Computed in two passes (`_compute_large_stage_stats`): a lightweight chunked pass
over `paper_type`/`filter_method`/`filter_confidence`/`filter_evidence`/`year_r`,
then a second pass that reads `doi_r`/`url_r`/`abstract_r` for the
replication+reproduction subset only, via parquet predicate pushdown.

### extracted / extracted-test

| Key | Meaning |
| --- | ------- |
| `total` | Row count |
| `target_pending_count` | Rows where `link_method = target_pending` |
| `by_match_type` | `{match_type: count}` |
| `by_link_method` | `{method: count}` — key set defined by `_METHOD_KEYS` constant |
| `by_model` | `{family: count}` where family is `gemini`, `gpt`, `qwen`, `other`, or `none` |
| `by_outcome` | `{outcome: count}` — key set defined by `_OUTCOME_KEYS` constant |
| `by_doi_verification` | `{status: count}` |
| `by_type` | `{replication\|reproduction: count}` |
| `by_outcome_replication` / `by_outcome_reproduction` | `by_outcome` split on `type`. Kept apart because the two record types use different outcome vocabularies, so one merged distribution is meaningless |
| `by_year` | `{year: count}` from `year_r` |

---

## Read cascade

The dashboard `api_csv_stats` endpoint tries sources in order:

1. **`stats.json`** — instant; loaded with `load_stats()`. Used when all three CSV
   stages are present in it.
2. **Live compute** for whichever of those stages is missing, through
   `dashboard_cache.compute_stage_stats(stage)`, so both paths share one
   implementation of every aggregation. That call reads **Parquet** where the mirror
   exists (`pq.read_table(path, columns=[...])` — a column-only read, typically < 1 s
   even for 1 M rows) and falls back to **chunked CSV** (`pd.read_csv(...,
   chunksize=100_000, usecols=...)`) where it does not.

The **pool** is never computed on a request: it is gigabytes of parquet.
`_stats_json_to_api` overlays the cheap footer-only count for the machine that has
one, and a machine without a pool shows the "not available here" panel rather than a
zero.

The Check page (`/api/check/search` and `/api/check/download`) also tries Parquet before falling back to CSV.

---

## Rebuilding manually

If a Parquet file or `stats.json` becomes stale (e.g. rows were edited directly in the CSV):

```bash
# Rebuild Parquet + stats for one stage
python -c "from shared.dashboard_cache import refresh; refresh('extracted')"

# Or call each step separately
python -c "from shared.dashboard_cache import write_parquet, update_stats; write_parquet('extracted'); update_stats('extracted')"
```
