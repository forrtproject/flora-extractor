# Stage 1 — Search

**Input:** External APIs and curated lists  
**Output:** `data/candidates.csv`  
**Run:**

```bash
python -m search.run_search
```

Results are viewable in the **Search** tab of the Stage 4 web app (`http://localhost:5001/search`).

***

## Year range and max_per_phrase

Two knobs control how much **new** data is fetched in a run:

- **Year range** (`--from-year`, `--to-year`)  
  - Limits which publication years are requested.
  - Checkpoints are per *(phrase, year range)*, so changing the year range starts an independent job with its own cursor/offset files.  
  - The **cache harvest at the start of the run ignores year**: all previously cached pages (any year) are always merged into `candidates.csv` before new API calls.

- **`--max-per-phrase`**  
  - Caps the number of **new rows per phrase per run** from OpenAlex and Semantic Scholar.  
  - The cursor/offset is saved at a page boundary, so the next run continues from exactly where this one stopped (no rows are lost, you just spread ingestion over multiple runs).
  - It does **not** affect rows loaded from cache or existing rows in `candidates.csv`; those are always merged in first.

***

## API keys required

Set these in your `.env` file before running:

| Variable               | Required for            | Get one at                                                                 |
| ---------------------- | ----------------------- | -------------------------------------------------------------------------- |
| `RESEARCHER_EMAIL`     | OpenAlex (polite pool)  | Any valid email — used as `mailto=` in API requests                        |
| `SEMANTIC_SCHOLAR_KEY` | Semantic Scholar        | https://www.semanticscholar.org/product/api#api-key-form   |

***

## What This Stage Does

Discovers every academic paper that might be a replication or reproduction study. It casts a wide net across multiple sources, collects basic metadata for each paper, and writes a single deduplicated list of candidates. Stage 2 then decides which candidates are genuine.

The goal is **high recall over precision** — it is better to include a false positive here than to miss a real replication. Stage 2 will filter.

***

## Pipeline Flow

```
OpenAlex keyword search      ← primary bibliographic source (cursor-paginated)
Semantic Scholar search      ← supplementary bibliographic source (offset-paginated)
Bob Reed list scrape         ← external curated list (pluggable)
I4R list scrape              ← external curated list (pluggable)
         │
         ▼
  Cache harvest (all prior API pages, any year range)
         │
         ▼
  Merge all sources
         │
         ▼
  Deduplicate: openalex_id_r → doi_r → fuzzy title
         │
         ▼
  data/candidates.csv   (grows monotonically across runs)
```

The CSV is **append-only** — each run merges new results into the existing file and deduplicates rather than overwriting.

***

## Sources

### Primary — Bibliographic Databases

Each phrase in `SEARCH_PHRASES` is searched independently across both APIs.

| Source           | File                               | Method                                            |
| ---------------- | ---------------------------------- | ------------------------------------------------- |
| OpenAlex         | `search/openalex_search.py`        | Exact-phrase search via `/works` REST API         |
| Semantic Scholar | `search/semantic_scholar_search.py`| Relevance search via `/paper/search` REST API     |

**OpenAlex** uses `title_and_abstract.search:"<phrase>"` — exact-phrase matching, cursor-based pagination, 200 results per page, no hard result cap.

**Semantic Scholar** uses bag-of-words relevance search — results may include false positives for broad phrases. Hard cap of 1,000 results per query (offset ≤ 1,000). Authenticated access requires a free API key set via `SEMANTIC_SCHOLAR_KEY` in `.env`.

### Secondary — External Curated Lists (Pluggable)

Hand-maintained lists of known replications. Each is an independent loader in `search/external_lists.py`.

| Source        | Coverage         | Method                                |
| ------------- | ---------------- | ------------------------------------- |
| Bob Reed list | Economics        | Fetch ReplicationNetwork Google Sheet |
| I4R reports   | Multi-discipline | Scrape from IDEAS I4R catalogue |

***

## Search Phrases

Both OpenAlex and Semantic Scholar are queried with the same 24 phrases (expanded from the original 10 using the hackathon keyword list):

```
"replication of"
"replication study"
"direct replication"
"reproduction study"
"close replication"
"we replicated"
"conceptual replication"
"attempts to replicate"
"registered replication report"
"pre-registered replication"
"failed to replicate"
"we attempted to replicate"
"replication attempt"
"exact replication"
"high-powered replication"
"multi-site replication"
"replication and extension"
"reproducibility of"
"we reproduced"
"we aimed to replicate"
"aimed to replicate"
"preregistered replication"
"systematic replication"
"independent replication"
```

***

## Resumability and Caching

Both APIs use per-phrase checkpoint files so interrupted runs resume from the last saved position rather than restarting.

| Source           | Checkpoint file                          | Resumes on         |
| ---------------- | ---------------------------------------- | ------------------ |
| OpenAlex         | `cache/openalex/<hash>.cursor.json`      | Next cursor page   |
| Semantic Scholar | `cache/semantic_scholar/<hash>.offset.json` | Next offset page |

The checkpoint key includes the phrase and year range, so different year-range runs have independent checkpoints and do not interfere with each other.

Every raw API response page is also cached to disk as `<hash>.json`. At the start of each run, **all cached pages are harvested regardless of which year range was active when they were downloaded**, so results from previous runs are always incorporated into `candidates.csv` without re-fetching.

To wipe all checkpoints and start fresh:

```bash
python -m search.run_search --reset-cursors
```

***

## CLI Reference

| Flag | Description |
| --- | --- |
| `--from-year YYYY` | Earliest publication year to fetch (inclusive). |
| `--to-year YYYY` | Latest publication year to fetch (inclusive). |
| `--max-per-phrase N` | Limit new rows fetched per phrase per run for OpenAlex and S2. Checkpoints saved so the next run continues from the same page. |
| `--source SOURCE` | Only fetch from this source (repeatable). Values: `openalex`, `semantic_scholar`, `bob_reed`, `i4r`. Default: all sources. |
| `--reset-cursors` | Wipe all OpenAlex cursor and S2 offset checkpoint files, then start fresh. |
| `--auto-advance` | Incremental cycling mode — one phrase/year per invocation, advances via `cache/search_state.json`. |

```bash
# Incremental run: one phrase/year per invocation, cycling 2011–2021 (recommended for regular use)
python -m search.run_search --auto-advance --from-year 2011 --to-year 2021 --max-per-phrase 200

# Traditional batch: all phrases, restricted year range
python -m search.run_search --from-year 2015 --to-year 2023

# Only fetch from OpenAlex (skip Semantic Scholar, I4R, Bob Reed)
python -m search.run_search --source openalex --from-year 2020

# Only fetch I4R and Bob Reed lists (fast — no API pagination)
python -m search.run_search --source i4r --source bob_reed

# Only fetch Semantic Scholar for a specific year window
python -m search.run_search --source semantic_scholar --from-year 2018 --to-year 2022

# Quick smoke-test: 1 page per phrase from OpenAlex only
python -m search.run_search --source openalex --max-per-phrase 200

# Wipe all checkpoints and start fresh
python -m search.run_search --reset-cursors
```

**`--source`** is repeatable — `--source openalex --source i4r` fetches both. Sources not listed are silently skipped (logged at INFO level).

**`--auto-advance`** — incremental cycling mode. Reads `cache/search_state.json` to determine which phrase and year to process next, fetches up to `--max-per-phrase` rows for that one slot, then saves the updated state. On the next invocation it advances to the next phrase; after all phrases are done for a year it increments the year and wraps back to the first phrase. This spreads ingestion over many runs without any single run taking too long.

**`--max-per-phrase`** limits rows fetched *this run* per phrase without losing the checkpoint — the next run continues from the same page boundary.

**`--reset-cursors`** wipes all per-phrase checkpoint files so the next run starts from scratch.

***

## Deduplication

Handled in `search/deduplicate.py`. Applied in two places: within the new batch before merging, and again when merging into `candidates.csv`.

**Within the new batch** — `deduplicate_candidates(df)`:

- **Pass 0a — DOI-pattern exclusions:** drops rows whose DOI identifies a non-paper object:
  - `10.6084/` — figshare datasets and figures; never a scholarly paper.
  - `/reviews/` in the DOI path — PeerJ embeds peer-review texts as citable objects distinct from the reviewed article.
- **Pass 0b — Versioned preprint deduplication:** collapses DOIs ending in `_vN` (e.g. `_v1`, `_v2`). For each group sharing the same base DOI: if a canonical (non-versioned) DOI with that base already exists in the dataset, all versioned variants are dropped; otherwise, only the highest-version row is kept and lower versions are dropped. Surviving rows then flow through Pass 1.
- **Pass 1 — Exact DOI:** rows sharing the same `clean_doi()` value are collapsed; the row with the most non-empty fields is kept.
- **Pass 2 — Fuzzy title:** for rows without a DOI, `rapidfuzz.fuzz.token_sort_ratio` is computed between remaining title pairs. Pairs scoring ≥ 90 are collapsed.

**Merging into `candidates.csv`** — three-pass strategy to handle the different identifier coverage across sources:

1. Rows with `openalex_id_r` → deduplicate on `openalex_id_r`
2. Rows without `openalex_id_r` but with `doi_r` → deduplicate on `doi_r` (covers most S2 rows)
3. Rows with neither → deduplicate on lowercased `title_r` (best-effort fallback)

New rows win over existing rows on any key clash, so re-fetched metadata replaces stale entries.

***

## Output Schema — `candidates.csv`

| Column          | Type | Description                                               |
| --------------- | ---- | --------------------------------------------------------- |
| `doi_r`         | str  | Cleaned DOI (no `https://doi.org/` prefix)                |
| `title_r`       | str  | Paper title                                               |
| `abstract_r`    | str  | Abstract text                                             |
| `year_r`        | int  | Publication year                                          |
| `authors_r`     | str  | Semicolon-separated author names                          |
| `journal_r`     | str  | Journal name                                              |
| `url_r`         | str  | Open-access PDF URL if available                          |
| `openalex_id_r` | str  | OpenAlex work ID (e.g. `W2741809807`); `None` for S2-only rows |
| `source`        | str  | `openalex` / `semantic_scholar` / `bob_reed` / `i4r`     |
| `ref_r`         | str  | FLoRA display reference: `"Surname · Year · Journal"` built from first author surname, year, and journal |

Schema is defined in `shared/schema.py:CANDIDATES_COLS`. Never add columns outside this list.

***

## Files

| File                                  | Description                                                   |
| ------------------------------------- | ------------------------------------------------------------- |
| `search/run_search.py`                | Orchestrator — cache harvest, calls all sources, merges CSV   |
| `search/openalex_search.py`           | OpenAlex exact-phrase search with cursor resumability         |
| `search/semantic_scholar_search.py`   | Semantic Scholar relevance search with offset resumability    |
| `search/external_lists.py`            | Bob Reed and I4R scrapers                                     |
| `search/deduplicate.py`               | DOI dedup, fuzzy title dedup                                  |

***

## Rules

- Never add columns to `candidates.csv` that are not in `shared/schema.py:CANDIDATES_COLS`
- Always clean DOIs with `clean_doi()` before writing
- Respect OpenAlex rate limit: `OPENALEX_RATE_SEC` sleep between pages
- S2 authenticated rate limit: 1 req/s with key (`SEMANTIC_SCHOLAR_KEY`), 3 s without
- All API responses must be cached to `cache/` before writing results to disk

***

## Testing

Verify the output schema:

```bash
python -c "
import pandas as pd
from shared.schema import CANDIDATES_COLS
df = pd.read_csv('data/candidates.csv')
missing = [c for c in CANDIDATES_COLS if c not in df.columns]
assert not missing, f'Missing columns: {missing}'
print('Schema OK —', len(df), 'rows')
"
```
