# Stage 1: Search — Code Flow

**Entry point:** `python -m search.run_search`

## What it does

Discovers candidate papers from multiple sources and appends new ones to `data/candidates.csv`.

## Step-by-step

```
run_search.py
    │
    ├── load candidates index (cache/candidates_index.txt)
    │       If missing: build from existing candidates.csv in 50k-row chunks
    │
    ├── OpenAlex search (search/openalex_search.py)
    │       fetch_openalex_candidates()
    │           └── for each keyword phrase:
    │                   paginate OpenAlex /works with cursor
    │                   extract_row() → standardise to schema
    │                   cache each page to cache/openalex/
    │
    ├── External list scrapers (search/external_lists.py)
    │       fetch_bob_reed_list()    → Bob Reed replication database
    │       fetch_i4r_list()         → Institute for Replication list
    │
    ├── (Optional) Semantic Scholar search
    │       fetch_semantic_scholar_candidates()
    │
    ├── Deduplicate (search/deduplicate.py)
    │       merge_candidates(all_sources)
    │           → DOI-normalised set union
    │           → fuzzy title dedup (Jaccard)
    │           → cross-check against data/flora_entry_sheet.csv
    │
    └── _merge_into_candidates_csv(new_rows)
            filter rows already in index
            append to candidates.csv (utf-8-sig first write; utf-8 subsequent)
            update candidates index
```

## Large-file handling

The candidates index (`cache/candidates_index.txt`) stores all identifiers ever written. Key priority: `doi` → `oa:<openalex_id>` → `url:<url>` → `title:<lowercased title>`. Each row stores up to 4 keys so any identifier can detect a duplicate.

## Rate limits

- OpenAlex: 0.1 s between requests (polite pool)
- With `OPENALEX_API_KEY`: higher limits + authenticated content endpoint

## Key functions

| Function | File | Description |
|----------|------|-------------|
| `fetch_openalex_candidates()` | `search/openalex_search.py` | Main OpenAlex paginator |
| `_get_page()` | `search/openalex_search.py` | Single page fetch + disk cache |
| `extract_row()` | `search/openalex_search.py` | Map OpenAlex work → schema columns |
| `merge_candidates()` | `search/deduplicate.py` | Deduplicate across all sources |
| `_merge_into_candidates_csv()` | `search/run_search.py` | Append-only write with index |
