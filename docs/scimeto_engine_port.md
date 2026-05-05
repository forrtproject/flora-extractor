# SciMeto Discovery Engine — Python port

This document is a team-facing reference for the Stage-1 discovery engine that
was ported from SciMeto in commit `e901d76` (and the rate-limit audit) on
2026-05-04. Read this if you are adding a source, changing the spec, or
debugging what the runner does on a wide search.

## TL;DR

- **What is it?** A YAML-spec-driven Stage-1 search runner that issues *one*
  OR-bundled phrase query per source per run, then post-fetches phrase-level
  attribution. Drops in alongside Amy's per-source scripts; opt in with
  `FLORA_USE_ENGINE=1` or invoke directly via `python -m search.engine.cli`.
- **Why?** OpenAlex deprecated the polite pool on **2026-02-13**; the free
  tier now allows 1,000 search calls/day. The per-source scripts issue one
  call per phrase, exhausting that quota in a single wide run. The engine
  bundles every phrase into one paginated query (≈20 calls/run total).
- **Where did it come from?** `apps/worker/src/services/replication/discovery/`
  in the SciMeto repo. The hand-off was designed there explicitly for this
  port — the YAML files travel verbatim, the algorithm modules are
  re-implementations.

## File map

```
search/
├── spec/                                 ← portable YAML artifact (verbatim from SciMeto)
│   ├── search-keywords.yaml              17 keyword IDs, ~84 phrase variants
│   ├── exclusion-patterns.yaml           4 non-scholarly contexts to drop post-fetch
│   ├── source-configs.yaml               endpoints, auth env vars, rate limits, query templates
│   ├── ranking-weights.yaml              search_score formula
│   └── README.md                         hand-off contract — read before touching the YAMLs
├── RATE_LIMITS_VERIFIED.md               2026-05-04 audit of OpenAlex / Crossref / S2 limits
├── engine/
│   ├── __init__.py
│   ├── types.py                          dataclasses (KeywordSpec, RawCandidate, NormalizedCandidate, …)
│   ├── keyword_expander.py               wildcard syntax + spec → ExpandedKeyword list
│   ├── exclusion_filter.py               YAML-loaded post-fetch regex pass
│   ├── candidate_normalizer.py           RawCandidate → NormalizedCandidate, DOI canonicalization
│   ├── candidate_ranker.py               deterministic search_score formula
│   ├── runner.py                         one-run orchestrator (file-only; no DB / checkpoint)
│   ├── cli.py                            `python -m search.engine.cli`
│   └── sources/
│       ├── __init__.py
│       ├── token_bucket.py               monotonic-clock rate limiter w/ thread-safe refill
│       ├── source_adapter.py             abstract base class + SearchArgs dataclass
│       ├── openalex_adapter.py           OR-bundled /works query, cursor pagination
│       ├── crossref_adapter.py           OR-bundled /works query, cursor pagination
│       └── semantic_scholar_adapter.py   OR-bundled /paper/search, offset pagination (cap 1000)
├── engine_source.py                      DataFrame adapter so run_search.py can call the engine
├── run_search.py                         orchestrator; opt-in via FLORA_USE_ENGINE=1
└── ... (Amy's per-source scripts unchanged)
```

## Function reference

### `search.engine.keyword_expander`

| Function | Purpose |
|----------|---------|
| `expand_wildcard(s)` | Apply the four-step wildcard rules to one input string and return the literal phrases it stands for. Order: quoted literal → alternation → optional char → trailing star. |
| `expand_spec_keyword(spec)` | Turn one `KeywordSpec` (either `permutations:` or `template:`+`qualifiers:`) into flat `ExpandedKeyword` rows. |
| `expand_user_input(raws)` | Same, but for ad-hoc user wildcards. Each gets a synthetic `USER_<slug>` id. |
| `expand_all(specs, user_kw)` | Combine spec + user inputs, dedup by lowercase phrase. Spec entries win on conflict. |
| `load_spec_keywords(dir)` | Parse `search-keywords.yaml`. |

Wildcard syntax (mirrors the Discover UI):

| Pattern                          | Behaviour                                                         |
|----------------------------------|-------------------------------------------------------------------|
| `replicat*`                      | Expanded via `STEM_DICT` (replicate, replicated, replicates, …).  |
| `pre-?registered`                | Optional preceding char → both `pre-registered` and `preregistered`. |
| `(close\|high-powered) replication` | Alternation group → `close replication`, `high-powered replication`. |
| `"exact phrase"`                 | Quoted literal — no expansion.                                    |

Stem coverage is intentionally narrow (`replicat`, `reproduc`). Adding a stem
keeps determinism intact; introducing a stemmer library would not.

### `search.engine.exclusion_filter`

| Function | Purpose |
|----------|---------|
| `apply_exclusions(text, patterns)` | Return the first matching exclusion pattern's id, or `excluded=False`. |
| `compile_exclusions(patterns)` | Pre-compile patterns once for hot loops. |
| `apply_compiled_exclusions(text, compiled)` | Same check with pre-compiled patterns. |
| `load_exclusion_patterns(dir)` | Parse `exclusion-patterns.yaml`. |

The four shipped patterns cover DNA / RNA / cell / viral replication, code &
data &c. replication, the verb form of the same, and structural biology terms
(replication fork, origin, stress, timing).

### `search.engine.candidate_normalizer`

| Function | Purpose |
|----------|---------|
| `normalize_doi(doi)` | Lowercase, strip `https?://(dx\.)?doi\.org/`, strip `doi:`, strip trailing slash. |
| `normalize_candidate(raw)` | Convert `RawCandidate → NormalizedCandidate`; trims fields, sets `search_score=0`. |
| `merge_candidates(a, b)` | Same-DOI merge: keep first non-null metadata, dedup `matched_keywords`, max `search_score`. |

### `search.engine.candidate_ranker`

`compute_search_score(cand, sources_matched, weights)` returns a 0–1 score
from `ranking-weights.yaml`. Contributions:

| Field                    | Weight | Condition                                          |
|--------------------------|--------|----------------------------------------------------|
| `title_match`            | 1.0    | Any keyword permutation matched the title.         |
| `abstract_match`         | 0.5    | Same, but only counted when no title match fired.  |
| `multi_keyword_bonus`    | 0.2    | Two or more distinct keyword IDs hit.              |
| `source_diversity_bonus` | 0.1    | Two or more sources returned the same DOI.         |

Score is capped at `cap` (1.0).

### `search.engine.sources.source_adapter`

Abstract base class. Concrete adapters implement:

```python
class SourceAdapter(ABC):
    id: SourceId
    verified_at: datetime

    def search(self, args: SearchArgs) -> Iterator[SearchPage]: ...
    def report_limits(self) -> RateLimitReport: ...
```

`SearchArgs` carries the full ExpandedKeyword list plus a `RunFilters` (year
window, languages, source whitelist) and an optional resume cursor. Adapters
yield `SearchPage(candidates, next_cursor)` tuples until exhaustion.

Behavioural contract every adapter follows:

1. Slice the keyword list at `max_phrases_per_query` (default 100; URL-length safety).
2. Build a single OR-bundled phrase query.
3. Pull from the upstream API with a `TokenBucket` blocking each call.
4. On `429`: increment `consecutive_429`, halve the bucket rate, sleep
   `Retry-After`, retry the *same* cursor (idempotent). Bail after 3 in a row.
5. Emit RawCandidates with the *first* keyword stamped on each — the runner
   re-attributes hits across all keywords post-fetch via the spec regexes.

### `search.engine.sources.token_bucket`

Plain monotonic-clock token bucket. `take()` blocks until a token refills.
`set_rate(x)` halves the rate after a 429. Thread-safe via `Lock`.

### `search.engine.sources.openalex_adapter`

- Endpoint: `GET https://api.openalex.org/works`
- Bundle: `?search=("phrase1" OR "phrase2" OR …)`
- Auth: `Authorization: Bearer $OPENALEX_API_KEY` since 2026-02-13.
- Pagination: cursor (`*` initial), `per-page=50`, capped at `max_pages_per_query=20`.
- Filters: `type:article,has_abstract:true`, optional year + language.
- Abstract reconstruction: `abstract_inverted_index` → text via position sort.

### `search.engine.sources.crossref_adapter`

- Endpoint: `GET https://api.crossref.org/works`
- Bundle: `?query.bibliographic="phrase1" OR "phrase2" OR …`
- Auth: `User-Agent: flora-extractor/1.0 (mailto:$RESEARCHER_EMAIL)`; also as `mailto` query.
- Pagination: cursor, `rows=100`, capped at 20 pages.
- Abstract cleaning: strip `<jats:*>` and `<p>` wrappers, collapse whitespace.

### `search.engine.sources.semantic_scholar_adapter`

- Endpoint: `GET https://api.semanticscholar.org/graph/v1/paper/search`
- Bundle: `?query="phrase1" | "phrase2" | …` (S2 uses `|` for OR).
- Auth: `x-api-key: $SEMANTIC_SCHOLAR_API_KEY` if present.
- Pagination: offset/limit (limit=100, hard cap offset 999 → 1000 max).

### `search.engine.runner`

```python
result = run_discovery(config: RunConfig, adapters, on_candidate)
```

- Reads spec from `config.spec_dir`.
- `check_spec_freshness` raises if any adapter's `verified_at` is older than
  `SPEC_FRESHNESS_DAYS` (60).
- Iterates one task per source. For each page:
  1. Normalize each `RawCandidate`.
  2. Drop any whose `title + abstract` matches an exclusion pattern.
  3. Group by DOI, merging `matched_keywords` and metadata within the page.
  4. Score via `compute_search_score`.
  5. Stream to the caller via `on_candidate`.
- Per-source `max_candidates_per_source` short-circuits when reached.
- Per-source errors are counted; a `429 threshold exceeded` ends the run early
  with `status='failed'` so callers can pause.

### `search.engine.cli`

`python -m search.engine.cli [...]` — args mirror Discover UI inputs:

| Flag                | Effect |
|---------------------|--------|
| `--spec-dir`        | Override `search/spec/`. |
| `--sources`         | Comma-separated source whitelist. |
| `--keywords`        | Comma-separated user wildcards (in addition to the YAML). |
| `--year-from / --year-to` | Publication-year window. |
| `--max-per-source`  | Stop a source after N kept candidates (0 = no cap). |
| `--out`             | CSV path; defaults to `data/candidates_engine.csv`. |
| `--languages`       | Comma-separated ISO codes; default `en`. |
| `--verbose`         | DEBUG logging. |

Output is the canonical `CANDIDATES_COLS` schema, so the file can be fed
directly to `filter/run_filter.py`.

### `search.engine_source`

`fetch_engine_candidates(...)` is the boundary between Amy's
`run_search.py` and the engine. Returns a pandas DataFrame in
`CANDIDATES_COLS` so the existing dedup + flora-cross-check passes work
unchanged. `is_engine_enabled()` is the env-var gate (`FLORA_USE_ENGINE=1`).

## Algorithm equivalence

The TS engine in SciMeto is the source of truth; this port is meant to be
behaviour-equivalent under the *same YAML spec*. The benchmark harness at
`scripts/replication/discovery-benchmark/` in the SciMeto repo measures
recall against a FReD-derived ground truth and precision against
adversarial negatives. If you change semantics here (different stem dict,
different score formula, different exclusion handling), run that benchmark
before merging — the spec README hand-off says so explicitly.

## Rate-limit budget (verified 2026-05-04)

| Source           | rate    | hard cap        | notes                                                 |
|------------------|---------|-----------------|-------------------------------------------------------|
| OpenAlex         | 5 r/s   | 100 r/s         | API key required; 1,000 search calls/day free tier.   |
| Crossref         | 1.5 r/s | 3 r/s polite    | `mailto` required.                                    |
| Semantic Scholar | 0.5 r/s | 1 r/s with key  | offset cap at 1000.                                   |

The runner refuses to start when any source's `verified_at` in
`source-configs.yaml` is older than 60 days. To pass the gate: re-verify
upstream policy, update both `verified_at` and `RATE_LIMITS_VERIFIED.md`,
and commit.

## How to extend safely

- **Add a keyword.** Edit `search-keywords.yaml`, run the SciMeto benchmark.
  No code change required; the engine reads the YAML at boot.
- **Add an exclusion.** Same flow with `exclusion-patterns.yaml`.
- **Add a source.** Implement `SourceAdapter` under `engine/sources/`.
  Register it in `runner.build_default_adapters`. Add a new top-level
  block to `source-configs.yaml` matching the existing shape. Add tests.
- **Tweak the score formula.** Edit `ranking-weights.yaml` only. Code in
  `candidate_ranker.py` is generic enough to consume any contribution
  whose `field` matches one of `title_match`, `abstract_match`,
  `multi_keyword_bonus`, `source_diversity_bonus`.

## Tests

`tests/test_search_engine.py` (8 cases) covers:

- Spec loads and contains the expected IDs.
- Spec expansion is unique by phrase (case-insensitive).
- User wildcards (`"exact"`, alternation, optional char, trailing star) work.
- DNA exclusion pattern fires; "conceptual replication of …" does not.
- DOI normalization handles all the ugly variants.
- Same-DOI merge keeps the richer record.
- Score formula respects every contribution and the cap.

Run them directly (the project's `tests/conftest.py` requires Flask, which
isn't needed for the engine):

```bash
python - <<'PY'
import sys; sys.path.insert(0, '.')
from tests import test_search_engine as t
import inspect
for name, fn in inspect.getmembers(t, inspect.isfunction):
    if name.startswith('test_'):
        fn(); print('PASS', name)
PY
```

## Pointers

- Spec hand-off README: [`search/spec/README.md`](../search/spec/README.md)
- Rate-limit audit:    [`search/RATE_LIMITS_VERIFIED.md`](../search/RATE_LIMITS_VERIFIED.md)
- Engine README:       [`search/engine/README.md`](../search/engine/README.md)
- Walkthrough scripts: [`examples/discover_example.bat`](../examples/discover_example.bat)
                     · [`examples/discover_example.sh`](../examples/discover_example.sh)
- Upstream source: `apps/worker/src/services/replication/discovery/` in the SciMeto repo.
