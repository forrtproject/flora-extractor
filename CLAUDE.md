# CLAUDE.md — FLoRA Extractor

Primary instruction document for AI coding agents and human contributors. Other agent
runtimes: see [AGENTS.md](AGENTS.md), which points here.

## What This Project Does

**FLoRA Extractor** discovers, extracts, and validates replication and reproduction
studies for the [FLoRA database](https://forrt.org/replication-hub/flora/). For each
candidate paper it identifies which original study is targeted and what the outcome was
(success / failure / mixed / descriptive / statistically_successful_but_flawed /
uninformative / cannot_be_determined / not_a_replication).

## Architecture — 4-Stage Pipeline

```text
Stage 1: search/        → SEARCHES only            → the survivor pool (parquet)
Stage 2: filter/engine/ → routes the pool into piles → data/filtered.csv (handoff)
Stage 3: extract/       → finds original + outcome   → data/extracted.csv
Stage 4: validate/      → read-only monitoring dashboard (no CSV output)
```

```bash
python -m search.run_search --scan  # Stage 1 → the survivor pool (--scan is required)
python -m filter.engine route       # Stage 2 → routing release in the DuckDB store
python -m filter.engine screen --tier screen_expensive --run   # the claimed LLM tier
python -m filter.engine handoff --out data/filtered.csv        # → Stage 3's input
python -m extract.run_extract       # Stage 3 → data/extracted.csv (streamed row-by-row)
python -m validate.app              # Stage 4 dashboard → http://localhost:5001
```

**Stage 3's output is `data/extracted.csv`, and that is where this repo stops
writing.** Human validation lives in a separate Supabase-backed repo, and the push of
resolved rows into the three Supabase validation tables (`unvalidated`,
`record_metadata`, `validation_queue`; validator slots `human_1`/`human_2`/`llm`) is
performed there, by `csv_to_db.py` in the `flora-validation` repo (confirmed in
issue #172). That script reads `data/extracted.csv`, runs one psycopg2 transaction
and inserts `ON CONFLICT (pair_id) DO NOTHING`, so it is atomic and idempotent. This
repo only READS those tables, through `shared/supabase_client.py`, for the Stage 4
dashboard. The final artifact is the Supabase `validated` table — there is no
`data/validated.csv`.

A second, older importer used to live here as `extract/csv_to_db.py`. Nothing
imported it, it was never run against the current schema, and it carried a real
defect (three non-atomic PostgREST inserts per row, no retry, orphan rows the pair-id
dedup never reconciles). It is parked on the `wip/csv-to-db` branch with a `WIP.md`
explaining what would make it correct; do not revive it.

**Test sandbox:** `python -m extract.run_extract --extracted-test` writes to
extracted-test.csv (skipping DOIs already resolved in extracted.csv). Every Stage 3
run RESUMES by default — resolved rows are carried forward and only `target_pending`
is re-run; `--fresh` discards the output CSV and re-extracts (and re-pays for)
everything. `python -m extract.promote_test --all|--doi …|--dry-run` promotes rows to production.
Set-asides belong to the output CSV they came out of (`set_aside_dir()` in
`shared/schema.py`): production's sit in `data/`, the sandbox's in
`data/extracted-test-set-aside/` — a test-run quarantine must not settle a paper for
the production resume, which treats every key in a settled set-aside file as done.

## Module Map

`shared/` was ported from the *OpenAlexLLM* prototype: it runs in production but some
thresholds/heuristics (notably `find_all_candidates()` in `openalex_client.py`) have
never been independently validated. Discuss shared changes with all stage teams.

| File | Purpose |
| ---- | ------- |
| `shared/openalex_client.py` | OpenAlex API wrapper + `find_all_candidates()` (Stage 3 logic) |
| `shared/openalex_keys.py`   | OpenAlex key rotation, shared by all stages |
| `shared/llm_client.py`      | Gemini/OpenAI/OpenRouter calls — one model per call site, named explicitly, with no fallback to another provider — JSON parsing; `classify_replication()` (front-door screen, called by Stage 2's expensive tier), `cached_classification()` (read-only cache door, for the handoff), `screen_gate()`, `screen_voters()`, `identify_targets_with_llm()` (the one target call behind the abstract, reference-list and full-text rungs), `screen_references_with_llm()` (reference-list target pick) |
| `shared/target_keys.py`     | `assign_target_keys()` — one deduplicated `@smith2009` namespace over a paper's candidates and references, plus the key → record map |
| `shared/token_usage.py`     | Per-day/provider/model token recording (`cache/token_usage.json`) + the OpenAI daily budget check |
| `shared/rate_limit.py`      | `throttle(service, interval)` — one reservation queue per remote service, so N worker threads share one rate rather than each sleeping its own |
| `shared/prompts.py`         | Every LLM prompt + `prompt_version()` (hash of the prompt text and every spliced fragment) |
| `shared/prescreen.py`       | What the cheap discard-only tier ASKS: `hard_signal()`, `prescreen_bypass()`, `prescreen_voters()`, `prescreen_vote()` (the public seam). The gate and the run loop are Stage 2's, in `filter/engine/tiers.py`; the tier is dormant (its specs are all shadow) |
| `shared/cache.py`           | Cache helpers; `content_key()` builds the content-complete LLM cache key |
| `shared/row_key.py`         | Row identity: `row_keys()` / `primary_key()` (doi → oa: → url: → title:) |
| `shared/csv_index.py`       | Sidecar index load/save/append/build + shared CSV dedup (streaming writes) |
| `shared/pdf_sources.py`     | Multi-tier PDF acquisition waterfall |
| `shared/pdf_parsing.py`     | Six PDF parse methods; `parse_all()`, `best_parse_result()` scoring |
| `shared/grobid.py`          | GROBID reference extraction |
| `shared/disambiguation.py`  | Two string helpers only: `jaccard_similarity()` (used by `link_original.py` and `doi_verify.py`) and `is_umbrella_paper()`. The same-author/year resolvers it was named for are gone; nothing here decides a candidate any more |
| `shared/doi_verify.py`      | doi_o verification/correction (CrossRef → OpenAlex) |
| `shared/utils.py`           | `clean_doi()`, `cache_key()`, `non_article_doi()`, helpers |
| `shared/config.py`          | All paths, env loading, rate limits — every tunable lives here |
| `shared/schema.py`          | CSV column definitions — the contract between stages |
| `shared/supabase_client.py` | Read client for the Supabase validation tables |
| `shared/dashboard_cache.py` | Parquet mirror + `data/dashboard/stats.json` so Stage 4 reads fast; each runner calls `refresh(stage)` |
| `shared/flora_skip.py`      | Two skip lists: already-in-FLoRA (entry sheet + `flora.csv`), read by `run_extract`; and already-in-the-validation-tables (`data/validated_skip.csv`, work id or DOI), read by Stage 3 alone. The second is the frozen legacy `record_metadata` set, materialised once by `analysis/build_validated_skip.py` so a run needs no Supabase |
| `shared/token_counter.py`   | In-process per-stage token attribution; `set_stage()` before a call block |
| `shared/abstract_store.py`  | The abstract cache as one SQLite file (`cache/abstracts.sqlite`). One row per identifier: the text, or NULL for a definitive miss. **The row IS the checkpoint** — it absorbed `fetch_abstracts_done.txt` and the `fetch_abstracts_found.txt` sidecar, both of which existed only because file-per-key made whole-cache questions cost half a million syscalls, and either could drift from the cache it described. A transient failure is never recorded. Migration: `python -m shared.abstract_store --migrate` |
| `shared/hf.py`              | The Hugging Face plumbing shared by `pool_sync`, `cache_sync` and the engine tiers: which exceptions establish ABSENCE as opposed to unreadability, which failures a different token would fix, batched commits. The caller imports `huggingface_hub` and passes it in |
| `shared/cache_sync.py`      | Share the API caches through the same private dataset repo (`--push` / `--pull [--parts …]`). Safe because keys are content-complete; a differing checkout misses rather than mis-reads. Misses are shared too, except an unproven one — a gated source the pusher got zero hits from, that the puller is configured for, has its `__none__` entries AND checkpoint lines dropped. `cache/engine/responses` is the one cache it does not carry — the tiers push that themselves |

| Stage | Files |
| ----- | ----- |
| `search/` | `run_search.py` (the Stage 1 entry point: `--scan` runs the ledger-backed snapshot scan (sample scans use a scratch `FLORA_CACHE_DIR`); a bare invocation never starts a 400 GB scan), `snapshot_scan.py` (the bulk-parquet scan: **the search gate** → the survivor pool), `pool_sync.py` (share the pool through a private HF dataset repo: `--push` / `--pull`), `fetch_abstracts.py` (the six abstract-source phase runners — a library now, whose one consumer is `filter/engine/backfill.py`). Stage 1 searches and does not filter: the non-snapshot discovery sources were retired to `wip/api-harvest-sources` (PR #158) because nothing downstream read `data/candidates.csv` |
| `filter/` | `phrase_detection.py` — the token/stem vocabulary the **search gate** is built from. It is Stage 1's only keyword logic; Stage 2 does not call it. The old `rule_filter.py`/`run_filter.py` path is retired (#146) |
| `filter/engine/` | The issue #146 filter engine, which IS Stage 2: declarative JSON specs in `filter/spec/` routed by precedence into piles (`discard` / `screen_expensive` / `screen_cheap` / `needs_human` / `pending`) over the survivor pool; claimed, budget-gated LLM tiers; `handoff` writes Stage 3's input. Rules route and discard; only LLMs admit. Design: [`docs/filter-engine.md`](docs/filter-engine.md); policy (precedence bands, pile→status mapping, measurement levels): `filter/spec/CONVENTIONS.md`. CLI: `python -m filter.engine specs\|route\|diagnose\|worklist\|screen\|export\|reconcile\|handoff\|status` |
| `db/migrations/` | The engine's Postgres state authority (claims, permanent verdicts, audit, validation lineage) — SQL the maintainer runs in Supabase |
| `extract/` | `run_extract.py` (orchestrator: chunked read, the screen verdict read off the row, per-target adapter), `link_original.py` (resolution ladder), `code_outcome.py` (outcome coding; reproductions use the computation/robustness axes), `sanity_check.py` (post-run quarantine to set-aside CSVs; runs on completion and Ctrl-C), `promote_test.py`, `audit_dois.py`, `audit_extracted.py` (read-only pre-validation audit), `backfill_authors.py` (retroactive `authors_o`/`ref_o` from OpenAlex), `clean_parse_cache.py` |
| `validate/` | Read-only Flask dashboard: `app.py` registers the `dashboard`, `check` and `batch` blueprints only |
| `misc/` | Reference examples and small sample CSVs — do not import |

## CSV Schema

Authoritative: **`shared/schema.py`**; column reference: [`docs/csv-schema.md`](docs/csv-schema.md).
Never change a column name without updating `schema.py` and notifying all teams.

- `filter_confidence` is categorical (`high|medium|low`), not a float.
- `uninformative` is the AUTHORS' verdict; `cannot_be_determined` is OURS.
- `study_o` holds target study NUMBER(s) inside the original paper (several studies of
  one paper = one row; several papers = several rows).
- `api_error` in any field = failed after retries; distinct from `pending`.
- `type` may be empty on a screened row where nothing (screen or Stage 2) assigned a
  paper type; such rows are not ready for validation.
- `screen_categories` is |-joined multi-select — match by substring/split, never equality.
- `pdf_source` and `parse_method` are full-text provenance: the acquisition tier that
  supplied the document and the parser that won `best_parse_result()`. Both blank when
  the row acquired or parsed nothing — a `llm_fulltext` row with a blank `pdf_source`
  is a contradiction. There is no landing-page HTML substitute for a document any
  more, and a content-free OpenAlex XML result (`openalex_xml_has_content()` in
  `shared/pdf_sources.py`) is no document either: it ends the row at
  `no_fulltext_available` and is never cached as a success.

## Stage 3 — Front Door and Resolution

**Both screens are Stage 2's.** Neither the cheap tier nor the validated front door
runs here any more: the rule book routes a row to the `screen_cheap` or
`screen_expensive` pile and `filter/engine/tiers.py` runs the tier over it, claimed
and budget-gated. They are described here because their verdicts decide Stage 3's
rows. Stage 3 READS the expensive screen's answer off its input CSV (`SCREEN_COLS`)
and never votes: an input with no `screen_verdict` column is refused at startup, a
row whose value is blank is written `target_pending`, and `--screen-here` is the
explicit fallback that screens such rows in Stage 3 (an `--as-routed` handoff, a
hand-made CSV).

**The cheap discard-only tier** (`shared/prescreen.py` asks; `_cheap_judge()` in
`filter/engine/tiers.py` gates) is DORMANT — all three `screen_cheap` specs are
`shadow: true`, so no live row reaches it; waking it is one spec promotion plus the
re-measurement (`docs/filter-engine.md`, "Activating the cheap tier"). There is no
global on/off, deliberately: a flag would apply the cheap gate to rows the rule book
routed to the expensive tier. Two very small models
(`PRESCREEN_MODEL_1`, `PRESCREEN_MODEL_2` in `shared/config.py`, both OpenRouter
ids by default) are asked one question with one field of answer; voter 2 is asked only when voter 1 said
"no", because once the row can no longer be discarded a second opinion changes nothing.
The tier may only DISCARD, and only on two explicit noes — one keep, an unrecognised
label, an unreadable reply or a provider failure all pass the row through to the screen
unchanged, and non-answers are never cached. Three classes of row are never pre-screened
at all: text that states the design outright (`hard_signal()`), rows from a
`CURATED_SOURCES` list, and rows with under `PRESCREEN_MIN_ABSTRACT_CHARS` of abstract.
Stage 2's own high-confidence `replication` verdict is deliberately not a bypass — 98%
of rows reaching Stage 3 carry it, including every screen-confirmed negative. A cheap
verdict never ADMITS: its `proceed` means "on to the expensive screen", so it settles
nothing for the screened-only handoff, and a live discard simply drops the row there.
`link_method = prescreen_discard` has no live writer for that reason; historical rows
carry it, `sanity_check` still files them in `data/prescreen_discard.csv`, and
`--rescreen` still reopens them. Evidence: `analysis/prescreen_eval/REPORT.md`.

**Front-door screen** (`classify_replication()`, run by `screen --tier
screen_expensive`): two voters — `SCREENING_MODEL_1`
(default `gemini-3.5-flash-lite`) and `SCREENING_MODEL_2` (default `gpt-5.4-mini`);
each id routes to its own provider through `provider_for()` — each answer the validated v3.2
schema: `classification` ∈ {replication, reproduction, both, none, unclear}, boolean
`confident`, `categories` (11-value enum), `evidence_quote`, `reasoning`. Prompt:
`_CLASSIFY_PROMPT` in `shared/prompts.py`, now at v3.3 — v3.2 plus the
partial-overlap rule (evaluated copy: `analysis/screening_eval/prompt_v33.txt`;
evidence: `analysis/screening_eval/report_v33.md`, with `report_v32.md` behind it).

**The gate is `screen_gate()`, defined once** (G-softqual, 89% hard-negative discard,
zero settled misses):

- **discard** — all votes `none`, OR one confident `none` with every other vote
  qualifying-or-unclear at `confident: false` → `not_a_replication`.
- **proceed** — everything else, including confident splits. There is no
  `screen_disagreement` terminal state any more (historical rows on disk are still
  routed by the value in `schema.py`, `sanity_check.py` and `run_extract.py`).
- **no decision** — fewer than two votes: 1 vote → `target_pending` (re-run decides),
  0 votes → `api_error`. Incomplete screens are never cached.

On a pass, the screen's `record_type` (both voters agreeing wins; splits fall back to
the first qualifying voter; `both` → replication) becomes `type` and overwrites
`filter_status` (`filter_method = "screen"`); with no qualifying vote at all, Stage 2's
values are kept and `type` stays empty. `screen_categories` (union of both voters) is
written on every screened row. Voter models are folded into the classify cache key, so
changing a voter or the prompt invalidates exactly those verdicts — and mints a new
SCREENING GENERATION, which is what makes those works claimable again and is the
first half of what `--rescreen` needs (the second is re-running `handoff`).

The verdict reaches Stage 3 through the handoff, in `SCREEN_COLS`:
`screen_verdict`, `screen_record_type`, `screen_categories`, `screen_votes`,
`screen_evidence`, `screen_reasoning`. `screen_votes` carries each voter's
classification and confidence because a summary of the gate is not enough — the
`llm_title_search` rung is gated on both voters qualifying AND confident.
`_screen_from_row()` in `run_extract.py` rebuilds the `classify_replication()` dict
from them, so everything below the front door is unchanged.

**Resolution ladder** (`run_for_doi()`, cheapest first, returns at first resolution):
title-pattern match → rule-based citation/candidate match → abstract LLM over
candidates → reference-list target pick (`llm_references`) → pre-PDF title search
(`llm_title_search`, only when both voters were qualifying AND confident) → PDF
acquisition + full-text LLM. `llm_title_search` is provisional (~50% precision): not
in `RESOLVED_LINK_METHODS`, never outcome-coded, never imported, set aside for human
confirmation.

**One target prompt for the three LLM rungs.** `build_target_prompt()` serves the
abstract, reference-list and full-text stages; only the evidence blocks differ, never
the task or the acceptance rule. Candidates and references are one deduplicated
`@smith2009` namespace (`assign_target_keys()` in `shared/target_keys.py`), so a work
in both lists is offered once and a re-fetched list cannot renumber a cached pick onto
another paper. `identify_targets_with_llm()` makes the call: it validates every key
against that call's key_map (invented key → unmatched target, repeated key keeps the
first), takes `doi_o` from the mapped record rather than from the model, keeps the
mapped record on each target, and reports `stated_count` / `unidentified_count` so a
shortfall lands in `link_evidence`. A target is accepted only when the model marks it
`match_certain`, and `replication_study_numbers` gives `study_r` — which study of the
REPLICATION re-tests this original.

**The per-target adapter.** A ladder that named targets without accepting one as THE
link goes to `_per_target_rows()`, which writes one row per original PAPER: resolve the
key through its record, collapse several studies of one original into one row, guard,
apply `--resolved-only`, then code the outcome once per original through
`extract_outcome`. Ranks are renumbered after the drops. Unmatched targets get no row;
the shortfall is reported in `link_evidence`. Because the ladder returns at its first
success, `may_stop_at_a_rule()` in `link_original.py` withholds a deterministic pick
whenever the paper's own text does not rule out a second target. The pick is withheld
only UNTIL something that can enumerate targets speaks: it is restored at every exit
where nothing did (`--no-pdf`, no document, no context, an incomplete screen — and
`--no-llm` never withholds at all), and after the full-text call when that call named
nothing or named the same work. A provider failure is not that answer, so it does not
restore. `original_match_type`, `original_match_confidence` and `n_originals` are
observations, settled after the guard's demotions and `--resolved-only`'s drops.

**Two outcome prompts, one per vocabulary.** `build_outcome_prompt()` (replication)
and `build_repro_outcome_prompt()` (reproduction) each serve both passes: supplying the
full-text passage selects the full-text pass, which adds the PAPER TEXT block and asks
for `record_type_check`. A reproduction is coded on two independent axes —
`outcome_computation` (computationally reproducible · computational issues · technical
failure · not checked) and `outcome_robustness` (robust · robustness challenges · not
checked), each with `cannot_be_determined` as its own escape and its own quote and
quote source — and its `outcome` is the two settled values joined. Escalation is
per axis: either axis unresolved reads the full text, and that call replaces both.
`record_type_check == "neither"` sets `not_a_replication`; naming the other vocabulary
re-codes the row once under the other prompt and corrects `type`.

**Outcome coding runs only on a resolved link** (`_outcome_without_coding()` gates on
`RESOLVED_LINK_METHODS`); unresolved rows are written `pending`, except
`not_a_replication` where the screen's verdict is the outcome.

## API Budgets and Usage

Every LLM call records input/output tokens per day/provider/model in
`cache/token_usage.json` (`shared/token_usage.py`). OpenAI spend is hard-capped by
`OPENAI_DAILY_TOKEN_BUDGET` (default 8,000,000/day; `0` disables): when exhausted,
`TokenBudgetExhausted` stops the run cleanly (rows written so far stay, sanity_check
runs). Dashboard display of usage: issue #115.

**OpenAlex is metered too, and not uniformly.** It bills credits per request against
a daily budget that resets at midnight UTC (`shared/openalex_keys.py` owns the key
pointer and the rotation on exhaustion). What matters when designing a code path is
the *ratio*, which spans three orders of magnitude:

| Call shape | Relative cost |
| ---------- | ------------- |
| Single-entity lookup by id or DOI | **free** |
| List / filter query (e.g. the 50-DOI batch fetch in `shared/openalex_client.py`) | 1× |
| Free-text `search` query | **10×** a filter query |
| Content download (full text / XML) | **100×** a filter query |

Current prices: <https://help.openalex.org/hc/en-us/articles/24397762024087-Pricing>
— read them there rather than copying numbers into code or docs.

The design consequence is the resolution ladder's shape. Resolving a known DOI costs
nothing, so verification and candidate enrichment are effectively free; a *title
search* is a free-text query and therefore dominates the OpenAlex bill of any row
that reaches it. That is a second reason — alongside its ~50% precision — to keep
`llm_title_search` at the bottom of the ladder and provisional.

## Caching

Every API call must be cached. **A cache key names everything the answer depends on** —
for an LLM call: the prompt version, the model, and the inputs sent:

```python
key = content_key("outcome", doi_r, prompt_version("build_outcome_prompt"),
                  cache_model_id(OUTCOME_MODEL, OUTCOME_EFFORT), prompt)
```

A model reaches a key only through `cache_model_id(model, effort)`, which appends the
reasoning effort the call sends — how hard a model thinks changes its answer, so the
two settings never share a cache entry. **The effort belongs to the call site, never
to the model id**: `LINKING_MODEL`, `OUTCOME_MODEL` and `SCREENING_MODEL_2` are the
same string today, so each caller passes its own value (`LINKING_EFFORT`,
`OUTCOME_EFFORT`, the screen's per-voter `SCREENING_EFFORT_1`/`SCREENING_EFFORT_2`, or
`""` for the pre-screen) to `call_model` AND the same value to `cache_model_id`. Each
is a constant per call site rather than one per provider, and `llm_client` sends it
under whichever name the id routes to: Gemini's `thinkingLevel`, OpenAI's and
OpenRouter's `reasoning_effort`. The screen carries one per VOTER, from the
`screen_voters()` slot that names the model, because the two voters were evaluated at
different rungs — and Gemini's is stated as `"minimal"` rather than left off, so a
changed provider default cannot move the voter without moving the key.

`prompt_version(name)` hashes the prompt text plus every spliced fragment — editing a
prompt invalidates exactly its caches, nothing to register. Cache non-answers too (a
decline is an answer; a 503 is not). Plain API responses keyed by identifier use
`cache_key()`. Cache lives in `cache/` (gitignored).

**Declared equivalences** (issue #171) are the one opt-in exception, for when a key
moved but the answer provably did not — a mislabelled key component, or a prompt edit
reviewed and judged answer-preserving. The default is unchanged: invalidation is
automatic and strict. A call site that wants otherwise registers the LEGACY key parts
in a module-level constant with the rationale in a comment (`_CLASSIFY_LEGACY_KEY_PARTS`
in `shared/llm_client.py` is the first) and reads through
`read_cache_migrating(cache_dir, key, legacy_keys, migrate_note)`. Only the declared
component is substituted, so an equivalence stops matching by itself once anything else
about the call changes. A legacy hit is re-stored under the current key carrying a
`cache_migrated` note — the prompt version and models it is now filed under, and the
key it came from — so every response on disk stays traceable to what produced it; the
legacy entry is left in place for other checkouts and the shared HF cache.

Content-complete keys are also what makes the cache **shareable**: an entry from
another machine is provably the answer this checkout would have computed, so
`python -m shared.cache_sync --pull` saves a collaborator the provider bill and the
500k-identifier abstract crawl, and a checkout whose prompts or models differ simply
misses. See `shared/cache_sync.py` and [`docs/cli-reference.md`](docs/cli-reference.md).

## Error Handling on API Failures

Log with DOI, retry 3× with backoff (1s/2s/4s), then set the field to `api_error` and
continue — never crash the pipeline. A transient failure must never be checkpointed as
a definitive miss.

**One model per call site, and no fallback.** Every model the pipeline calls is a
constant in `shared/config.py`, named for the QUESTION it answers rather than the
vendor serving it: `PRESCREEN_MODEL_1`/`_2`, `SCREENING_MODEL_1`/`_2`, `LINKING_MODEL`,
`OUTCOME_MODEL`, `PDF_PARSE_MODEL`. That constant is the only model that can answer
its call. Retries are against the same model; when they are exhausted the row records
the failure. A provider ladder used to run Gemini → OpenAI → OpenRouter, which made an
outage invisible — the row got an answer from a model no evaluation covered, and the
cache key had to over-name every model that might have produced it. Any fallback is
now an explicit decision at the call site.

**The provider follows the model id**, through `provider_for()` in `llm_client`: a
"/" means OpenRouter, a leading `gemini` means Google, anything else OpenAI direct.
`call_model(prompt, model)` dispatches on it — a dispatcher, not a ladder: it picks
the ONE provider that can serve the named model, and when that provider fails the
call fails. Nothing may hardcode "this call site goes to Gemini", or swapping a
constant across vendors becomes a 404 from the wrong API. The one exception is
`PDF_PARSE_MODEL`: the document calls build a Gemini request body with no
OpenAI-compatible equivalent, and `llm_client` refuses to import if that constant
routes elsewhere.

**Never let a swallowed error become an empty result.** A rate-limit 429 caught and
turned into `return []` is indistinguishable downstream from a genuine "no
candidates / no references / no results", and the pipeline will happily record the
zero as a finding. Every handler that returns a collection must distinguish *no
answer* from *an empty answer* — raise, or return a sentinel the caller checks — and
must never cache the empty one.

## Large Files

filtered.csv exceeds 1M rows; extracted reads it in 50k-row chunks with sidecar
indexes in `cache/` (`shared/csv_index.py`; keys from `shared/row_key.py`:
doi → `oa:` → `url:` → `title:`), one resume key per row. Missing indexes rebuild
automatically; `--rebuild-index` forces it. First write of a CSV is `utf-8-sig`;
appends are plain `utf-8` (no BOM mid-file).

Stage 1's corpus is the survivor pool (parquet), not a CSV. `data/candidates.csv` —
the admission-gated corpus of the old Stage 1 — is retired; `CANDIDATES_COLS`
survives only as the column contract a pool row is rebuilt into.

**Count on disk, never from the code.** At this size the file and the code
routinely disagree — a partly-merged run, a set-aside CSV, a legacy segment — and
reading the logic tells you what *should* be there. Any number that goes into a
report, a commit message or a decision is read off the artifact.

## Code Style

1. Python with type hints on all signatures. (R contributions welcome if CSV schemas match.)
2. No unnecessary abstractions — no helper until used three times.
3. Comments only where the WHY is non-obvious; file docstrings say what and why.
4. Error handling only at system boundaries.
5. CSV writes `utf-8-sig` (appends plain `utf-8`).
6. All DOIs pass through `clean_doi()` before writing or comparing.
7. All API responses cached before use.
8. Two kinds of configurable value. A **constant** — model ids, thresholds, prices,
   closed vocabularies — is a plain Python value with no `os.getenv`: it decides what
   the pipeline concludes, so it is changed by a commit, not by a machine. It lives
   with the code it governs (the dry run's prices in `filter/engine/tiers.py`); only
   constants with several consumers stay in `shared/config.py`. **Model ids are the
   exception and all live in `shared/config.py`**, named for the question each
   answers — the set of models is the one thing a reader needs whole to know what
   graded a row, and scattering it across the modules that call them hid that. A **tunable** — rate limit, batch size, timeout, path —
   is `os.getenv` with a default and always lives in `shared/config.py`, which is the
   whole `.env` surface in one file. If an override could make two collaborators grade
   the same row differently, it is a constant. LLM rate
   intervals are charged per provider, so the screen's two votes never wait on each other.
9. API key values live in `.env` only; `config.py` only reads env. `.env.defaults` is
   committed, so nothing secret may go in it.

## Testing

Mock all external APIs in regular tests (`unittest.mock.patch`); never make live calls
in `pytest` runs. Live tests go in `tests/live/` behind `TEST_LIVE_API=1`. Each stage
has a schema test via `validate_csv_columns()`. Do not over-test: one test per seam,
and delete tests when their code path dies.

## Environment

Copy `.env.example` to `.env`. Three files, in precedence order — real environment >
`.env` (gitignored: secrets and per-machine settings) > `.env.defaults` (committed:
shared non-secret project identifiers only, currently `SUPABASE_URL` and
`FLORA_POOL_REPO`) > the default in `shared/config.py`. Both files are loaded once, by
`config.py`. Never restate a `config.py` default in `.env.defaults`: a tunable's value
and its rationale belong together in one committed place. Key variables:

```bash
RESEARCHER_EMAIL=...            # required: OpenAlex/CrossRef politeness headers
GEMINI_API_KEY=...              # required
OPENAI_API_KEY=...              # required for Stage 3 (default screen voter 2)
OPENROUTER_API_KEY=...          # only if SCREENING_MODEL_2 contains "/"
OPENAI_DAILY_TOKEN_BUDGET=8000000   # 0 disables the cap
GEMINI_USE_FLEX=true            # 50% discount on paid keys; flex uses GEMINI_FLEX_TIMEOUT
OPENAI_USE_FLEX=true            # same trade on OpenAI; refused flex falls back to standard
GEMINI_PAID_KEY_SLOTS=1         # which key SLOTS are billing-enabled, not key values
EXTRACT_WORKERS=4               # Stage 3 rows in flight at once; 1 = no pool
FLORA_CACHE_DIR=                # move cache/ to an SSD; FLORA_POOL_DIR does the same
```

Constants are **not env vars anywhere** — see code-style rule 8. Every model id,
`LINKING_EFFORT` and `CURATED_SOURCES` are in `shared/config.py` — one place
that answers "what graded this row"; `PRESCREEN_MIN_ABSTRACT_CHARS` in
`shared/prescreen.py`;
`OUTCOME_FULLTEXT_ESCALATION` in `extract/code_outcome.py`; the dry run's price list in
`filter/engine/tiers.py`; `SNAPSHOT_POOL_COMPRESSION` in `search/snapshot_scan.py`.

Gemini quota is per project, not per key — extra `GEMINI_API_KEY_N` slots buy failover,
not throughput; the intended configuration is one paid (Tier 1) project with flex.
GROBID is optional: an unreachable `GROBID_URL` logs a warning and falls back.

## Git Workflow

Base feature branches on `origin/main`, open PRs with `--base main` (the `dev` branch
is stale — do not use it). Use PRs for larger bits of work that should be reviewable,
and commit directly to `main` for minor fixes made on clear instructions. Open PRs when
a feature is stable, not just at the end. **`data/` is NOT gitignored** — over twenty files under
it are tracked (`extracted.csv`, the set-aside CSVs, `flora.csv`, the entry sheet, the
`.zip.dvc` pointers). What is ignored is specific: the multi-GB zips and their unzipped
working copies (`data/.gitignore` + the `data/*.zip` / `data/*.bak` rules in the root
`.gitignore`). Check `git status` before committing in `data/`. `cache/` is fully
gitignored — samples go in `misc/`.

## DOI Verification

Every newly written row passes `_verify_row()`: the metadata `doi_o` actually points to
is fetched (CrossRef → OpenAlex) and compared; mismatches are re-resolved from
title+author (three tiers, strictest first — a wrong correction is worse than a flag),
with `doi_r` always excluded as a correction target. **Verification happens once**: the
three tiers issue up to three OpenAlex free-text searches per row at 10× a filter query,
so a resume carries a row whose `doi_o_verification` is already settled forward as
written (`SETTLED_VERIFICATIONS` in `run_extract.py`) and only re-verifies the unsettled
values — `api_error` and blank. The cheap half of `_finalise_row` (work-id fill, control
characters, the year assertion) still runs on every written row. Retroactive audit:
`python -m extract.audit_dois [--apply|--doi …|--status api_error|--extracted-test]` —
the only thing that re-verifies a settled row. Thresholds are constants in
`shared/doi_verify.py`. The searches go through `_oa_get`, so they are throttled,
key-rotated, counted (`search_query_count()`, printed at the end of a run) and a quota
refusal raises `OpenAlexQuotaExhausted` instead of reading as "no match".

## Further Reference

- **[`docs/README.md`](docs/README.md) — the documentation index.** Every guide and
  reference is listed there; this file keeps no second list.
- Seeding from prior FLoRA data (skip Stages 1–2): the prior-pipeline CSVs on the
  shared drive. `data/all_replications.csv` and
  `data/FLoRA entry sheet - replication list.csv` are the two a local checkout is
  expected to have — the latter is the file `shared/flora_skip.py` and
  `shared/config.py` actually read. Ask the maintainer for the drive listing rather
  than assuming a filename.
