# CLAUDE.md — FLoRA Extractor

## What This Project Does

**FLoRA Extractor** discovers, extracts, and validates replication and reproduction
studies for the [FLoRA database](https://forrt.org/replication-hub/flora/). For each
candidate paper it identifies which original study is targeted and what the outcome was
(successful / failed / mixed / descriptive only / statistically successful but flawed /
uninformative / cannot_be_determined / not_a_replication).

## Architecture — 4-Stage Pipeline

```text
Stage 1: search/        → SEARCHES only            → the survivor pool (parquet)
Stage 2: filter/engine/ → routes the pool into piles → screen verdicts in the store
Stage 3: extract/       → finds original + outcome   → verdicts → data/extracted.csv
Stage 4: validate/      → read-only monitoring dashboard here, main validation tasks are in separate repo flora-validation
```

```bash
python -m search.run_search --scan  # Stage 1 → the survivor pool (--scan is required)
python -m filter.engine route       # Stage 2 → routing release in the DuckDB store
python -m filter.engine screen --tier screen_expensive --run   # the claimed LLM tier
python -m filter.engine export-csv --out <file>     # optional: a release's screened rows as a record CSV
python -m extract.tier --run        # Stage 3 → claimed extraction, verdicts in Postgres
python -m extract.export --release <id>   # the verdicts → data/extracted.csv
python -m validate.app              # Stage 4 dashboard → http://localhost:5001
```

**Stage 3's output is `data/extracted.csv`, and that is where this repo stops
writing.** Human validation lives in a separate Supabase-backed repo, and the push of
resolved rows into the  Supabase validation tables is
performed, by `csv_to_db.py` in the `flora-validation` repo. 
That script reads `data/extracted.csv`.
This repo only READS the validaiton tables, through `shared/supabase_client.py`, for the Stage 4
dashboard. The final artifact is the Supabase `validated` table.

**Stage 3 runs as a claimed engine tier, and the export is the only writer of
`data/extracted.csv`.** `python -m extract.tier --run` claims works, runs the ladder
and stores one permanent RESULT verdict per work whose payload rebuilds every
`EXTRACTED_COLS` row offline; `python -m extract.export --release <id>` renders those
payloads into the CSV, whole, sorted and atomically. It renders only the works that
release put in an admitted pile, because a verdict outlives the routing that bought it
— a work today's rule book discards would otherwise keep reaching the validation
import forever. Omitted, the release is the store's when it holds exactly one, and a
store holding several refuses; `--all-releases` renders every stored verdict whatever
routing says. Resume is the verdict row, not the file: the
worklist subtracts every work whose latest current-generation result SETTLES it.
`target_pending` and `api_error` do not settle — but a current-generation
`target_pending` RESTS indefinitely: it is subtracted like a settled work until a
new generation reopens it or `--redo`/`--redo-status` names it, because re-running
the same evidence re-buys the same answer. `api_error` retries immediately. The export applies the FLoRA/validated skip
lists at render too, so a work that enters FLoRA after extraction stops shipping.

**Two ways to reopen, and the ladder is not one of them.** `--redo W1,W2` re-extracts
named works; `--redo-status unidentified_original,no_original_found` names a
POPULATION — any result verdict (`resolved`, `provisional`, `not_a_replication`,
`no_original_found`, `target_pending`, `api_error`) or any `link_method`, since two
link methods can share one verdict and a change that reaches one need not reach the
other. Both supersede the previous result row. Editing a prompt or a model mints a
new extract GENERATION, which reopens every work at once, because it changes what the
pipeline ASKS.

`EXTRACT_LADDER_VERSION` was in that fingerprint until 2026-08-10 and is not any
more. A ladder change alters how an original is FOUND, and it reaches a population its
author knows when writing it: ladder 22 was measured over the 105
`unidentified_original` rows, and 23 addressed the same class — while reopening every
settled work costs a whole campaign's wall clock for 3,025 works. So the reopen is
named, not inferred, and each changelog entry in `link_original.py` carries the
command that reopens what it fixed. This gave up no safety that was being enforced:
the export carries forward rows from a superseded generation by default, so a
stale-ladder verdict shipped either way. `--current-generation-only` is the separate,
honest lever for that question.

The CSV runner (`python -m extract.run_extract`, with `--fresh`, `--rescreen`,
`--extracted-test` and `--screen-here`) is retired — it prints a pointer and exits 2.
It is parked on the `wip/csv-runner` branch with a `WIP.md` saying what a revival
would have to satisfy; do not revive it as a second writer.

**Test sandbox:** `python -m extract.tier --run --mode validation` records real
verdicts the live export ignores (the mode lives in `claim.meta.mode`), and
`python -m extract.export --release <id> --mode validation --out data/extracted-test.csv`
renders them. There is no promotion step: re-running the work live is the promotion, and it
is near-free on cached calls. Set-asides belong to the CSV they came out of
(`set_aside_dir()` in `shared/schema.py`): production's sit in `data/`, the sandbox's
in `data/extracted-test-set-aside/`.

## Module Map

`shared/` was ported from the *OpenAlexLLM* prototype: it runs in production but some
thresholds/heuristics (notably `find_all_candidates()` in `openalex_client.py`) have
never been independently validated. Discuss shared changes with all stage teams.

| File | Purpose |
| ---- | ------- |
| `shared/openalex_client.py` | OpenAlex API wrapper + `find_all_candidates()` (Stage 3 logic) |
| `shared/openalex_keys.py`   | OpenAlex key rotation, shared by all stages |
| `shared/llm_client.py`      | Gemini/OpenAI/OpenRouter calls — one model per call site, named explicitly, with no fallback to another provider — JSON parsing; `classify_replication()` (front-door screen, called by Stage 2's expensive tier), `cached_classification()` (read-only cache door, for the export-csv record export), `screen_gate()`, `screen_voters()`, `resolve_targets_and_outcomes()` (the one call behind the abstract, reference-list and full-text rungs — target AND outcome), `screen_references_with_llm()` (reference-list target pick) |
| `shared/target_keys.py`     | `assign_target_keys()` — one deduplicated `@smith2009` namespace over a paper's candidates and references, plus the key → record map |
| `shared/token_usage.py`     | Per-day/provider/model token recording (`cache/token_usage.json`) + the OpenAI daily budget check |
| `shared/rate_limit.py`      | `throttle(service, interval)` — one reservation queue per remote service, so N worker threads share one rate rather than each sleeping its own |
| `shared/prompts.py`         | Every LLM prompt + `prompt_version()` (hash of the prompt text and every spliced fragment) |
| `shared/prescreen.py`       | What the cheap discard-only tier ASKS: `hard_signal()`, `prescreen_bypass()`, `prescreen_voters()`, `prescreen_vote()` (the public seam). The gate and the run loop are Stage 2's, in `filter/engine/tiers.py`; the tier is dormant (its specs are all shadow) |
| `shared/cache.py`           | Cache helpers; `content_key()` builds the content-complete LLM cache key |
| `shared/row_key.py`         | Row identity: `row_keys()` / `primary_key()` (doi → oa: → url: → title:) |
| `shared/pdf_sources.py`     | Multi-tier PDF acquisition waterfall. An up-front on-disk check replays the tier that really supplied a cached PDF (`pdfsrc_<key>.json`) instead of crediting whichever tier happened to re-derive a URL first. Two 14-day retry DELAYS, never verdicts, keep the waterfall from re-probing what already answered: one per (DOI, tier) when a tier came back empty, one per URL the server answered 404/410 for |
| `shared/pdf_parsing.py`     | Six PDF parse methods; `parse_all()`, `best_parse_result()` scoring |
| `shared/grobid.py`          | GROBID reference extraction |
| `shared/disambiguation.py`  | Two string helpers only: `jaccard_similarity()` (used by `link_original.py` and `doi_verify.py`) and `is_umbrella_paper()`. The same-author/year resolvers it was named for are gone; nothing here decides a candidate any more |
| `shared/doi_verify.py`      | doi_o verification/correction (CrossRef → OpenAlex) |
| `shared/utils.py`           | `clean_doi()`, `cache_key()`, `non_article_doi()`, helpers |
| `shared/config.py`          | All paths, env loading, rate limits — every tunable lives here |
| `shared/schema.py`          | CSV column definitions — the contract between stages |
| `shared/supabase_client.py` | Read client for the Supabase validation tables |
| `shared/dashboard_cache.py` | Parquet mirror + `data/dashboard/stats.json` so Stage 4 reads fast; each runner calls `refresh(stage)` |
| `shared/flora_skip.py`      | Two skip lists: already-in-FLoRA (entry sheet + `flora.csv`), read by the extract tier's worklist; and already-in-the-validation-tables (`data/validated_skip.csv`, work id or DOI), read by Stage 3 alone. The second is the frozen legacy `record_metadata` set, materialised once by `analysis/build_validated_skip.py` so a run needs no Supabase |
| `shared/token_counter.py`   | In-process per-stage token attribution; `set_stage()` before a call block |
| `shared/abstract_store.py`  | The abstract cache as one SQLite file (`cache/abstracts.sqlite`). One row per identifier: the text, or NULL for a definitive miss. **The row IS the checkpoint** — it absorbed `fetch_abstracts_done.txt` and the `fetch_abstracts_found.txt` sidecar, both of which existed only because file-per-key made whole-cache questions cost half a million syscalls, and either could drift from the cache it described. A transient failure is never recorded. Migration: `python -m shared.abstract_store --migrate` |
| `shared/hf.py`              | The Hugging Face plumbing shared by `pool_sync`, `cache_sync` and the engine tiers: which exceptions establish ABSENCE as opposed to unreadability, which failures a different token would fix, batched commits. The caller imports `huggingface_hub` and passes it in |
| `shared/cache_sync.py`      | Share the API caches through the same private dataset repo (`--push` / `--pull [--parts …]`). Safe because keys are content-complete; a differing checkout misses rather than mis-reads. Misses are shared too, except an unproven one — a gated source the pusher got zero hits from, that the puller is configured for, has its `__none__` entries AND checkpoint lines dropped. `cache/engine/responses` is the one cache it does not carry — the tiers push that themselves |

| Stage | Files |
| ----- | ----- |
| `search/` | `run_search.py` (the Stage 1 entry point: `--scan` runs the ledger-backed snapshot scan (sample scans use a scratch `FLORA_CACHE_DIR`); a bare invocation never starts a 725 GB scan), `snapshot_scan.py` (the bulk-parquet scan: **the search gate** → the survivor pool; also `pool_fingerprint()`, the pool's identity in a Stage 2 release id, and the `_pool_provenance.json` sidecar it reads — the gate the pool's rows were ADMITTED under and the file count that completes it, written by the scan and by `--pull`, stamped onto an older pool with `--stamp-pool`), `pool_sync.py` (share the pool through a private HF dataset repo: `--push` / `--pull`), `fetch_abstracts.py` (the six abstract-source phase runners — a library now, whose one consumer is `filter/engine/backfill.py`). Stage 1 searches and does not filter: the non-snapshot discovery sources were retired to `wip/api-harvest-sources` (PR #158) because nothing downstream read `data/candidates.csv` |
| `filter/` | `phrase_detection.py` — the token/stem vocabulary the **search gate** is built from. It is Stage 1's only keyword logic; Stage 2 does not call it. The old `rule_filter.py`/`run_filter.py` path is retired (#146) |
| `filter/engine/` | The issue #146 filter engine, which IS Stage 2: declarative JSON specs in `filter/spec/` routed by precedence into piles (`discard` / `screen_expensive` / `screen_cheap` / `needs_human` / `pending`) over the survivor pool; claimed, budget-gated LLM tiers; Stage 3 reads the screen verdicts from the store, and `export-csv` writes a release's screened rows as an ad-hoc record CSV. Rules route and discard; only LLMs admit. Design: [`docs/filter-engine.md`](docs/filter-engine.md); policy (precedence, pile→status mapping, measurement levels): `filter/spec/CONVENTIONS.md`. CLI: `python -m filter.engine specs\|route\|diagnose\|worklist\|screen\|export\|reconcile\|export-csv\|release-claim\|status` |
| `db/migrations/` | The engine's Postgres state authority (claims, permanent verdicts, audit, validation lineage) — SQL the maintainer runs in Supabase |
| `extract/` | `tier.py` (**the entry point**: Stage 3 as a claimed, budget-gated engine tier — worklist, claim + lease heartbeat, the judge, the stored result payload, `--redo`, and `supersede_targets()` for retroactive corrections), `export.py` (**the only writer of `data/extracted.csv`**: renders the stored payloads, sorted, atomic, partitioned into the set-aside CSVs, with a generation fallback and `--check`), `run_extract.py` (the per-row pipeline as a LIBRARY — `_process_row` and everything under it; its CLI is retired and parked on `wip/csv-runner`), `link_original.py` (resolution ladder), `code_outcome.py` (outcome coding; reproductions use the computation/robustness axes), `sanity_check.py` (the integrity REPORT over the exported CSV, plus the two `--deep` network buckets; it moves nothing), `audit_dois.py`, `audit_extracted.py` (read-only pre-validation audit), `backfill_authors.py` (retroactive `authors_o`/`ref_o` from OpenAlex), `clean_parse_cache.py` |
| `validate/` | Read-only Flask dashboard: `app.py` registers the `dashboard` and `check` blueprints only. The `batch` blueprint is parked on `wip/batch-blueprint` |
| `misc/` | Reference examples and small sample CSVs — do not import |

## CSV Schema

Authoritative: **`shared/schema.py`**; column reference: [`docs/csv-schema.md`](docs/csv-schema.md).
Never change a column name without updating `schema.py` and notifying all teams.

- `paper_type` (the `replication|reproduction|false_positive|needs_review` field) was
  called `filter_status` until issue #93. The validation database column keeps the old
  name — `csv_to_db.py` in `flora-validation` reads either and writes
  `record_metadata.filter_status` — and `render_payload()` in `extract/tier.py` maps
  the old key on payloads stored before the rename.
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
  is a contradiction.

  **A document need not be a PDF, but every source has a content check that
  a record page fails.** Four sources hand back a sections dict instead of a file
  (`_STRUCTURED_SOURCES` in `shared/pdf_sources.py`), and each is paired with the
  test that says whether what came back is a document:

  | `pdf_source` | What it is | The check it must pass |
  | ------------ | ---------- | ---------------------- |
  | `openalex_xml` | OpenAlex GROBID XML | `openalex_xml_has_content()` — any section text or any reference |
  | `epmc_xml` | Europe PMC JATS full text (`parse_jats_sections()` in `shared/grobid.py`) | `epmc_xml_has_content()` — any section text or any reference |
  | `osf_registration` | The OSF registration form, from the API | `osf_registration_has_content()` — ≥ 1,000 chars of description + form fields |
  | `html_landing` | The row's own page, parsed with lxml | `html_document_has_content()` — ≥ 10,000 chars BEYOND the abstract, or a ≥ 2,000-char reference block |

  Europe PMC is ONE tier with two routes: the JATS full text, which exists for the
  OA-licensed subset only, and — when that answers 404 — the article page's rendered
  PDF (`europepmc.org/articles/<PMCID>?pdf=render`). Both are keyed on the PMC id the
  tier's one search returns, and both share the tier's single retry slot.

  A result that fails its check is no document: it ends the row at
  `no_fulltext_available` and is never cached as a success. Each guard lives in
  `pdf_sources` alone — `acquire_pdf` never lets a shell out as a document, so the
  duplicate demotion in `run_for_doi` is gone.

  The HTML check exists because a repository landing page restates the abstract and
  adds citation chrome; coding a row from one looks like full text and is not. The
  abstract is SUBTRACTED rather than the total thresholded, because chrome inflates
  length without adding a word of the paper (measured 2026-08-07: five landing pages
  carried 0–1,706 chars beyond their abstracts, three full texts 49,193–71,641).
  Section headings are deliberately not the test — the PDF-oriented splitter finds no
  intro in any of PLOS, PMC or eLife.

  `osf_registration` covers the 33% of the worklist on the `10.17605` registrant,
  whose DOIs are registrations rather than files (`osf.io/download/<guid>/` answers
  HTTP 500). Stage 3 does not judge which registrations are worth reading — Stage 2
  already did: `osf-registration-protocol` (live, discard) drops the preregistration
  templates and `osf-registration-completed` (live, `screen_expensive`) admits the
  post-completion forms and the Open-Ended Registrations carrying the replication
  stem.

## Stage 3 — Front Door and Resolution

**Both screens are Stage 2's.** Neither the cheap tier nor the validated front door
runs here any more: the rule book routes a row to the `screen_cheap` or
`screen_expensive` pile and `filter/engine/tiers.py` runs the tier over it, claimed
and budget-gated. They are described here because their verdicts decide Stage 3's
rows. Stage 3 READS the expensive screen's answer off the row it is handed
(`SCREEN_COLS`) and never votes — structurally: `classify_replication` is not imported
into `extract/run_extract.py` at all. A row whose `screen_verdict` is blank is written
`target_pending`, and the tier's worklist never offers one: it holds back every work
a live, current-generation screen verdict has not admitted.

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
nothing for the screened-only worklist, and a live discard simply drops the row there.
`link_method = prescreen_discard` has no live writer for that reason; historical rows
carry it and the export still partitions them into `data/prescreen_discard.csv`. Evidence: `analysis/prescreen_eval/REPORT.md`.

**Front-door screen** (`classify_replication()`, run by `screen --tier
screen_expensive`): two voters — `SCREENING_MODEL_1`
(default `deepseek/deepseek-v4-flash` at effort `low`; the effort is load-bearing —
at `none` the same model discarded 7 settled positives) and `SCREENING_MODEL_2`
(default `gpt-5.4-mini`);
each id routes to its own provider through `provider_for()` — each answer the validated v3.2
schema: `classification` ∈ {replication, reproduction, both, none, unclear}, boolean
`confident`, `categories` (11-value enum), `evidence_quote`, `reasoning`. Prompt:
`_CLASSIFY_PROMPT` in `shared/prompts.py`, now at v3.3 — v3.2 plus the
partial-overlap rule (evaluated copy: `analysis/screening_eval/prompt_v33.txt`;
evidence: `analysis/screening_eval/report_v33.md`; the DeepSeek voter eval and the
gate change: `analysis/screening_eval/cheap_voter_2026-08.md`; earlier generations
are under `archive/analysis/screening_eval/`).

**The gate is `screen_gate()`, defined once** (G-unanimous — no single voter
discards alone; measured with the shipped pair at 1 settled miss and 86–90%
hard-negative discard across two runs):

- **discard** — all votes `none`, at any confidence → `not_a_replication`.
- **proceed** — everything else, including confident splits and a lone confident
  `none`. There is no `screen_disagreement` terminal state any more (historical rows
  on disk are still routed by the value in `schema.py`, `sanity_check.py` and
  `extract/export.py`).
- **no decision** — fewer than two votes: 1 vote → `target_pending` (re-run decides),
  0 votes → `api_error`. An incomplete screen is never a verdict, but each vote that
  did answer is cached on its own key (see below), so the re-run buys only the gap.

On a pass, the screen's `record_type` (both voters agreeing wins; splits fall back to
the first qualifying voter; `both` → replication) becomes `type` and overwrites
`paper_type` (`filter_method = "screen"`); with no qualifying vote at all, Stage 2's
values are kept and `type` stays empty. `screen_categories` (union of both voters) is
written on every screened row. **The classify cache is one entry per VOTE**
(`classifyvote_*`, keyed on the prompt version, that voter's `model@effort` and the
prompt): swapping one voter re-buys exactly that voter's answers while the other's
stay cache hits. Entries from the pair-keyed era are split on first read
(`_cached_vote()` in `shared/llm_client.py` lifts a vote out of a joint entry for
the model AT the effort the joint era ran, `_JOINT_ERA_EFFORTS`). A voter or prompt
change still mints a new SCREENING GENERATION, which is what makes those works
claimable again — and, once they are re-screened, what puts them back in the extract
tier's worklist.

The verdict reaches Stage 3 on the worklist row, in `SCREEN_COLS`:
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
acquisition + full-text LLM. The search-based links (`llm_title_search`,
`llm_author_year_search`) resolve through a pooled candidate list — CrossRef and
OpenAlex title hits plus the author-and-year shortlist — adjudicated by the linking
model with decline first-class. Since 2026-08-08 they are RESOLVED: outcome-coded and
imported at `link_confidence` low (measured 98–99% across two cross-vendor triages,
`analysis/stage3_eval/model_triage_2026-08-08.md`; the historical ~50% belonged to
the pre-pooling first-hit resolver).

**A rung ends the row only when it resolved AND settled the outcome**
(`OUTCOME_DESCENT` in `link_original.py`, not settable — it changes what a row is CODED
AS). A rung that accepted a link but could not settle its verdict CARRIES that
resolution and the ladder keeps descending, towards the closing sections that state it;
"settled" is `outcome_is_settled()` in `shared/schema.py` (replication: `outcome` not
`cannot_be_determined`; reproduction: neither axis unsettled). A carried resolution
outranks a withheld rule pick at every no-answer exit — `--no-pdf`, no document, no
context, an incomplete screen, a full-text provider failure — so an outage below an
accepted link no longer writes `target_pending` over it. When the full-text rung does
answer, its reading replaces the carried one, except that a later UNSETTLED outcome
never overwrites an earlier settled one (`_union_targets`, tracked in `outcome_stage`).
`EXTRACT_LADDER_VERSION` records what the ladder was when a row was written. Nothing
reads it — it is provenance, deliberately: a bump reopens no work by itself, and the
population a bump reaches is named on the command line (`--redo-status`).

**One prompt per vocabulary for the three LLM rungs, asking BOTH questions.**
`build_target_outcome_prompt()` (replication) and `build_repro_target_outcome_prompt()`
(reproduction) serve the abstract, reference-list and full-text stages; only the
evidence blocks differ, never the task or the acceptance rule. Each target the model
lists carries its own outcome, coded from the same reading: the two judgments are
separate, so a target the model is sure of may carry an outcome the evidence does not
settle. Candidates and references are one deduplicated `@smith2009` namespace
(`assign_target_keys()` in `shared/target_keys.py`), so a work in both lists is offered
once and a re-fetched list cannot renumber a cached pick onto another paper.
`resolve_targets_and_outcomes()` makes the call: it validates every key against that
call's key_map (invented key → unmatched target, repeated key keeps the first), takes
`doi_o` from the mapped record rather than from the model, keeps the mapped record on
each target, and reports `stated_count` / `unidentified_count` so a shortfall lands in
`link_evidence`. A target is accepted only when the model marks it `match_certain`, and
`replication_study_numbers` gives `study_r` — which study of the REPLICATION re-tests
this original. The outcome half goes through `normalise_outcome_block()` in
`shared/schema.py`, the same normaliser the standalone coder uses, so an
out-of-vocabulary verdict lands as `cannot_be_determined` rather than on a row.
`record_type_check` is asked only when the closing sections were sent — it is a
judgment about the methods, and a rung that read no body has not seen them. The cache
key names the rung (`abstract` | `reftarget` | `fulltext`) and the record type; its
prefix is `targetoutcome`, and the old `llm`/`reftarget` entries stay on disk unread
because the question changed.

**The per-target adapter.** A ladder that named targets without accepting one as THE
link goes to `_per_target_rows()`, which writes one row per original PAPER: resolve the
key through its record, collapse several studies of one original into one row, guard,
apply `--resolved-only`, then write each target's own outcome — the one the call that
named it coded — falling back to `extract_outcome` only where the row carries none.
Merging two studies of one original merges their axes too (`_aggregate_axes()`).
Ranks are renumbered after the drops. Unmatched targets get no row;
the shortfall is reported in `link_evidence`. Because the ladder returns at its first
success, `may_stop_at_a_rule()` in `link_original.py` withholds a deterministic pick
whenever the paper's own text does not rule out a second target. That gate is
necessary but not sufficient: the two methods in `_HELD_ONLY_METHODS`
(`single_candidate_after_requery`, `same_author_year_title_overlap`) are held
whatever it says, because neither carries a semantic check — only Path A's citation
score and the title-pattern rung may stop. The pick is withheld
only UNTIL something that can enumerate targets speaks: it is restored at every exit
where nothing did (`--no-pdf`, no document, no context, an incomplete screen — and
`--no-llm` never withholds at all), and after the full-text call when that call named
nothing or named the same work. A provider failure is not that answer, so it does not
restore. `original_match_type`, `original_match_confidence` and `n_originals` are
observations, settled after the guard's demotions and `--resolved-only`'s drops.

**The standalone outcome coder is for links no LLM chose.** `build_outcome_prompt()`
(replication) and `build_repro_outcome_prompt()` (reproduction), called by
`extract/code_outcome.py`, code the rows a deterministic rule resolved — where nothing
has read the paper and checked the original. The original is therefore given as
evidence to CHECK, with the link evidence that produced it, and the model answers
`target_check` (this_original · other_original · no_original · unclear) alongside
`record_type_check`. There is ONE call, over every passage the row has: the abstract,
and — when a document was acquired — two named blocks, INTRODUCTION and DISCUSSION /
CONCLUSION, the latter carrying a provenance line (`PROVENANCE_LABEL` in
`shared/prompts.py`). A reproduction is coded on two independent axes —
`outcome_computation` (computationally reproducible · computational issues · technical
failure · not checked) and `outcome_robustness` (robust · robustness challenges · not
checked), each with `cannot_be_determined` as its own escape and its own quote and
quote source — and its `outcome` is the two settled values joined.
`record_type_check == "neither"` and `target_check == "no_original"` both set
`not_a_replication`; `other_original` drops `link_confidence` to low and says so in
`link_evidence`; naming the other vocabulary re-codes the row once under the other
prompt and corrects `type`. That recode also fires from the combined call's per-target
`record_type_check`. There is no full-text escalation any more: it could not fire,
because a row resolved from the abstract never acquired a document. Reading on for an
unsettled verdict is the ladder's job — see `OUTCOME_DESCENT` below.

**Outcome coding runs only on a resolved link** (`_outcome_without_coding()` gates on
`RESOLVED_LINK_METHODS`); unresolved rows are written `pending`, except
`not_a_replication` where the screen's verdict is the outcome.

## Before a Run That Spends

Three rules, each written after it was broken on 2026-08-07. They cost minutes; the
runs they guard cost money and can close works permanently with a wrong verdict.

1. **The first run of changed Stage 3 code goes through the sandbox.**
   `python -m extract.tier --run --mode validation` records real verdicts the live
   export ignores, and re-running the same works live is the promotion — near-free,
   because the LLM calls are cached. There is no reason for the first exercise of
   new code to write live verdicts. A live pilot of a new resolution path closed 15
   works as `no_original_found` whose own stored evidence named the original in plain
   text; in the sandbox those would have been shadow rows to read and discard.

2. **Read the implementation of any worklist-changing flag before spending through
   it.** `--redo`, `--only` and `--limit` decide what gets bought. `--redo` was passed
   to a live run without reading the batch loop it feeds; the loop re-applies the redo
   set on every worklist rebuild, so the same 29 works were re-extracted nine times in
   ten minutes before the run was killed. `--redo` and `--redo-status` ADD to the
   worklist — they re-admit works the checkpoint subtracted; only `--only` RESTRICTS
   it. The same command without `--run` prints the worklist size for free, so the dry
   run is the check that settles what a run will actually extract. A `--redo` run
   whose worklist holds works nobody named refuses; `--allow-extra-works` accepts it.

3. **Run `/code-review` on the diff before any run that spends more than trivially.**
   A review pass is a rounding error against a $20 campaign.

The general form, which is what actually separated the useful work from the wasted
work that day: **verify against the artifact before spending, and read the output
before concluding.** Probing the OSF API showed the obvious regex fix would not have
helped; reading the stored payloads showed a clean-looking verdict distribution was
wrong; measuring eight pages set a threshold that reasoning about HTML would not have.
Every mistake came from acting before one of those checks.

## API Budgets and Usage

Every LLM call records input/output tokens per day/provider/model in
`cache/token_usage.json` (`shared/token_usage.py`). OpenAI spend is hard-capped by
`OPENAI_DAILY_TOKEN_BUDGET` (default 9,500,000/day — the free allocation, resetting midnight UTC; `0` disables): when exhausted,
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
that reaches it. That cost is the reason the search rungs stay at the bottom of the
ladder, below every rung a cached candidate list can answer.

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
in a module-level constant with the rationale in a comment
(`_TITLE_SEARCH_LEGACY_SHAPES` in `shared/openalex_client.py`, and the `doisearch_v2`
key in `shared/doi_verify.py`) and reads through
`read_cache_migrating(cache_dir, key, legacy_keys, migrate_note)`. Only the declared
component is substituted, so an equivalence stops matching by itself once anything else
about the call changes. A legacy hit is re-stored under the current key carrying a
`cache_migrated` note — the prompt version and models it is now filed under, and the
key it came from — so every response on disk stays traceable to what produced it; the
legacy entry is left in place for other checkouts and the shared HF cache.

### Editing a prompt without invalidating its cache

A prompt edit that provably cannot change any answer — a category renamed, a typo, a
reordering the model cannot read differently — does not have to re-buy every answer
already on disk. It is not automatic, and nothing detects it: the maintainer declares
it, once, and the declaration is reviewable. **An answer-preserving edit has three
invalidation surfaces, and missing any one of them silently undoes the whole exercise.**

1. **The LLM cache key.** Two components move: `prompt_version(builder)`, and — where
   the call site hashes the rendered prompt, as `resolve_targets_and_outcomes` does —
   the prompt TEXT. Freeze the pre-edit version as a literal, make the pre-edit text
   REPRODUCIBLE, and read through
   `read_cache_migrating(cache_dir, key, legacy_keys, migrate_note)`. Reproducible
   means the varying part is a parameter of the builder, not a copy of its output: the
   outcome vocabulary, for instance, is written into the prompt fragments as `«success»`
   markers and rendered from one dict (`_vocab` in `shared/prompts.py`), so a builder
   can render either vocabulary exactly rather than approximately. A search-and-replace
   over the rendered text is not good enough — these prompts use "failed" and
   "successful" as ordinary English too.
2. **The tier's GENERATION fingerprint**, if the prompt is in `_GENERATION_PROMPTS`
   (`extract/tier.py`, `filter/engine/tiers.py`). This one is easy to forget and
   expensive to miss: the cache can be perfect and the run still re-extracts every
   settled work, paying the OpenAlex verification bill again. Declare the pair in
   `_GENERATION_PROMPT_EQUIVALENCES` as `prompt: (version_after, version_before)`.
3. **Every seam an old ANSWER comes back through**, if the edit changed the vocabulary
   of the answer rather than only the wording of the question. The answers are not
   re-bought, so they arrive in the old vocabulary for as long as they live on disk —
   this is a permanent translation, not a migration step. One map
   (`OUTCOME_LEGACY_MAP` / `canonical_outcome` in `shared/schema.py`), applied at four
   places, and **the two cache-READ seams are the ones that get missed**: a cached
   answer is a stored ROW, normalised when it was written, so it never passes through
   the normaliser again.
   - `normalise_outcome_block` (`shared/schema.py`) — a fresh LLM answer.
   - the cache-hit branch of `resolve_targets_and_outcomes` (`shared/llm_client.py`).
   - the cache-hit branch of `_outcome_result` (`extract/code_outcome.py`).
   - `_normalise` in `extract/export.py` — a stored payload becoming a CSV cell.

   Miss the read seams and the damage is silent and downstream: `_aggregate_outcomes`
   in `run_extract.py` compares against the new labels, finds nothing substantive in a
   legacy-spelled block, and ships `cannot_be_determined` over a verdict the pipeline
   had already paid for.

**Every equivalence is pinned to the reviewed post-edit version, in both directions.**
`(after, before)` pairs and `if version == …_RENAMED_VERSION` guards mean the next
edit to that prompt produces a third version, matches no declaration, and falls back
to strict invalidation on its own. Nothing has to be un-declared later.

**Before editing, snapshot; after editing, assert.** Render the affected prompts with
fixed synthetic inputs and keep the digests, because a legacy rendering that is one
space out fails as a cache MISS, not as an error — the run just costs money. Pin the
digests in `tests/test_prompt_versions.py`, and pin the generation against the declared
pair in `tests/test_extract_tier.py`
(`test_a_declared_prompt_equivalence_reports_the_earlier_version`). A test that fails
loudly when a later edit breaks the equivalence is the only way this stays true.

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

**The big artifact is the survivor pool, not a CSV.** The pool is a few GB of parquet
and is shared through Hugging Face; the OpenAlex snapshot it is scanned out of is
725 GB. Nothing in `data/` is close to that: `data/extracted.csv` is a few hundred
KB, and an ad-hoc `export-csv` record of a release's screened rows a few thousand
rows (its manifest sidecar names the exact count for the file on disk).

The multi-GB `filtered.csv` this section was written for is the RETIRED pre-engine
file — the DVC-tracked `filtered.zip` still holds it at 1.7 GB. Stage 3 never reads a
CSV at all: the tier builds its worklist in process, straight off the pool through
`iter_export_rows` + `screen_columns` — the same two functions `export-csv` writes
its record file with — one batch of `EXTRACT_CLAIM_BATCH` works at a time, so
nothing about the input's size reaches memory.

Neither is resume a file any more. The checkpoint is the permanent verdict row, and
the worklist is REBUILT between batches by subtracting the works whose latest
current-generation result settles them. The whole read-and-truncate-and-carry-back
dance the old output CSV needed — and the 76 rows it deleted on 2026-08-05 — went with
it. `data/extracted.csv` is written whole, once, by `python -m extract.export`, through
a temp file and one rename; nothing appends to it, so there is no BOM-mid-file
question left.

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

**Run targeted tests while iterating, the full suite once.** `.venv/bin/pytest -q`
over `tests/` takes 100–440 s. During iteration run only the affected file or class
(`.venv/bin/pytest tests/test_engine_147_rules.py -q`); run the full suite once
before pushing.

## Environment

**The interpreter is `.venv/bin/python`, and that is how every command in this repo is
written.** Bare `python` is not on the maintainer's PATH, and the system `python3` is
missing `pyarrow`, so it cannot even import Stage 2. A command written for a human to
run — in a report, a commit message, a doc, a changelog's `reopen:` line — must be
pasteable from the project root as it stands: `.venv/bin/python -m extract.export
--release <id>`. The bare `python -m …` form appears in this file's examples as a
shorthand for the module path; it is not a command to hand over.

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
OPENAI_DAILY_TOKEN_BUDGET=9500000   # 0 disables the cap (default = the free daily allocation, resets midnight UTC)
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
`OUTCOME_DESCENT` in `extract/link_original.py`; the dry run's price list in
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
with `doi_r` always excluded as a correction target. **Verification happens once**: it
runs INSIDE the tier's judge, before the result payload is written, and the answer is
stored on the row. The three tiers issue up to three OpenAlex free-text searches per
row at 10× a filter query, so an export that re-verified would pay that bill on every
render — it renders what the verdict already holds. Retroactive audit:
`python -m extract.audit_dois [--apply|--doi …|--status api_error|--mode validation]` —
the only thing that re-verifies a settled row. It reads the exported CSV and, under
`--apply`, writes a corrected result verdict that supersedes the old one
(`supersede_targets()` in `extract/tier.py`); `python -m extract.export --release <id>`
then renders the correction. `extract/backfill_authors.py` takes the same route for
`authors_o`/`ref_o`. Thresholds are constants in
`shared/doi_verify.py`. The searches go through `_oa_get`, so they are throttled,
key-rotated, counted (`search_query_count()`, printed at the end of a run) and a quota
refusal raises `OpenAlexQuotaExhausted` instead of reading as "no match".

**Verification checks the DOI against the row's own metadata, never the record
against the target the paper NAMED** — the wrong entry picked from the right list
passes it as `verified`. That class is caught by the keyed-record check (issue #186
Shape 1): `_confirm_keyed_row()` in `extract/run_extract.py`, inside `_finalise_row`,
adjudicates every LLM-accepted keyed link cold — a separate cached call
(`confirm_keyed_original` in `shared/llm_client.py`) shown only the study's
title/abstract, the quoted evidence and the record. A confident "not the named
target" demotes the row to `link_method = keyed_link_disputed` (provisional: settles,
quarantined to `keyed_link_disputed.csv`, not imported), keeping the link, the
outcome and both readings for a human; an unconfident "no" only flags; no answer
writes `api_error` so the row is not settled on a transient failure. Measured before
wiring over all 63 settled keyed links in the evaluation samples: the one known-wrong
link flagged, zero false positives (`analysis/stage3_eval/keyed_confirm_eval.py`).

**The search-based links are GRADED, and the grade sets `link_confidence`** (issues
#183 and #186 shape 2). `_confirm_search_row()` in `extract/run_extract.py`, also
inside `_finalise_row`, sends every accepted `llm_title_search` /
`llm_author_year_search` link through `confirm_search_original()` — the same cold
inputs as the keyed check — and gets one of four grades: `clearly_target` ·
`likely_target` · `unlikely_target` · `clearly_not_target`. The grade decides the
row's `link_confidence` (`clearly_target` → high, `likely_target` → medium, the two
negative grades → low) and is appended to `link_evidence` as
`search_confirm: <grade> — <reasoning>`; it never changes the link or the method, and
no grade drops a row. A provider failure records `search_confirm: api_error`, leaves
the confidence at low and leaves the row settled. Graded rather than binary because
the binary check was measured on this class at 0 flags in 200 rows. Because the grade
decides a shipped field, `build_search_confirm_prompt` is in `_GENERATION_PROMPTS`;
whether the negative grades should also gate a discard or a review queue is read off
the campaign's collected grades (`analysis/stage3_eval/search_confirm_plan.md`).

## Further Reference

- **[`docs/README.md`](docs/README.md) — the documentation index.** Every guide and
  reference is listed there; this file keeps no second list.
- Seeding from prior FLoRA data (skip Stages 1–2): the prior-pipeline CSVs on the
  shared drive. `data/all_replications.csv` and
  `data/FLoRA entry sheet - replication list.csv` are the two a local checkout is
  expected to have — the latter is the file `shared/flora_skip.py` and
  `shared/config.py` actually read. Ask the maintainer for the drive listing rather
  than assuming a filename.
