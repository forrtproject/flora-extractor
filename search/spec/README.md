# Discovery Spec — Portable Artifact

These YAML files are the **single source of truth** for replication-discovery's keyword list, exclusion patterns, source endpoints, and ranking weights.

flora-extractor's eventual Python port re-implements `engine/` over these files unchanged. **Do NOT duplicate this knowledge in TS code** — the engine reads these YAMLs at boot.

## Files

- `search-keywords.yaml` — what to search for (18 keywords, expanded into explicit phrase permutations because API search endpoints don't accept regex)
- `exclusion-patterns.yaml` — what to drop *after* fetch (4 non-scholarly contexts: DNA replication, code/data replication, replication fork/origin/stress/timing, etc.)
- `source-configs.yaml` — endpoint URLs, auth env vars, query templates, rate limits (with verified-as-of date for the freshness gate)
- `ranking-weights.yaml` — `search_score` formula

## Freshness gate

If any source's `verified_at` date in `source-configs.yaml` is older than 60 days, the engine refuses to start a run. To pass the gate:

1. Re-verify the rate-limit and endpoint policy at the provider's docs.
2. Update the `verified_at` date in `source-configs.yaml`.
3. Update `requests_per_second` (and any other affected fields) if they changed.
4. Commit. The engine will accept new runs.

The reasoning is recorded in `../RATE_LIMITS_VERIFIED.md`. Update that file too when you re-verify.

## How to add a new keyword

1. Add an entry under `keywords:` in `search-keywords.yaml` with:
   - unique `id` (UPPER_SNAKE_CASE)
   - canonical `phrase`
   - `weight` (0.0–1.0)
   - `permutations:` (explicit phrase variants — **no regex**)
   - `fields: [title, abstract]` (which fields to search)
2. If using a template-with-qualifiers pattern (like `REP_QUALIFIED`), use `template:` and `qualifiers:` instead of `permutations:`.
3. Run the benchmark to confirm precision/recall don't regress (see `scripts/replication/discovery-benchmark/README.md`).

## How to add a new exclusion pattern

1. Add an entry under `patterns:` in `exclusion-patterns.yaml` with:
   - unique `id`
   - PCRE-compatible `regex`
   - `flags:` (typically `[i]`)
   - `description:` (so future readers understand the trap)
2. Run the benchmark to confirm true positives aren't accidentally caught.

## How to add a new source

Out of scope for the workshop. Plan in `docs/superpowers/plans/2026-05-04-replication-discovery.md` Task 28+ describes adding Bob Reed list, I4R, FReD-data GitHub adapters as Phase-2 work.

When adding:

1. Add a top-level entry in `source-configs.yaml` matching the existing shape.
2. Add a `SourceAdapter` implementation under `engine/sources/<name>Search.ts`.
3. Register in the runner's adapter registry.
4. Add tests with mocked fetch.

## Hand-off to flora-extractor

When the FORRT/flora-extractor team ports this to Python:

1. Copy these YAML files verbatim into their repo.
2. Re-implement `engine/keywordExpander`, `engine/exclusionFilter`, `engine/candidateRanker` in Python — the algorithms are simple enough that the spec file IS the spec.
3. Per-source `SourceAdapter` implementations need Python equivalents using `requests` or `httpx`. The query templates and rate limits transfer unchanged.
4. Confirm byte-identical output for a fixed config via `scripts/replication/discovery-benchmark/`.
