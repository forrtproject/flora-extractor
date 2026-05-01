# Stage 1 — Search
**Input:** External APIs and curated lists  
**Output:** `data/candidates.csv`  
**Run:** `python search/run_search.py`

---

## What This Stage Does

Discovers every academic paper that might be a replication or reproduction study. It casts a wide net across multiple sources, collects basic metadata for each paper, and writes a single deduplicated list of candidates. Stage 2 then decides which candidates are genuine.

The goal is **high recall over precision** — it is better to include a false positive here than to miss a real replication. Stage 2 will filter.

---

## Pipeline Flow

```
OpenAlex keyword search
Bob Reed list scrape
I4R list scrape
SCORE CSV import
         │
         ▼
  Merge all sources
         │
         ▼
  Deduplicate by DOI → then by fuzzy title
         │
         ▼
  Cross-check against FLoRA entry sheet (skip already-in-FLoRA DOIs)
         │
         ▼
  data/candidates.csv
```

---

## Sources

| Source | File | Method | Notes |
|--------|------|--------|-------|
| OpenAlex | `search/openalex_search.py` | Keyword search via REST API | Free, broad coverage |
| Bob Reed list | `search/external_lists.py` | HTML scrape | Economics-focused curated list |
| I4R reports | `search/external_lists.py` | HTML scrape | Institute for Replication reports |
| SCORE CSV | `search/external_lists.py` | Static CSV import | Contact Luke/Theresa for file |
| Semantic Scholar | future | API search | Potential future source |

### OpenAlex Search Keywords

The search queries OpenAlex for papers matching these phrases in their title or abstract:

```
"replication of"
"direct replication"
"close replication"
"conceptual replication"
"replication study"
"reproduction study"
"we replicated"
"attempts to replicate"
"registered replication report"
```

Each keyword query uses the OpenAlex `/works` endpoint with `filter=title_and_abstract.search`. Results are paginated (200 per page) and rate-limited to 10 req/sec. All API responses are cached to `cache/openalex/`.

### Bob Reed List

Scrapes [replicationnetwork.com/replication-studies/](https://replicationnetwork.com/replication-studies/). Each entry has a title and a link; DOIs are resolved via CrossRef or OpenAlex lookup. Economics-heavy.

### I4R Reports

Scrapes [i4replication.org/reports/](https://i4replication.org/reports/). Each report corresponds to one replication paper. Metadata extracted from the page HTML. DOI resolved via OpenAlex.

### SCORE CSV

A static CSV provided by the SCORE project (contact Luke/Theresa). Load and map to `CANDIDATES_COLS` schema.

---

## Deduplication

Handled in `search/deduplicate.py`. Two passes:

**Pass 1 — Exact DOI match**  
All rows with the same `clean_doi()` value are deduplicated; the row with the richest metadata (most non-empty fields) is kept.

**Pass 2 — Fuzzy title match**  
For rows without a DOI, `rapidfuzz.fuzz.token_sort_ratio` is computed between all remaining title pairs. Pairs scoring ≥ 90 are collapsed. The row with more metadata is kept.

**Cross-check against FLoRA**  
Any `doi_r` already present in `data/flora_entry_sheet.csv` is dropped. These are already in the database; no need to re-process. If the FLoRA sheet is not present, this step is skipped with a warning (do not crash).

---

## Output Schema — `candidates.csv`

| Column | Type | Description |
|--------|------|-------------|
| `doi_r` | str | Cleaned DOI (no `https://doi.org/` prefix) |
| `title_r` | str | Paper title |
| `abstract_r` | str | Abstract text |
| `year_r` | int | Publication year |
| `authors_r` | str | Semicolon-separated author list |
| `journal_r` | str | Journal name |
| `url_r` | str | Open-access URL if available |
| `openalex_id_r` | str | OpenAlex work ID (e.g. `W2741809807`) |
| `source` | str | `openalex` / `bob_reed` / `i4r` / `score` / `semantic_scholar` |

All DOIs must pass through `clean_doi()` before being written. All API responses must be cached using `cache_key()`.

---

## Files

| File | Status | Description |
|------|--------|-------------|
| `search/run_search.py` | Stub | Orchestrator — calls all sources, merges, writes CSV |
| `search/openalex_search.py` | To implement | OpenAlex keyword search queries |
| `search/external_lists.py` | To implement | Bob Reed scraper, I4R scraper, SCORE loader |
| `search/deduplicate.py` | To implement | DOI dedup, fuzzy title dedup, FLoRA cross-check |

---

## What Needs to Be Implemented

- [ ] `fetch_openalex_candidates()` — paginate through OpenAlex search results for each keyword, extract metadata, return DataFrame
- [ ] `fetch_bob_reed()` — scrape the Bob Reed list, resolve DOIs via CrossRef, return DataFrame
- [ ] `fetch_i4r()` — scrape I4R reports, return DataFrame
- [ ] `load_score_csv(path)` — load and map SCORE CSV columns to `CANDIDATES_COLS`
- [ ] `deduplicate_candidates(df)` — two-pass deduplication + FLoRA cross-check

---

## Rules (from RULEBOOK.md)

- Never add columns to `candidates.csv` that are not in `shared/schema.py:CANDIDATES_COLS`
- Always clean DOIs with `clean_doi()` before writing
- Respect OpenAlex rate limit: 0.1s sleep between calls (`OPENALEX_RATE_SEC`)
- Bob Reed and I4R scrapers must handle HTTP errors gracefully — try/except, log and continue
- If the FLoRA sheet is missing, skip the cross-check and log a warning; do not crash

---

## Testing

Run against sample data to verify the output schema:

```bash
python -c "
import pandas as pd
from shared.schema import CANDIDATES_COLS
df = pd.read_csv('misc/sample_candidates.csv')
missing = [c for c in CANDIDATES_COLS if c not in df.columns]
assert not missing, f'Missing columns: {missing}'
print('Schema OK —', len(df), 'rows')
"
```
