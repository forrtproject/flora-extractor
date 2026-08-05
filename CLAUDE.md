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
python -m extract.csv_to_db         # push resolved rows into the Supabase validation tables
python -m validate.app              # Stage 4 dashboard → http://localhost:5001
```

Human validation lives in a separate Supabase-backed repo. `extract/csv_to_db.py`
pushes resolved rows into three Supabase tables (`unvalidated`, `record_metadata`,
`validation_queue`; validator slots `human_1`/`human_2`/`llm`). The final artifact is
the Supabase `validated` table — there is no `data/validated.csv`.

**Test sandbox:** `python -m extract.run_extract --extracted-test` writes to
extracted-test.csv (skipping DOIs already resolved in extracted.csv). Every Stage 3
run RESUMES by default — resolved rows are carried forward and only `target_pending`
is re-run; `--fresh` discards the output CSV and re-extracts (and re-pays for)
everything. `python -m extract.promote_test --all|--doi …|--dry-run` promotes rows to production.

## Module Map

`shared/` was ported from the *OpenAlexLLM* prototype: it runs in production but some
thresholds/heuristics (notably `find_all_candidates()` in `openalex_client.py`) have
never been independently validated. Discuss shared changes with all stage teams.

| File | Purpose |
| ---- | ------- |
| `shared/openalex_client.py` | OpenAlex API wrapper + `find_all_candidates()` (Stage 3 logic) |
| `shared/openalex_keys.py`   | OpenAlex key rotation, shared by all stages |
| `shared/llm_client.py`      | Gemini/OpenAI/OpenRouter calls, JSON parsing; `classify_replication()` (front-door screen), `screen_gate()`, `screen_voters()`, `identify_targets_with_llm()` (the one target call behind the abstract, reference-list and full-text rungs), `screen_references_with_llm()` (reference-list target pick) |
| `shared/target_keys.py`     | `assign_target_keys()` — one deduplicated `@smith2009` namespace over a paper's candidates and references, plus the key → record map |
| `shared/token_usage.py`     | Per-day/provider/model token recording (`cache/token_usage.json`) + the OpenAI daily budget check |
| `shared/rate_limit.py`      | `throttle(service, interval)` — one reservation queue per remote service, so N worker threads share one rate rather than each sleeping its own |
| `shared/prompts.py`         | Every LLM prompt + `prompt_version()` (hash of the prompt text and every spliced fragment) |
| `shared/prescreen.py`       | The cheap discard-only tier: `hard_signal()`, `prescreen_bypass()`, `prescreen()`. Run by Stage 2 over the `screen_cheap` pile |
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
| `shared/flora_skip.py`      | Two skip lists: already-in-FLoRA (entry sheet + `flora.csv`), shared by `run_extract` and `csv_to_db`; and already-in-the-validation-tables (`data/validated_skip.csv`, work id or DOI), read by Stage 3 alone. The second is the frozen legacy `record_metadata` set, materialised once by `analysis/build_validated_skip.py` so a run needs no Supabase |
| `shared/token_counter.py`   | In-process per-stage token attribution; `set_stage()` before a call block |

| Stage | Files |
| ----- | ----- |
| `search/` | `run_search.py` (the Stage 1 entry point: `--scan` runs the ledger-backed snapshot scan, `--snapshot-pilot` a sample; a bare invocation never starts a 400 GB scan), `snapshot_scan.py` (the bulk-parquet scan: **the search gate** → the survivor pool), `pool_sync.py` (share the pool through a private HF dataset repo: `--push` / `--pull`), `fetch_abstracts.py` (the six abstract-source phase runners — a library now, whose one consumer is `filter/engine/backfill.py`). Stage 1 searches and does not filter: the non-snapshot discovery sources were retired to `wip/api-harvest-sources` (PR #158) because nothing downstream read `data/candidates.csv` |
| `filter/` | `phrase_detection.py` — the token/stem vocabulary the **search gate** is built from. It is Stage 1's only keyword logic; Stage 2 does not call it. The old `rule_filter.py`/`run_filter.py` path is retired (#146) |
| `filter/engine/` | The issue #146 filter engine, which IS Stage 2: declarative JSON specs in `filter/spec/` routed by precedence into piles (`discard` / `screen_expensive` / `screen_cheap` / `needs_human` / `pending`) over the survivor pool; claimed, budget-gated LLM tiers; `handoff` writes Stage 3's input. Rules route and discard; only LLMs admit. Design: [`docs/filter-engine.md`](docs/filter-engine.md); policy (precedence bands, pile→status mapping, measurement levels): `filter/spec/CONVENTIONS.md`. CLI: `python -m filter.engine specs\|verify\|route\|diagnose\|worklist\|screen\|export\|reconcile\|handoff\|status` |
| `db/migrations/` | The engine's Postgres state authority (claims, permanent verdicts, audit, validation lineage) — SQL the maintainer runs in Supabase |
| `extract/` | `run_extract.py` (orchestrator: chunked read, front-door screen, per-target adapter), `link_original.py` (resolution ladder), `code_outcome.py` (outcome coding; reproductions use the computation/robustness axes), `sanity_check.py` (post-run quarantine to set-aside CSVs; runs on completion and Ctrl-C), `promote_test.py`, `audit_dois.py`, `audit_extracted.py` (read-only pre-validation audit), `backfill_authors.py` (retroactive `authors_o`/`ref_o` from OpenAlex), `csv_to_db.py`, `clean_parse_cache.py` |
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
  paper type; such rows are not imported by `csv_to_db`.
- `screen_categories` is |-joined multi-select — match by substring/split, never equality.
- `pdf_source` and `parse_method` are full-text provenance: the acquisition tier that
  supplied the document and the parser that won `best_parse_result()`. Both blank when
  the row acquired or parsed nothing — a `llm_fulltext` row with a blank `pdf_source`
  is a contradiction. There is no landing-page HTML substitute for a document any
  more, and a content-free OpenAlex XML result (`openalex_xml_has_content()` in
  `shared/pdf_sources.py`) is no document either: it ends the row at
  `no_fulltext_available` and is never cached as a success.

## Stage 3 — Front Door and Resolution

**The cheap discard-only tier** (`shared/prescreen.py`) is NOT part of Stage 3: which
rows get it is a Stage 2 routing decision — the rule book sends a row to the
`screen_cheap` pile and `filter/engine/tiers.py` runs the tier over that pile, claimed
and budget-gated. It is described here because its verdicts land in Stage 3's CSV.
There is no global on/off, deliberately: a flag would apply the cheap gate to rows the
rule book routed to the expensive tier. Two very small models
(`PRESCREEN_VOTER1_MODEL`, `PRESCREEN_VOTER2_MODEL`, both OpenRouter by default) are
asked one question with one field of answer; voter 2 is asked only when voter 1 said
"no", because once the row can no longer be discarded a second opinion changes nothing.
The tier may only DISCARD, and only on two explicit noes — one keep, an unrecognised
label, an unreadable reply or a provider failure all pass the row through to the screen
unchanged, and non-answers are never cached. Three classes of row are never pre-screened
at all: text that states the design outright (`hard_signal()`), rows from a
`CURATED_SOURCES` list, and rows with under `PRESCREEN_MIN_ABSTRACT_CHARS` of abstract.
Stage 2's own high-confidence `replication` verdict is deliberately not a bypass — 98%
of rows reaching Stage 3 carry it, including every screen-confirmed negative. A discard
writes `link_method = prescreen_discard`, is quarantined by `sanity_check` to its own
`data/prescreen_discard.csv`, and is reopened by `--rescreen`.
Evidence: `analysis/prescreen_eval/REPORT.md`.

**Front-door screen** (`classify_replication()`): two voters — Gemini
(`GEMINI_LIGHT_MODEL`) and `SCREEN_VOTER2_MODEL` (default `gpt-5.4-mini`; an id with
`/` routes to OpenRouter, otherwise OpenAI direct) — each answer the validated v3.2
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
changing a voter or the prompt invalidates exactly those verdicts.

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
                  GEMINI_MODEL, prompt)
```

A Gemini model reaches a key only through `cache_model_id()` (via
`ladder_fingerprint()`), which appends any active `GEMINI_THINKING_LEVEL` — a
thinking level changes the answer, so the two settings never share a cache entry.

`prompt_version(name)` hashes the prompt text plus every spliced fragment — editing a
prompt invalidates exactly its caches, nothing to register. Cache non-answers too (a
decline is an answer; a 503 is not). Plain API responses keyed by identifier use
`cache_key()`. Cache lives in `cache/` (gitignored).

## Error Handling on API Failures

Log with DOI, retry 3× with backoff (1s/2s/4s), then set the field to `api_error` and
continue — never crash the pipeline. A transient failure must never be checkpointed as
a definitive miss.

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
   with the code it governs (the pre-screen's voters in `shared/prescreen.py`, the dry
   run's prices in `filter/engine/tiers.py`); only constants with several consumers
   stay in `shared/config.py`. A **tunable** — rate limit, batch size, timeout, path —
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
OPENROUTER_API_KEY=...          # only if SCREEN_VOTER2_MODEL contains "/"
OPENAI_DAILY_TOKEN_BUDGET=8000000   # 0 disables the cap
GEMINI_USE_FLEX=true            # 50% discount on paid keys; flex uses GEMINI_FLEX_TIMEOUT
OPENAI_USE_FLEX=true            # same trade on OpenAI; refused flex falls back to standard
GEMINI_PAID_KEY_SLOTS=1         # which key SLOTS are billing-enabled, not key values
EXTRACT_WORKERS=4               # Stage 3 rows in flight at once; 1 = no pool
FLORA_CACHE_DIR=                # move cache/ to an SSD; FLORA_POOL_DIR does the same
```

Constants are **not env vars anywhere** — see code-style rule 8. Model ids,
`GEMINI_THINKING_LEVEL` and `CURATED_SOURCES` are in `shared/config.py`; the
pre-screen's voters and threshold in `shared/prescreen.py`;
`OUTCOME_FULLTEXT_ESCALATION` in `extract/code_outcome.py`; the dry run's price list in
`filter/engine/tiers.py`; `SNAPSHOT_POOL_COMPRESSION` in `search/snapshot_scan.py`.

Gemini quota is per project, not per key — extra `GEMINI_API_KEY_N` slots buy failover,
not throughput; the intended configuration is one paid (Tier 1) project with flex.
GROBID is optional: an unreachable `GROBID_URL` logs a warning and falls back.

## Git Workflow

Base feature branches on `origin/main`, open PRs with `--base main` (the `dev` branch
is stale — do not use it). `main` is protected (PR + 1 review). Open PRs when a feature
is stable, not just at the end. **`data/` is NOT gitignored** — over twenty files under
it are tracked (`extracted.csv`, the set-aside CSVs, `flora.csv`, the entry sheet, the
`.zip.dvc` pointers). What is ignored is specific: the multi-GB zips and their unzipped
working copies (`data/.gitignore` + the `data/*.zip` / `data/*.bak` rules in the root
`.gitignore`). Check `git status` before committing in `data/`. `cache/` is fully
gitignored — samples go in `misc/`.

## DOI Verification

Every written row passes `_verify_row()`: the metadata `doi_o` actually points to is
fetched (CrossRef → OpenAlex) and compared; mismatches are re-resolved from
title+author (three tiers, strictest first — a wrong correction is worse than a flag),
with `doi_r` always excluded as a correction target. Retroactive audit:
`python -m extract.audit_dois [--apply|--doi …|--extracted-test]`. Thresholds are
constants in `shared/doi_verify.py`.

## Further Reference

- **[`docs/README.md`](docs/README.md) — the documentation index.** Every guide and
  reference is listed there; this file keeps no second list.
- Seeding from prior FLoRA data (skip Stages 1–2): the prior-pipeline CSVs on the
  shared drive. `data/all_replications.csv` and
  `data/FLoRA entry sheet - replication list.csv` are the two a local checkout is
  expected to have — the latter is the file `shared/flora_skip.py` and
  `shared/config.py` actually read. Ask the maintainer for the drive listing rather
  than assuming a filename.
