# Stage 1: Search — Code Flow

**Entry point:** `python -m search.run_search`

## What it does

Discovers candidate papers from multiple sources and appends new ones to `data/candidates.csv`.
Each run has two phases: harvest all previously cached API pages, then issue new live requests.

---

## Step-by-step

```text
run_search.py
    │
    ├── Phase 1: cache harvest
    │       _harvest_oa_cache()   → read every *.json in cache/openalex/ (phrase + concept pages)
    │       _harvest_s2_cache()   → read every *.json in cache/s2/
    │       _merge_into_candidates_csv(combined)
    │
    └── Phase 2: live fetch (one source per call in --auto-advance mode)
            │
            ├── OpenAlex phrase search  (source = "openalex")
            │       fetch_openalex_candidates()
            │           └── for each phrase in SEARCH_PHRASES (37 total):
            │                   paginate /works?filter=title_and_abstract.search:"<phrase>"
            │                   _extract_row() → standardise to CANDIDATES_COLS schema
            │                   cache each page to cache/openalex/<hash>.json
            │                   save cursor to cache/openalex/<hash>.cursor.json
            │
            ├── OpenAlex concept search  (source = "openalex_concept")
            │       fetch_openalex_concept_candidates()
            │           └── for each concept_id in CONCEPT_IDS:
            │                   paginate /works?filter=concepts.id:<concept_id>
            │                   _extract_row() → same schema, source = "openalex_concept"
            │                   cache each page + save cursor (same cache/openalex/ dir)
            │
            ├── Semantic Scholar phrase search  (source = "semantic_scholar")
            │       fetch_semantic_scholar_candidates()
            │           └── for each phrase in SEARCH_PHRASES (37 total):
            │                   paginate /graph/v1/paper/search
            │                   save offset to cache/s2/<hash>.offset.json
            │
            └── _merge_into_candidates_csv(new_rows)
                    filter rows already in candidates index
                    enrich_abstracts() — fill missing abstracts from CrossRef/S2
                    append to candidates.csv (utf-8-sig on first write; utf-8 on appends)
                    _append_to_candidates_index(new_keys)
```

---

## Search phrases (37 total)

Defined in `SEARCH_PHRASES` in `search/openalex_search.py` and mirrored in
`search/semantic_scholar_search.py`. Applied to both OpenAlex and Semantic Scholar.

Original tier (high precision):

```text
"replication of"          "direct replication"       "close replication"
"conceptual replication"  "replication study"        "reproduction study"
"we replicated"           "attempts to replicate"    "registered replication report"
"pre-registered replication"
```

Added tier (broader coverage):

```text
"failed to replicate"     "did not replicate"        "we replicate"
"replicating the findings""could not reproduce"      "successfully replicated"
"reproducibility of"      "replication and extension""replicability of"
"attempt to replicate"    "failure to replicate"     "non-replication"
"reproducibility study"   "reproduce the findings"
```

Abstract-only tier (catches confirmed replications that phrase replication
only inside the abstract, not the title):

```text
"our results replicate"   "our findings replicate"   "results replicate the"
"confirm and replicate"   "replication across"       "cross-cultural replication"
"independent replication" "partial replication"      "multi-site replication"
"multisite replication"   "preregistered replication""exact replication"
"systematic replication"
```

---

## Concept-based search

Defined in `CONCEPT_IDS` in `search/openalex_search.py`.

Concept search complements phrase search by catching papers that:

- have no abstract stored in OpenAlex (common for pre-2015 papers), so
  `title_and_abstract.search` can only check the title
- describe replication implicitly ("we confirm", "cross-cultural validation") without
  using any of the 37 phrases

OpenAlex assigns concept tags using its own ML model over the full paper text, so
concept search can surface papers the phrase search misses.

Current verified concept IDs (verified 2026-06-23 via `--list-concepts`):

| Concept ID | Name | Works |
| --- | --- | --- |
| `C12590798` | Replication (statistics) | ~263k |
| `C9893847` | Reproducibility | ~121k |

To find additional concept IDs:

```bash
python -m search.run_search --list-concepts "replication"
python -m search.run_search --list-concepts "reproducibility"
```

Update `CONCEPT_IDS` in `search/openalex_search.py` with verified IDs.
Rows from concept search are tagged `source = "openalex_concept"` in candidates.csv
so they can be filtered separately (e.g. `python -m filter.run_filter --source openalex_concept`).

---

## Large-file handling

The candidates index (`cache/candidates_index.txt`) stores all identifiers ever written.
Key priority per row: `doi` → `oa:<openalex_id>` → `url:<url>` → `title:<lowercased title>`.
Each row stores up to 4 keys so a duplicate is caught regardless of which identifier is present.

This avoids loading the full CSV (~2M rows) into memory on every merge.

---

## Auto-advance mode

`--auto-advance` processes exactly one (source, phrase/concept, year) job per call.
Jobs cycle in this order within each year before advancing to the next year:

1. OpenAlex phrase jobs (37 × N years)
2. Semantic Scholar phrase jobs (37 × N years)
3. OpenAlex concept jobs (2 × N years, as of 2026-06-23)

State is saved in `cache/search_state.json`. Run in a loop until exit code 2
(all cursors exhausted for the year range):

```powershell
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 } until ($LASTEXITCODE -eq 2)
```

To run a single source only:

```powershell
# Concept search only
do { python -m search.run_search --auto-advance --source openalex_concept --from-year 2011 --to-year 2026 --max-per-phrase 10000 } until ($LASTEXITCODE -eq 2)
```

---

## Cache layout

```text
cache/
  openalex/
    <hash>.json          ← one cached API page (phrase search or concept search)
    <hash>.cursor.json   ← cursor checkpoint for that job
  s2/
    <hash>.json
    <hash>.offset.json
  candidates_index.txt   ← all identifiers ever written to candidates.csv
  search_state.json      ← auto-advance position (year + job index)
```

All page caches survive cursor deletion. If cursors are accidentally deleted, run
`--harvest-only` to recover all previously downloaded rows without re-hitting the API:

```bash
python -m search.run_search --harvest-only
```

---

## Rate limits

| Source | Limit |
| --- | --- |
| OpenAlex (unauthenticated) | 0.1 s between requests |
| OpenAlex (with `OPENALEX_API_KEY`) | higher quota + authenticated content |
| Semantic Scholar | 1 s between requests |

---

## Key functions

| Function | File | Description |
| --- | --- | --- |
| `fetch_openalex_candidates()` | `search/openalex_search.py` | Phrase search over all `SEARCH_PHRASES` |
| `fetch_openalex_concept_candidates()` | `search/openalex_search.py` | Concept search over all `CONCEPT_IDS` |
| `fetch_concept()` | `search/openalex_search.py` | Single concept, resumable cursor |
| `fetch_phrase()` | `search/openalex_search.py` | Single phrase, resumable cursor |
| `list_oa_concepts()` | `search/openalex_search.py` | Live concept ID lookup helper |
| `_extract_row()` | `search/openalex_search.py` | Map OpenAlex work → `CANDIDATES_COLS` |
| `_merge_into_candidates_csv()` | `search/run_search.py` | Append-only write with index dedup |
| `run_search_auto_advance()` | `search/run_search.py` | One-job-per-call orchestrator |
| `build_candidates_index()` | `search/run_search.py` | Rebuild index from CSV in chunks |
