# Parquet Cache & Stats JSON

`shared/dashboard_cache.py` maintains two fast-read artefacts so the dashboard never needs to scan large CSVs at request time.

---

## Files produced

| File | Location | Purpose |
| ---- | -------- | ------- |
| `stats.json` | `data/dashboard/stats.json` | Pre-computed counts for all KPI cards |
| `{stage}.parquet` | `data/dashboard/{stage}.parquet` | Columnar copy of each stage CSV (snappy-compressed) |

`data/dashboard/` is created automatically on first write.

---

## When files are updated

Every pipeline runner calls `refresh(stage)` in a `try/finally` block so the cache is updated even when a run is interrupted:

| Runner | Stage refreshed |
| ------ | --------------- |
| `search/run_search.py` | `candidates` |
| `filter/run_filter.py` | `filtered` |
| `extract/run_extract.py` | `extracted` or `extracted-test` |

`refresh(stage)` does two things in order:

1. **`write_parquet(stage)`** — reads the stage CSV in 50 k-row chunks and writes a Parquet file via `pyarrow.parquet.ParquetWriter` (snappy compression). Writes to a `.tmp.parquet` file first and atomically renames it on success, so a partial write never corrupts the live file.

2. **`update_stats(stage)`** — reads only the columns needed for stats from the Parquet file (or CSV fallback), computes counts, and merges the result into `stats.json`. The JSON key is the stage name with hyphens replaced by underscores, e.g. `extracted_test`.

You can also call `refresh` manually from a Python shell:

```python
from shared.dashboard_cache import refresh
refresh("candidates")   # or "filtered", "extracted", "extracted-test"
```

---

## Stats computed per stage

### candidates

| Key | Meaning |
| --- | ------- |
| `total` | Row count |
| `no_doi` | Rows where `doi_r` is blank |
| `no_doi_or_url` | Rows where both `doi_r` and `url_r` are blank |
| `no_abstract` | Rows where `abstract_r` is blank |
| `by_source` | `{source_name: count}` dict |

### filtered

| Key | Meaning |
| --- | ------- |
| `total` | Row count |
| `by_filter_status` | `{status: count}` for each `filter_status` value |
| `by_filter_method` | `{method: count}` |
| `by_filter_confidence` | `{level: count}` |
| `rep_repro_total` | Rows where `filter_status` is `replication` or `reproduction` |
| `rep_repro_no_doi` | Rep+repro rows with blank `doi_r` |
| `rep_repro_no_doi_or_url` | Rep+repro rows with blank `doi_r` AND blank `url_r` |
| `rep_repro_no_abstract` | Rep+repro rows with blank `abstract_r` |

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

---

## Read cascade

The dashboard `api_csv_stats` endpoint tries sources in order:

1. **`stats.json`** — instant; loaded with `load_stats()`. Used when all four stages are present.
2. **Parquet** — `pq.read_table(path, columns=[...]).to_pandas()`. Column-only read, typically < 1 s even for 1 M rows.
3. **Chunked CSV** — `pd.read_csv(..., chunksize=50_000, usecols=...)`. Slowest; used only when both faster sources are absent.

The Check page (`/api/check/search` and `/api/check/download`) also tries Parquet before falling back to CSV.

---

## Rebuilding manually

If a Parquet file or `stats.json` becomes stale (e.g. rows were edited directly in the CSV):

```bash
# Rebuild Parquet + stats for one stage
python -c "from shared.dashboard_cache import refresh; refresh('extracted')"

# Or call each step separately
python -c "from shared.dashboard_cache import write_parquet, update_stats; write_parquet('filtered'); update_stats('filtered')"
```
