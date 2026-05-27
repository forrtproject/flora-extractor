
# Stage 3 — Extract

**Input:** `data/filtered.csv`
**Output:** `data/extracted.csv` (streamed — each row written immediately after processing)

---

## Running Stage 3

### Full pipeline

```bash
python -m extract.run_extract
```

Results are streamed to `data/extracted.csv` one row at a time. You can open the Extract tab in the Stage 4 web app while the pipeline is still running.

### CLI flags

| Flag | Description |
| --- | --- |
| `--no-llm` | Skip all LLM calls. Uses rules and heuristics only. Useful for a fast first pass or testing without API keys. |
| `--match-type-only` | Classify `original_match_type` for every row only. Writes `data/match_type_only.csv`. Mutually exclusive with `--outcome-only`. |
| `--outcome-only` | Run only the outcome extraction step (keyword + LLM). Writes `data/outcome_only.csv`. Mutually exclusive with `--match-type-only`. |
| `--limit N` | Process only the first `N` non-false-positive rows. Useful for spot-checks during development. |
| `--no-pdf` | Skip PDF download and all fulltext processing. Stages 2.5, 3, and 4 (title-pattern, rule-based, abstract LLM) still run. If those resolve → resolved. If not → writes `target_pending` immediately without downloading a PDF or calling the fulltext LLM. Use `--resume` on a second pass to fill these in. |
| `--no-multiple-originals` | Write `multiple_original` rows as `target_pending` instead of running the expensive multi-original LLM. Useful for a first pass. |
| `--no-reproductions` | Skip rows with `filter_status = reproduction` (write as `target_pending`). |
| `--skip-flora-validated` | Skip DOIs already validated in `data/FLoRA entry sheet - replication list.csv` where `validation_status` is `validated - unchanged` or `validated - changed`. Avoids re-extracting 1,100+ already-confirmed papers. |
| `--resume` | Carry forward already-resolved rows from `extracted.csv` unchanged; re-run only rows whose `link_method == "target_pending"`. Rows that are fully resolved are written directly without any API calls. Designed for the two-pass workflow below. |
| `--resolved-only` | Only write rows that are fully resolved — `link_method` must be one of `author_year_match`, `llm_abstract`, or `llm_fulltext` **and** `doi_o` must be non-empty. Rows that end up as `target_pending`, `api_error`, or `no_original_found` are silently skipped (not written to `extracted.csv`). Use this on the first pass to build a clean set of high-confidence resolved rows, then follow up with `--resume` (without `--resolved-only`) to fill in the rest. |
| `--from-year YYYY` | Only process rows from `filtered.csv` where `year_r >= YYYY`. Useful for targeting a specific publication-year window (e.g. 2011–2021 for the failure-weighted extraction pass). |
| `--to-year YYYY` | Only process rows from `filtered.csv` where `year_r <= YYYY`. Combine with `--from-year` for a closed year range. |
| `--predicted-outcome OUTCOME` | Pre-screen rows using keyword-only outcome prediction on title + abstract **before** any API calls. Choices: `failure \| success \| mixed \| descriptive \| uninformative \| other` (`other` = any predicted outcome except failure). No LLM is used for the pre-screen — it runs the same regex patterns as Pass 1 of outcome extraction. Rows the keywords cannot classify fall into `uninformative`, which `other` captures. Use `failure` to build a failure-weighted extraction run and `other` for the complementary pass. |

#### Examples

```bash
# Quick test — 5 rows, no PDF download
python -m extract.run_extract --limit 5 --no-pdf

# Full run skipping already-validated FLoRA DOIs
python -m extract.run_extract --skip-flora-validated

# Fast first pass — rules + abstract LLM only, write only what resolves cleanly
python -m extract.run_extract --resume --no-llm --no-pdf --resolved-only --no-multiple-originals --no-reproductions --skip-flora-validated

# Second pass — fill in remaining target_pending with full pipeline (PDF + fulltext LLM)
python -m extract.run_extract --resume

# Rule-based only, no API calls
python -m extract.run_extract --no-llm

# Inspect match-type classification for first 10 rows
python -m extract.run_extract --match-type-only --limit 10

# Targeted: only failures from 2011–2021
python -m extract.run_extract --resume --from-year 2011 --to-year 2021 --predicted-outcome failure --no-multiple-originals --no-reproductions --skip-flora-validated

# Targeted: non-failures from any year (to fill the 25% "other" pool)
python -m extract.run_extract --resume --predicted-outcome other --no-multiple-originals --no-reproductions --skip-flora-validated
```

#### Two-pass workflow (recommended for large runs)

```bash
# Pass 1 — fast, resolves ~60% without any PDF downloads
python -m extract.run_extract --no-pdf --no-multiple-originals --no-reproductions --skip-flora-validated

# Pass 2 — carry forward resolved rows, fill in target_pending with full pipeline
python -m extract.run_extract --resume
```

`--resume` can be combined with other flags on the second pass (e.g. `--resume --no-multiple-originals` to still defer multi-original rows).

#### Three-pass workflow (cleanest extracted.csv — write only what resolves)

Use this when you want `extracted.csv` to contain only high-confidence resolved rows, with unresolved rows added later rather than cluttering the file as `target_pending`.

```bash
# Pass 1 — rule-based only, no PDF, no LLM; write ONLY rows that resolve cleanly.
#           Reproductions and multiple-originals are silently skipped (not written).
#           target_pending / api_error rows are also silently skipped.
python -m extract.run_extract --resume --no-llm --no-pdf --resolved-only --no-multiple-originals --no-reproductions --skip-flora-validated

# Pass 2 — abstract LLM pass; still no PDF; still write only resolved rows.
#           --resume carries forward what Pass 1 wrote; re-runs everything else.
python -m extract.run_extract --resume --no-pdf --resolved-only --no-multiple-originals --no-reproductions --skip-flora-validated

# Pass 3 — full pipeline (PDF + fulltext LLM) for everything still unresolved.
#           --resolved-only is dropped so target_pending rows now appear in the file.
python -m extract.run_extract --resume
```

**What happens to skipped rows across passes:**

| Flag combination | Reproductions | Multiple-originals | Unresolved (target_pending) |
| --- | --- | --- | --- |
| `--no-reproductions --resolved-only` | Not written | — | Not written |
| `--no-multiple-originals --resolved-only` | — | Not written | Not written |
| `--resume` on final pass (no `--resolved-only`) | Written as `target_pending` if still unresolved | Written as `target_pending` if still deferred | Written as `target_pending` |

Rows not yet in `extracted.csv` are always processed on the next `--resume` run — nothing is permanently lost by deferring them.

#### Validation-mix workflow (75% failures + 25% other outcomes)

Use when you want the validator queue weighted toward failures but not exclusively so.

```bash
# Step 1 — extract failures from the target year range
python -m extract.run_extract --resume --from-year 2011 --to-year 2021 \
    --predicted-outcome failure --no-multiple-originals --no-reproductions --skip-flora-validated

# Step 2 — extract other outcomes from any year
python -m extract.run_extract --resume --predicted-outcome other \
    --no-multiple-originals --no-reproductions --skip-flora-validated

# Step 3 — build the mixed sample (writes data/validation_sample.csv)
python -m extract.mix_for_validation --failure-pct 75 --failure-year-from 2011 --failure-year-to 2021

# Cap at 400 rows total
python -m extract.mix_for_validation --failure-pct 75 --failure-year-from 2011 --failure-year-to 2021 --n 400

# Try a 70/30 split
python -m extract.mix_for_validation --failure-pct 70 --failure-year-from 2011 --failure-year-to 2021
```

`mix_for_validation` reads `extracted.csv`, samples `failure_pct`% from failure rows in the given year range and the remainder from any non-failure resolved row, shuffles the result, and writes `data/validation_sample.csv`. Only fully resolved rows (`author_year_match / llm_abstract / llm_fulltext`) are eligible — `target_pending` and `api_error` rows are excluded from the mix.

> **Note on `--predicted-outcome` accuracy:** The pre-screen uses keyword regex on title + abstract only — no LLM. It will mis-classify some rows (e.g. a paper that mentions "failed to replicate" in background but succeeded). The actual extracted `outcome` may differ from the prediction. The mix step uses the real extracted outcome, so the final `validation_sample.csv` ratio is accurate even if the extraction passes were approximate.

#### Why `target_pending` rows are hidden in the web app

The Extract tab always hides rows with `link_method = target_pending` — they are present in `extracted.csv` but not shown to reviewers because there is nothing actionable to validate yet. To inspect them, filter the CSV directly:

```bash
python -c "
import pandas as pd
df = pd.read_csv('data/extracted.csv', dtype=str, encoding='utf-8-sig').fillna('')
pending = df[df['link_method'] == 'target_pending']
print(pending[['doi_r', 'title_r', 'filter_status', 'original_match_type']].to_string())
"
```

> **`--no-llm` vs. `--match-type-only`:**
> `--no-llm` still runs the full pipeline (linking + outcome) for every row — it just skips every LLM call. Rules and Jaccard-based heuristics resolve what they can; the rest gets `target_pending`.
> `--match-type-only` is a separate run mode that only outputs a classification CSV — it does not touch `extracted.csv`.

### Quick re-run a single DOI (from the web app)

Select a row in the Extract tab → choose a model → click "Run selected with model".

---

## What This Stage Does

For every confirmed replication or reproduction in `filtered.csv`, this stage resolves two questions:

1. **Which original study does this paper replicate?** (linking)
2. **What was the outcome?** (success / failure / mixed / uninformative)

Stage 3 determines `original_match_type` as its first step (see Classification below), then routes each paper through one of two pipelines. Both pipelines start with an early LLM resolution step designed to resolve ~60% of papers before any PDF is needed.

False positives (`filter_status = false_positive`) are **passed through** to `extracted.csv` with all extraction columns left empty and `link_method = target_pending`. This keeps the full candidate pool visible in Stage 4.

---

## Match-Type Classification (First Step)

Before any extraction, `run_extract.py` classifies each confirmed paper's `original_match_type`. This uses the abstract + OpenAlex referenced works:

1. Extract author-year citation patterns from `abstract_r` using `extract_author_year_patterns()`
2. Fetch referenced works from OpenAlex, match against patterns
3. Call LLM with: title, abstract, matched candidates, count of distinct patterns

Returns `original_match_type` and `original_match_confidence`. Cached as `cache_key(doi_r + "_match_type")`.

| Value               | Meaning                                                               |
| ------------------- | --------------------------------------------------------------------- |
| `single_original`   | Paper clearly targets one specific original study                     |
| `multiple_match`    | 2–5 OpenAlex candidates with same author/year — disambiguation needed |
| `multiple_original` | Paper genuinely targets several distinct independent originals        |

If the OpenAlex lookup fails, default to `single_original`.

---

## Routing by `original_match_type`

```text
data/filtered.csv
         │
         ▼
  filter_status == false_positive? → pass through (empty extraction cols, target_pending)
         │
         ▼
  Match-type classification (Step 1, for replication/reproduction rows)
         │
         ├── single_original ──┐
         │                     ├─→ Shared Pipeline (A/B)
         └── multiple_match ───┘

         └── multiple_original ──→ Multi-Original Pipeline (C)
```

`run_extract.py` reads `filtered.csv`, routes each row, and writes `extracted.csv`. For `multiple_original` papers, the output expands to N rows (one per original), distinguished by `original_rank`.

---

## Shared Pipeline — Single Original & Multiple Match (Pipeline A/B)

Used for both `single_original` and `multiple_match`. The only difference is that `multiple_match` starts with 2–5 pre-fetched OpenAlex candidates; `single_original` may have 0–1.

### Step 1 — LLM Abstract + Reference Matching (Early Exit)

**This is the first step, not the last.** It targets ~60% early resolution, avoiding the expensive PDF stages for most papers.

Input:

- `title_r`, `abstract_r` from `filtered.csv`
- Referenced works fetched from OpenAlex (via `shared/openalex_client.py`)

Process:

1. Extract author-year citation patterns from the abstract using `extract_author_year_patterns()`
2. Fetch referenced works via OpenAlex API and match against patterns
3. Call LLM with: title, abstract, candidate list

If the model returns `confidence = high` → resolve immediately. Set `link_method = llm_abstract`, skip Steps 2–6.

If `confidence = medium` or `low`, or if no candidates were found → continue to Step 2.

Cached to `cache/llm/` using `cache_key(doi_r + "_abstract_link")`.

### Step 2 — OpenAlex Author-Year Re-query

For papers not resolved in Step 1.

1. Extract author-year citation patterns from title → abstract (8 regex patterns, Unicode surname support, name prefixes)
2. Fetch the paper's full referenced works from OpenAlex, batching 50 IDs per request
3. Match patterns against references: year tolerance ±1, fuzzy surname prefix matching (3+ chars)

Produces a candidate list: `{openalex_id, doi, title, year, all_authors, cited_pattern}`. Cached per `doi_r`.

### Step 3 — Same-Author/Year Disambiguation (Fast Heuristic)

If exactly one non-umbrella candidate remains after Step 2, resolve immediately. No LLM or PDF needed.

Umbrella paper guard: titles matching EEGManyLabs, ManyLabs, PSA, StudySwap, or similar framework patterns are excluded from fast-path resolution and continue to Step 4.

If disambiguation resolves: set `link_method = author_year_match`, skip Steps 4–6.

### Step 4 — PDF Acquisition

If still unresolved. 11-tier waterfall — each tier is skipped once a PDF is downloaded. PDFs saved to `cache/pdf/<cache_key(doi_r)>.pdf`.

| Tier | Source                                                               |
| ---- | -------------------------------------------------------------------- |
| 1    | arXiv (DOI pattern `10.48550/arXiv.*`)                               |
| 2    | OSF (DOI pattern `10.3123*/osf.io/*`)                                |
| 3    | OpenAlex OA URL                                                      |
| 4    | Unpaywall direct PDFs (all `url_for_pdf` locations)                  |
| 5    | Semantic Scholar Graph API                                           |
| 6    | CORE.ac.uk                                                           |
| 7    | Europe PMC                                                           |
| 8    | Unpaywall landing page scraper (DSpace, HAL, Pure repos)             |
| 9    | SerpAPI / Google Scholar (multi-key rotation on 429)                 |
| 10   | Playwright headless Chromium (publisher-specific CSS selectors)      |
| 11   | HTML text extraction (up to 50 000 chars as full-text substitute)    |

### Step 5 — Reference Extraction

Uses **pdfminer.six** locally — no GROBID server required. Extracts four sections:

| Section      | Content                                          |
| ------------ | ------------------------------------------------ |
| `abstract`   | Paper abstract                                   |
| `intro`      | Introduction text                                |
| `methods`    | Methods section text                             |
| `references` | Parsed `{authors, year, title, raw_ref}` structs |

**Fallback 1 — Direct PDF to LLM** (`success_direct_llm`): if pdfminer extracts 0 references, the full PDF is sent to the LLM as inline `application/pdf` data. Efficient for native-text PDFs.

**Fallback 2 — PyMuPDF image rendering** (`success_image_llm`): if direct-PDF also returns nothing, the last ~20% of pages (max 6) are rendered as 1.5× grayscale PNGs and sent to the LLM. Used for scanned PDFs.

GROBID fast-path: after extraction, if one candidate matches the reference list exactly by DOI or author+year (Jaccard similarity), resolve immediately. Set `link_method = author_year_match`.

### Step 6 — Full LLM Identification

Builds a structured prompt:

- Replication title + abstract
- Numbered candidate list with full author lists and OpenAlex IDs
- PDF intro (1 000 chars), methods (700 chars), up to 80 reference entries
- If PDF failed but URL available: URL passed for LLM URL grounding
- If HTML text extracted: used as intro substitute

Returns: `doi_o`, `title_o`, `link_evidence`, `link_confidence`. Sets `link_method = llm_fulltext`.

If the LLM fails after retries: `link_method = api_error`, `link_confidence = low`.

### Step 7 — Outcome Extraction

Run after linking, regardless of which step resolved the link.

Implemented in `extract/code_outcome.py`.

**Pass 1 — Keyword matching:**

- Scans abstract and title for outcome phrases
- `success`: `"replicated"`, `"consistent with"`, `"confirmed"`, `"effect was reproduced"`
- `failure`: `"failed to replicate"`, `"no evidence"`, `"could not replicate"`, `"null result"`
- `mixed`: `"partial replication"`, `"mixed results"`, `"some but not all"`
- `uninformative`: no clear outcome phrase found

**Pass 2 — LLM outcome extraction** (for `uninformative` or low-confidence keyword matches):

- Sends title + abstract (+ fulltext intro if available) to LLM
- Returns `outcome`, `outcome_phrase` (supporting quote), `outcome_confidence`, `out_quote_source`
- Cached to `cache/llm/` using `cache_key(doi_r + "_outcome")`

---

## Multi-Original Pipeline (Pipeline C)

Used when `original_match_type = multiple_original`. The paper genuinely replicates several independent original studies and must produce N rows in `extracted.csv`.

### Step 1 — LLM Abstract + Reference Matching

Same as Shared Pipeline Step 1, but with a multi-original prompt. If the model identifies all originals with `confidence = high` → resolve all immediately. Set `link_method = llm_abstract` for each row.

If not resolved → continue to Step 2.

### Step 2 — PDF Acquisition

Same 11-tier waterfall as Shared Pipeline Step 4. No same-author/year disambiguation (not applicable for multi-original papers).

### Step 3 — Reference Extraction

Same pdfminer / LLM fallback process as Shared Pipeline Step 5.

### Step 4 — Multi-Original LLM

Different prompt from the single-original case. Asks the model to:

- Determine if the paper is truly multi-original or a false positive (only 1 original)
- List **all** original studies being replicated with evidence and confidence for each

Returns:

- `is_false_positive` — if true, treat as `single_original` and pass back through Shared Pipeline
- `originals[]` — one entry per original: `rank`, `doi`, `title`, `year`, `evidence`, `confidence`

Results expanded to N rows in `extracted.csv`. `original_rank` distinguishes each row (1, 2, 3...). `n_originals` is set to the total count on all rows.

Cached as `multi_<hash>.json`.

### Step 5 — Outcome Extraction

Same keyword + LLM process as Shared Pipeline Step 7. Run once per original (each row gets its own `outcome`).

---

## Output Schema — `extracted.csv`

`pair_id` is the leading column, followed by all columns from `filtered.csv`, then:

| Column                      | Type | Description                                                                   |
| --------------------------- | ---- | ----------------------------------------------------------------------------- |
| `pair_id`                   | str  | MD5 of `doi_r` + `doi_o` -- uniquely identifies a replication-original pair  |
| `original_match_type`       | str  | single_original / multiple_match / multiple_original                          |
| `original_match_confidence` | str  | high / medium / low                                                           |
| `doi_o`                     | str  | Cleaned DOI of the original study                                             |
| `title_o`                   | str  | Original study title                                                          |
| `year_o`                    | int  | Original study publication year                                               |
| `authors_o`                 | str  | Original study authors                                                        |
| `ref_o`                     | str  | FLoRA display reference: `"Surname / Year / Journal"` (fetched from OpenAlex)|
| `link_method`               | str  | author_year_match / llm_abstract / llm_fulltext / target_pending / api_error  |
| `link_evidence`             | str  | Quote or pattern used for linking                                             |
| `link_confidence`           | str  | high / medium / low                                                           |
| `link_llm_model`            | str  | Model used for DOI resolution; empty for rule-based links                     |
| `outcome`                   | str  | success / failure / mixed / uninformative / descriptive / pending / api_error |
| `outcome_phrase`            | str  | Supporting quote from the paper                                               |
| `outcome_confidence`        | str  | high / medium / low                                                           |
| `out_quote_source`          | str  | abstract / fulltext / title                                                   |
| `type`                      | str  | replication / reproduction                                                    |
| `original_rank`             | int  | 1 for single; 1, 2, 3... for multi-original                                   |
| `n_originals`               | int  | Total originals in this paper (1 for single)                                  |

---

## `link_method` Values

| Value               | When set                       | Meaning                                                          |
| ------------------- | ------------------------------ | ---------------------------------------------------------------- |
| `llm_abstract`      | Step 1                         | Resolved by LLM using abstract + OpenAlex references             |
| `author_year_match` | Step 3 or Step 5 (GROBID path) | Resolved by same-author/year heuristic or GROBID reference match |
| `llm_fulltext`      | Step 6                         | Resolved by LLM using full PDF context                           |
| `no_original_found` | Step 1 or Step 6               | LLM ran but found no identifiable original; not an API failure   |
| `target_pending`    | Step 6 — no result             | LLM returned no usable result; re-processed on `--resume`        |
| `api_error`         | Step 6 — all retries failed    | API failed after 3 retries with exponential backoff              |

---

## Files

| File                         | Status        | Description                                                                  |
| ---------------------------- | ------------- | ---------------------------------------------------------------------------- |
| `extract/run_extract.py`     | Implemented   | Orchestrator — match-type classify, route, write CSV; CLI flags              |
| `extract/link_original.py`   | Implemented   | Shared Pipeline (A/B) — 7-step single/multiple-match; title-pattern stage    |
| `extract/multi_original.py`  | Ported        | Multi-Original Pipeline (C) — detection logic may need further tuning        |
| `extract/code_outcome.py`    | Implemented   | Keyword + LLM outcome extraction; `no_llm` flag supported                    |
| `shared/pdf_parsing.py`      | Implemented   | Five-method PDF parse comparison (`parse_all`); uniform result shape         |
| `shared/pdf_sources.py`      | Implemented   | OpenAlex GROBID XML as Tier 0 PDF source; 11-tier waterfall                  |
| `shared/llm_client.py`       | Implemented   | Gemini → OpenAI → OpenRouter fallback chain; `llm_response` stored in cache  |

---

## Recent Improvements (2026-05-05)

### A — CLI testing flags (`run_extract.py`)

`--no-llm`, `--match-type-only`, `--outcome-only`, `--limit N` added to `run_extract.py`. See "Running Stage 3" above.
The `no_llm` flag is threaded as a function parameter — never a global — through `classify_match_type`, `run_for_doi`, and `extract_outcome`.

### B — Title-pattern disambiguation (`link_original.py`)

Nine compiled regex patterns (e.g. `"replication of X"`, `"replicating X"`, `"reproduction of X"`) extract the name of the target study from the replication paper's own title. Results are Jaccard-scored against OpenAlex candidates:

- Single strong match (score ≥ 0.4 AND 1.5× gap over second) → resolved immediately as `author_year_match`, no LLM needed
- Multiple plausible matches → a `TITLE PATTERN HINT` is injected into the LLM prompt

This fires as Stage 2.5 in the pipeline, after author-year heuristics and before the abstract LLM call.

### C — OpenAlex GROBID XML (Tier 0) + LLM chain order

**Tier 0 PDF acquisition:** `get_openalex_fulltext(openalex_id)` checks `has_content.grobid_xml` on the OpenAlex metadata API. If true, downloads pre-parsed TEI XML from `content.openalex.org/works/W{id}.grobid-xml`. This is faster and more reliable than any PDF download — uses the same cached GROBID parse that OpenAlex already produces. Cached in `cache/openalex_xml/`.

**LLM chain reorder:** All LLM calls now use Gemini → OpenAI → OpenRouter (Qwen) order. Previously OpenRouter was first. Raw JSON responses are stored in LLM cache files under `"llm_response"` for UI display.

### D — PDF parse comparison (`shared/pdf_parsing.py`)

`parse_all(doi_r, pdf_path, oa_xml=None)` runs five methods and returns a uniform dict keyed by method name:

| Method | Source |
| --- | --- |
| `openalex_xml` | OpenAlex pre-parsed TEI (Tier 0) |
| `pdfminer` | Local pdfminer.six text extraction |
| `grobid` | GROBID pipeline (pdfminer + LLM fallbacks) |
| `docpluck` | Docpluck library (if installed) |
| `docling` | Docling library (if installed) |

Each result has shape `{source, title, abstract, intro, references: list, raw_text, error: str|None}`. Results cached to `cache/parse/parse_{key}.json` after each DOI is processed.

### E — Extract tab UI (2026-05-05)

- **PDF button**: "↓ PDF" link in the expanded detail panel when a cached PDF exists. Served via `GET /api/pdf/<doi>`.
- **Parse comparison block**: table in the detail panel showing all five methods side by side (abstract, intro, ref count). Only visible after a row has been processed.
- **LLM two-panel I/O**: when `llm_response` / `outcome_llm_response` are cached, the LLM tabs split into Prompt | Response columns instead of showing only the prompt.

---

## Recent Improvements (2026-05-06)

### F — parse_all integration into Stage 6 (`link_original.py`)

Stage 6 (previously "GROBID") now runs **all five parsers in parallel** and picks the richest result to send to the LLM, rather than calling GROBID directly:

```text
openalex_xml  → parse_openalex_xml()
pdfminer      → parse_pdfminer()
grobid        → parse_grobid()   (pdfminer + server + LLM fallbacks)
docpluck      → parse_docpluck()
docling       → parse_docling()
```

`_best_parse_result()` scores each result by `len(references) × 500 + len(abstract) + len(intro)`. References are weighted 500× because they are the most useful input for the LLM linking step. The highest-scoring non-errored result wins; if all methods fail, the GROBID result is used as the fallback.

The winner is logged at INFO level (`parse_all best=<method> refs=N abstract=N intro=N`). All five methods are logged at DEBUG level so scores are visible for diagnostics.

`no_llm=True` is threaded into `parse_grobid` → `run_grobid`, so Gemini fallback tiers inside GROBID are also skipped when the flag is set.

The `grobid_status` column in `extracted.csv` now reads `"parse_all:<winning_method>"` (e.g. `"parse_all:pdfminer"`) to show which parser produced the context sent to the LLM.

### G — Stage 2 streaming pipeline (`filter/run_filter.py`)

`run_filter.py` was rewritten with the same row-by-row streaming pattern as Stage 3:

- Each candidate row is classified (rule → LLM if `needs_review`) and **immediately appended** to `filtered.csv`.
- On startup, reads any existing `filtered.csv` to build a set of already-processed DOIs — these are skipped without reprocessing, making interrupted runs safely resumable.
- Public per-row APIs added: `classify_row(row: dict)` in `rule_filter.py`; `classify_with_llm(title, abstract)` in `llm_filter.py`.

### H — False positives excluded from `extracted.csv`

`filter_status = false_positive` rows are now **completely skipped** in Stage 3 — they are not written to `extracted.csv` at all. Previously they were passed through as empty extraction rows. This keeps `extracted.csv` clean (only genuine replications and reproductions) and reduces noise in Stage 4 validation.

---

## Recent Improvements (2026-05-11)

### I — `--no-pdf` now exits at Stage 5 instead of continuing with empty PDF context

Previously, `--no-pdf` only skipped the `acquire_pdf()` call and set a placeholder PDF dict; Stages 6 and 7 (PDF parsing and fulltext LLM) still ran against empty inputs. Now, when `no_pdf=True`, the pipeline exits immediately at Stage 5 if Stages 2.5, 3, and 4 (title-pattern, rule-based, abstract LLM) did not resolve the paper. The row is written as `link_method = target_pending` (`resolution_method = "needs_fulltext"` internally) without any parse or LLM calls. This makes `--no-pdf` runs substantially faster and avoids wasted LLM calls with no useful input.

`_save_parse_cache()` in `run_extract.py` is also skipped when `no_pdf=True`, since there is no PDF to parse and no parse results to save.

### J — `--resume` flag for two-pass extraction

`run_extract()` accepts a new `resume=True` parameter (exposed as `--resume` on the CLI).

Behaviour:

1. On startup, reads the existing `extracted.csv` and partitions rows by DOI:
   - **Resolved** — all rows for this DOI have `link_method != "target_pending"` → carried forward unchanged.
   - **Pending** — at least one row has `link_method == "target_pending"` → re-processed through the full pipeline.
2. In the main loop, resolved DOIs are written directly to the output without any API or LLM calls.
3. Pending DOIs and DOIs not yet in `extracted.csv` are processed normally.

This enables the recommended two-pass workflow: a fast `--no-pdf` first pass followed by a `--resume` second pass that runs the full pipeline only for unresolved rows.

---

## Rules

- False positives are excluded from `extracted.csv` entirely — do not pass them through
- `run_extract.py` must classify `original_match_type` before routing — it is not in `filtered.csv`
- LLM abstract + reference matching must be Step 1 (the first step), not a fallback
- All LLM responses must be cached before writing to disk
- Rate limits: OpenAlex 0.1s (`OPENALEX_RATE_SEC`), LLM 1s (`LLM_RATE_SEC`)
- Retry LLM calls up to 3 times with exponential backoff; set `api_error` after 3 failures
- `multiple_original` papers expand to N rows in extracted.csv — `original_rank` must be set
- If `is_false_positive` comes back from Multi-Original LLM, re-route through Shared Pipeline
- All DOIs written to `doi_o` must pass through `clean_doi()`

---

## Testing

```bash
python -c "
import pandas as pd
from shared.schema import EXTRACTED_COLS
df = pd.read_csv('misc/sample_extracted.csv')
missing = [c for c in EXTRACTED_COLS if c not in df.columns]
assert not missing, f'Missing columns: {missing}'
print('Schema OK —', len(df), 'rows')
"
```
