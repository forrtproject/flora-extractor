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
others; `conventions.json` declares them; `export-csv` exports the two screen piles
only, so an exported row's `pending_reason` is always empty.

## Module map (`filter/engine/`)

| Module | Contract |
| --- | --- |
| `spec.py` | `FilterSpec` (frozen dataclass mirroring the JSON), `load_specs(spec_dir) -> list[FilterSpec]` (validated, sorted by precedence desc, ids unique), `bundle_hash(spec_dir: Path) -> str` (sha256 over the bundle directory's (filename, canonical JSON) pairs in name order, **including `conventions.json`** — see "The bundle a release is bound to"), `canonical_json_digest(path) -> bytes` (the one place a bundle input becomes bytes to hash: the parsed JSON re-serialised with sorted keys and no layout), `validate_spec(dict) -> list[str]` (error strings), `re2_error(pattern) -> str | None` — RE2's own complaint about a pattern it cannot run (lookaround, backreferences, conditionals, `\G`, atomic groups, possessive quantifiers), asked of pyarrow at spec load so it names the file rather than crashing a routing run. |
| `backends.py` | The one evaluator: `eval_spec_batch(spec, batch: pa.RecordBatch) -> pa.BooleanArray` (pyarrow compute). `eval_spec_rows(spec, rows: list[dict]) -> list[bool]` is the row-shaped entry point onto it — it builds a batch of the readable columns and calls `eval_spec_batch()`, so an analysis script and a routing run cannot read a spec differently. `match_evidence(spec, batch) -> list[str]` reports WHERE each matched row matched, for `filter_evidence`. |
| `route.py` | `route_batch(specs, batch, aliases=None, evals=None) -> pa.Table` — the seven columns of `ROUTING_SCHEMA`: `work_id (int64), pile (str), pending_reason (str), rule_id (str), precedence (int32), matched_rules (list<str>), evidence (str)`; `matched_rules` holds every non-shadow match (overlap diagnostics need the full cross-product), shadow matches are recorded separately in evaluations. |
| `workids.py` | `work_id(openalex_id: str) -> int` (`https://openalex.org/W123` → `123`); `load_aliases(path) -> dict[int, int]` from `filter/spec/aliases.json` (old_id → canonical_id, empty to start); `alias_release(path) -> str` (the alias file's `canonical_json_digest()`, for the same reason the bundle hash is canonical). |
| `release.py` | `routing_release(pool_manifest_hash, overlay_hash, bundle_hash, engine_version, alias_release, schema_version) -> str` (sha256 of the canonical JSON); `write_release(...)`/`read_release(...)` under `cache/engine/releases/<id>.json`. Overlay hash is `None` until M3 (text overlays); pool manifest hash comes from `--pool-manifest-hash`, else `search.snapshot_scan.pool_fingerprint(pool_dir)` — a hash over the search gate the pool's rows were admitted under (read from the pool's `_pool_provenance.json`, never from the local checkout, so a shared pool fingerprints identically on every machine) plus every pool parquet as `(filename, size_bytes, num_rows)`, read from the footer. It names the POOL, which is what routing consumes, rather than one machine's scan ledger, which a pulled pool does not have; a directory with no parquet in it, or one holding fewer files than its sidecar says complete it, gives `unmanifested:<12 hex>` (a genuine anomaly — you cannot route a pool you do not have whole), never a hash of nothing; the suffix hashes the parquet names and sizes so two different unfingerprintable pools do not mint one release id and share its claims and verdicts, and the visible prefix keeps it from reading as provenance. `route` re-computes it after the routing pass and rolls the build back if it moved. |
| `store.py` | Local DuckDB acceleration cache (gitignored, disposable): `open_store(path)`, `build_routing(con, pool_dir, specs, release_id, aliases=None, batch_size=50_000, batches=None)` (streams pool parquet through `route_batch`, persists `routing` and `evaluations(release_id, work_id, spec_id, spec_hash, matched)` incl. shadow specs; `batches` is the M3 overlay seam — `pool_reader.iter_pool_batches()` is passed in through it), `pile_counts(store, release_id)`, `sample_pile(con, release_id, pile, n=20, seed=17)`, `drop_release(con, release_id)` (the one thing that deletes a release, for the caller that learns only after the build that it must not exist — the pool moved under it). `routing` is keyed `PRIMARY KEY (release_id, work_id)` and inserts `ON CONFLICT DO NOTHING`: a pool holding both a merged id and its canonical id holds two rows for ONE work, and first-writer-wins is what keeps that one routed work and one exported row. A build is one transaction — the delete and every insert commit together — so an interrupted run leaves the release absent or as its previous complete build, never half-replaced. Deleting the DB loses nothing: everything rebuilds from pool + specs. |
| `diagnostics.py` | `diagnose(pool_dir, spec_dir, spec_id, *, baseline_dir=None, sample_n=20, seed=17) -> dict` — routes the pool twice, with and without the spec (the baseline bundle defaults to the same directory minus the spec). The §3 rule-diagnostics function: rows moved per (source pile → destination pile); overlap/agreement matrix vs every other rule (exclusive hits vs covered); a readable random sample (n≈20, seeded) of moved rows; holdout effect (reads `filter/spec/holdout.json`; reports `"holdout": "not_constructed"` until decision #146-2 lands); for discard specs, whether a `measured` entry exists (else the spec must be shadow). Renders JSON + a human-readable text block. |
| `export.py` | `export_pile(con, pool_dir, pile, out_csv, release_id, from_year=None, to_year=None, conventions=None, specs=None, aliases=None, spec_dir=SPEC_DIR, expect_bundle_hash=None, expect_alias_release=None, overlay_dir=None, expect_overlay_hash=UNCHECKED, created_at="")` — writes the Stage 3 contract: `ENGINE_EXPORTED_COLS` = `FILTERED_COLS` + `ENGINE_EXPORT_COLS` (see below) + `SCREEN_COLS`, the last six blank because `export` applies no tier verdicts; writing them blank rather than omitting them is what lets Stage 3 accept an exported pile at all. Written through `write_rows_tmp()`, the one atomic CSV writer this module shares with `handoff.py`, so an interrupted export leaves the previous file rather than half of a new one. `utf-8-sig`, `paper_type`/`filter_method`/`filter_evidence`/`filter_confidence` derived via the conventions mapping. Also `export_manifest(...)`: a JSON naming release id, pile, row count, and content hash next to the CSV (immutable once written). |
| `cli.py` / `__main__.py` | `python -m filter.engine specs\|route\|diagnose\|export\|screen\|reconcile\|export-csv\|worklist\|release-claim\|status`. The subcommand list is `cli.py`'s `add_parser` calls; `--help` is authoritative, `docs/cli-reference.md` is the prose. Two flags cut across the subcommands: `--release <id>` (on `export`, `screen`, `export-csv`, `worklist` and `release-claim`) names which release to read, defaulting to the store's only one and refusing when the store holds several — a re-route must never be resolved silently; `diagnose --sample N` (default 20) sets how many seeded example rows the diagnosis prints. |

`ENGINE_VERSION` lives in `filter/engine/__init__.py` and is bumped whenever routing
behavior changes without a spec change.

### The bundle a release is bound to

`bundle_hash()` covers the spec files **and `conventions.json`**, and it hashes each
file's parsed JSON re-serialised canonically rather than its bytes: reindenting a
spec or reordering its keys is not a new bundle, while a change to any value —
including one character of a regex, which the reserialisation reproduces exactly —
still is. `alias_release` is canonical over JSON in the same way. The engine routes
a row into a pile; the conventions decide what that pile is *called* in an export
(`paper_type`, `filter_confidence`, whether the winning rule's `vocabulary`
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

### One regex engine, and what Unicode does to it

Every spec is evaluated by pyarrow compute, whose matcher is RE2, and by nothing
else. There used to be a second implementation in Python `re` for row-at-a-time
callers, kept equal to the first by `re2_safe()` and by a sampling `verify`
command. The equality was never provable: RE2-safe syntax does not make the two
engines' character classes identical — `re` reads `\w`, `\b` and `IGNORECASE` as
Unicode-aware, RE2 reads `\w` and `\b` as ASCII — so a pattern legal in both
could still claim different rows. Sampling found this in the wild: over 289,141
pool rows, `reproduction-signal`'s `\bre-?analy[sz](?:is|es|ed|ing|e)\s+of\b`
matched one row in RE2 that `re` rejected, because the abstract's mojibake `Â`
sits where the trailing `\b` looks and `re` counts it as a word character. The
duplicate is deleted rather than defended; `eval_spec_rows()` is now the
row-shaped entry point onto `eval_spec_batch()`.

The retired `phrase-with-cite` rule is the historical version of the same hazard:
its surname atom `[\w'’.-]` matched `García et al. (2020)` in `re` only, and was
replaced by the explicit negated class `[^ \t\n\r\f,;:()\[\]]` (archived in
`filter/spec/rule_ideas.md`). With one engine the question a `\w` now raises is
narrower — not "do the two agree" but "does RE2 read this the way the rule
author meant" — and it is answered by writing the intended class out.

`re2_error()` is what remains of the safety check, and it is a syntax check, not
an equality claim: it hands the pattern to RE2 at spec load, so a lookaround or a
possessive quantifier is a named error against a named file instead of an
exception partway through a routing run. RE2 rejects more than a hand-written
scanner of banned constructs could enumerate, which is why the check asks RE2
itself. Patterns are also `re.compile`d at load, because `match_evidence()` uses
`re` — to report the SPAN inside a string the backend has already matched, which
is the one thing RE2-through-pyarrow cannot report. That locator decides nothing:
where the two engines read a span differently it finds none, and the evidence
string names the condition instead of a phrase.

Text is folded and NFC-normalised where it becomes matchable
(`_normalize_array()` in `backends.py`, called from `BatchContext.__init__`): every Unicode space separator to a plain space, the zero-width
space and BOM away. That is why the bundle needs no decomposed-Unicode twin of its
multilingual stem arm, and why an abstract using U+00A0 as its word separator
still matches a spec's literal space. `pc.utf8_normalize` is not used: on pyarrow
25 it returns its input unchanged for NFC.

The standing test is `test_the_row_entry_point_is_the_batch_backend` in
`tests/test_engine_route.py`, which runs every shipped spec over both corpora —
including `UNICODE_CORPUS`, accented Latin, NFD, CJK, Hangul, Cyrillic and
fullwidth. A new pattern that needs `\w` or `\b` next to text that may not be
ASCII belongs in that corpus before it ships.

### Provenance columns (`ENGINE_EXPORT_COLS` in `shared/schema.py`)

Issue #148's requirement: what the pool knows must survive into materialized
artifacts. Appended after `FILTERED_COLS`:
`oa_type, hit_concept, route_rule, route_precedence, matched_rules, pending_reason,
release_id`. (`matched_rules` is |-joined; match by substring/split like
`screen_categories`.) Stage 3 ignores trailing columns it does not read, so its
contract is untouched.

**They stop here, on purpose.** `EXTRACTED_COLS` excludes `ENGINE_EXPORT_COLS`, so
Stage 3 does not carry the block into `extracted.csv` and the validation import does
not push it. Provenance is **linked, not duplicated**: that import writes
`record_metadata.work_id` (the int64 id, via `filter/engine/workids.work_id()`),
and every routing column is recoverable by joining that against the `routing` table
for a release — the same join `filter/engine/supersede.py` already uses. Widening
the CSV schema would create a second copy that can drift from the store.

### Pile → `paper_type` mapping

Lives in `filter/spec/conventions.json` (machine-read by `export.py`), explained in
`CONVENTIONS.md` — read the JSON, not this table, when the answer has to be right:

| pile | paper_type | filter_confidence |
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
its rows are exported as `needs_review`/high, while a cheap rule that names
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
    "url_regex": null,
    "fields": {"type": ["dataset"]},
    "abstract_missing": null,
    "any_of": [],
    "all_of": [],
    "none_of": []
  },
  "domain": null,
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
  `abstract_text`; `text_regex` over `title + "\n" + abstract_text`;
  `url_regex` over the row's own URL — `open_access.oa_url`, falling back to
  `primary_location.landing_page_url`. The pool has no `url` column: those two
  are JSON strings, and `backends._url_array()` pulls the value out of the JSON
  text, on FIRST USE only, so a bundle where nothing writes `url_regex` pays
  nothing (~6 s over the 5.1M-row pool when something does). All regexes
  are **RE2-safe** (no lookaround/backreferences — enforced by `validate_spec`)
  and case-insensitive by default (`(?i)` is prepended; `re2_error()` rejects at
  spec load anything RE2 cannot run).
- `doi_prefix` matches after `clean_doi()`; `fields` is exact membership on pool
  columns (`type`, `publication_year`), except `fields.concept_ids`, which tests
  membership of bare concept ids in the row's `concepts` (both URL-form JSON and
  bare forms).
- `abstract_missing: true` matches rows with empty/null `abstract_text`.
- A pattern RE2 cannot run cannot ship. There is no escape hatch: the retired
  `pyre_regex` key, which recorded a lookaround original next to its RE2
  decomposition, is now an unknown-key error, and the original belongs in
  [`rule_ideas.md`](../filter/spec/rule_ideas.md) beside the arms that replaced it.
- `pile` must be one of the four routable piles; `pending` is never a spec target.
- `vocabulary` (`"replication"` / `"reproduction"` / null) feeds the status mapping.
- `domain` (optional) is a match object naming the population the rule claims to
  govern. It decides no routing. After a route, every live spec that declares one
  is reported with its domain size, its match count, and the works in its domain
  it did not match that another rule admitted to a paying pile — the count that
  would have caught the 2026-08-08 campaign, where a live discard over OSF
  registrations reached only the ones the text overlay had written text for.
  A domain must name its population by every identifier the pool gives it: the
  two `osf-registration-*` specs declare the OSF registrant OR a DOI-less row
  with an osf.io URL, because 202 of the 367 OSF records in the 2026-08-08
  export have no DOI and a DOI-only domain reported them as no population at
  all.
  Policy and how to write one: `filter/spec/CONVENTIONS.md`, "`domain`".

A LIVE (`shadow: false`) `discard` spec has three extra validation rules, all in
`validate_spec()`:

1. It needs at least one `measured` entry.
2. At least one of those entries must be at an **autonomous** level
   (`human`, `downstream`, `trusted`, or `llm:<model>`) — heuristic-only evidence
   may not discard.
3. If it reads `abstract_regex` or `text_regex`, it must carry
   `"abstract_missing": false` at the top level of its match. A row with no text
   has said nothing, and an empty string is not evidence for deleting the work.

`shadow: true` lifts all three.

The full accepted key sets, from `spec.py`: top level `id, description, match,
domain, pile, vocabulary, precedence, shadow, measured`; inside a match or a
domain `doi_prefix,
doi_regex, title_regex, abstract_regex, text_regex, url_regex, fields,
abstract_missing, any_of, all_of, none_of`; inside
`fields` `type, publication_year, concept_ids`; inside a `measured` entry
`level, precision, n, sample, date, owner, rationale`.

## The shipped bundle (rule book v2)

Sixteen specs, designed in `redesign/rulebook_v2.html` and measured there. The book
is a **whitelist**: nothing is screened unless a positive rule admits it, so
there are no exclusion or rescue rules. The nineteen specs it replaced
— the six vocabulary exclusions, the two rescues, the four phrase/stem rules and
the identifier/type rules it absorbed — are archived with their patterns and
evidence in [`filter/spec/rule_ideas.md`](../filter/spec/rule_ideas.md).

| spec | pile | prec. | shadow | content |
| --- | --- | --- | --- | --- |
| `not-a-paper-doi` | discard | 960 | | `/reviews/`\|`/decisions/` paths · terminal `.suppl` |
| `deposit-registrant` | discard | 958 | | 11 deposit-only registrants. Figshare proper (10.6084) is not among them — D1 took it off the prefix list, and it has its own title-gated rule below |
| `figshare-attachment` | discard | 956 | | figshare (10.6084) DOIs whose title marks the object as an attachment to a paper rather than the paper |
| `not-a-paper-title` | discard | 955 | | the start-anchored genre-plus-parent title pattern |
| `not-a-report-type` | discard | 940 | | `type ∈ {component, database, dataset, software, supplementary-materials}` — not a report of a study |
| `osf-registration-completed` | screen_expensive | 936 | | admission on the OSF registration TEMPLATE (registrant 10.17605) |
| `osf-registration-protocol` | discard | 935 | | the discard twin: an OSF registration whose template marks it a protocol, not a completed study |
| `replication-claim-cited-title` | screen_expensive | 760 | | a claim arm in the TITLE **and** an author-year citation in the title — the narrowest of the three live admissions |
| `replication-claim-title-strong` | screen_expensive | 750 | | the two title arms that measure as high-precision on their own |
| `replication-claim-title-broad` | screen_expensive | 740 | ✓ | the other ten arms of the twelve-arm title family |
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
(`0003_claim_release_message.sql` re-states the claim RPC with a corrected
rejection message; a database created from today's `0001` already has it.
`0004_claim_expiry.sql` adds the claim lease described below and must be run
before any checkout that sends one — the client refuses to claim without it and
names the file.)

| table | role |
| --- | --- |
| `engine_releases` | server-side registry of routing releases (the six-input id + payload). A claim naming a release with no row here is REJECTED. `route` registers the release it writes (best-effort, so routing still works offline) and the claim path registers it once on demand, so re-routing is never the fix — the missing row is. |
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

**A claim is a lease.** A run killed outright never reaches its completion path, so
every claim also carries `expires_at = now + CLAIM_TTL_HOURS` (6 hours, a plain
constant in `filter/engine/claims.py` — LLM tier runs are measured in hours). Once
the lease passes, the claim blocks nothing: the RPC's conflict check and
`claimed_work_ids()` both require `expires_at > now()`. The status is untouched and
so are the verdicts — expiry frees works, it never retracts evidence.
`python -m filter.engine release-claim` lists open claims with their item counts and
leases, and `--claim <id> [--status failed] --yes` ends one immediately through the
same `engine_release_claim` RPC a finishing run calls.

**Permanence is enforced by the database.** Triggers reject every DELETE on
`engine_verdicts` / `engine_human_labels` / `engine_audit`, and every UPDATE except
`superseded_by`, `response_state`, and a write-once fill of `response_hash` (the
`response_pending_upload` reconciliation path). A verdict a script can quietly
rewrite is not evidence.

**Client:** `filter/engine/claims.py` — `ClaimsClient` (house PostgREST style, the
same `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` `shared/supabase_client.py` reads).
`register_release` · `claim` · `release_claim` · `record_verdict` ·
`supersede_verdict` · `active_claims` · `claimed_work_ids` · `claim_item_count`. An
unset `SUPABASE_URL` raises `ClaimsNotConfigured` at construction: the engine must
not silently run unclaimed. A conflict raises `ClaimConflict` naming the tier, and a
database without the claim lease raises `ClaimExpiryUnsupported` naming
`db/migrations/0004_claim_expiry.sql`.

**Every call goes through one transport seam, and a blip is retried** (#189). A tier
run makes one claims call per verdict, so an hours-long run meets a connection error
or a read timeout eventually — two overnight extract campaigns died on exactly that.
`ClaimsClient._request` retries a connection error, a timeout, an HTTP 5xx and a 429
three times at 1s/2s/4s, and then raises `ClaimsError`: nothing is ever swallowed, and
a 4xx that is not 429 is not retried at all, so an auth or config failure still fails
at once. What each write does about a retry follows from whether the database can
absorb the same write twice:

| call | on a retry |
| --- | --- |
| reads, `mark_uploaded`, `supersede_verdict`, `register_release`, `renew_claim` | idempotent already (filtered PATCHes, `resolution=ignore-duplicates`, a lease that never shortens) |
| `record_verdict` | the row carries an id minted client-side, once, and the insert upserts on it, so a replay updates the row to the values it already holds rather than adding a second verdict for the work |
| `release_claim` | a `claim_not_active` refusal is forgiven **after a retry only** — an earlier attempt reached the server and ended the claim, which is what the call wanted |
| `claim` | the RPC is one transaction and cannot be replayed. A `ClaimConflict` met after a retry says so, because the claim holding the works may be the run's own; nothing here can tell it from a second runner. The orphan lapses with its lease either way |
| `record_supersession` | not retried. `engine_supersessions` is append-only, so there is no upsert to replay into, and its callers are operator commands re-run whole |

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
| `backfill.py` | `python -m filter.engine.backfill --worklist F [--overlay-dir D] [--run] [--phase all\|bulk\|targeted] [--limit N] [--source S] [--batch-size N] [--scopus-limit N] [--include-openalex] [--dry-run] [--freeze]` — the six abstract sources. `--phase bulk` with no `--source` runs Europe PMC alone and says so in one line, because OpenAlex is bulk-shaped but opt-in. over a worklist in two pathways (bulk, then targeted), results appended as an overlay chunk. `--overlay-dir` defaults to `OVERLAY_DIR`, the same directory the engine's commands read. |

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
routed under, exactly as it refuses a moved bundle; `export-csv` and `screen` refuse
the same way. An overlay directory holding chunks but no frozen manifest raises
rather than routing: a release must not be bound to bytes nobody named.

**The overlay loads itself.** `route`, `export`, `screen` and `export-csv` read
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
sources, their measured order, their batch shapes, their per-identifier rows in
the abstract store and their per-source checkpoint namespaces; `backfill.py`
imports its phase runners rather than restating any of it. Only the two ends are
new: the worklist comes from the routing table, and results land in an overlay
chunk instead of a CSV merge. The shared cache means a DOI Stage 1 already asked
about costs nothing here, and a miss recorded here is one Stage 1 will not
re-buy. Dataset-prefix DOIs are dropped from the worklist — no source has an
abstract for a deposit.

**Two pathways, because the sources do not cost the same thing.** The `--phase`
argument names them:

| Pathway | Sources | Shape | Which rows |
| --- | --- | --- | --- |
| `bulk` | Europe PMC (OpenAlex opt-in) | batched, keyless, unquota'd; one request answers about `EPMC_BATCH_SIZE` / `OA_BATCH_SIZE` identifiers | every worklist row, and cheap enough for a worklist much wider than a release's `no_text` rows |
| `targeted` | OSF → Semantic Scholar → CrossRef → Scopus | one call per DOI, or gated by a key, an IP-bound entitlement or a ~10k/week quota | only the rows bulk left without text, on the worklist that matters |

OpenAlex is in the bulk pathway's shape but not in its default run: it needs
`--include-openalex` (or `--source openalex`). Measured yield on this corpus is 0
of 200 — the pool was discovered via OpenAlex and the live API's abstracts come
from the same deposit stream the snapshot did — so it pays only against a
snapshot old enough for post-snapshot deposits to be plausible.

`--phase all` (the default) runs both over the one worklist. Running them
separately is what the split buys: a wide `--phase bulk` pass fills the shared
abstract store for rows no release needs yet — a miss recorded there is one
nobody re-buys — and a later `--phase targeted` pass over the release's `no_text`
worklist spends the gated calls only on what is still missing.

Europe PMC carries the bulk pathway because it has no id-list endpoint but does
take a boolean query, so `DOI:"a" OR DOI:"b" …` in one form POST to `searchPOST`
*is* its batch API, with no URL-length ceiling (verified live 2026-08-05: a
500-DOI, 12.9 kB query answers HTTP 200). A page holding fewer records than the
response's `hitCount` is refused as a whole batch rather than checkpointed: a
truncated page says nothing about the DOIs it left out, and a miss is permanent.

The one exception to the narrowing is OSF. Every other source is asked for an
abstract, and one abstract is as good as another; the OSF phase is asked for the
registration template line the two `osf-registration-*` specs read, which no
abstract substitutes for — so its targets are the full worklist's registrant
DOIs, and `SOURCE_ORDER` (the order a recovered text is ATTRIBUTED in, distinct
from the order calls are spent in) keeps `osf` first so nothing displaces it.

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
stem; `osf-registration-protocol` (935, live) discards the rest, which is the
preregistration vocabulary. Both outrank the admission rules in the 700s because for
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

## Milestone 4 — the claimed LLM tiers and what leaves the engine

Rules route; only these tiers admit. Two runners in `filter/engine/tiers.py`, one
record export in `filter/engine/handoff.py`, two CLI subcommands.

| Module | Contract |
| --- | --- |
| `tiers.py` | `pile_works(con, release_id, pile, pool_dir, …) -> list[Work]` (routing joined to overlay-aware pool text); `estimate(works, tier)` / `render_estimate(est)` (the §6 dry run); `run_tier(spec, client, release_id, works, *, mode, batch_label, run)` (the generic runner) with the two pile-reading wrappers `run_screen_cheap(...)` / `run_screen_expensive(con, client, release_id, work_ids=None, *, mode, batch_label, limit, run)`; `tier_decisions(client, release_id, tier, mode="live")` — `release_id=None` reads every release, because a verdict follows the work — and `decided_work_ids(client, tier)` (the checkpoint). Both read only the current SCREENING GENERATION — `screening_generation(tier)`, the hash of the tier's voter pair and its prompt, recorded in each claim's `meta.generation`: change a voter model or the classify prompt and the old verdicts stop settling their works and stop steering the export, while rows written before the field are grandfathered on their recorded `model`. |
| `tiers.py` — `TierSpec` | What differs between tiers, as one frozen record: `name`, `judge` (ask the question), `decide` (read the stored rows back as a decision), `generation`, `accepts_legacy`, `estimate` / `render_estimate`, and the `workers` / `ttl_seconds` / `batch_size` a tier wants instead of `ENGINE_TIER_WORKERS` / `CLAIM_TTL_HOURS` / `FLORA_HF_COMMIT_BATCH`. The two screen tiers register theirs (`SCREEN_CHEAP`, `SCREEN_EXPENSIVE`) at the bottom of the module; anything else calls `register_tier(spec)` at import time and is then reachable by name through `tier_spec(name)`. `run_tier()` takes a spec and a list of `Work`, so a tier defined outside `filter/engine` runs on this spine without this package importing it — the import direction is one-way, and must stay so. |
| `handoff.py` | `write_handoff(con, pool_dir, out_csv, release_id, *, drop, screen, decided, …)` — both screen piles in `ENGINE_EXPORTED_COLS` order, `screen_expensive` first; `decided` is a set of work ids for the screened-only export and `None` for as-routed — `set(screen)` is what a caller reading from `decisions()` passes, since the works the expensive tier settled are exactly that map's keys; `decisions(client) -> (drop, screen)` from the live, current-generation verdict rows of every release (the release scopes the piles, not the evidence). `screen` maps a work to what the EXPENSIVE tier decided — outcome, record type, votes — and `screen_columns()` writes that onto the row as `SCREEN_COLS`. **Only the expensive tier admits**: a cheap verdict contributes to `drop` and to nothing else, because its `proceed` means "on to the expensive screen" and a `prescreen_bypass` means "we did not ask". |

### The two tiers

- **`screen_cheap`** asks `shared/prescreen.py`'s question through
  `prescreen_vote()` — same prompt, same models, same parsing, same three bypasses
  (`hard_signal`, `short_text`, a curated source) that refuse to let a 3B model end
  a row — and applies the gate itself, in `_cheap_judge()`: two explicit noes
  discard, everything else proceeds. A bypass is recorded as a verdict rather than
  skipped: deciding not to ask is a decision. The tier is **dormant** — see
  "Activating the cheap tier" below.
- **`screen_expensive`** is the validated front door — `classify_replication()`
  plus `screen_gate()`. It runs **here and nowhere else**: Stage 3 used to run the
  same call as its first step, free on the shared cache key but written twice, and
  only one of the two copies could be claimed, budget-gated or recorded as
  evidence. The verdict now travels to Stage 3 on the row (`SCREEN_COLS`), and
  `pile_works()` still reproduces `_row_from_snapshot()`'s `doi_r`/`title_r`
  mapping exactly — that identity is what lets `screen_columns()` read the category union
  and the voters' reasoning back out of the classify cache
  (`cached_classification()`, which can only read).

### Activating the cheap tier

It is wired, tested and unreachable: all three `screen_cheap` specs ship
`"shadow": true`, so no live row is routed to the pile and `screen --tier
screen_cheap` finds nothing to claim. That is deliberate — issue #146 §2 requires
the tier's zero-miss evidence, measured on a post-gate distribution, to be
re-validated on this one before it discards autonomously.

Waking it is one spec edit plus the measurement, in this order:

1. Drop `"shadow": true` from one of `replication-signal`, `reproduction-signal` or
   `replication-probe` (`filter/spec/`). Re-`route`; the pile now has rows.
2. Run the tier in its default `validation` mode (`screen --tier screen_cheap
   --run`, no `--live`). Both votes are recorded, nothing is discarded, and the run
   report's `revalidation` block compares what it WOULD have discarded against the
   piles the rules chose — the re-measurement issue #168 asks for.
3. Only then `--live`, which is what lets its discards take effect.

Nothing else changes: the prices, the voters, the bypasses, the verdict plumbing
and the `prescreen_discard` set-aside path are all in place and under test
(`tests/test_prescreen.py`, `tests/test_engine_tiers.py`). A cheap verdict still
never admits a row to Stage 3 — it can only drop one.

### Modes, and where a verdict takes effect

Issue #146 §2: the cheap tier's zero-miss evidence was measured on a post-gate
distribution and must be re-validated on this one. So `mode="validation"` is its
default — both votes are recorded, nothing is discarded, and the run report
compares the discards it *would* have made against the pile the rules chose.
`--live` is what makes them count. `screen_expensive` is live either way.

A run's mode lives on its **claim** (`meta.mode`), which is the only place the
fact is written down; verdict rows stay pure evidence. Nothing is ever written
into the routing table — routing is derived data and the next `route` erases it —
so a live discard takes effect where the rows leave the engine: Stage 3's
worklist and the record export both read the verdicts.

### Four invariants both runners hold

- **Claim before spend.** One `client.claim()` for the whole batch before the
  first voter. `ClaimConflict` ends the run with a refusal and no calls.
- **Dry run by default.** Without `--run`, nothing is claimed, fetched or spent;
  the row count, the abstract token-length distribution and "N rows → tier X ≈ $Y"
  are printed from the rough per-1k prices in `filter/engine/tiers.py`
  (`TIER_PRICE_PER_1K_IN`, `TIER_PRICE_PER_1K_OUT`, `TIER_OUTPUT_TOKENS`).
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

### The record export

`python -m filter.engine export-csv --out <file>` writes one release's screen
piles as a CSV: `screen_expensive` first, in `ENGINE_EXPORTED_COLS` order, with a
live `screen_expensive` record type written into `paper_type`
(`filter_method = screen`). `--out` is required — this file is a record someone
asked for, not an input anything re-reads, so it is named at the call.

Stage 3 consumes no file. `extract/tier.py` builds each work's row in process from
the routing release and the pool, through the same `iter_export_rows` +
`screen_columns` pair this command writes with, so the export and the extractor
see the same row by construction.

A row travels on a verdict, not on a routing decision, and only on the EXPENSIVE
tier's. Rules route and only the validated pair admits, so the default export is
screened-only: a work a live run discarded is left out as
`dropped_by_tier_verdict`, and a work no live `screen_expensive` run settled —
never screened, screened only by the cheap tier, or short of the second vote a
gate needs — is left out as `skipped_unscreened`. The two counts plus `rows`
account for every work in the piles.

**A screen that did not complete is not a decision, on either side.** When a
voter fails, the vote rows are still written — a call was made and
`engine_verdicts` is append-only — but they add up to nothing, so the work is
neither decided nor handed off: `decided_work_ids()` ignores it and the next
ordinary `screen --run` asks it again, with no flag to remember. Complete means
two DISTINCT voters answered: a re-run re-asks both, so the voter that already
answered answers again, and `_answer_rows()` keeps one answer per voter (the
latest) so a retry cannot stand in for the vote that never arrived. The cheap
tier reads the same way — it asks voter 2 only after a `no`, so `no` + a failed
call is incomplete, not a proceed: the missing answer is exactly the second `no`
that would have discarded. A run that
leaves works in that state says so (`incomplete screens N`), because a strand
nobody can see is a strand nobody fixes. And the cheap tier cannot overrule the
expensive one: a live cheap discard applies only to a work the expensive tier has
no verdict for. `--as-routed` exports the piles as routed
with whatever verdicts exist, and is the only mode available without Supabase;
asked for screened-only with no claims client the command refuses rather than
writing an empty file. The two modes write the same columns, so a file's name is
the only thing that says which one produced it — name an as-routed export for
what it is.

**What the row carries.** Because the screen does not run again downstream, the
screen's answer is written onto the row: `screen_verdict`, `screen_record_type`,
`screen_categories`, `screen_votes`, `screen_evidence`, `screen_reasoning`
(`SCREEN_COLS`, `shared/schema.py`). `screen_votes` is the one that is not a
summary — Stage 3's pre-PDF title-search rung fires only when both voters gave a
qualifying answer AND stood behind it, and that cannot be recovered from a record
type. `screen_evidence` carries BOTH voters' quotes, as `<model>: <quote>` segments
joined by ` || ` (a quote may contain a single `|`), because the gate is the pair's
decision. An `--as-routed` row carries them blank for every work no live screen
settled. There is no way to run the screen in Stage 3.

`export-csv` reuses `export_pile()`'s row logic via `iter_export_rows()` and keeps
its release-binding refusal, and writes a manifest sidecar naming the release, the
row count and the file's sha256. That manifest is rewritable, unlike `export`'s:
the same release re-exported after more works were screened is a newer record of
the same thing. `export` remains the command for an immutable copy.

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

Two columns on `record_metadata` carry it: `work_id` (the int64 OpenAlex id derived
from `openalex_id_r` via `filter/engine/workids.work_id`) and `release_id` (the
export's release). The live import in the `flora-validation` repo does not send
either yet — see issue #172 — so records pushed so far have null lineage.
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
uuid string minted by the validation repo's import.

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

## Milestone 6 — the extract tier

Issue #146 §4 says money is spent under a claim and every judgment leaves permanent
evidence. Stage 3 was the last place that was not true of: it decided which original
a paper targets and what the outcome was, and the only record of it was a row
appended to a CSV on one machine. M6 runs the same ladder as a tier.

Nothing about the extraction moves. `extract/tier.py` registers a `TierSpec` whose
`judge` calls `run_extract._process_row` — the one per-row pipeline there has ever
been — and runs it through the same `run_tier()` spine the two screen tiers use.

**Status: shipped, and the authority is flipped.** `python -m extract.tier --run` is
the only front door, and `python -m extract.export` is the only writer of
`data/extracted.csv`. The CSV runner is deleted from `main` — its orchestration half
is parked on `wip/csv-runner`, and `extract/run_extract.py` remains as the per-row
pipeline library. `extract/promote_test.py` is gone with the `--extracted-test`
sandbox it promoted from; the sandbox is `--mode validation` plus a render.
`extract/sanity_check.py` reports rather than moves, and the two retroactive tools
(`audit_dois`, `backfill_authors`) write corrected, superseding verdicts through
`supersede_targets()` instead of editing the CSV.

| Module | Contract |
| --- | --- |
| `extract/tier.py` | `extract_generation()` / `generation_inputs()` (the ladder version, the six `_GENERATION_PROMPTS` versions and three model ids at their call sites' efforts); `extract_works(con, client, pool_dir, release_id, *, only, limit, redo, …) -> list[ExtractWork]` (the worklist); `run_extract_tier(con, client, release_id, *, mode, limit, run, batch_size, …)` (the batched claims loop); `estimate(works)` / `render_estimate(est)` (the per-rung dry run); `result_payload(source_row, doi_r, rows, observed)` and `render_payload(payload)` (the two halves of the round trip); `settled_work_ids(client)` (the checkpoint). CLI: `python -m extract.tier [--run] [--limit N] [--batch-size N] [--batch-label …] [--mode live\|validation] [--only ids] [--redo ids] [--release <id>] [--overlay path\|--no-overlay] [--store …] [--pool …] [--spec-dir …]`. `--release` is not optional in practice: the tier refuses when the store holds several releases, exactly as `extract.export` and the `filter.engine` subcommands do. |
| `extract/export.py` | `latest_results(client, *, mode, current_generation_only) -> (work → result row, superseded count)`; `rows_from_results(results)`; `partition(rows) -> (main, {set-aside file: rows})`; `render(client, …)`; `write(report, out_csv)`; `check(report, out_csv)`. CLI: `python -m extract.export [--out …] [--mode …] [--check] [--current-generation-only] [--release <id>\|--all-releases]`. |

### Two verdict-row kinds

The `verdict` column tells them apart.

- **`evidence`** — one per LLM call attributable to the work: the model, the version
  of the prompt it was asked, and a summary in the blob. Written before the result
  row that summarises them (§4's ordering), and never read back to make a decision.
- **the RESULT row** — exactly one per work per run. Its `verdict` is the row's
  ENDING, not a gate outcome: `resolved` · `provisional` · `not_a_replication` ·
  `no_original_found` · `target_pending` · `api_error`. Its `prompt_hash` is the
  extract generation and its `payload` (migration 0005) is the whole answer.

### The self-sufficiency rule

**A result payload rebuilds its work's `EXTRACTED_COLS` rows with no network, no
cache, no routing store and no pool.** Not an optimisation — the definition of what
the state authority holds. A payload that needed the pool would make the permanent
verdict a pointer into a multi-GB artifact that is re-scanned and re-released on its
own schedule; one that needed the cache would point into a directory that is
explicitly expendable (`shared/cache_sync.py`). Either way the row would stop being
evidence and become a bookmark.

    {"kind": "result", "generation": …, "doi_r": …,
     "input":   {FILTERED_COLS + SCREEN_COLS, exactly as the judge read them},
     "link":    {which rung answered, whether it accepted a single link,
                 n_targets, stated/unidentified counts, pdf_source, parse_method,
                 what stopped it},
     "targets": [one per original PAPER, post-guard, post-collapse, post-renumber:
                 every EXTRACTED_COLS value not inherited unchanged from `input`]}

`targets` is derived from the finished row dicts rather than from the ladder's
intermediate answers, so the export is a near-identity render and the round trip is
exact by construction. Deriving rows at export instead would put two copies of the
row-building rules in the codebase and make the export a place a row can silently
change shape. `tests/test_extract_export.py` is the gate: real rows from
`data/extracted.csv` through the payload and back, field for field, plus byte
equality of the written CSV.

Four columns look derivable from `input` and are stored anyway, because none is
derivable in general: `make_pair_id()`'s multi-original fallback hashes the target
RECORD's OpenAlex id, which is not on the row; `oa_work_id_r` falls back to an
OpenAlex lookup when `openalex_id_r` is blank; `bibtex_ref_r` is built from
FILTERED_COLS values the row may have rewritten; and `screen_categories` is blank
rather than copied on a row that reached no screen dict.

DOI verification runs inside the judge, once, and its answer is stored — it costs up
to three OpenAlex free-text searches at 10× a filter query, and an export that
re-verified would pay that bill on every render.

### `target_pending` and `api_error` do not settle

The checkpoint means "has a live current-generation result row whose verdict
settles". Those two endings are the ones a re-run is meant to redo — the same pair
`REOPENED_SET_ASIDE_FILES` names in `shared/schema.py` — so they leave the work in
the worklist. Counting either as decided turns a five-minute provider outage into a
permanent hole in the corpus.

A `target_pending` result nevertheless RESTS while it is fresh. One younger than
`EXTRACT_PENDING_RETRY_DAYS` (14, in `extract/tier.py`) is subtracted from the
worklist exactly as a settled work is, and comes back when the delay lapses, when a
new generation reopens everything, or when `--redo` names it. Without the rest, five
runs of one campaign each re-bought the same ~830 unresolvable works' queries.
`api_error` gets no rest — it retries on the next run.

That is why the tier reads its own checkpoint (`_decide`, `settled_work_ids`) rather
than the screens' `decided_work_ids`, whose semantics is wrong here twice over: it
calls a work decided when ANY row adds up to a decision, so a work with two evidence
rows and no result row would read as done and never be extracted; and it has no
notion of an ending that happened but settled nothing.

Two result rows for one work are two RUNS, not two voters, so the latest wins
(`created_at`, then the row id — the primary key is a uuid, so id order is not time
order). `accepts_legacy` is `False`: this tier's first verdict row is written by the
commit that adds it, so a claim with no generation is unattributable, not legacy.

### The worklist

Works admitted by a live, current-generation `screen_expensive` PROCEED verdict
(`handoff.decisions()`), minus its discards, minus works this tier has settled,
minus works resting on a `target_pending` younger than `EXTRACT_PENDING_RETRY_DAYS`
(14), minus works held by another runner's unexpired extract claim, minus the two skip
lists Stage 3 has always honoured (already in published FLoRA; already in the
Supabase validation tables). Routing says "this deserves an LLM's attention"; only
the validated pair says "this reaches Stage 3".

The rows are built in process from `iter_export_rows` + `screen_columns` — the same
two functions `export-csv` writes with — so a work extracted through this tier reads
exactly the row an export of that release would show. Writing a CSV and parsing it
back would be a third representation of one thing, and a place for two of them to
drift.

### Claims, the lease, and the heartbeat

`EXTRACT_CLAIM_BATCH = 200` works per claim under a `EXTRACT_CLAIM_TTL_MINUTES = 45`
lease, renewed by a daemon thread every third of it. A work here is minutes of PDF
download and full-text LLM, not seconds of abstract screening, so the lease is short
and renewed rather than long and unattended: a host lost mid-batch frees its works in
under an hour instead of six. A lease is only safe to make short if something renews
it.

`ClaimLeaseLost` from the heartbeat means the lease is already gone and another
runner may hold these works, so the batch in flight finishes and records what it paid
for and **no further batch is claimed**. Verdicts already written stand — expiry
frees works, it never retracts evidence. A transport failure is not a lost lease: the
next beat retries, and there are three of them before the lease could expire.

`run_tier()` gained one parameter for this: `on_claim(claim_id)`, called the moment
the batch is claimed and before the first work is judged. `run_tier` takes the claim,
so the id is not knowable to the caller until it returns — hours too late to start
renewing. It also now forwards a vote's `cost` and `payload` to `record_verdict` when
the vote carries them; the two screens carry neither, and `payload` is sent only when
given so a pre-0005 database still accepts a screen verdict.

### Pricing

Per RUNG, not per row, because the ladder returns at its first success and what a row
costs is almost entirely how far down it went. `EXTRACT_RUNG_REACH` is measured:
the `link_method` distribution of `data/extracted.csv` on 2026-08-06 (285 rows —
`llm_references` 81, `llm_cited_candidates` 51, `llm_fulltext` 9, and 144 a
deterministic rule resolved). It is a starting point only — that file is the output
of runs made under an older ladder — and the numbers to replace it with are the rung
shares in `cache/engine/runs/extract-*.json` once this tier has run.

`EXTRACT_RESOLVED_SHARE` is the one judgement rather than a measurement: it cannot be
read off `extracted.csv`, because every unresolved row is partitioned out of that
file. It comes from the ratio of that file to the ten set-aside CSVs beside it
(285 against 1,176).

OpenAlex is reported in **credits**, on its own line, never converted to dollars:
OpenAlex bills a daily credit budget that resets at midnight UTC, so a dollar figure
would answer a question nobody is asked at the point of spending.

### The export

`python -m extract.export --release <id>` renders `data/extracted.csv` from the stored
verdicts of the works that release admits — a verdict outlives the routing that bought
it, so an unfiltered render keeps shipping works the rule book now discards
(`--all-releases` asks for exactly that, and is the only invocation that reads no
routing store). Otherwise a pure render.

**Two generations, and why the older one still counts.** `--current-generation-only`
gives the strict view: a verdict from a superseded ladder, prompt or model says
nothing about what today's code would find. The DEFAULT is not strict, because the
two questions differ. The tier's worklist asks *what should I pay to extract now*,
where a stale verdict must not stop a re-extraction. The export asks *what has this
pipeline concluded*, where dropping a paper because a prompt was edited would delete
a real finding and hand the validators a shorter file with no explanation. So a work
with no current-generation result row falls back to its newest row of any generation,
and the count is printed: `rows from a superseded generation: N`. Those are rows
awaiting re-extraction, not rows to discard.

**Mode is the claim's, not the row's.** A `validation`-mode run records real verdicts
that must not reach the live file, and `claim.meta.mode` is where that is written
down — the same place the screens keep it.

**Quarantine happens on the way out.** The export applies the partition as it
writes, through `classify_row()` in `sanity_check` — which is now the only thing that
module does with it, since the pass reports and moves nothing. One definition of where
a row belongs, and one writer. The set-asides go to `set_aside_dir(out_csv)`, so a
render to a sandbox path partitions into that sandbox's own directory.

**The skip lists apply at render too.** The same two lists the worklist honours —
already in published FLoRA, already in the Supabase validation tables — are applied
as rows are written, and the count is reported as `already_in_flora`. A work that
entered either AFTER it was extracted keeps its verdict as evidence, but its rows
stop shipping; otherwise a paper added to FLoRA between two campaigns would keep
reaching the validation import forever.

**One writer, one whole file.** Each render writes `data/extracted.csv` complete,
sorted by `(work_id, original_rank)`, through a temp file and one rename. Nothing
appends. Several campaigns have shipped through it since: the file tracked in git
now holds 1,899 data rows, all of them rendered by this command. Any change to what
the verdicts say therefore arrives as one whole-file diff — expected, and visible in
advance through `--check`.

`--check` rebuilds in memory, diffs against the file on disk by whole-row content,
prints the counts and exits non-zero on any difference. It writes nothing.
