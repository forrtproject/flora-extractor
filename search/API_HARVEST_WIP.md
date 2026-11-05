# WIP: non-snapshot discovery sources

This branch preserves the API-harvest half of Stage 1, removed from `main` on
2026-08-04. Nothing here runs in production. It is kept because the *questions* it
answers may come back, not because the code can be restored as-is — see
"Why re-adding this is a rewrite, not a revert".

## What is here

| Module | What it did |
| --- | --- |
| `search/openalex_search.py` | Cursor-paginated phrase and concept harvest against the OpenAlex API; owned `SEARCH_PHRASES` and `CONCEPT_IDS` |
| `search/semantic_scholar_search.py` | The same over Semantic Scholar |
| `search/engine/**`, `search/engine_source.py` | A second discovery backend behind `FLORA_USE_ENGINE`, with its own `search/spec/*.yaml` |
| `search/deduplicate.py` | Cross-source dedup for the merged CSV |
| `search/external_lists.py` | I4R / Replication Network scrapers, never wired (see also `wip/external-lists-curated-harvest`, PR #156) |
| `search/backfill_gap_rows.py` | Filled gap rows into `candidates.csv` |
| `run_search`'s harvest legs | `_merge_into_candidates_csv`, the candidates index, source orchestration |

## Why it left `main`

Not because the sources were judged low-value. Because **they were already
disconnected**: nothing in `filter/` or `extract/` reads `data/candidates.csv`.
Stage 2 routes the survivor pool (`SNAPSHOT_POOL_DIR`) and Stage 3 reads
`filtered.csv` from `handoff`. Every API-harvest leg therefore wrote into a file no
downstream stage opened — a discovery path that discovered into a dead end.

The maintainer's rule is that nothing outside `archive/` should sit off the main
path. A harvest that cannot reach Stage 2 is off the main path by definition, and
leaving it on `main` advertised a capability the pipeline did not have.

## Why re-adding this is a rewrite, not a revert

The sink changed. A future non-snapshot source must write **pool rows** — parquet
matching `_POOL_SCHEMA` in `search/snapshot_scan.py` — not CSV rows. There is no
ingestion path for non-snapshot rows into the pool today; building one is the real
work, and it is shared with
[#150](https://github.com/forrtproject/flora-extractor/issues/150) (curated
harvester for OSF / I4R / HAL / EconStor).

Three specific traps for whoever picks this up:

1. **`source` must reach the pool row.** `filter/engine/tiers.py`'s `Work` carries a
   `source` field and passes it to `shared/prescreen.py`'s `prescreen_bypass()`,
   which protects `CURATED_SOURCES` rows from small-model discards. `_POOL_SCHEMA`
   has no `source` column, so it defaults to `""` and that protection cannot fire.
   Inert today (every pool row is the snapshot); load-bearing the moment another
   source exists.

2. **Deduplication is now the engine's, not the harvester's.** `deduplicate.py` and
   the candidates key-index existed because the CSV had no identity model. The pool
   is keyed by `work_id` with alias resolution (`filter/engine/workids.py`), and
   routing is `PRIMARY KEY (release_id, work_id)`. A new source needs `work_id`s,
   not a dedup pass.

3. **Abstract backfill already moved.** `filter/engine/backfill.py` fills a text
   overlay from the routing worklist, importing `search/fetch_abstracts.py`'s phase
   runners (which stayed on `main` for exactly this reason). Do not restore the
   `candidates.csv` merge wrapper that was deleted from around them.

## What was NOT deleted

`search/fetch_abstracts.py` (its phase runners serve the pool's overlay),
`search/snapshot_scan.py`, `search/pool_sync.py`'s `--push`/`--pull`, and the search
vocabulary — `REPLICATION_STEM_PATTERN` and `CONCEPT_IDS`, both now in
`filter/phrase_detection.py`, which is the one deliberate exception to "one rule
set": Stage 1 searches, Stage 2 decides.

## Recovering a module

```bash
git show wip/api-harvest-sources:search/openalex_search.py
git log --follow -- search/openalex_search.py
```
