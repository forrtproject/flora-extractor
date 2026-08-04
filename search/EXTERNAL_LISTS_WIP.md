# WIP: curated external list harvesting

This branch is the holding place for `search/external_lists.py`, which is being
removed from `main` as unwired code. Nothing here runs in production.

## Why it left `main`

`fetch_i4r()` and `fetch_replication_network()` return `CANDIDATES_COLS`-schema
frames, but no production path has ever called them: `search/run_search.py` never
imported the module, and `_ALL_SOURCES` never contained `i4r` or `bob_reed` as
fetchable sources. That was recorded in
[#46](https://github.com/forrtproject/flora-extractor/issues/46) (closed) and the
module then sat in the tree for months, shipped and documented but unreachable —
the worst of the three states it could be in, because readers and agents assumed
the feature existed. `run_search`'s own docstring claimed the curated lists "are
also fetched and merged", which was never true.

The 2026-08-04 cleanup removed it rather than leaving the limbo in place. The
maintainer's call was to move it off `main` into a WIP PR with a linked issue
describing what finalising it needs, which is this branch.

## What finalising it requires

The live successor issue is
[#150](https://github.com/forrtproject/flora-extractor/issues/150) — a curated
harvester for OSF / I4R / HAL / EconStor reproduction reports. Wiring this module
in is a subset of that, and it is more than a registration:

1. **Register the source.** Add to `_ALL_SOURCES` in `search/run_search.py` and to
   `SOURCE_VALUES` in `shared/schema.py`. Note historical rows on disk already
   carry `bob_reed` / `i4r` values, so the enum entries were deliberately left in
   place when the module was deleted.

2. **Land the rows where Stage 2 can see them.** This is the part that changed
   underneath the module. Stage 1 no longer writes an admission-gated
   `data/candidates.csv`; the filter engine reads the **survivor pool**
   (`SNAPSHOT_POOL_DIR`) and never reads the CSV. A curated row merged into
   `candidates.csv` today would therefore never reach Stage 2 at all. Any wiring
   must write into the pool — which currently has no ingestion path for non-snapshot
   sources. That path is the real work in #150, not the scrapers.

3. **Carry `source` through to the cheap tier.** `shared/prescreen.py`'s
   `prescreen_bypass()` protects rows whose `source` is in `CURATED_SOURCES`
   (`i4r`, `bob_reed`, `backfill_old_pipeline`) from being discarded by a small
   model. `filter/engine/tiers.py`'s `Work` now has a `source` field and passes it
   to that bypass, but the pool schema has no `source` column, so it defaults to
   `""` and the protection cannot fire. This is inert today because every pool row
   is `openalex_snapshot`; it becomes load-bearing the moment curated rows enter
   the pool. Adding `("source", pa.string())` to `_POOL_SCHEMA` and a `"source"`
   key in `_pool_record()` (`search/snapshot_scan.py`) is a prerequisite, not a
   nice-to-have — without it, curated rows can be discarded by the cheap tier.

4. **Decide scope against the rule book.** A curated list entry is evidence a
   replication exists, not necessarily a paper record. `filter/spec/` currently
   discards deposit DOIs and registry non-article types structurally; some curated
   entries will look like those. Decide whether curated provenance outranks a
   structural discard, and if so express it as a precedence, not a special case.

5. **Check the scrapers still work.** They were last exercised in
   `tests/live/test_search_live.py` (removed with the module) and target sites that
   may have changed. Treat the parsing as unverified.

## Recovering the code

The module is on this branch at `search/external_lists.py`, with full history:

```bash
git log --follow -- search/external_lists.py
git show wip/external-lists-curated-harvest:search/external_lists.py
```
