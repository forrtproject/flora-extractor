# CLAUDE.md — FLoRA Extractor

This file is the primary instruction document for AI coding agents (Claude Code, Cursor, Copilot, etc.) and for human contributors. Read it fully before writing any code.

Other agent runtimes: see [AGENTS.md](AGENTS.md), which points here.

---

## What This Project Does

**FLoRA Extractor** discovers, extracts, and validates replication and reproduction studies for the [FLoRA database](https://forrt.org/replication-hub/flora/). Starting from keyword searches of academic databases, it identifies which paper each replication targets, what the outcome was, and exports the results for entry into FLoRA.

The pipeline produces structured records identifying:

1. Which original target study a replication targets
2. What the replication result was (success / failure / mixed / descriptive /
   statistically_successful_but_flawed / uninformative / cannot_be_determined /
   not_a_replication)

---

## Architecture — 4 Stage Pipeline

```text
Stage 1: search/     → discovers candidate papers → data/candidates.csv
Stage 2: filter/     → removes false positives    → data/filtered.csv
Stage 3: extract/    → finds original + outcome   → data/extracted.csv
Stage 4: validate/   → read-only monitoring dashboard (no CSV output)
```

Stages 1–3 each read one CSV and write a richer CSV. Stage 4 is a **read-only
monitoring dashboard** over those CSVs plus Supabase validation stats — it does
not write any pipeline CSV.

**Human validation lives in a separate repo backed by Supabase**, not in this one.
`extract/csv_to_db.py` pushes resolved rows from `data/extracted.csv` into three
Supabase tables (`unvalidated`, `record_metadata`, `validation_queue`, with three
validator slots per record: `human_1`, `human_2`, `llm`). The final artifact is the
Supabase `validated` table — there is no `data/validated.csv`.

Stages are independently runnable:

```bash
python -m search.run_search         # Stage 1 → data/candidates.csv
python -m filter.run_filter         # Stage 2 → data/filtered.csv
python -m extract.run_extract       # Stage 3 → data/extracted.csv  (streamed row-by-row)
python -m extract.csv_to_db         # push resolved rows into the Supabase validation tables
python -m validate.app              # Stage 4 monitoring dashboard → http://localhost:5001
```

Stage 3 streams results to `data/extracted.csv` one row at a time, so you can open the
Extract tab in the web app while the pipeline is still running.

**Test sandbox** — run new pipelines (multiple originals, reproductions) safely before
promoting to `extracted.csv`:

```bash
# Write to extracted-test.csv instead — skips already-resolved DOIs from extracted.csv
python -m extract.run_extract --extracted-test [--resume] [other flags]

# Promote test rows to production when satisfied
python -m extract.promote_test --all           # promote everything
python -m extract.promote_test --doi 10.xxx/y  # promote one row
python -m extract.promote_test --all --dry-run # preview without writing
```

The monitoring app registers these routes (see `validate/app.py`):

- `/dashboard`      — pipeline stats (CSV column reads) + Supabase validation KPIs
- `/check`          — Check page: filter/inspect extracted rows, download subsets
- `/batch`          — batch disambiguation for multiple-match papers (skipped when `FLORA_READONLY=1`)
- `/multi-originals`— multi-original paper review (skipped when `FLORA_READONLY=1`)
- `/`, `/pipeline`  — redirect to `/dashboard`

`validate/routes/` holds exactly these four blueprints; every one of them is
registered.

---

## Module Map — What Each File Does

### `shared/` — Shared utilities

> **Important caveats:**
>
> - `shared/` code was ported from an internal prototype called *OpenAlexLLM* (an earlier FLoRA extraction pipeline). It runs without errors and has been used in production, but it has **not been validated for correctness** — functions do what their names say, but thresholds and heuristics (e.g. Jaccard score cutoffs in `disambiguation.py`) have not been independently verified.
> - `shared/openalex_client.py` contains `find_all_candidates()`, which is Stage 3 extraction logic wrapped around an API call. It is not a neutral utility — Stage 3 teams should review and potentially revise the candidate-matching logic.
> - `shared/disambiguation.py` in particular is a key function for Stage 3 that **needs validation** before relying on it. The minimum acceptable Jaccard score and the tie-breaking logic should be reviewed by the team working on original-study linking.
> - If you need to change a shared function, discuss with all stage teams first.

| File                           | Purpose                                                                     |
| ------------------------------ | --------------------------------------------------------------------------- |
| `shared/openalex_client.py`    | OpenAlex API wrapper + `find_all_candidates()` (Stage 3 logic)              |
| `shared/llm_client.py`         | Gemini + OpenAI calls with key rotation, prompt builders, JSON parsing; `classify_replication()` is Stage 3's two-model front-door screen and `screen_references_with_llm()` the Stage 4.5 reference-list target pick |
| `shared/pdf_sources.py`        | Multi-tier PDF acquisition waterfall (arXiv → OSF → Unpaywall → CORE → …)   |
| `shared/pdf_parsing.py`        | Six PDF parse methods (openalex_xml, pdfminer, GROBID, docpluck, opendataloader, markitdown); `parse_all()` orchestrator; `score_parse_result()`, `best_parse_result()`, `best_parse_method_name()` scoring API |
| `shared/grobid.py`             | GROBID reference extraction from PDFs                                       |
| `shared/disambiguation.py`     | Same-author/year candidate disambiguation — needs validation                |
| `shared/doi_verify.py`         | DOI verification: `fetch_doi_metadata()` (CrossRef→OpenAlex), `resolve_doi_by_metadata()` search, `verify_and_correct()` orchestrator |
| `shared/utils.py`              | `clean_doi()`, `cache_key()`, common helpers                                |
| `shared/config.py`             | All paths, env var loading, rate limits; `MARKITDOWN_CACHE_DIR = cache/markdown/` |
| `shared/schema.py`             | CSV column definitions — the contract between pipeline stages               |
| `shared/prompts.py`            | Every LLM prompt in one module, plus `prompt_version()` / `prompt_versions()` — a prompt's version is the hash of its own text and every fragment it splices in |
| `shared/cache.py`              | Cache read/write/clear helpers; `content_key()` builds the content-complete LLM cache key, `clear_content_keys()` purges one paper's entries |

### `search/` — Stage 1

| File                         | Purpose                                                                     |
| ---------------------------- | --------------------------------------------------------------------------- |
| `search/openalex_search.py`  | Query OpenAlex API for papers with replication keywords                     |
| `search/external_lists.py`   | Bob Reed list scraper, I4R list scraper (pluggable — see Stage 1 docs)      |
| `search/deduplicate.py`      | Merge sources, deduplicate by DOI + fuzzy title, cross-check FLoRA sheet    |
| `search/run_search.py`       | Orchestrator: calls all sources, appends to `data/candidates.csv` via index |
| `search/fetch_abstracts.py`  | Backfills missing `abstract_r`: OpenAlex → Europe PMC → S2 → CrossRef → Scopus, each with its own checkpoint namespace; `enrich_abstracts()` is the per-row version called during the Stage 1 merge |

### `filter/` — Stage 2

| File                     | Purpose                                                                        |
| ------------------------ | ------------------------------------------------------------------------------ |
| `filter/rule_filter.py`  | Rule-based classifier: keyword patterns, author-year check                     |
| `filter/llm_filter.py`   | LLM classifier for uncertain cases only                                        |
| `filter/run_filter.py`   | Orchestrator: reads candidates.csv in 50k-row chunks, streams to filtered.csv  |

### `extract/` — Stage 3

| File                       | Purpose                                                                      |
| -------------------------- | ---------------------------------------------------------------------------- |
| `extract/run_extract.py`   | Orchestrator: screens each row at the front door (`_front_door_row()`), then classifies match type and routes to single or multi-original; `_resolve_and_code()` runs the ladder, guard and outcome gate for one row; supports `--extracted-test` flag; `_best_fulltext_from_cache()` feeds the best-scoring parse result to the outcome LLM; `_fill_work_ids()` stamps `oa_work_id_r`/`oa_work_id_o` on every row after DOI verification |
| `extract/link_original.py` | Single-original pipeline. `run_for_doi()` escalates through the resolution ladder below and only reaches the PDF when every cheaper stage declines; runs `parse_all()` on the PDF, scores all methods, uses the winner's text for the DOI-resolution LLM via shared `best_parse_result()` |
| `extract/multi_original.py`| Multi-original pipeline — finds all target studies (needs improvement)       |
| `extract/code_outcome.py`  | Outcome coding. `extract_outcome()` reads the abstract with an LLM and escalates to a second, fulltext-based call when the abstract cannot settle it (`OUTCOME_FULLTEXT_ESCALATION`); that call also applies the `is_genuine_attempt` veto that yields `outcome = not_a_replication`. The keyword patterns are the `--no-llm` fallback and the engine behind `predict_outcome_keyword()` / `--predicted-outcome`. Reproductions are coded on the 3×3 computation/robustness grid |
| `extract/promote_test.py`  | CLI + library: merge rows from extracted-test.csv into extracted.csv; `--all`, `--doi`, `--dry-run`, `--force` |
| `extract/audit_dois.py`    | CLI: retroactive DOI verification of extracted.csv; dry-run by default, `--apply` writes corrections; `--doi`, `--extracted-test` |
| `extract/sanity_check.py`  | Post-extraction quarantine pass; runs automatically at the end of `run_extract` (completion AND Ctrl-C). First-match-wins routing of problem rows to set-aside CSVs: `screen_disagreement` → `screen_disagreement.csv` (**before** the outcome rule, so a disagreement never lands in the agreed-no file), `not_a_replication`/non-article DOIs → `not_a_replication.csv`, self-links → `unresolved_self_links.csv`, `doi_o_verification==mismatch` → `unresolved_doi_mismatch.csv`, `llm_title_search` (provisional links) → `provisional_title_search.csv`, `target_pending` → `target_pending.csv`, and (with `--deep`) `doi_r` whose registry work type is a non-study object (dataset, software, peer-review, supplementary-materials) → `not_a_replication.csv` and fabricated `doi_o` → `fabricated_original_doi.csv`. `cannot_be_determined` is kept in extracted.csv. Standalone: `python -m extract.sanity_check [--input …] [--deep] [--report-only]` |
| `extract/clean_parse_cache.py` | CLI: delete all-empty parse caches from `cache/parse/` (written by pre-B4 runs that never fetched a PDF and then masked the real parse). Dry run by default, `--apply` deletes |
| `extract/csv_to_db.py`     | CLI: push resolved extracted.csv rows into the Supabase validation DB (creates 1 `unvalidated` + 1 `record_metadata` + 3 `validation_queue` rows per record; slots `human_1`/`human_2`/`llm`); `--input`, `--dry-run` |

### `validate/` — Stage 4 (read-only monitoring dashboard)

The Flask app is a **read-only monitoring dashboard**. It does not write to any
pipeline CSV or SQLite database (the old SQLite/SQLAlchemy voting app — `import_csv.py`,
`models.py`, `routes/review.py`, `routes/flora.py`, `routes/export.py`, the `/vote`
endpoint, `data/validated.csv` — has been removed). Human validation runs in a
separate Supabase-backed repo.

| File                           | Purpose                                                             |
| ------------------------------ | ------------------------------------------------------------------- |
| `validate/app.py`                  | Flask entry point, `create_app()` factory. Registers **only** the `dashboard`, `check`, `batch`, and `multi_originals` blueprints; `/` redirects to `/dashboard` |
| `validate/state.py`                | Shared in-memory DataFrames (FLoRA sheet, etc.) populated at startup and imported by blueprints |
| `validate/routes/dashboard.py`     | `GET /dashboard`; CSV pipeline stats (link-method / model-family breakdowns) + Supabase validation KPIs via `shared/supabase_client.py` |
| `validate/routes/check.py`         | `GET /check`; filter/inspect extracted rows and download subsets |
| `validate/routes/batch.py`         | `GET /batch`; batch disambiguation for multiple-match papers |
| `validate/routes/multi_originals.py` | `GET /multi-originals`; multi-original paper review |
| `shared/supabase_client.py`        | Read client for the Supabase validation tables; backs the dashboard's Supabase KPIs |

### `misc/` — Reference only, do not import

| File                           | Purpose                                      |
| ------------------------------ | -------------------------------------------- |
| `misc/openalex_api_example.py` | Standalone example: how to call OpenAlex API |
| `misc/gemini_api_example.py`   | Standalone example: how to call Gemini API   |
| `misc/sample_candidates.csv`   | 20-row sample for Stage 1 output testing     |
| `misc/sample_filtered.csv`     | 20-row sample for Stage 2 output testing     |
| `misc/sample_extracted.csv`    | 20-row sample for Stage 3 output testing     |

---

## CSV Schema — The Contract Between Stages

The authoritative schema definition is **`shared/schema.py`**. Human-readable column reference: **[`docs/csv-schema.md`](docs/csv-schema.md)**.

Never change a column name without updating `schema.py` and notifying all teams.

Key constraints:

- `filter_confidence` is categorical (`high | medium | low`), not a float — a single LLM call cannot produce calibrated probabilities.
- `outcome = descriptive`: paper replicated methods in a different context but does not test the original claim. Include in extraction; flag for review during validation.
- `outcome = uninformative` vs `cannot_be_determined`: the first is the AUTHORS' verdict (their attempt cannot speak to the original — underpowered, design failure), the second is OURS (we could not reach a verdict from the text available). Coding one as the other makes a correctly-coded paper look unread.
- `study_o` holds the target study NUMBER(s) inside the original paper (FLoRA's coding level: several studies from one original paper are one row, several original papers are several rows). The validation DB uses the same name for a title — see `extract/csv_to_db.py`.
- `api_error` in any field means extraction failed after retries — reviewers see these in the Validate tab.
- `original_match_type` is determined by Stage 3 as its first routing step — not inherited from Stage 2.

---

## Stage 3 Routing Logic

Stage 3's **front door** is the classification screen: before anything else, two
models vote on "is this paper a replication or reproduction at all?"
(`classify_replication()` in `shared/llm_client.py`). 58% of screened rows are
discarded there, so every call made before that vote is spent on a row that is then
thrown away. Only rows that survive the front door pay for match-type
classification, the resolution ladder, PDF acquisition or outcome coding.

```python
# run_extract.py, per row:

screen = classify_replication(doi_r, title_r, abstract_r)
# confident agreed "no"  → write not_a_replication and continue
# models disagree        → write screen_disagreement and continue
# one vote / no votes    → write target_pending / api_error and continue

original_match_type = classify_match_type(row)   # Stage 3's own classification

if original_match_type == "multiple_original":
    # Paper targets N independent originals
    results = run_multi_original(doi_r, ...)
    # → expand to N rows in extracted.csv (original_rank = 1, 2, 3...)
else:
    # single_original or multiple_match: same pipeline
    result = run_single(doi_r, ...)
    # → 1 row in extracted.csv
```

`classify_match_type` calls its LLM only when the abstract carries **≥ 2 distinct
author-year pairs** — the only abstract evidence that several different originals are
being targeted. Below that it returns `single_original` without a call (it answered
`single_original` for 94% of rows anyway). The deterministic multi-title / "replication
of N studies" rules run regardless, so Many Labs-style papers still route correctly.

**Outcome coding runs only on a resolved link.** `_outcome_without_coding()` in
`run_extract.py` is the single gate, derived from `RESOLVED_LINK_METHODS`: a row whose
`link_method` is not in that set (`target_pending`, `api_error`, `no_original_found`,
`screen_disagreement`, `not_a_replication`, `llm_title_search`) has no confirmed original
to code against, so no outcome LLM runs and the row is written `pending` — except
`not_a_replication`, where the screen's verdict *is* the outcome. The order per row is
resolve → merge → `_guard_original_link` → `--resolved-only` → outcome, so a row the
guard demotes or `--resolved-only` discards never reaches the outcome call.

### Original-study resolution ladder

`run_for_doi()` in `extract/link_original.py` works from cheapest to most
expensive and returns at the first stage that resolves. Every stage before the
PDF runs on metadata the pipeline already holds, so full-text acquisition is a
last resort rather than the normal path.

| # | Stage | Fires when | `link_method` on success |
| - | ----- | ---------- | ------------------------ |
| 2 | OpenAlex candidate re-query | always — builds the candidate pool from the paper's `referenced_works` | (not a resolver) |
| 2.5 | Title-pattern resolver | the title matches "A Replication of X" and one candidate matches it | `title_pattern_match` |
| 3 | Rule-based resolver | the abstract carries an author-year citation matching a candidate, or exactly one candidate came back | `citation_context_match`, `same_author_year_title_overlap`, `single_candidate_after_requery` |
| 4 | Abstract LLM | the abstract carries author-year patterns, with candidates to choose from | `llm_cited_candidates` |
| 4.5 | **Reference-list target pick** | there are referenced works (regardless of citation patterns) | `llm_references` |
| 4.6 | Title search on a named-but-unmatched target | the screen agreed at high confidence that this is a replication and named a target it could not match to a reference | `llm_title_search` (**provisional** — see below) |
| 5 | PDF acquisition + full-text LLM | everything above declined | `llm_fulltext`, `llm_title_search` (**provisional**) |

`llm_title_search` is the one link method whose answer is not chosen from a bounded
candidate list, and a hand-check measured it at roughly 50% precision. It is therefore
**not** in `RESOLVED_LINK_METHODS`: `link_confidence` is forced to `low`, no outcome is
coded, `csv_to_db` does not import the row, and `sanity_check` sets it aside in
`data/provisional_title_search.csv` for human confirmation.

Stages 2.5–4 all depend on `find_all_candidates()`, which returns `[]` unless the
title or abstract contains a parseable `(Author, Year)` citation. Many abstracts —
clinical and life-sciences ones especially — carry no such citation, so those
stages cannot fire at all for them.

**Stage 4.5** covers that gap. The screen asks two questions, split across two
functions in `shared/llm_client.py` because they are now decided at different points
in the pipeline:

1. *Is this a replication or reproduction at all?* — `classify_replication()`, run at
   Stage 3's front door (above). A confident agreed **no** ends the row there:
   `outcome = not_a_replication`, no match-type call, no ladder, no PDF, no outcome
   LLM, and `sanity_check` routes the row to `data/not_a_replication.csv`. Cached at
   `cache/llm/classify_{key}.json`.
2. *Which reference is the target?* — `screen_references_with_llm()`, here at Stage
   4.5, where the reference list has been fetched. It takes the front door's verdict
   as its `classification` argument rather than voting again (a caller without a
   verdict, e.g. the batch tools, lets it vote). A target is accepted **only at
   `confidence == "high"`**; at medium or low the row escalates to full text — a wrong
   original is worse than a slow one, and the prompt states explicitly that most
   abstracts do not name their target and that declining is the expected answer.
   Cached separately at `cache/llm/reftarget_{key}.json`.

**Two providers are required.** Question 1 is voted on by Gemini (`GEMINI_LIGHT_MODEL`)
*and* OpenRouter (`SCREEN_VOTER2_MODEL`, default `mistralai/ministral-14b-2512`), and the
screen acts only when both answer and agree, so `GEMINI_API_KEY` and `OPENROUTER_API_KEY`
must both be set — `extract.run_extract` refuses to start otherwise (unless `--no-llm`).
Voter 2 is deliberately outside the Google lineage: on adjudicated hard cases this pair
correctly discards 89% of true negatives (gpt-5-mini's pair managed 25%) while still
losing no genuine replication. Changing either voter changes the cache key by itself —
both voters' model names are folded into it — so one pair's verdicts can never be
replayed as another's. An incomplete screen is reported as such rather than as a verdict, and is
never cached:

| Votes | `resolution_method` | `link_method` |
| ----- | ------------------- | ------------- |
| 2 | agreement/disagreement as above | resolved, `not_a_replication`, or `screen_disagreement` |
| 1 | `llm_refscreen_partial` | `target_pending` — one vote is not a disagreement; the row waits for a re-run |
| 0 | `llm_refscreen_failed` | `api_error` |

Rows the screen sets aside (`not_a_replication`, `screen_disagreement`) carry the
screen's models in `link_llm_model` and its verdicts/evidence in `link_evidence`, so a
reviewer can see what decided them. On a resolved `llm_references` row those fields
instead name the model that picked the reference — that call, not the Q1 vote, made the
link.

---

## LLM Models

Do not hardcode specific model names. Teams should choose models appropriate to their task:

- For **simple pattern matching** (e.g. "is this a replication?"), try a smaller/cheaper model first (e.g. Flash Lite). Smaller models are often sufficient and have higher rate limits.
- For **complex linking or reasoning** (e.g. identifying the original study from an abstract), use a more capable model.
- Test quality on a sample before committing to a model for a full run.

Configure model names in `.env` so they can be changed without editing code:

```bash
GEMINI_MODEL=gemini-2.0-flash       # override as needed
OPENAI_MODEL=gpt-4o-mini            # override as needed
```

---

## Caching

Every API call (OpenAlex, Gemini, OpenAI, CrossRef) must be cached so that re-runs don't repeat expensive calls.

**A cache key must name everything the cached answer depends on.** A key that omits an
input silently answers one question with another question's answer, and a cache that
cannot be invalidated is a cache that pins a bug. For an LLM call that means the prompt
version, the model that will answer, and the inputs actually sent:

```python
from shared.cache import content_key, read_cache, write_cache
from shared.prompts import prompt_version

prompt = build_filter_prompt(title, abstract)
key = content_key("filter", doi_r, prompt_version("build_filter_prompt"),
                  GEMINI_MODEL, prompt)
cached = read_cache(LLM_CACHE_DIR, key)
if cached is None:
    result = call_api(prompt)
    write_cache(LLM_CACHE_DIR, key, result)
else:
    result = cached
```

`content_key()` produces `<prefix>_<doi hash>_<content hash>`. The DOI hash is in the
name only so one paper's entries can be found and purged together —
`clear_content_keys(dir, "outcome", doi)` — never as the key itself.

`prompt_version(name)` (see `shared/prompts.py`) is the sha256 of the prompt's own text
plus every fragment it splices in, so editing a prompt or a shared fragment invalidates
exactly the caches that depended on the old wording. There is nothing to register and no
version constant to remember to bump.

Cache non-answers too. A model that declines to identify a target has answered; a
provider that returned a 503 has not. Caching only the successes made every declined
full-text call repay its API cost on every re-run.

For plain API responses (OpenAlex, CrossRef) keyed by identifier, `cache_key()` from
`shared/utils.py` on that identifier is enough.

Cache files are stored in `cache/` (gitignored). They persist across runs; clear manually if you need fresh data.

---

## Error Handling on API Failures

On any API call failure (LLM, OpenAlex, CrossRef):

1. Log the error with the DOI and error code.
2. Retry up to **3 times** with exponential backoff: 1 s, 2 s, 4 s.
3. After 3 failures: set the relevant field to `api_error` (e.g. `outcome = api_error`, `link_method = api_error`) and continue to the next record — do not crash the pipeline.
4. This produces an `api_error` status that reviewers can see in the Validate tab, distinct from `pending` (not yet processed).

---

## Large-File Handling — Index-Based Deduplication

candidates.csv and filtered.csv grow beyond 1 million rows over a full search run. Loading either file entirely into memory causes OOM on typical developer machines. Both Stage 1 and Stage 2 use persistent index files to avoid this.

### How it works

Instead of reading the full CSV to check for duplicates or resume progress, each stage maintains a sidecar index in `cache/`:

| Index file                    | Used by          | Contents                                               |
| ----------------------------- | ---------------- | ------------------------------------------------------ |
| `cache/candidates_index.txt`  | Stage 1 merge    | All identifiers ever written to candidates.csv         |
| `cache/filtered_index.txt`    | Stage 2 resume   | One resume key per row already written to filtered.csv |

Each line in an index file is one key. Keys use the same priority fallback as the rest of the pipeline:

```text
doi (cleaned)  →  oa:<openalex_id>  →  url:<url>  →  title:<lowercased title>
```

The candidates index stores **all** keys for each row (up to four per row) so a duplicate can be caught via any identifier. The filtered index stores **one** key per row (highest-priority identifier only), which is sufficient for resume.

### First run / migration

If an index file is missing, it is built automatically from the existing CSV in **50k-row chunks** before the first merge or filter run. This is a one-time cost (~30s for 800k rows). After that, all subsequent runs load only the small index file (~1s).

### Stage 1 merge behaviour

`_merge_into_candidates_csv` in `search/run_search.py` now:

1. Loads the candidates index
2. Filters the incoming batch to rows whose keys are not in the index
3. **Appends** only the new rows to candidates.csv (never reads the full CSV)
4. Updates the index after a successful write

Because rows are appended rather than merged into a full rewrite, the file encoding rule has a nuance: the initial write uses `utf-8-sig` (BOM); all subsequent appends use plain `utf-8` to avoid embedding BOM mid-file. Excel reads both correctly.

### Stage 2 read behaviour

`run_filter` reads candidates.csv in **50k-row chunks**, applying year and source filters per chunk. The filtered candidate set passed to the rule/LLM classifiers is therefore never larger than what passed those filters — not the full CSV.

### Rebuild commands

If an index becomes stale (e.g. rows were added to a CSV manually outside the pipeline):

```bash
python -m search.run_search --rebuild-index   # rebuilds cache/candidates_index.txt
python -m filter.run_filter --rebuild-index   # rebuilds cache/filtered_index.txt
```

---

## Code Style Rules

1. **Python primary.** Type hints on all function signatures.
2. **R is welcome.** Teams may implement individual stage functions in R, provided input/output CSV schemas are identical. Include equivalent test cases. We can help translate to Python later if needed.
3. **No unnecessary abstractions.** Three similar lines is fine; don't create a helper unless it's used three or more times.
4. **Comments:** Default to no comments. Add one only when the WHY is non-obvious — a hidden constraint, a threshold that was empirically chosen, a workaround for a specific API quirk. File-level docstrings should be a short paragraph explaining what the file does and why it exists, not just a list of functions.
5. **Error handling only at system boundaries** (API calls, file I/O). Don't wrap internal logic in try/except.
6. **All CSV writes use `utf-8-sig` encoding** (BOM, Excel-compatible). Exception: when appending to an existing file, use plain `utf-8` to avoid embedding BOM mid-file — Excel handles both correctly.
7. **All DOIs pass through `clean_doi()`** from `shared/utils.py` before writing or comparing.
8. **All API responses must be cached** using the pattern above before any result is used.
9. **Rate limiting:** every interval lives in `shared/config.py` and is overridable via env. OpenAlex 0.3 s (`OPENALEX_RATE_SEC`); Gemini 1 s (`GEMINI_RATE_SEC`), OpenAI 0.5 s (`OPENAI_RATE_SEC`), OpenRouter 0.5 s (`OPENROUTER_RATE_SEC`). The LLM intervals are charged **per provider**, against that provider's own last-call timestamp — the screen's two votes go to different providers and neither waits on the other.

---

## Testing

### Schema tests (no mocking needed)

Each stage should include a test that reads the stage's output CSV and checks it has all required columns:

```python
import pandas as pd
from shared.schema import validate_csv_columns

df = pd.read_csv("misc/sample_filtered.csv")
missing = validate_csv_columns(list(df.columns), "filtered")
assert not missing, f"Missing columns: {missing}"
```

### Unit tests with mocked APIs

Use `unittest.mock.patch` or `pytest-mock` to mock external API calls in unit tests. Never make live API calls in regular `pytest` runs.

```python
from unittest.mock import patch

def test_classify_replication(tmp_path):
    with patch("shared.llm_client.call_gemini") as mock_gemini:
        mock_gemini.return_value = ({"classification": "replication",
                                     "confident": True, "categories": ["clearly_declared"],
                                     "evidence_quote": "", "reasoning": ""}, None)
        vote = _classify_once("prompt", "gemini")
    assert vote["classification"] == "replication"
```

### Live API tests

Place live API tests in `tests/live/`. Guard them with an environment variable so they never run in CI unless explicitly enabled:

```python
import os
import pytest

@pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run live API tests"
)
def test_openalex_live():
    ...
```

Run with: `TEST_LIVE_API=1 python -m pytest tests/live/`

---

## Environment Variables

Copy `.env.example` to `.env`. The example file includes all variables and their defaults.

Key variables:

```bash
RESEARCHER_EMAIL=you@example.com      # required for OpenAlex/Crossref politeness headers
GEMINI_API_KEY=...                    # required for LLM calls
GEMINI_API_KEY_2=...                  # optional: failover key (does NOT raise quota — see below)
OPENAI_API_KEY=...                    # optional fallback LLM
OPENROUTER_API_KEY=...                # required for Stage 3 (screen voter 2)
S2_API_KEY=...                        # optional: Semantic Scholar API key (Stage 1)
GROBID_URL=http://localhost:8070      # default; override if GROBID runs elsewhere
GEMINI_MODEL=gemini-3-flash-preview   # primary Gemini model
GEMINI_HEAVY_MODEL=gemini-3-flash-preview  # used for DOI resolution (defaults to GEMINI_MODEL)
OPENAI_MODEL=gpt-5-mini               # OpenAI fallback
SCREEN_VOTER2_MODEL=mistralai/ministral-14b-2512  # Stage 4.5 screen, voter 2 (OpenRouter)
FILTER_OPENAI_MODEL=gpt-5-mini        # Stage 2 filter primary model
GEMINI_USE_FLEX=true                  # 50% cost reduction; paid keys only
GEMINI_PAID_KEYS=1                    # 1-based key slots that are paid; flex applies to these
```

### Gemini quota: billing, not key rotation

Gemini rate limits are applied **per project, not per API key**. Extra
`GEMINI_API_KEY_N` slots from the same project therefore share one bucket — they
buy failover against a revoked or misconfigured key, not throughput. Sharding a
workload across projects to multiply free quota is both against Google's terms
and arithmetically hopeless here.

The binding constraint is the heavy model's free-tier ceiling of **20 requests per
day**. A single row costs 1–4 heavy calls as it escalates the resolution ladder,
so the free tier sustains roughly 5–20 rows/day: a 2,000-row run is not runnable.
Enabling billing (**Tier 1**) raises that ceiling to **10,000 RPD**.

Billing is not a layer on top of the free tier — it replaces it, so usage is
billed from the first token, and a project past its spend cap returns
`429 RESOURCE_EXHAUSTED` rather than falling back to free quota. Paid-tier
prompts and responses are also excluded from Google's product-improvement use,
which matters for unpublished abstracts.

**Intended configuration: one paid project with `GEMINI_USE_FLEX=true`.** Flex
carries the same 50% discount as Batch with no job-submission plumbing, and the
pipeline applies it to every Gemini call — including the PDF and image calls,
which carry the largest payloads. Flex requests can queue, so they use
`GEMINI_FLEX_TIMEOUT` (default 900s) instead of the standard per-call timeout; if
the API rejects the flex tier, the call is retried once at standard tier.

GROBID is optional. If `GROBID_URL` points to a server that is not running, the PDF extraction step logs a warning and falls back to abstract-only processing. It does not crash.

---

## Git Workflow

```text
main     ← protected; PR + 1 review required; no direct commits
  └── feature/*   ← feature branches (search / filter / extract / validate)
```

**Actual practice (as of 2026-07):** recent PRs branch from `main` and merge **directly
back to `main`**. The `dev` integration branch described in earlier versions of this doc
still exists but is **~34 commits behind `main` and effectively stale** — do not base new
work on it. Base feature branches on `origin/main` and open PRs with `--base main` unless
the team decides to revive `dev`.

- **Open PRs when a feature is stable, not just at the end.** Partial, working functionality is better to merge than a giant branch at deadline.
- `data/` and `cache/` are gitignored — add sample files to `misc/` instead.
- Branch protection is enforced on `main` (PR + 1 review required).
- **Team decision needed:** either revive `dev` as a real integration branch or drop it
  from the documented workflow. This section documents current practice, not an endorsement.

---

## PDF Parsing — How the Best Parser Is Selected

`parse_all()` in `shared/pdf_parsing.py` runs six methods and returns a dict keyed by method name. Both `link_original.py` (DOI resolution) and `_get_outcome` in `run_extract.py` (outcome extraction) call `best_parse_result()` to pick the winner:

```text
score = refs × 300  +  abstract_len  +  intro_len × 2  +  min(raw_text_len ÷ 5, 1000)
```

The winner's `abstract + intro` is fed to the LLM. Structured references (for citation pattern matching) come from whichever method the winner is — if MarkItDown wins but has sparse references, the LLM prompt's reference section will be thin; this is acceptable because citation matching runs as a rule-based step before the LLM fires.

Parse results are cached at `cache/parse/parse_{key}.json`. MarkItDown's raw `.md` output is additionally cached at `cache/markdown/{key}.md` (human-readable). The detail panel in the web app shows a **★ USED BY LLM** badge on the winning column plus each method's score.

If a row's parse cache exists but is missing the `markitdown` key (written before MarkItDown was added), the detail panel runs MarkItDown lazily on first open and updates the cache.

---

## DOI Verification — Catching Hallucinated doi_o Values

LLM resolution can produce the right title/author with a **wrong DOI** (a registered DOI pointing to a different paper). `shared/doi_verify.py` catches this by fetching the metadata each `doi_o` actually points to (CrossRef, the registry of record; OpenAlex fallback) and comparing title/year. On mismatch it re-resolves the DOI from title+author via CrossRef bibliographic search, with OpenAlex as fallback because OpenAlex also indexes DOI-less works (old papers, book chapters). The replication's own `doi_r` is always excluded as a correction target — replication titles often echo the original's title, so the search can return the replication paper itself.

**Runs automatically:** every row written by `extract.run_extract` (single-original, multi-original, and `--extracted-test`) passes through `_verify_row()` in `_append_row()` before hitting the CSV. No flag needed. On a correction, `doi_o` is replaced, `pair_id` is recomputed, `ref_o` is re-fetched, and a note is appended to `link_evidence`. On a `mismatch`, `link_confidence` is downgraded to `low`.

**Retroactive audit** of an existing CSV:

```bash
python -m extract.audit_dois                  # dry-run: console summary + data/doi_audit_report.csv
python -m extract.audit_dois --apply          # write corrections into extracted.csv
python -m extract.audit_dois --doi 10.x/y     # single row
python -m extract.audit_dois --extracted-test # audit extracted-test.csv instead
```

Matching thresholds are constants in `shared/doi_verify.py` (`VERIFY_TITLE_JACCARD = 0.5`, `RESOLVE_TITLE_JACCARD = 0.7`, `TITLE_ONLY_JACCARD = 0.6`, `TITLE_ONLY_GAP = 1.5`, `YEAR_TOLERANCE = 1`). Auto-correction tries three tiers strictest-first — a wrong correction is worse than a flag. `doi_o_verification` status values and their meanings are in [`docs/csv-schema.md`](docs/csv-schema.md).

---

## Known Limitations & Revisit Obligations

See [`docs/limitations.md`](docs/limitations.md) for the recall bound imposed by the
Stage-2 phrase gate, exclusion-pattern misfires, the uninformative `filter_confidence`
field, and the missing-abstract backfill — each with its revisit obligation.

## Implementation Status

All core pipeline modules are implemented and running. `shared/` was ported from the *OpenAlexLLM* prototype — it works but predates this project's test suite; see caveats in the Module Map above.

For the full feature list and what each module does, read the module map above and the docs:

- [`docs/csv-schema.md`](docs/csv-schema.md) — all columns across all stages
- [`docs/cli-reference.md`](docs/cli-reference.md) — all CLI commands and flags
- [`docs/dashboard-guide.md`](docs/dashboard-guide.md) — 6-tab dashboard, downloadable stats
- [`docs/check-page.md`](docs/check-page.md) — Check page filters and download API
- [`docs/parquet-cache.md`](docs/parquet-cache.md) — Parquet backend and stats.json cascade

## What Needs to Be Written

All core pipeline modules are now implemented. Known gaps:

- Live LLM integration tests in `tests/live/` (the directory now exists with `test_doi_verify_live.py`; LLM tests still missing), guarded by `TEST_LIVE_API=1`
- Unit tests for standalone scripts: `search/sensitivity_check.py`, `extract/csv_to_db.py`
- Unit tests for orchestrators: `search/deduplicate.py`, `filter/run_filter.py` (currently tested only indirectly)
- Unit tests for `extract/promote_test.py` promote logic (currently smoke-tested only)

---

## Seeding With Existing Data

The following CSVs from prior FLoRA extraction work can be used to skip Stages 1–2:

- `data/openalex_candidates.csv` — confirmed replications with OpenAlex metadata
- `data/all_replications.csv` — full known replication set from all pathways
- `data/flora_entry_sheet.csv` — use for deduplication in Stage 1 (skip DOIs already in FLoRA)
- `data/flora_selected.csv` — 107 curated rows from prior FLoRA work (legacy seed; the current Stage 4 app is read-only and does not load this file)

These files are in `data/` on the shared drive. If you are setting up from scratch and the files are not present, contact the project leads.

Stages 1–2 are only needed for discovering new replications not yet in these files.
