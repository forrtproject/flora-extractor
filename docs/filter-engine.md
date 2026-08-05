# Filter Engine — declarative routing over the survivor pool

Design and contract for the issue #146 architecture, milestone 1: one engine applies
declarative filter specs to the survivor pool and routes every row into a pile.
Rules route and discard; only LLM tiers admit. This document is the authority for
module interfaces; `filter/spec/CONVENTIONS.md` is the authority for policy
(precedence, pile→status mapping, measurement levels).

## Semantics (from issue #146)

- **Piles:** `discard` (the only rule-terminal state), `screen_expensive` (two-voter
  classify gate), `screen_cheap` (discard-only small-model tier), `needs_human`,
  `pending`. Pending rows carry a `pending_reason` — see below.
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

### `pending` and `pending_reason` are two different statements

They read redundantly if you take `pending_reason` for a restatement of the pile.
It is not. **`pending` is the pile: no decision exists for this row.**
**`pending_reason` says why no decision exists**, and the two ways that happens
want completely different work from a human.

| `pending_reason` | What happened | What would move the row |
| --- | --- | --- |
| `no_filter_matched` | The row was routed and **no rule claimed it**. Every spec was evaluated and none matched, so nothing said discard and nothing sent it to a tier. It is *unclassified*. | A rule that covers it — this pile is the bundle's coverage gap, and its size is the honest measure of how much of the pool the rules say nothing about. |
| `no_text` | A rule **did** claim it, for `screen_expensive` or `screen_cheap`, but `abstract_text` is empty and the engine downgraded it. It is *claimed but unreadable*. | Text. The M3 overlay path — `worklist` → `filter.engine.backfill` → `freeze` → `route --overlay` — exists for exactly this pile. |

The distinction is what keeps "the rules do not cover this" from being silently
counted as "we could not read this", which would hide a bundle gap behind a data
gap. `build_routing()` in `filter/engine/route.py` emits these two values and no
others; `conventions.json` declares them; `handoff` exports the two screen piles
only, so an exported row's `pending_reason` is always empty.

## Module map (`filter/engine/`)

| Module | Contract |
| --- | --- |
| `spec.py` | `FilterSpec` (frozen dataclass mirroring the JSON), `load_specs(spec_dir) -> list[FilterSpec]` (validated, sorted by precedence desc, ids unique), `bundle_hash(spec_dir: Path) -> str` (sha256 over the bundle directory's (filename, bytes) pairs, order-independent, **including `conventions.json`** — see "The bundle a release is bound to"), `validate_spec(dict) -> list[str]` (error strings), RE2-safety check `re2_safe(pattern) -> bool` (rejects lookaround, backreferences, conditionals, `\G`, atomic groups, possessive quantifiers). |
| `backends.py` | Two evaluators with identical semantics: `eval_spec_rows(spec, rows: list[dict]) -> list[bool]` (Python `re`) and `eval_spec_batch(spec, batch: pa.RecordBatch) -> pa.BooleanArray` (pyarrow compute). `verify_backends(specs, table) -> list[str]` returns per-spec mismatch reports (empty = equal); used by tests and by `python -m filter.engine verify`. |
| `route.py` | `route_batch(specs, batch) -> pa.Table` with columns `work_id (int64), pile (str), pending_reason (str), rule_id (str), precedence (int32), matched_rules (list<str>)`; `matched_rules` holds every non-shadow match (overlap diagnostics need the full cross-product), shadow matches are recorded separately in evaluations. |
| `workids.py` | `work_id(openalex_id: str) -> int` (`https://openalex.org/W123` → `123`); `load_aliases(path) -> dict[int, int]` from `filter/spec/aliases.json` (old_id → canonical_id, empty to start); `alias_release(path) -> str` (file hash). |
| `release.py` | `routing_release(pool_manifest_hash, overlay_hash, bundle_hash, engine_version, alias_release, schema_version) -> str` (sha256 of the canonical JSON); `write_release(...)`/`read_release(...)` under `cache/engine/releases/<id>.json`. Overlay hash is `None` until M3 (text overlays); pool manifest hash comes from `search.pool_sync.pool_manifest()`'s ledger hash or `--pool-manifest-hash`. |
| `store.py` | Local DuckDB acceleration cache (gitignored, disposable): `open_store(path)`, `build_routing(store, pool_dir, specs, release_id)` (streams pool parquet through `route_batch`, persists `routing` and `evaluations(work_id, spec_id, spec_hash, matched)` incl. shadow specs), `pile_counts(store, release_id)`, `sample_pile(con, release_id, pile, n=20, seed=17)`. `routing` is keyed `PRIMARY KEY (release_id, work_id)` and inserts `ON CONFLICT DO NOTHING`: a pool holding both a merged id and its canonical id holds two rows for ONE work, and first-writer-wins is what keeps that one routed work and one exported row. A build is one transaction — the delete and every insert commit together — so an interrupted run leaves the release absent or as its previous complete build, never half-replaced. Deleting the DB loses nothing: everything rebuilds from pool + specs. |
| `diagnostics.py` | `diagnose(pool_dir, spec_dir, spec_id, *, baseline_dir=None, sample_n=20, seed=17) -> dict` — routes the pool twice, with and without the spec (the baseline bundle defaults to the same directory minus the spec). The §3 rule-diagnostics function: rows moved per (source pile → destination pile); overlap/agreement matrix vs every other rule (exclusive hits vs covered); a readable random sample (n≈20, seeded) of moved rows; holdout effect (reads `filter/spec/holdout.json`; reports `"holdout": "not_constructed"` until decision #146-2 lands); for discard specs, whether a `measured` entry exists (else the spec must be shadow). Renders JSON + a human-readable text block. |
| `export.py` | `export_pile(con, pool_dir, pile, out_csv, release_id, from_year=None, to_year=None, conventions=None, specs=None, aliases=None, spec_dir=SPEC_DIR, expect_bundle_hash=None, expect_alias_release=None, overlay_dir=None, expect_overlay_hash=UNCHECKED, created_at="")` — writes the Stage 3 contract: `FILTERED_COLS` + `ENGINE_EXPORT_COLS` (see below), `utf-8-sig`, `filter_status`/`filter_method`/`filter_evidence`/`filter_confidence` derived via the conventions mapping. Also `export_manifest(...)`: a JSON naming release id, pile, row count, and content hash next to the CSV (immutable once written). |
| `cli.py` / `__main__.py` | `python -m filter.engine specs\|verify\|route\|diagnose\|export\|screen\|reconcile\|handoff\|worklist\|status`. The subcommand list is `cli.py`'s `add_parser` calls; `--help` is authoritative, `docs/cli-reference.md` is the prose. |

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
engines on non-ASCII text. This is not hypothetical: the retired `phrase-with-cite` rule's author-year cite
clause used `[\w'’.-]` as its surname atom, and `García et al. (2020)` matched the
`re` backend only. The fix was an explicit negated class
(`[^ \t\n\r\f,;:()\[\]]`), which both engines read identically; the pattern is
archived in `filter/spec/rule_ideas.md`. Live rules still use `\w` — rule B's
`we (…0-2 words…) replicat(ed)` arm — so the hazard is current, not historical.

Text is NFC-normalised at the one seam per backend where it becomes matchable
(`_nfc()` and `BatchContext`), which is why the bundle needs no decomposed-Unicode
twin of its multilingual stem arm. `pc.utf8_normalize` is not used: on pyarrow 25
it returns its input unchanged for NFC, which would divide the backends on exactly
those rows.

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

**They stop here, on purpose.** `EXTRACTED_COLS` excludes `ENGINE_EXPORT_COLS`, so
Stage 3 does not carry the block into `extracted.csv` and `csv_to_db` does not push
it. Provenance is **linked, not duplicated**: `extract/csv_to_db.py` writes
`record_metadata.work_id` (the int64 id, via `filter/engine/workids.work_id()`),
and every routing column is recoverable by joining that against the `routing` table
for a release — the same join `filter/engine/supersede.py` already uses. Widening
the CSV schema would create a second copy that can drift from the store.

### Pile → `filter_status` mapping

Lives in `filter/spec/conventions.json` (machine-read by `export.py`), explained in
`CONVENTIONS.md` — read the JSON, not this table, when the answer has to be right:

| pile | filter_status | filter_confidence |
| --- | --- | --- |
| discard | `false_positive` | high |
| screen_expensive | the winning rule's `vocabulary`, else `needs_review` | high |
| screen_cheap | the winning rule's `vocabulary`, else `needs_review` | medium |
| needs_human | `needs_review` | low |
| pending | not exported | — |

A pile substitutes the rule's vocabulary for its own status only where its policy
sets `vocabulary_names_status` — true for both screen piles, false for `discard` and
`needs_human`. Whether a rule names a vocabulary is the rule's own decision,
recorded in its spec: the `replication-claim-*` tiers leave it null on purpose — admission to
the two-voter screen asks for attention rather than settling what the row is — so
its rows reach `filtered.csv` as `needs_review`/high, while a cheap rule that names
its vocabulary exports it at `screen_cheap`/medium.

`filter_method` is always `engine:<release_id_prefix>`; `filter_evidence` is
`rule:<id>` plus the matched evidence (phrase, prefix, type…) the backend recorded.

## Spec format (v1)

One JSON file per filter under `filter/spec/`; the file's content hash is its
version. Verbatim shape:

```json
{
  "id": "not-a-paper-doi",
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
    {"level": "trusted", "date": "2026-08-04", "owner": "…", "rationale": "…"}
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

## The shipped bundle (rule book v2)

Nine specs, designed in `redesign/rulebook_v2.html` and measured there. The book
is a **whitelist**: nothing is screened unless a positive rule admits it, so
there are no exclusion or rescue rules. The nineteen specs it replaced
— the six vocabulary exclusions, the two rescues, the four phrase/stem rules and
the identifier/type rules it absorbed — are archived with their patterns and
evidence in [`filter/spec/rule_ideas.md`](../filter/spec/rule_ideas.md).

| spec | pile | prec. | shadow | content |
| --- | --- | --- | --- | --- |
| `not-a-paper-doi` | discard | 960 | | `/reviews/`\|`/decisions/` paths · terminal `.suppl` |
| `deposit-registrant` | discard | 958 | | 10 deposit-only registrants (figshare dropped, D1) |
| `not-a-paper-title` | discard | 955 | | the start-anchored genre-plus-parent title pattern |
| `not-a-report-type` | discard | 940 | | `type ∈ {component, database, dataset, software, supplementary-materials}` — not a report of a study |
| `replication-claim-cited-title` | screen_expensive | 760 | | a claim arm in the TITLE **and** an author-year citation in the title — the only live admission |
| `replication-claim-title` | screen_expensive | 750 | ✓ | any claim arm in the title |
| `replication-claim-text` | screen_expensive | 730 | ✓ | the 8 strong claim arms anywhere in title+abstract |
| `replication-claim-residual` | screen_expensive | 710 | ✓ | the 4 measured-weak arms: fail/attempt · aim/set out · success* · the negation matrix |
| `not-a-study-type` | discard | 500 | | `type ∈ {grant, libguides, paratext, peer-review, standard}` — a crosswalk that can be wrong about a real paper |
| `replication-signal` | screen_cheap | 300 | ✓ | multilingual title stems · English title stem · concept ids · bare `replication of` |
| `reproduction-signal` | screen_cheap | 262 | ✓ | the reproduction-anchored patterns, carried over unchanged pending #155 |
| `replication-probe` | screen_cheap | 100 | ✓ | unmeasured candidate vocabularies (revisiting, reconsidered, independent test, many-analyst …) |

Each rule keeps one match clause per arm for legibility and per-arm audit of the
`measured` entries; the engine's attribution unit is the RULE — `matched_rules`
records rule ids and `filter_evidence` the winning rule's first matched
substring, so counting an individual arm means evaluating that arm's pattern
separately.

Why each rule outranks or yields to its neighbours is argued in its own
`description`; the shadow flags are likewise per-rule decisions recorded in the
specs. Rows no live rule claims land in `pending/no_filter_matched`.

## Known intended divergences from the retired keyword filter

Recorded here because parity was measured against *intended* semantics (#148):

1. **Concept arm**: a concept-only row routes to `screen_cheap`; the old Stage 2
   wrote it `false_positive` terminally. A regression test asserts the divergence.
2. **No-abstract rows** route to `pending/no_text` instead of being screened blind.

The other two divergences on this list were properties of `phrase-with-cite` — the
RE2-safe cite regex replacing the same-sentence gate, and the row-scoped rather
than sentence-scoped GWAS guard — and both went with the rule. Nothing is admitted
by a citation any more, and no arm admits "we replicated the association" without
a first-person or qualifier construction.

## Engine input, and the one keyword decision upstream of it

The engine reads the survivor pool parquet (`_POOL_SCHEMA`, year-sharded files in a
flat directory) directly. Batches stream via `pq.ParquetFile(...).iter_batches()`
as in `snapshot_scan.py`. `work_id` is the int64 OpenAlex id; aliases (merged
works) resolve through `filter/spec/aliases.json` before any state is keyed.

**Stage 1 searches; Stage 2 filters.** The scan's only keyword decision is the
**search gate** — a broad token/stem alternation over the title and the raw
abstract inverted-index JSON, **or** membership of a replication concept — and it
exists because 510M works cannot be routed one rule bundle at a time. Everything
that follows from it is Stage 2's: exclusions, phrase precision, vocabulary,
rescues. Stage 1 applies no exclusion pattern and makes no precision judgement, so
there is exactly **one** rule set that decides what is a replication, and it is the
spec bundle in `filter/spec/`.

The engine therefore evaluates the specs itself, over pool text. It never called
`keyword_verdict()`, and that function no longer exists: with Stage 1 reduced to
the search gate, nothing evaluated it and it was deleted along with the phrase
lists, guards and exclusion loader that only it used.

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
| `pool_reader.py` | `iter_pool_batches(pool_dir, overlay_dir=None, batch_size=50_000, aliases=None)` — the engine's single input path: pool batches with overlay text written over the pool's `abstract_text`, empty or not. `overlay_manifest_hash(overlay_dir) -> str \| None` (defined in `overlay.py`, re-exported here so the input path is one import). The overlay is loaded once as a `work_id -> text` dict and applied per batch; with no overlay the stream is `iter_batches()` untouched. |
| `overlay.py` | `worklist(con, release_id, pool_dir, out_path, aliases=None) -> int` (the `pending_reason='no_text'` rows joined to the pool for doi/title/year); `write_chunk()`, `load_overlay()`, `overlay_work_ids()`; `validate(overlay_dir) -> list[str]`; `freeze(overlay_dir, pool_manifest_hash=None) -> dict`; `overlay_manifest_hash()`, `read_manifest()`. |
| `backfill.py` | `python -m filter.engine.backfill --worklist F [--overlay-dir D] [--run] [--limit N] [--source S] [--freeze]` — the six abstract sources over a worklist (OSF registrations first), results appended as an overlay chunk. `--overlay-dir` defaults to `OVERLAY_DIR`, the same directory the engine's commands read. |

**An overlay row wins, present pool text or not.** Every overlay row was written
deliberately by a backfill this project ran, against a worklist this project
built, so it is not a stray source quietly rewriting the corpus — and the rows
that need REPLACING rather than filling are exactly the ones a fill-only overlay
would leave in front of the voters: the boilerplate `abstract_text` the snapshot
ships for some records ("International audience", a bare keyword list) is a
non-empty cell carrying no evidence. The overlay hash names the text either way,
so a release is still bound to the bytes its rules were routed against.

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
change the release id — `route` folds the frozen hash into `routing_release(...)`,
and `export` refuses when the directory's hash is not the one the release was
routed under, exactly as it refuses a moved bundle; `handoff` and `screen` refuse
the same way. An overlay directory holding chunks but no frozen manifest raises
rather than routing: a release must not be bound to bytes nobody named.

**The overlay loads itself.** `route`, `export`, `screen` and `handoff` read
`OVERLAY_DIR` (`shared/config.py`, `FLORA_OVERLAY_DIR`; default
`<cache>/engine/overlay`) whenever that directory holds overlay chunks. An absent
flag means "use the overlay if there is one", because a rule that matches only
overlay text — the `osf-registration-*` pair reads the registration template on
the overlay's first line — fires on nothing without it, and a live rule matching
nothing is invisible in every count the run prints. `--overlay DIR` uses another
directory; `--no-overlay` asks for the bare pool out loud. Each of the four
commands prints one line, `overlay: <dir> (hash <12>)` or `overlay: none`, so
which text a run read is in its output rather than in its shell history.

**The backfill reuses Stage 1's fetchers.** `search/fetch_abstracts.py` owns the
sources, their measured order (OSF → OpenAlex → Europe PMC → S2 → CrossRef →
Scopus), their batch shapes, their per-identifier cache under `cache/abstracts/` and their
per-source checkpoint namespaces; `backfill.py` imports its phase runners rather
than restating any of it. Only the two ends are new: the worklist comes from the
routing table, and results land in an overlay chunk instead of a CSV merge. The
shared cache means a DOI Stage 1 already asked about
costs nothing here, and a miss recorded here is one Stage 1 will not re-buy.
Dataset-prefix DOIs are dropped from the worklist — no source has an abstract
for a deposit.

**The OSF source is the one that recovers a decision rather than an abstract.**
The registrant `10.17605` covers 25,819 pool rows, 3,016 of them textless, and 21
known FLoRA papers — so neither the registrant nor the missing abstract may
discard. What separates them is the registration TEMPLATE
(`attributes.registration_supplement`), and a spec cannot call an API at routing
time, so the phase writes it as the FIRST LINE of the recovered text
(`OSF registration template: <name>`) with the registration's responses form
under it — a median 5,268 characters that OSF keeps out of `description`. Two
specs read that line: `osf-registration-completed` (936) admits a post-completion
template on its own, and an `Open-Ended Registration` carrying the replication
stem; `osf-registration-protocol` (935, shadow) discards the rest, which is the
preregistration vocabulary. Both sit above the 700s admission band because for
these rows the template line is a better statement of what the record is than
any phrase in the responses text — a preregistration says "we will replicate
Smith (2009)" and means it has not happened yet. The phase runs first in the
source order so nothing displaces that first line, and it is restricted to its
registrant: the endpoint answers about OSF GUIDs and nothing else. `OSF_TOKEN`
is optional and only raises the throttle.

**Dry-run is the default** (§6). Without `--run`, the CLI prints per-source
targets, request counts and quota caps computed against the live checkpoint —
what a run would do *next*, not what a fresh worklist would cost — and fetches
nothing. Resumption is two independent mechanisms: the phase runners skip
checkpointed identifiers, and the chunk write skips work ids the overlay already
covers, so an interrupted run re-fetches nothing and cannot write a work into a
second chunk.

## Editing the rule book

Two standing facts about spec edits, both worth stating rather than rediscovering.

**Rules are Stage 2's alone.** No spec pattern runs in the Stage 1 path: the scan's
only keyword decision is the search gate. Narrowing or widening a rule changes
which pool rows the engine routes and nothing about which rows enter the pool, so a
wrong call costs a `route` re-run over the pool, never a rescan.

**A new admission arm usually needs no scan change.** Every arm of
the `replication-claim-*` tiers and `replication-signal` contain the `replicat` stem or a
multilingual equivalent, so the search gate's token alternation already keeps those
rows in the survivor pool. An arm built on vocabulary the gate does not carry — most
of `replication-probe` — reaches only the rows some other gate token admitted, and
its counts have to be read with that in mind.

The measured history of the rules this bundle replaced, including issue #147's four
narrowings and the evidence behind each, is in
[`filter/spec/rule_ideas.md`](../filter/spec/rule_ideas.md).

Every same-sentence regex leaves 11–16 genuinely qualifying GWAS replications
unreachable: they attribute the prior report in a different sentence from the
"we replicated…" claim. That is a screen judgment, not a keyword one.

## Milestone 4 — the claimed LLM tiers and the Stage 3 switch

Rules route; only these tiers admit. Two runners in `filter/engine/tiers.py`, one
handoff in `filter/engine/handoff.py`, two CLI subcommands.

| Module | Contract |
| --- | --- |
| `tiers.py` | `pile_works(con, release_id, pile, pool_dir, …) -> list[Work]` (routing joined to overlay-aware pool text); `estimate(works, tier)` / `render_estimate(est)` (the §6 dry run); `run_screen_cheap(...)` / `run_screen_expensive(con, client, release_id, work_ids=None, *, mode, batch_label, limit, run)`; `tier_decisions(client, release_id, tier, mode="live")` — `release_id=None` reads every release, because a verdict follows the work — and `decided_work_ids(client, tier)` (the checkpoint). |
| `handoff.py` | `write_handoff(con, pool_dir, out_csv, release_id, *, drop, record_types, decided, …)` — both screen piles in `ENGINE_EXPORTED_COLS` order, `screen_expensive` first; `decided` is a set of work ids for the screened-only export and `None` for as-routed; `decisions(client) -> (drop, record_types, decided)` from the live verdict rows of every release (the release scopes the piles, not the evidence). |

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
  `cache/engine/responses/<hash>.json` *before* `record_verdict`, which inserts the
  row `response_pending_upload`. Blobs go to Hugging Face in **multi-file commits**
  of `FLORA_HF_COMMIT_BATCH`, plus a final flush — one commit per blob is what HF
  answers with `HTTP 429` — and only a commit that was accepted turns the state
  into `uploaded` (`client.mark_uploaded()`). A push that did not happen, an
  unconfigured HF (`HF_TOKEN`/`FLORA_POOL_REPO`) and `ENGINE_TIER_HF_UPLOAD=false`
  all leave the row `response_pending_upload`, never `uploaded`, for a later
  reconciliation run.
- **Per-work checkpointing, server-side, in parallel.** A verdict is written per
  work; an interrupted run's claim is failed and the next run re-claims only the
  works with no verdict. Works are independent, so `ENGINE_TIER_WORKERS` (default
  8) of them are judged at once; the request rate is bounded by the per-provider
  limiter in `shared/llm_client.py`, which reserves a slot per call under a lock
  rather than sleeping on a shared timestamp. `TokenBudgetExhausted` fails the
  claim and stops cleanly, keeping what was decided.

### The Stage 3 switch

`python -m filter.engine handoff --out data/filtered.csv` writes the file Stage 3
reads: both screen piles, `screen_expensive` first, in `ENGINE_EXPORTED_COLS`
order, with a live `screen_expensive` record type written into `filter_status`
(`filter_method = screen`).

A row travels on a verdict, not on a routing decision. Rules route and only LLMs
admit, so the default export is screened-only: a work a live run discarded is
left out as `dropped_by_tier_verdict`, and a work no live run settled — never
screened, or short of the second vote a gate needs — is left out as
`skipped_unscreened`. The two counts plus `rows` account for every work in the
piles. `--as-routed` exports the piles as routed with whatever verdicts exist,
and is the only mode available without Supabase; asked for screened-only with no
claims client the command refuses rather than writing an empty file.

It reuses `export_pile()`'s row logic
via `iter_export_rows()` and keeps its release-binding refusal — but its manifest
is rewritable, because the handoff is a materialized view Stage 3 re-reads, not an
immutable artifact. `export` remains the command for an immutable copy.

`export --pile needs_human` prints the pile's size prominently: §2 asks for it
"exported with a size attached", and the size is the queue a person has to work
through.

### Retirement

`filter/rule_filter.py`, `filter/run_filter.py` and `filter/reset_backfilled.py`
are deleted, with their tests. `filter/phrase_detection.py` stays, but only for
what the **search gate** needs — the stem/token alternation the scan runs over the
snapshot. It applies no exclusion patterns and gates no admission, so there is no
second rule set to hold in parity with the bundle: the engine is the only thing
that decides a row's fate, and the pool is the only thing it decides over.

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
