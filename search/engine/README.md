# `search/engine/` — discovery engine (Python port of SciMeto's TS engine)

This is a port of `apps/worker/src/services/replication/discovery/` from the
SciMeto repo. The hand-off was designed by the SciMeto team in collaboration
with FORRT — see [`search/spec/README.md`](../spec/README.md) for the contract.

## What's here

```
search/engine/
├── types.py                    — dataclasses (KeywordSpec, RawCandidate, …)
├── keyword_expander.py         — wildcard + spec → flat ExpandedKeyword list
├── exclusion_filter.py         — post-fetch regex exclusions
├── candidate_normalizer.py     — RawCandidate → NormalizedCandidate, DOI normalize, merge
├── candidate_ranker.py         — deterministic search_score formula
├── runner.py                   — one-run orchestrator (file-only, no DB)
├── cli.py                      — `python -m search.engine.cli` entry point
└── sources/
    ├── token_bucket.py         — monotonic-clock rate limiter
    ├── source_adapter.py       — abstract base + SearchArgs
    ├── openalex_adapter.py     — OR-bundle phrase search
    ├── crossref_adapter.py     — OR-bundle phrase search
    └── semantic_scholar_adapter.py — OR-bundle phrase search
```

The YAML spec lives at [`search/spec/`](../spec/):

- `search-keywords.yaml` — 17 keyword IDs with phrase permutations
- `exclusion-patterns.yaml` — 4 non-scholarly contexts to drop after fetch
- `source-configs.yaml` — endpoints, rate limits, query templates (verified 2026-05-04)
- `ranking-weights.yaml` — `search_score` formula

[`search/RATE_LIMITS_VERIFIED.md`](../RATE_LIMITS_VERIFIED.md) records when
the rate-limit values were last reviewed against the providers' docs. The
runner refuses to start if any source's `verified_at` is more than 60 days old.

## How it differs from `search/openalex_search.py` (Amy's Phase-1 work)

Amy's existing per-source scripts each issue a phrase-by-phrase search and
deduplicate downstream. The engine instead OR-bundles every phrase into a
single search call per source and recovers per-keyword attribution
post-fetch. The benefit is **dramatically fewer API calls**: SciMeto verified
on 2026-05-04 that OpenAlex's free tier is 1,000 search calls/day; bundling
brings a wide run from "hundreds of calls" to roughly 20.

Both modes are kept side by side. Amy's code is the default path in
`run_search.py`. The engine is invoked explicitly via the CLI or via
`fetch_engine_candidates()` in `run_search.py`.

## Quick start

```bash
# Set whichever keys you have (OpenAlex requires one since Feb 13, 2026).
export OPENALEX_API_KEY=...
export RESEARCHER_EMAIL=you@example.com   # used for the Crossref polite pool
export SEMANTIC_SCHOLAR_API_KEY=...        # optional but recommended

# One-shot run, capped per source for testing
python -m search.engine.cli \
    --sources openalex \
    --max-per-source 50 \
    --year-from 2018 --year-to 2024 \
    --out data/candidates_engine.csv
```

## Rate-limit budget

Default settings from `source-configs.yaml` (50% safety factor):

| Source           | rate    | hard cap        | notes                                               |
|------------------|---------|-----------------|-----------------------------------------------------|
| OpenAlex         | 5 r/s   | 100 r/s         | API key required since Feb 13, 2026 (mailto-only no longer authenticates the polite pool) |
| Crossref         | 1.5 r/s | 3 r/s polite    | `mailto` required                                   |
| Semantic Scholar | 0.5 r/s | 1 r/s with key  | offset-based pagination capped at 1000 results      |

If you hit `429 threshold exceeded`, the adapter has already halved its rate
twice and is out of grace. Stop the run and either wait or narrow the spec.

## Algorithm equivalence

Behaviour should track the SciMeto TS engine on a fixed input. The SciMeto
benchmark at `scripts/replication/discovery-benchmark/` is the source of
truth; if you change semantics here (different stem dict, different score
formula, different exclusion handling), run that benchmark before merging.
