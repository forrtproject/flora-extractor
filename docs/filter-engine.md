# Filter Engine — declarative routing over the survivor pool

Design and contract for the issue #146 architecture, milestone 1: one engine applies
declarative filter specs to the survivor pool and routes every row into a pile.
Rules route and discard; only LLM tiers admit. This document is the authority for
module interfaces; `filter/spec/CONVENTIONS.md` is the authority for policy
(precedence bands, pile→status mapping, measurement levels).

## Semantics (from issue #146)

- **Piles:** `discard` (the only rule-terminal state), `screen_expensive` (two-voter
  classify gate), `screen_cheap` (discard-only small-model tier), `needs_human`,
  `pending`. Pending rows carry a reason: `unevaluated`, `no_filter_matched`,
  `no_text`, `budget_blocked`.
- **Precedence:** every spec carries an integer precedence; **higher number wins**.
  Multi-match is expected; the pile resolves once, by the highest-precedence
  matching rule. Routing of unclaimed rows is a pure function of
  (pool text, text revision, filter bundle, engine version).
- **No rule admits.** A discard rule needs measured precision; a routing rule needs
  diagnostics. An unmeasured discard rule runs in **shadow** (`"shadow": true`):
  evaluations are recorded, the pile is unaffected.
- **No text ⇒ no LLM.** A row resolved to `screen_expensive`/`screen_cheap` whose
  `abstract_text` is empty is downgraded by the engine to `pending/no_text`
  (discards still discard — structural rules don't read the abstract). This is
  engine policy, not a spec: absence of evidence must not convert into a proceed.

## Module map (`filter/engine/`)

| Module | Contract |
| --- | --- |
| `spec.py` | `FilterSpec` (frozen dataclass mirroring the JSON), `load_specs(spec_dir) -> list[FilterSpec]` (validated, sorted by precedence desc, ids unique), `bundle_hash(specs) -> str` (sha256 over each file's canonical bytes, order-independent), `validate_spec(dict) -> list[str]` (error strings), RE2-safety check `re2_safe(pattern) -> bool` (rejects lookaround, backreferences, conditionals, `\G`, atomic groups, possessive quantifiers). |
| `backends.py` | Two evaluators with identical semantics: `eval_spec_rows(spec, rows: list[dict]) -> list[bool]` (Python `re`) and `eval_spec_batch(spec, batch: pa.RecordBatch) -> pa.BooleanArray` (pyarrow compute). `verify_backends(specs, table) -> list[str]` returns per-spec mismatch reports (empty = equal); used by tests and by `python -m filter.engine verify`. |
| `route.py` | `route_batch(specs, batch) -> pa.Table` with columns `work_id (int64), pile (str), pending_reason (str), rule_id (str), precedence (int32), matched_rules (list<str>)`; `matched_rules` holds every non-shadow match (overlap diagnostics need the full cross-product), shadow matches are recorded separately in evaluations. |
| `workids.py` | `work_id(openalex_id: str) -> int` (`https://openalex.org/W123` → `123`); `load_aliases(path) -> dict[int, int]` from `filter/spec/aliases.json` (old_id → canonical_id, empty to start); `alias_release(path) -> str` (file hash). |
| `release.py` | `routing_release(pool_manifest_hash, overlay_hash, bundle_hash, engine_version, alias_release, schema_version) -> str` (sha256 of the canonical JSON); `write_release(...)`/`read_release(...)` under `cache/engine/releases/<id>.json`. Overlay hash is `None` until M3 (text overlays); pool manifest hash comes from `search.pool_sync.pool_manifest()`'s ledger hash or `--pool-manifest-hash`. |
| `store.py` | Local DuckDB acceleration cache (gitignored, disposable): `open_store(path)`, `build_routing(store, pool_dir, specs, release_id)` (streams pool parquet through `route_batch`, persists `routing` and `evaluations(work_id, spec_id, spec_hash, matched)` incl. shadow specs), `pile_counts(store, release_id)`, `sample_pile(store, pile, n)`. Deleting the DB loses nothing: everything rebuilds from pool + specs. |
| `diagnostics.py` | `diagnose(store_before, store_after, spec_id, ...) -> dict` — the §3 rule-diagnostics function: rows moved per (source pile → destination pile); overlap/agreement matrix vs every other rule (exclusive hits vs covered); a readable random sample (n≈20, seeded) of moved rows; holdout effect (reads `filter/spec/holdout.json`; reports `"holdout": "not_constructed"` until decision #146-2 lands); for discard specs, whether a `measured` entry exists (else the spec must be shadow). Renders JSON + a human-readable text block. |
| `export.py` | `export_pile(store, pile, out_csv, release_id, from_year, to_year)` — writes the Stage 3 contract: `FILTERED_COLS` + `ENGINE_EXPORT_COLS` (see below), `utf-8-sig`, `filter_status`/`filter_method`/`filter_evidence`/`filter_confidence` derived via the conventions mapping. Also `export_manifest(...)`: a JSON naming release id, pile, row count, and content hash next to the CSV (immutable once written). |
| `cli.py` / `__main__.py` | `python -m filter.engine route\|verify\|diagnose\|export\|specs\|status` (see `docs/cli-reference.md`). |

`ENGINE_VERSION` lives in `filter/engine/__init__.py` and is bumped whenever routing
behavior changes without a spec change.

### Provenance columns (`ENGINE_EXPORT_COLS` in `shared/schema.py`)

Issue #148's requirement: what the pool knows must survive into materialized
artifacts. Appended after `FILTERED_COLS`:
`oa_type, hit_concept, route_rule, route_precedence, matched_rules, pending_reason,
release_id`. (`matched_rules` is |-joined; match by substring/split like
`screen_categories`.) Stage 3 ignores trailing columns it does not read, so its
contract is untouched.

### Pile → `filter_status` mapping

Lives in `filter/spec/conventions.json` (machine-read by `export.py`), explained in
`CONVENTIONS.md`. M1 mapping:

| pile | filter_status | filter_confidence |
| --- | --- | --- |
| discard | `false_positive` | high |
| screen_expensive | `replication` / `reproduction` (by the winning rule's `vocabulary` field) | high |
| screen_cheap | `needs_review` (or the rule's vocabulary at medium, if it names one) | medium |
| needs_human | `needs_review` | low |
| pending | not exported | — |

`filter_method` is always `engine:<release_id_prefix>`; `filter_evidence` is
`rule:<id>` plus the matched evidence (phrase, prefix, type…) the backend recorded.

## Spec format (v1)

One JSON file per filter under `filter/spec/`; the file's content hash is its
version. Verbatim shape:

```json
{
  "id": "deposit-doi-prefixes",
  "description": "Why this rule exists and what it measured.",
  "match": {
    "doi_prefix": ["10.7910"],
    "doi_regex": null,
    "title_regex": null,
    "abstract_regex": null,
    "text_regex": null,
    "fields": {"type": ["dataset"]},
    "abstract_missing": null,
    "any_of": [],
    "all_of": [],
    "none_of": []
  },
  "pile": "discard",
  "vocabulary": null,
  "precedence": 960,
  "shadow": false,
  "measured": [
    {"level": "human", "precision": 0.995, "n": 282,
     "sample": "30k-candidates-2026-07", "date": "2026-08-01",
     "owner": "…", "rationale": "…"}
  ]
}
```

Match semantics:

- Every present, non-null top-level condition must hold (AND). `any_of` /
  `all_of` / `none_of` hold nested match objects (same shape, recursion allowed)
  combined as OR / AND / NOR against the row.
- `title_regex` runs over `coalesce(display_name, title)`; `abstract_regex` over
  `abstract_text`; `text_regex` over `title + "\n" + abstract_text`. All regexes
  are **RE2-safe** (no lookaround/backreferences — enforced by `validate_spec`)
  and case-insensitive by default, so the pyarrow and `re` backends cannot diverge.
- `doi_prefix` matches after `clean_doi()`; `fields` is exact membership on pool
  columns (`type`, `publication_year`), except `fields.concept_ids`, which tests
  membership of bare concept ids in the row's `concepts` (both URL-form JSON and
  bare forms).
- `abstract_missing: true` matches rows with empty/null `abstract_text`.
- `pile` must be one of the four routable piles; `pending` is never a spec target.
- `vocabulary` (`"replication"` / `"reproduction"` / null) feeds the status mapping.
- A `discard` spec with no `measured` entry fails validation unless `shadow` is true.

## The consolidated starter bundle

Everything the pipeline currently knows as scattered Python/YAML constants becomes a
spec file. Sources: `shared/utils.py` (deposit prefixes, non-article DOI/type),
`filter/phrase_detection.py` (phrases, stems, guards), the former
`filter/spec/exclusion-patterns.yaml` (folded in and deleted), and
`search/openalex_search.py` (concept ids). Precedence bands per `CONVENTIONS.md`:
900s structural discards · 600s rescues · 500s vocabulary-exclusion discards ·
300s screen_expensive routes · 200s screen_cheap routes.

| spec | pile | prec. | content |
| --- | --- | --- | --- |
| `deposit-doi-prefixes` | discard | 960 | the 10 `_DATA_REPOSITORY_PREFIXES` + figshare rule |
| `non-article-doi` | discard | 955 | `/reviews/`\|`/decisions/` DOI paths (peer-review objects) |
| `dataset-type` | discard | 950 | OpenAlex `type == dataset` — the #149 rule; measured on the 2026-08-03 pilot; its rows stay in the pool and are auditable by rule id (route-not-delete) |
| `non-article-type` | discard | 945 | the remaining `_NON_ARTICLE_TYPES` (paratext, peer-review, erratum, …) |
| `editorial-artifact` | discard | 555 | former YAML `EDITORIAL_ARTIFACT` |
| `data-availability` | discard | 550 | former YAML `DATA_AVAILABILITY` |
| `biological` · `structural` · `biological-of` | discard | 545 · 544 · 543 | former YAML `BIOLOGICAL`, `STRUCTURAL`, `BIOLOGICAL_OF` |
| `technical-object` · `technical-verb` | discard | 541 · 540 | former YAML `TECHNICAL_OBJECT`, `TECHNICAL_VERB` |
| `exclusion-rescue` | screen_cheap | 650 | exclusion context AND phrase AND cite — the #44 readmission, outranks the 500s |
| `phrase-with-cite` | screen_expensive | 350 | replication/reproduction phrase AND author-year cite, `none_of` GWAS vocabulary |
| `phrase-reproduction` | screen_cheap | 262 | the reproduction-anchored patterns, `vocabulary: reproduction` |
| `phrase-replication` | screen_cheap | 260 | the remaining replication phrases, `vocabulary: replication` |
| `title-stem` | screen_cheap | 240 | the 15 multilingual stems, title only |
| `concept-replication` | screen_cheap | 220 | concepts C12590798 / C9893847 — the arm Stage 2 used to kill terminally |

The 500s band ships as seven files rather than four, one per former YAML pattern:
`filter/phrase_detection.py` maps a spec id back to its legacy pattern id
(uppercase, hyphens → underscores) to keep `filter_evidence` unchanged, and a
merged spec would have lost that.

Shadow specs (deferred #142 findings, awaiting diagnostics): `reproduce-verb-arms`,
`nfd-stems` — `"shadow": true`, no pile effect, evaluations recorded. `biological-of`
and `data-availability` are shadow for a different reason: their faithful form needs
a lookaround, so the spec carries an RE2 decomposition that *widens* the discard
alongside the exact original under the loader-only `pyre_regex` key (see
`CONVENTIONS.md`). `keyword_verdict()` reads the original; the engine may not
discard on the wider one.

## Known intended divergences from `keyword_verdict()`

Recorded here because parity is measured against *intended* semantics (#148):

1. **Concept arm**: a concept-only row routes to `screen_cheap`; the old Stage 2
   wrote it `false_positive` terminally. A regression test asserts the divergence.
2. **Cite proximity**: the same-sentence gate and the blacklist-filtered
   `extract_author_year_patterns()` are replaced by one RE2-safe cite regex; the
   distinction only orders spend (expensive vs cheap), it no longer admits.
3. **Guard scoping**: GWAS guards were sentence-scoped; the spec `none_of` is
   row-scoped. A GWAS-flavoured phrase row falls to `screen_cheap` instead of
   being guard-suppressed.
4. **No-abstract rows** route to `pending/no_text` instead of being screened blind.

`keyword_verdict()` remains the Stage 1 admission gate unchanged; the engine
supersedes Stage 2's *decision* layer, not Stage 1's scan.

## Engine input

The engine reads the survivor pool parquet (`_POOL_SCHEMA`, year-sharded files in a
flat directory) directly — not candidates.csv. Batches stream via
`pq.ParquetFile(...).iter_batches()` as in `snapshot_scan.py`. `work_id` is the
int64 OpenAlex id; aliases (merged works) resolve through `filter/spec/aliases.json`
before any state is keyed.

## Out of scope for M1 (later milestones)

Postgres claims/verdicts schema and claim RPC (M2); abstract backfill + text-overlay
releases (M3); LLM tier runners, `needs_human` export sizing, Stage 2 retirement
switch (M4); validation-table supersession (M5). Nothing in M1 may foreclose them:
release ids already reserve the overlay-hash and alias-release slots.
