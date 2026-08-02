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
Stage 1: search/     → discovers candidate papers → data/candidates.csv
Stage 2: filter/     → removes false positives    → data/filtered.csv
Stage 3: extract/    → finds original + outcome   → data/extracted.csv
Stage 4: validate/   → read-only monitoring dashboard (no CSV output)
```

```bash
python -m search.run_search         # Stage 1 → data/candidates.csv
python -m filter.run_filter         # Stage 2 → data/filtered.csv
python -m extract.run_extract       # Stage 3 → data/extracted.csv (streamed row-by-row)
python -m extract.csv_to_db         # push resolved rows into the Supabase validation tables
python -m validate.app              # Stage 4 dashboard → http://localhost:5001
```

Human validation lives in a separate Supabase-backed repo. `extract/csv_to_db.py`
pushes resolved rows into three Supabase tables (`unvalidated`, `record_metadata`,
`validation_queue`; validator slots `human_1`/`human_2`/`llm`). The final artifact is
the Supabase `validated` table — there is no `data/validated.csv`.

**Test sandbox:** `python -m extract.run_extract --extracted-test [--resume]` writes to
extracted-test.csv (skipping DOIs already resolved in extracted.csv);
`python -m extract.promote_test --all|--doi …|--dry-run` promotes rows to production.

## Module Map

`shared/` was ported from the *OpenAlexLLM* prototype: it runs in production but some
thresholds/heuristics (notably `disambiguation.py` and `find_all_candidates()` in
`openalex_client.py`) have never been independently validated. Discuss shared changes
with all stage teams.

| File | Purpose |
| ---- | ------- |
| `shared/openalex_client.py` | OpenAlex API wrapper + `find_all_candidates()` (Stage 3 logic) |
| `shared/openalex_keys.py`   | OpenAlex key rotation, shared by all stages |
| `shared/llm_client.py`      | Gemini/OpenAI/OpenRouter calls, JSON parsing; `classify_replication()` (front-door screen), `screen_gate()`, `screen_voters()`, `identify_targets_with_llm()` (ladder rungs 4/4.5/7), `screen_references_with_llm()` (Stage 4.5 target pick) |
| `shared/target_keys.py`     | `assign_target_keys()` — one deduplicated `@smith2009` namespace over a paper's candidates and references, plus the key → record map |
| `shared/token_usage.py`     | Per-day/provider/model token recording (`cache/token_usage.json`) + the OpenAI daily budget check |
| `shared/prompts.py`         | Every LLM prompt + `prompt_version()` (hash of the prompt text and every spliced fragment) |
| `shared/prescreen.py`       | The optional cheap pre-screen: `hard_signal()`, `prescreen_bypass()`, `prescreen()`. Off unless `PRESCREEN_ENABLED` |
| `shared/cache.py`           | Cache helpers; `content_key()` builds the content-complete LLM cache key |
| `shared/row_key.py`         | Row identity: `row_keys()` / `primary_key()` (doi → oa: → url: → title:) |
| `shared/csv_index.py`       | Sidecar index load/save/append/build + shared CSV dedup (streaming writes) |
| `shared/pdf_sources.py`     | Multi-tier PDF acquisition waterfall |
| `shared/pdf_parsing.py`     | Six PDF parse methods; `parse_all()`, `best_parse_result()` scoring |
| `shared/grobid.py`          | GROBID reference extraction |
| `shared/disambiguation.py`  | Same-author/year candidate disambiguation — needs validation |
| `shared/doi_verify.py`      | doi_o verification/correction (CrossRef → OpenAlex) |
| `shared/utils.py`           | `clean_doi()`, `cache_key()`, `non_article_doi()`, helpers |
| `shared/config.py`          | All paths, env loading, rate limits — every tunable lives here |
| `shared/schema.py`          | CSV column definitions — the contract between stages |
| `shared/supabase_client.py` | Read client for the Supabase validation tables |

| Stage | Files |
| ----- | ----- |
| `search/` | `run_search.py` (orchestrator; index-based merge/append), `openalex_search.py` (cursor-paginated phrase/concept harvest), `external_lists.py`, `deduplicate.py`, `fetch_abstracts.py` (abstract backfill: OpenAlex → EPMC → S2 → CrossRef → Scopus as uniform checkpointed phases) |
| `filter/` | `rule_filter.py` (deterministic classifier), `run_filter.py` (chunked orchestrator). No LLM: rule-undecidable rows are written through as `needs_review` and settled by Stage 3's screen |
| `extract/` | `run_extract.py` (orchestrator: chunked read, front-door screen, per-target adapter), `link_original.py` (resolution ladder), `code_outcome.py` (outcome coding; reproductions use the computation/robustness axes), `sanity_check.py` (post-run quarantine to set-aside CSVs; runs on completion and Ctrl-C), `promote_test.py`, `audit_dois.py`, `csv_to_db.py`, `clean_parse_cache.py` |
| `validate/` | Read-only Flask dashboard: `app.py` registers the `dashboard`, `check` and `batch` blueprints only |
| `misc/` | Reference examples and 20-row sample CSVs — do not import |

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

**The optional cheap pre-screen** (`shared/prescreen.py`, off unless
`PRESCREEN_ENABLED`) sits in front of everything below. Two very small models
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
`data/prescreen_discard.csv`, and is reopened by `--rescreen`; turning the flag off does
**not** reopen it. Evidence: `analysis/prescreen_eval/REPORT.md`.

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
  `screen_disagreement` terminal state any more (the value survives in schema and
  sanity_check only for historical rows on disk).
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

## Token Budget and Usage

Every LLM call records input/output tokens per day/provider/model in
`cache/token_usage.json` (`shared/token_usage.py`). OpenAI spend is hard-capped by
`OPENAI_DAILY_TOKEN_BUDGET` (default 8,000,000/day; `0` disables): when exhausted,
`TokenBudgetExhausted` stops the run cleanly (rows written so far stay, sanity_check
runs). Dashboard display of usage: issue #115.

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

## Large Files

candidates.csv and filtered.csv exceed 1M rows; extracted reads them in 50k-row chunks
with sidecar indexes in `cache/` (`shared/csv_index.py`; keys from `shared/row_key.py`:
doi → `oa:` → `url:` → `title:`). The candidates index stores all keys per row, the
filtered index one resume key per row. Missing indexes rebuild automatically;
`--rebuild-index` on either stage forces it. First write of a CSV is `utf-8-sig`;
appends are plain `utf-8` (no BOM mid-file).

## Code Style

1. Python with type hints on all signatures. (R contributions welcome if CSV schemas match.)
2. No unnecessary abstractions — no helper until used three times.
3. Comments only where the WHY is non-obvious; file docstrings say what and why.
4. Error handling only at system boundaries.
5. CSV writes `utf-8-sig` (appends plain `utf-8`).
6. All DOIs pass through `clean_doi()` before writing or comparing.
7. All API responses cached before use.
8. Every rate limit / tunable lives in `shared/config.py`, env-overridable. LLM rate
   intervals are charged per provider, so the screen's two votes never wait on each other.
9. API key values live in `.env` only; `config.py` only reads env.

## Testing

Mock all external APIs in regular tests (`unittest.mock.patch`); never make live calls
in `pytest` runs. Live tests go in `tests/live/` behind `TEST_LIVE_API=1`. Each stage
has a schema test via `validate_csv_columns()`. Do not over-test: one test per seam,
and delete tests when their code path dies.

## Environment

Copy `.env.example` to `.env`. Key variables:

```bash
RESEARCHER_EMAIL=...            # required: OpenAlex/CrossRef politeness headers
GEMINI_API_KEY=...              # required
OPENAI_API_KEY=...              # required for Stage 3 (default screen voter 2)
OPENROUTER_API_KEY=...          # only if SCREEN_VOTER2_MODEL contains "/"
GEMINI_MODEL=...  GEMINI_HEAVY_MODEL=...  OPENAI_MODEL=...
SCREEN_VOTER2_MODEL=gpt-5.4-mini
PRESCREEN_ENABLED=              # unset = off; the cheap pre-screen's discards are terminal
OPENAI_DAILY_TOKEN_BUDGET=8000000   # 0 disables the cap
GEMINI_USE_FLEX=true            # 50% discount on paid keys; flex uses GEMINI_FLEX_TIMEOUT
OPENAI_USE_FLEX=true            # same trade on OpenAI; refused flex falls back to standard
GEMINI_THINKING_LEVEL=          # unset = model default; "minimal" is in the cache key
```

Gemini quota is per project, not per key — extra `GEMINI_API_KEY_N` slots buy failover,
not throughput; the intended configuration is one paid (Tier 1) project with flex.
GROBID is optional: an unreachable `GROBID_URL` logs a warning and falls back.

## Git Workflow

Base feature branches on `origin/main`, open PRs with `--base main` (the `dev` branch
is stale — do not use it). `main` is protected (PR + 1 review). Open PRs when a feature
is stable, not just at the end. `data/` and `cache/` are gitignored — samples go in `misc/`.

## DOI Verification

Every written row passes `_verify_row()`: the metadata `doi_o` actually points to is
fetched (CrossRef → OpenAlex) and compared; mismatches are re-resolved from
title+author (three tiers, strictest first — a wrong correction is worse than a flag),
with `doi_r` always excluded as a correction target. Retroactive audit:
`python -m extract.audit_dois [--apply|--doi …|--extracted-test]`. Thresholds are
constants in `shared/doi_verify.py`.

## Further Reference

- [`docs/csv-schema.md`](docs/csv-schema.md) — all columns
- [`docs/cli-reference.md`](docs/cli-reference.md) — all CLI commands and flags
- [`docs/limitations.md`](docs/limitations.md) — known limitations and revisit obligations
- Seeding from prior FLoRA data (skip Stages 1–2): `data/openalex_candidates.csv`,
  `data/all_replications.csv`, `data/flora_entry_sheet.csv` on the shared drive.
