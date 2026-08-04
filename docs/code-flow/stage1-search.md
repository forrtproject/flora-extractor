# Stage 1: Search — Code Flow

**Entry point:** `python -m search.run_search`

## What it does

**Stage 1 searches. It does not filter.** It discovers candidate papers and writes
them to the **survivor pool**; every precision decision — exclusions, phrase
matching, vocabulary, rescues — belongs to Stage 2's spec bundle, which is the one
rule set that decides what is a replication.

The single keyword exception is the **search gate**, described under
[the snapshot scan](#the-snapshot-scan-and-the-survivor-pool) below: a broad
token/stem alternation plus concept membership, which exists because 510M works
cannot be routed one rule bundle at a time. It admits generously and judges
nothing.

The API legs run in two phases per invocation: harvest all previously cached API
pages, then issue new live requests.

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
            │           └── for each phrase in SEARCH_PHRASES:
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
            │           └── for each phrase in SEARCH_PHRASES (imported from openalex_search):
            │                   paginate /graph/v1/paper/search
            │                   save offset to cache/s2/<hash>.offset.json
            │
            └── _merge_into_candidates_csv(new_rows)
                    filter rows already in candidates index
                    enrich_abstracts() — fill missing abstracts from CrossRef/S2
                    append to candidates.csv (utf-8-sig on first write; utf-8 on appends)
                    _append_to_candidates_index(new_keys)
```

> **`data/candidates.csv` is retired as a corpus.** It was the admission-gated
> Stage 1 output, and both halves of that description are gone: Stage 1 no longer
> gates admission, and the corpus everything downstream reads is the survivor pool,
> shared through Hugging Face rather than kept as a multi-GB local CSV. The API
> legs above remain as supplementary discovery — the snapshot scan is the path that
> produces the pool — and `CANDIDATES_COLS` survives as the column contract a pool
> row is rebuilt into (see [csv-schema.md](../csv-schema.md)).

---

## Search phrases

The phrase list is `SEARCH_PHRASES` in `search/openalex_search.py`. **Both** phrase
sources use it: `search/semantic_scholar_search.py` imports the same list rather than
keeping its own (issue #47 — the two used to diverge, and the S2 leg searched a
smaller subset). Adding a phrase there changes both legs.

The list is grouped in three tiers, reproduced below for orientation only — the file
is the list, and a phrase added there will not appear here:

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

## The snapshot scan and the survivor pool

`search/snapshot_scan.py` is Stage 1's main path (`--source openalex_snapshot`).
Where the API legs can only find works whose title or abstract matches a phrase
someone thought to write down, the scanner reads the **whole** OpenAlex
bulk-parquet corpus once — 2,446 partitions, 725 GB, ~510M records — and keeps
what the search gate admits.

**The search gate** (vectorized, pyarrow) is the whole of Stage 1's keyword logic:

- a broad token/stem alternation over the title and the raw abstract
  inverted-index JSON (it runs against the un-reconstructed JSON, so it can test
  tokens but not phrases — word order does not exist there), **or**
- membership of a replication concept, which is the recall arm and mirrors what
  the `openalex_concept` API leg does.

Either hit admits. There is no second keyword stage, no exclusion pattern and no
phrase precision test in the scan: a row the gate keeps goes into the pool and the
filter engine decides everything else about it. The gate keeps well under 1% of
the corpus.

**The survivor pool** (`--survivor-pool PATH`) is Stage 1's output: every gate
survivor as a parquet dataset, one file per manifest partition, a few GB against
~725 GB of snapshot. It is the filter engine's direct input — Stage 2 routes the
pool parquet, and nothing between the scan and the engine holds a filtered copy of
it. Progress is checkpointed per manifest file in `cache/snapshot/ledger.json`, so
an interrupted scan resumes where it stopped.

Because the pool holds everything the gate saw, a **Stage 2 rule change is a local
`filter.engine route` re-run over the pool**, not a rescan. Only a change to the
search gate itself — its token alternation or `CONCEPT_IDS` — costs the full scan,
which is why the gate has its own fingerprint and why a token added there is
doubly expensive: it also enlarges the artifact every collaborator downloads.

Pool columns (`_POOL_SCHEMA` in `search/snapshot_scan.py`) are the identity and
metadata needed to rebuild a paper row without the snapshot — `id`, `doi`, `title`,
`display_name`, `publication_year`, `type`, the nested `authorships`,
`primary_location`, `open_access` and `concepts` as JSON strings, the
already-reconstructed `abstract_text`, and the three booleans recording *why* the
gate kept the row: `hit_token_title`, `hit_token_abstract`, `hit_concept`.

`search/pool_sync.py` shares the pool through a private Hugging Face dataset repo,
so nobody has to reproduce the scan:

```bash
python -m search.pool_sync --check-access         # prove write access before a long scan
python -m search.pool_sync --push / --pull        # the ~2-3 GB survivor pool itself
```

`pool_manifest.json` at the repo root records the search gate, snapshot date and
ledger the pool was scanned under; pushing over a pool scanned under a *different*
gate fingerprint is refused, because the mixture would be complete under neither
gate and nothing downstream could tell. Runbook for the full scan:
[aws-snapshot-scan.md](../aws-snapshot-scan.md).

---

## Concept-based search

Defined in `CONCEPT_IDS` in `search/openalex_search.py`.

Concept search complements phrase search by catching papers that:

- have no abstract stored in OpenAlex (common for pre-2015 papers), so
  `title_and_abstract.search` can only check the title
- describe replication implicitly ("we confirm", "cross-cultural validation") without
  using any `SEARCH_PHRASES` phrase

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
Rows from concept search are tagged `source = "openalex_concept"` so they can be
told apart downstream. In the filter engine the concept arm is its own
spec (`concept-replication`), so a concept-only row is identifiable by `route_rule`.

---

## Large-file handling

The candidates index (`cache/candidates_index.txt`) stores all identifiers ever written.
Key priority per row (`row_keys()` in `shared/row_key.py`): `doi` → `oa:<openalex_id>`
→ `url:<url>` → `title:<lowercased title>`. A row stores every identifier it has, so a
duplicate is caught regardless of which one is present — except that `title:` is a
**last-resort** key contributed only when the row has no other identifier at all
(issue #53: two distinct works sharing a title would otherwise collide).

This avoids loading the full CSV (~2M rows) into memory on every merge.

---

## Auto-advance mode

`--auto-advance` processes exactly one (source, phrase/concept, year) job per call.
Jobs cycle in this order within each year before advancing to the next year:

1. OpenAlex phrase jobs (`len(SEARCH_PHRASES)` × N years)
2. Semantic Scholar phrase jobs (the same list × N years)
3. OpenAlex concept jobs (`len(CONCEPT_IDS)` × N years)

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
| OpenAlex (unauthenticated) | 0.3 s between requests (`OPENALEX_RATE_SEC` default) |
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
