# Stage 2: Filter — Code Flow

**Entry point:** `python -m filter.engine <command>` — `route`, then `screen`, then
`handoff`.

## What it does

Applies a bundle of declarative filter specs to the survivor pool, routing every
work into a pile; spends LLM money only on the two screening piles, under a claim;
and materializes what survives as `data/filtered.csv` for Stage 3.

The dividing principle is that **rules route and discard, only LLM tiers admit**.
No spec can conclude that a paper is a replication; the most a spec does is send it
to a tier that is allowed to decide, or discard it on measured precision.

**Stage 2 owns every precision decision.** Stage 1 searches and admits generously
through the search gate; the spec bundle in `filter/spec/` is the one rule set that
says what a paper is, which is why a rule change costs a `route` re-run over the
pool and never a rescan.

Module contracts are in [filter-engine.md](../filter-engine.md); spec policy
(precedence, pile → status mapping) is in `filter/spec/CONVENTIONS.md`. This
document is the flow.

## Step-by-step

```
filter/spec/*.json                       the bundle — one file per rule
    │   spec.py loads and validates it, bundle_hash() names it
    ▼
python -m filter.engine route --pool cache/snapshot_pool
    │
    ├── release.py mints a release id from six inputs
    │       (pool manifest, overlay, bundle, engine version, aliases, schema)
    │
    ├── pool_reader.py streams the pool parquet in batches,
    │       coalescing any text overlay over empty abstract cells
    │
    ├── route.py evaluates every spec against every batch
    │       highest precedence wins; shadow matches are recorded, not applied
    │       screen pile + no abstract → pending/no_text
    │
    └── store.py persists routing + evaluations in the DuckDB store
            piles: discard · screen_expensive · screen_cheap · needs_human · pending
    ▼
python -m filter.engine screen --tier screen_expensive [--run]
python -m filter.engine screen --tier screen_cheap   [--run] [--live]
    │
    ├── tiers.py reads the pile's works, with pool text attached
    │
    ├── without --run: prints rows, token distribution and an estimated cost.
    │       Nothing is claimed, fetched or spent.
    │
    └── with --run: claims the whole batch through the Supabase claims RPC,
            then per work — judge, write the raw response, record one permanent
            verdict row per voter vote — and completes the claim.
    ▼
python -m filter.engine handoff --out data/filtered.csv
    │
    ├── handoff.py reads both screen piles, expensive first
    ├── drops works a LIVE tier run discarded
    ├── writes a live screen_expensive record type into filter_status
    └── → data/filtered.csv + data/filtered.csv.manifest.json
```

`export` writes a single pile as an immutable artifact with a manifest that may
never be overwritten; `handoff` writes the file Stage 3 reads and is rewritten
whenever the release or the verdicts move. `worklist` exports the `pending/no_text`
rows for the abstract backfill that produces an overlay.

## The two LLM tiers

| Tier | What asks | What it may conclude |
|------|-----------|----------------------|
| `screen_cheap` | the two small `shared/prescreen.py` voters, voter 2 only when voter 1 said no | discard on two explicit noes; every other shape proceeds |
| `screen_expensive` | Stage 3's front door — `classify_replication()` + `screen_gate()` | the gate's verdict and the paper type |

`screen_cheap` defaults to `mode="validation"`: the votes are recorded and its
would-be discards are reported against the piles the rules chose, but nothing is
discarded. Its zero-miss evidence was measured on a post-gate distribution, and
issue #146 §2 requires re-validating it on this one before it discards
autonomously. `--live` is what makes the discards take effect.

`screen_expensive` runs the same screen Stage 3 runs, ahead of Stage 3, so its
output can *be* the handoff rather than a second opinion on it.

**Live verdicts are applied at the handoff, not in the routing table.** Routing is
derived data — the next `route` recomputes it from pool and specs — so a verdict
written into it would be erased.

## `filter_status` and `filter_confidence`

The engine routes a work into a pile; `filter/spec/conventions.json` maps the pile
to the `filter_status` / `filter_confidence` an exported row carries (`discard` →
`false_positive` high; `screen_expensive` → high; `screen_cheap` → medium;
`needs_human` → `needs_review` low; `pending` is not exported). Both screen piles set
`vocabulary_names_status`, so a row there takes the winning rule's `vocabulary` as its
status and falls back to `needs_review` when the rule names none.
`replication-claim`, the only `screen_expensive` rule, names none by design — an
admission to the two-voter screen is a request for attention, not the verdict the
screen exists to produce — so its rows export `needs_review`/high. The three cheap
rules (`replication-signal`, `replication-probe`, `reproduction-signal`) do name
theirs, at `screen_cheap`/medium. `filter_method` is `engine:<release id prefix>` and
`filter_evidence` is `rule:<spec id>` plus what the backend matched — except on a
row a live `screen_expensive` run typed, where `filter_method` is `screen`.

`filter_confidence` is `high | medium | low` — categorical, not a float. A 3-level
label is more actionable than a continuous probability from a single LLM call.

Stage 3 still overwrites `filter_status` with its own screen's paper type when it
screens a row itself, and sets `filter_method = "screen"`.

## Key modules

| Module | Description |
|--------|-------------|
| `filter/engine/spec.py` | Loads, validates and hashes the spec bundle |
| `filter/engine/route.py` | Evaluates specs against a pool batch; resolves the pile |
| `filter/engine/store.py` | The DuckDB routing store — disposable, rebuilt by `route` |
| `filter/engine/tiers.py` | The claimed, budget-gated LLM tiers and their dry run |
| `filter/engine/handoff.py` | Both screen piles → `data/filtered.csv` for Stage 3 |
| `filter/engine/claims.py` | The Supabase claims/verdicts client |
| `filter/phrase_detection.py` | Stage 1's **search gate** vocabulary. Not part of Stage 2 — the engine evaluates the JSON specs itself and never calls it |
