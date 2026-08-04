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
| `spec.py` | `FilterSpec` (frozen dataclass mirroring the JSON), `load_specs(spec_dir) -> list[FilterSpec]` (validated, sorted by precedence desc, ids unique), `bundle_hash(specs) -> str` (sha256 over each file's canonical bytes, order-independent, **including `conventions.json`** — see "The bundle a release is bound to"), `validate_spec(dict) -> list[str]` (error strings), RE2-safety check `re2_safe(pattern) -> bool` (rejects lookaround, backreferences, conditionals, `\G`, atomic groups, possessive quantifiers). |
| `backends.py` | Two evaluators with identical semantics: `eval_spec_rows(spec, rows: list[dict]) -> list[bool]` (Python `re`) and `eval_spec_batch(spec, batch: pa.RecordBatch) -> pa.BooleanArray` (pyarrow compute). `verify_backends(specs, table) -> list[str]` returns per-spec mismatch reports (empty = equal); used by tests and by `python -m filter.engine verify`. |
| `route.py` | `route_batch(specs, batch) -> pa.Table` with columns `work_id (int64), pile (str), pending_reason (str), rule_id (str), precedence (int32), matched_rules (list<str>)`; `matched_rules` holds every non-shadow match (overlap diagnostics need the full cross-product), shadow matches are recorded separately in evaluations. |
| `workids.py` | `work_id(openalex_id: str) -> int` (`https://openalex.org/W123` → `123`); `load_aliases(path) -> dict[int, int]` from `filter/spec/aliases.json` (old_id → canonical_id, empty to start); `alias_release(path) -> str` (file hash). |
| `release.py` | `routing_release(pool_manifest_hash, overlay_hash, bundle_hash, engine_version, alias_release, schema_version) -> str` (sha256 of the canonical JSON); `write_release(...)`/`read_release(...)` under `cache/engine/releases/<id>.json`. Overlay hash is `None` until M3 (text overlays); pool manifest hash comes from `search.pool_sync.pool_manifest()`'s ledger hash or `--pool-manifest-hash`. |
| `store.py` | Local DuckDB acceleration cache (gitignored, disposable): `open_store(path)`, `build_routing(store, pool_dir, specs, release_id)` (streams pool parquet through `route_batch`, persists `routing` and `evaluations(work_id, spec_id, spec_hash, matched)` incl. shadow specs), `pile_counts(store, release_id)`, `sample_pile(store, pile, n)`. `routing` is keyed `PRIMARY KEY (release_id, work_id)` and inserts `ON CONFLICT DO NOTHING`: a pool holding both a merged id and its canonical id holds two rows for ONE work, and first-writer-wins is what keeps that one routed work and one exported row. A build is one transaction — the delete and every insert commit together — so an interrupted run leaves the release absent or as its previous complete build, never half-replaced. Deleting the DB loses nothing: everything rebuilds from pool + specs. |
| `diagnostics.py` | `diagnose(store_before, store_after, spec_id, ...) -> dict` — the §3 rule-diagnostics function: rows moved per (source pile → destination pile); overlap/agreement matrix vs every other rule (exclusive hits vs covered); a readable random sample (n≈20, seeded) of moved rows; holdout effect (reads `filter/spec/holdout.json`; reports `"holdout": "not_constructed"` until decision #146-2 lands); for discard specs, whether a `measured` entry exists (else the spec must be shadow). Renders JSON + a human-readable text block. |
| `export.py` | `export_pile(store, pile, out_csv, release_id, from_year, to_year)` — writes the Stage 3 contract: `FILTERED_COLS` + `ENGINE_EXPORT_COLS` (see below), `utf-8-sig`, `filter_status`/`filter_method`/`filter_evidence`/`filter_confidence` derived via the conventions mapping. Also `export_manifest(...)`: a JSON naming release id, pile, row count, and content hash next to the CSV (immutable once written). |
| `cli.py` / `__main__.py` | `python -m filter.engine route\|verify\|diagnose\|export\|specs\|status` (see `docs/cli-reference.md`). |

`ENGINE_VERSION` lives in `filter/engine/__init__.py` and is bumped whenever routing
behavior changes without a spec change.

### The bundle a release is bound to

`bundle_hash()` covers the spec files **and `conventions.json`**. The engine routes
a row into a pile; the conventions decide what that pile is *called* in an export
(`filter_status`, `filter_confidence`, whether the winning rule's `vocabulary`
names the status). A release that bound only the specs could be exported under a
different status mapping than it was routed under, so the policy file is part of
the bundle. `aliases.json` is not — it has its own release input,
`alias_release` — and neither is `holdout.json`, which names an evaluation set and
changes no row's status.

The export enforces the binding rather than trusting it. `export_pile()` reads the
bundle again (the routing table stores the winning rule id, not its vocabulary),
so `cli.py` passes the release record's `bundle_hash` and `alias_release` and the
export refuses outright when either has moved: a CSV labelled with a release id it
does not match is worse than no CSV. **There is no override flag** — a stale client
re-runs `route` and exports the new release (issue #146 §4).

### Backend equality and Unicode

`re2_safe()` rejects constructs one backend cannot run, but it does not make the
two backends' character classes identical. Python `re` reads `\w`, `\b` and
`IGNORECASE` as Unicode-aware; RE2 — and so pyarrow — reads `\w` and `\b` as
ASCII. A pattern that is RE2-safe can still match different rows in the two
engines on non-ASCII text. This is not hypothetical: the author-year cite clause
of `phrase-with-cite` and `exclusion-rescue` used `[\w'’.-]` as its surname atom,
and `García et al. (2020)` matched the `re` backend only. Both now use an explicit
negated class (`[^ \t\n\r\f,;:()\[\]]`), which both engines read identically, and
the trailing `\b` on the et-al form became an explicit non-digit.

Two limits remain, deliberately. `\s` outside a character class is still
Unicode-aware in `re` and ASCII in RE2, so text separated by non-breaking or
ideographic spaces could in principle divide the backends; no shipped pattern has
been shown to, and chasing it through all ~140 patterns would be a bigger change
than the risk. And `python -m filter.engine verify` is **sample-based** — the
first batch of up to `--sample-files` pool files — so it is evidence of equality,
not proof of it. The standing proof is
`test_the_two_backends_agree_on_non_ascii_text` in `tests/test_engine_route.py`,
which runs the whole shipped bundle over a corpus of accented Latin, NFD, CJK,
Hangul, Cyrillic and fullwidth rows. A new pattern that needs `\w` or `\b` next to
text that may not be ASCII belongs in that corpus before it ships.

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

## Milestone 2 — the Postgres state authority

Routing is derived data; a *claim* and a *verdict* are not. Postgres holds exactly
what cannot be recomputed: which works were pinned before spend, what came back,
what humans said, and an audit trail of both. Deployed as one migration the
maintainer runs — `db/migrations/0001_engine_baseline.sql`, idempotent, no DROP.

| table | role |
| --- | --- |
| `engine_releases` | server-side registry of routing releases (the six-input id + payload). A claim naming an unknown release is REJECTED, so a stale client must re-route before it can spend. |
| `engine_claims` | one claimed batch, one tier (`screen_cheap`/`screen_expensive`/`human`/`measurement`), one status (`active`/`complete`/`cancelled`/`failed`). |
| `engine_claim_items` | the works, each with the pile it was routed to **at claim time** — pinned evidence that spec edits cannot move. |
| `engine_verdicts` | permanent tier evidence: verdict, confidence, short quote, model + prompt hash, cost, and the `response_hash` naming the raw blob on HF. |
| `engine_human_labels` | permanent human labels (validation app, gold set, holdout session). |
| `engine_alias_exceptions` · `engine_holdout` | hand-made alias-merge exceptions; the holdout registry (construction is #146 open decision 2). |
| `engine_audit` | insert-only event log: every claim and every claim release. |

**Claiming is one server-side transaction.** `engine_claim_batch(release_id, tier,
items, meta)` verifies the release, checks conflicts under a per-tier advisory lock,
inserts claim + items and an audit row, and returns the claim id — never
select-then-insert from the client (#146 §4). Rejection is all-or-nothing: one
already-claimed work fails the whole batch.

**Concurrency rule (#146 §8 decision 4, implementer default):** a work may be held by
at most one *active* claim **per tier**; different tiers may hold it concurrently;
`measurement` claims neither take nor respect the lock. It is enforced in the RPC
rather than by a unique index because the "active, same tier" condition lives in
another table.

**Claim lifecycle.** Ending a claim is a status flip and nothing else — the conflict
check reads active claims only, so `cancelled` and `failed` free their items
immediately. There is no partial state: a run that finished half its batch completes
the claim and re-claims the remainder as a new claim. A wrong answer is corrected at
the *verdict* level (`superseded_by`), never by editing the claim, which is a record
of what was spent.

**Permanence is enforced by the database.** Triggers reject every DELETE on
`engine_verdicts` / `engine_human_labels` / `engine_audit`, and every UPDATE except
`superseded_by`, `response_state`, and a write-once fill of `response_hash` (the
`response_pending_upload` reconciliation path). A verdict a script can quietly
rewrite is not evidence.

**Client:** `filter/engine/claims.py` — `ClaimsClient` (house PostgREST style, the
same `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` `shared/supabase_client.py` reads).
`register_release` · `claim` · `release_claim` · `record_verdict` ·
`supersede_verdict` · `active_claims` · `claimed_work_ids`. An unset `SUPABASE_URL`
raises `ClaimsNotConfigured` at construction: the engine must not silently run
unclaimed. A conflict raises `ClaimConflict` naming the tier.

**Sizing** (`python -m filter.engine.sizing --rows 5146160 --verdict-rate 0.1`,
#146 §8 decision 1): 130 B per claim_item, 543 B per verdict, measured over
synthetic rows of the migration's exact shape (analytic model, or against a real
database when `DATABASE_URL` + `psycopg` are available). The 500 MB free tier holds
~2.8M claimed works at 0.1 verdicts/work, ~780k at 1.0 — but claiming the whole
5.1M pool costs 640 MB in `claim_items` alone, before any verdict exists. So:
campaign-sized claims over the post-discard residue stay free-tier (300k works at
1.0 verdicts/work ≈ 193 MB); a whole-pool claim forces the upgrade.

## Milestone 3 — text overlays

A row the scan kept with no abstract routes to `pending/no_text` — "No text ⇒ no
LLM" — so the only way it reaches a screening tier is for text to arrive from
somewhere else. That somewhere else is a **text overlay**: a frozen release of
recovered abstract text, layered over the pool at read time.

| Module | Contract |
| --- | --- |
| `pool_reader.py` | `iter_pool_batches(pool_dir, overlay_dir=None, batch_size=50_000, aliases=None)` — the engine's single input path: pool batches with overlay text coalesced over empty `abstract_text` cells. `overlay_manifest_hash(overlay_dir) -> str \| None`. The overlay is loaded once as a `work_id -> text` dict and applied per batch; with no overlay the stream is `iter_batches()` untouched. |
| `overlay.py` | `worklist(con, release_id, pool_dir, out_path, aliases=None) -> int` (the `pending_reason='no_text'` rows joined to the pool for doi/title/year); `write_chunk()`, `load_overlay()`, `overlay_work_ids()`; `validate(overlay_dir) -> list[str]`; `freeze(overlay_dir, pool_manifest_hash=None) -> dict`; `overlay_manifest_hash()`, `read_manifest()`. |
| `backfill.py` | `python -m filter.engine.backfill --worklist F --overlay-dir D [--run] [--limit N] [--source S] [--freeze]` — the five abstract sources over a worklist, results appended as an overlay chunk. |

**Overlays fill, never replace.** The pool's own `abstract_text` is what the
snapshot shipped and is primary evidence; the overlay applies only where the
pool cell is empty or null. A coalesce in the other direction would let a
backfill source quietly rewrite the corpus the scan and every measured rule were
calibrated on.

**The release is the files plus a frozen manifest.** Chunks are append-only
`overlay-<seq>.parquet` (`work_id int64, abstract_text string, source string,
fetched_at string`). `freeze()` writes `overlay_manifest-<hash12>.json` —
immutable, naming per-file sha256s, rows, per-source counts, the parent pool
manifest hash and `created_at` — and points the mutable `overlay_manifest.json`
at it. Refreezing changed files mints a new manifest and moves the pointer; the
old manifest still describes exactly the bytes it was written for (§4: the
pointer is mutable, its target is not). One work id may appear in one chunk only:
"later file wins" would make the overlay's content depend on which run was
interrupted, and `validate()` refuses it.

**Text revision invalidates routing.** `overlay_hash` is the release-id slot M1
reserved. Text arriving for a `no_text` row changes that row's pile, so it must
change the release id — `route --overlay DIR` folds the frozen hash into
`routing_release(...)`, and `export --overlay DIR` refuses when the directory's
hash is not the one the release was routed under, exactly as it refuses a moved
bundle. An overlay directory holding chunks but no frozen manifest raises rather
than routing: a release must not be bound to bytes nobody named.

**The backfill reuses Stage 1's fetchers.** `search/fetch_abstracts.py` owns the
sources, their measured order (OpenAlex → Europe PMC → S2 → CrossRef → Scopus),
their batch shapes, their per-identifier cache under `cache/abstracts/` and their
per-source checkpoint namespaces; `backfill.py` imports its phase runners rather
than restating any of it. Only the two ends are new: the worklist comes from the
routing table instead of `candidates.csv`, and results land in an overlay chunk
instead of a CSV merge. The shared cache means a DOI Stage 1 already asked about
costs nothing here, and a miss recorded here is one Stage 1 will not re-buy.
Dataset-prefix DOIs are dropped from the worklist — no source has an abstract
for a deposit.

**Dry-run is the default** (§6). Without `--run`, the CLI prints per-source
targets, request counts and quota caps computed against the live checkpoint —
what a run would do *next*, not what a fresh worklist would cost — and fetches
nothing. Resumption is two independent mechanisms: the phase runners skip
checkpointed identifiers, and the chunk write skips work ids the overlay already
covers, so an interrupted run re-fetches nothing and cannot write a work into a
second chunk.

## Milestone 4 — the claimed LLM tiers and the Stage 3 switch

Rules route; only these tiers admit. Two runners in `filter/engine/tiers.py`, one
handoff in `filter/engine/handoff.py`, two CLI subcommands.

| Module | Contract |
| --- | --- |
| `tiers.py` | `pile_works(con, release_id, pile, pool_dir, …) -> list[Work]` (routing joined to overlay-aware pool text); `estimate(works, tier)` / `render_estimate(est)` (the §6 dry run); `run_screen_cheap(...)` / `run_screen_expensive(con, client, release_id, work_ids=None, *, mode, batch_label, limit, run)`; `tier_decisions(client, release_id, tier, mode="live")` and `decided_work_ids(client, tier)` (the checkpoint). |
| `handoff.py` | `write_handoff(con, pool_dir, out_csv, release_id, *, drop, record_types, …)` — both screen piles in `ENGINE_EXPORTED_COLS` order, `screen_expensive` first; `decisions(client, release_id) -> (drop, record_types)` from the live verdict rows. |

### The two tiers

- **`screen_cheap`** is `shared/prescreen.py`'s discard-only pair, called through
  that module — same prompt, same models, same parsing, same "two explicit noes or
  the row proceeds" rule, and the same three bypasses (`hard_signal`, `short_text`,
  a curated source) that refuse to let a 3B model end a row. A bypass is recorded
  as a verdict rather than skipped: deciding not to ask is a decision.
- **`screen_expensive`** is Stage 3's validated front door — `classify_replication()`
  plus `screen_gate()` — run over the pile ahead of Stage 3. `pile_works()`
  reproduces `_row_from_snapshot()`'s `doi_r`/`title_r` mapping exactly, so the
  tier's call and Stage 3's front-door call share a content cache key and Stage 3
  replays the verdict for nothing.

### Modes, and where a verdict takes effect

Issue #146 §2: the cheap tier's zero-miss evidence was measured on a post-gate
distribution and must be re-validated on this one. So `mode="validation"` is its
default — both votes are recorded, nothing is discarded, and the run report
compares the discards it *would* have made against the pile the rules chose.
`--live` is what makes them count. `screen_expensive` is live either way.

A run's mode lives on its **claim** (`meta.mode`), which is the only place the
fact is written down; verdict rows stay pure evidence. Nothing is ever written
into the routing table — routing is derived data and the next `route` erases it —
so a live discard takes effect at the handoff, where the rows leave the engine.

### Four invariants both runners hold

- **Claim before spend.** One `client.claim()` for the whole batch before the
  first voter. `ClaimConflict` ends the run with a refusal and no calls.
- **Dry run by default.** Without `--run`, nothing is claimed, fetched or spent;
  the row count, the abstract token-length distribution and "N rows → tier X ≈ $Y"
  are printed from the rough per-1k prices in `shared/config.py`.
- **Evidence before the verdict naming it.** The raw response is written to
  `cache/engine/responses/<hash>.json` and pushed to HF (silently skipped without
  `HF_TOKEN`/`FLORA_POOL_REPO`) *before* `record_verdict`; a push that did not
  happen is `response_pending_upload`, never `uploaded`.
- **Per-work checkpointing, server-side.** A verdict is written per work; an
  interrupted run's claim is failed and the next run re-claims only the works with
  no verdict. `TokenBudgetExhausted` fails the claim and stops cleanly, keeping
  what was decided.

### The Stage 3 switch

`python -m filter.engine handoff --out data/filtered.csv` writes the file Stage 3
reads: both screen piles, `screen_expensive` first, in `ENGINE_EXPORTED_COLS`
order, minus works a live run discarded, with a live `screen_expensive` record
type written into `filter_status` (`filter_method = screen`). Without Supabase it
says so and hands off the piles as routed. It reuses `export_pile()`'s row logic
via `iter_export_rows()` and keeps its release-binding refusal — but its manifest
is rewritable, because the handoff is a materialized view Stage 3 re-reads, not an
immutable artifact. `export` remains the command for an immutable copy.

`export --pile needs_human` prints the pile's size prominently: §2 asks for it
"exported with a size attached", and the size is the queue a person has to work
through.

### Retirement

`filter/rule_filter.py`, `filter/run_filter.py` and `filter/reset_backfilled.py`
are deleted, with their tests. `filter/phrase_detection.py` stays: Stage 1's scan
calls `keyword_verdict()` and the spec bundle encodes the same decision. The two
are held together by `test_stage1_admits_exactly_what_the_engine_screens` in
`tests/test_snapshot_scan.py` — Stage 1 admits a row exactly when the engine
routes it to a screen pile; a rejected row is either engine-discarded or
unmatched (`pending/no_filter_matched`), never screened. Each case names the pile
it expects, so a spec edit that moved a row between screen tiers is visible too.

## Milestone 5 — validation lineage and supersession

Issue #146 §5: *decisions become immutable once sent for human validation; upstream
changes create superseding records with lineage.* M5 supplies both halves.

### Lineage flows through Stage 3

`extract/csv_to_db.py` writes two columns into `record_metadata` when it pushes a
row: `work_id` (the int64 OpenAlex id derived from `openalex_id_r` via
`filter/engine/workids.work_id`) and `release_id` (from the row's `release_id`
column, which engine handoff rows carry and legacy `extracted.csv` rows do not).
Both are nullable and null on rows imported before the engine — that is the shape of
the data, not a shim. Reconciliation keys on **work_id, not DOI**: a work is the
engine's identity, a DOI is a string a row may lack, share, or spell differently.

Migration: `db/migrations/0002_validation_lineage.sql` (idempotent; run in the
Supabase SQL editor after 0001).

### The supersession record

`engine_supersessions` is insert-only, with the same permanence trigger as
`engine_audit`. One row per (work, upstream change) names the validation records it
affects:

| kind | meaning |
| --- | --- |
| `reroute` | the work moved between non-terminal piles — the sent decision stands but was routed under a superseded release |
| `withdrawal` | the work is now rule-discarded — what was sent should not be in the corpus |
| `verdict` | an expensive-tier verdict was superseded upstream (`engine_verdicts.superseded_by`) |

`affected_record_ids` is `text[]`, because `record_id` in the validation schema is a
uuid string minted by `csv_to_db.py`.

### What reconciliation does — and does not — write

`filter/engine/supersede.py` reads the routing store for works whose pile changed
between two releases (`diff_releases()`, a self-join: a work must be in both
releases for its pile to have *changed*), reads `record_metadata` for the validation
records those works already have (`affected_validation_records()`, paged PostgREST,
splitting records already in `validated` from those still in flight), and writes one
`engine_supersessions` row per affected work (`supersede()`).

**It never updates or deletes a row in `unvalidated`, `validated` or
`validation_queue`.** A sent decision is the only account of what a validator was
asked and answered; the supersession row is lineage laid on top of it, and the
validation repo consumes it downstream to decide what to show. Works whose routing
changed but which were never pushed get no row: routing that moves unclaimed rows is
the normal free case (§1).

    python -m filter.engine.supersede --old <release> --new <release> [--run] [--reason "..."]

Dry-run is the default and prints the plan — per work, the pile move, the kind, and
how many named records are already validated versus still unvalidated. `--run` is
the only way to write.
