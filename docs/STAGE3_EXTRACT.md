
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
| `--extracted-test` | Write output to `data/extracted-test.csv` instead of `extracted.csv`. Skips DOIs already resolved in `extracted.csv`; re-runs `target_pending` rows and rows absent from `extracted.csv`. Use to safely test new pipeline options (multiple-originals, reproductions) before committing results to production. Combine with `--resume` to continue an interrupted test run. See **Test Sandbox** section below. |
| `--source SOURCE` | Only process rows from this source (case-insensitive). Values: `openalex`, `bob_reed`, `i4r`, `semantic_scholar`. Matches the `source` column in `filtered.csv`. Useful for targeted re-runs when only one source has new papers. |

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

#### Why `target_pending` rows are hidden on the Extract tab

The **Extract** tab hides rows with `link_method = target_pending` — they are present in `extracted.csv` but not shown to reviewers because there is nothing actionable to validate yet.

The **Extract Test** tab shows `target_pending` rows so you can monitor which DOIs are still unresolved during a test run. To inspect them from the CSV directly:

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

False positives (`filter_status = false_positive`) are **skipped entirely** — they are not written to `extracted.csv` and do not appear in Stage 4.

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

### Step 5 — Multi-Method PDF Parsing + Best Result Selection

`parse_all()` from `shared/pdf_parsing.py` runs **six parsers in parallel** on the downloaded PDF and returns a dict keyed by method name. Each result has the same uniform shape: `{abstract, intro, references, raw_text, error}`.

| Parser | What it does |
| --- | --- |
| `openalex_xml` | OpenAlex GROBID XML — structured sections when OpenAlex has it |
| `pdfminer` | Raw text extraction via pdfminer.six; section splitting via heuristics |
| `grobid` | GROBID server (optional); structured references + section splitting |
| `docpluck` | Lightweight structured extraction (optional dependency) |
| `opendataloader` | Java-based PDF-to-Markdown converter (requires JVM) |
| `markitdown` | Microsoft MarkItDown — PDF to clean Markdown; good prose quality |

**Scoring and winner selection:** each result is scored:

```text
score = refs × 300  +  abstract_len  +  intro_len × 2  +  min(raw_text_len ÷ 5, 1000)
```

The highest-scoring result is selected as `best`. Its `abstract`, `intro`, and `references` are used in Step 6. All results are cached to `cache/parse/parse_{key}.json`. MarkItDown's raw `.md` output is additionally cached to `cache/markdown/{key}.md`.

The **web app detail panel** shows all six methods side-by-side with a **★ USED BY LLM** badge on the winning column and each method's score.

**Fallbacks for PDF acquisition failure:** if no PDF was downloaded, `parse_all()` still runs — most parsers return an error result and the scoring falls back to the OpenAlex XML or abstract text. The pipeline does not crash.

GROBID fast-path (rule-based before Step 6): after parsing, if one candidate matches the reference list exactly by DOI or author+year (Jaccard similarity), resolve immediately. Set `link_method = author_year_match`.

### Step 6 — Full LLM Identification

Builds a structured prompt using the **best parse result** selected in Step 5:

- Replication title + abstract
- Numbered candidate list with full author lists and OpenAlex IDs
- Best parser's intro text and up to 80 reference entries
- If PDF failed but URL available: URL passed for LLM URL grounding
- If HTML text extracted: used as intro substitute

The winner's text is chosen by the same scoring formula as Step 5 — so if MarkItDown produced richer prose than GROBID for this paper, the LLM sees MarkItDown's intro. The structured reference list also comes from the winner (if the winner has sparse references, the LLM prompt's reference section will be thin — acceptable since citation matching already ran in Step 5's fast-path).

Returns: `doi_o`, `title_o`, `link_evidence`, `link_confidence`. Sets `link_method = llm_fulltext`.

If the LLM fails after retries: `link_method = api_error`, `link_confidence = low`.

### Step 7 — Outcome Extraction

Run after linking, regardless of which step resolved the link.

Implemented in `extract/code_outcome.py`.

**Pass 1 — Keyword matching:**

- Scans title → abstract → fulltext (first high-confidence hit wins)
- `success`: `"replicated"`, `"consistent with"`, `"confirmed"`, `"effect was reproduced"`
- `failure`: `"failed to replicate"`, `"no evidence"`, `"could not replicate"`, `"null result"`
- `mixed`: `"partial replication"`, `"mixed results"`, `"some but not all"`
- `descriptive`: `"adapted the method"`, `"in a different context"`
- `uninformative`: no clear outcome phrase found

When a keyword fires, `outcome_phrase` is set to the **matched sentence plus one sentence of context on each side** (via `_expand_to_sentences()`). This produces a meaningful quote rather than a bare keyword fragment. Abbreviations (`et al.`, `e.g.`, initials) are protected from being treated as sentence boundaries.

**Pass 2 — LLM outcome extraction** (no high-confidence keyword match):

- Sends title + abstract + fulltext to the LLM. The fulltext comes from `_best_fulltext_from_cache()`: reads `cache/parse/parse_{key}.json`, scores all parse methods with the same formula as Step 5, and uses the winner's `abstract + intro`. Falls back to the GROBID sections from the link step if no parse cache exists yet.
- When the original study is known from linking (`resolved_title_o` / `resolved_author_o` / `resolved_year_o`), prepends a citation block: `"This paper replicates: {authors} ({year}). {title}"` so the LLM can reason about whether *that specific effect* was confirmed
- Returns `outcome`, `outcome_phrase` (verbatim 2–3 sentence quote from the abstract), `outcome_confidence`, `out_quote_source`, and `outcome_reasoning` (one sentence explaining the classification choice)
- `outcome_reasoning` is empty for keyword-matched rows (Pass 1)
- Cached to `cache/llm/outcome_{cache_key(doi_r)}.json`; old cache entries without `outcome_reasoning` return `""` for that field — no cache invalidation needed

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
| `outcome_phrase`            | str  | Verbatim quote (multi-sentence) from the paper supporting the classification  |
| `outcome_confidence`        | str  | high / medium / low                                                           |
| `out_quote_source`          | str  | abstract / fulltext / title                                                   |
| `outcome_reasoning`         | str  | One-sentence LLM note explaining the classification; empty for keyword rows   |
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
| `extract/run_extract.py`     | Implemented   | Orchestrator — match-type classify, route, write CSV; CLI flags including `--extracted-test`; `_best_fulltext_from_cache()` for dynamic outcome text |
| `extract/link_original.py`   | Implemented   | Shared Pipeline (A/B) — 7-step single/multiple-match; runs `parse_all()`, scores all methods, uses winner for LLM |
| `extract/multi_original.py`  | Ported        | Multi-Original Pipeline (C) — detection logic may need further tuning        |
| `extract/code_outcome.py`    | Implemented   | Keyword + LLM outcome extraction; `no_llm` flag supported                    |
| `extract/promote_test.py`    | Implemented   | Merge rows from `extracted-test.csv` into `extracted.csv`; `--all`, `--doi`, `--dry-run`, `--force` |
| `shared/pdf_parsing.py`      | Implemented   | **Six**-method PDF parse comparison (`parse_all`); uniform result shape; `score_parse_result()`, `best_parse_result()`, `best_parse_method_name()` |
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

`parse_all(doi_r, pdf_path, oa_xml=None)` runs **six** methods and returns a uniform dict keyed by method name:

| Method | Source |
| --- | --- |
| `openalex_xml` | OpenAlex pre-parsed TEI (Tier 0) |
| `pdfminer` | Local pdfminer.six text extraction |
| `grobid` | GROBID pipeline (pdfminer + LLM fallbacks) |
| `docpluck` | Docpluck library (if installed) |
| `opendataloader` | Java-based PDF-to-Markdown (if JVM available) |
| `markitdown` | Microsoft MarkItDown — PDF to clean Markdown (June 2026) |

Each result has shape `{source, title, abstract, intro, references: list, raw_text, error: str|None}`. Results cached to `cache/parse/parse_{key}.json`. MarkItDown's raw `.md` output is additionally cached to `cache/markdown/{key}.md`.

### E — Extract tab UI (2026-05-05)

- **PDF button**: "↓ PDF" link in the expanded detail panel when a cached PDF exists. Served via `GET /api/pdf/<doi>`.
- **Parse comparison block**: table in the detail panel showing all five methods side by side (abstract, intro, ref count). Only visible after a row has been processed.
- **LLM two-panel I/O**: when `llm_response` / `outcome_llm_response` are cached, the LLM tabs split into Prompt | Response columns instead of showing only the prompt.

---

## Recent Improvements (2026-05-06)

### F — parse_all integration into Stage 6 (`link_original.py`)

Stage 6 (previously "GROBID") now runs **all parsers in parallel** via `parse_all()` and picks the richest result to send to the LLM, rather than calling GROBID directly. The scoring formula (unified with the UI badge — see June 2026 improvements below):

```text
score = refs × 300  +  abstract_len  +  intro_len × 2  +  min(raw_text_len ÷ 5, 1000)
```

`best_parse_result()` from `shared/pdf_parsing.py` is used — the same function as the UI winner badge — so the log, the badge, and the actual LLM input always agree. The highest-scoring non-errored result wins; if all methods fail, the GROBID result is used as the fallback.

The winner is logged at INFO level (`parse_all best=<method> refs=N abstract=N intro=N`). All methods are logged at DEBUG level so scores are visible for diagnostics.

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

## Recent Improvements (2026-05-27)

### K — Outcome extraction quality (`code_outcome.py`)

Two problems in the previous outcome extraction were identified and fixed:

#### 1. Quote quality (Pass 1 — keyword scan)

Previously, `outcome_phrase` was set to the bare regex match (e.g. the literal text `"failed to replicate"`). Reviewers in Stage 4 saw a bare keyword fragment instead of a meaningful quote.

Fix: `_expand_to_sentences(text, match_start, match_end, n_context=1)` was added. It splits the abstract on sentence boundaries — protecting common abbreviations (`et al.`, `e.g.`, `i.e.`, `vs.`, initials) from being treated as sentence ends — and returns the matched sentence plus one surrounding sentence on each side. `_keyword_scan()` now calls this instead of returning `m.group(0)`.

#### 2. Classification accuracy (Pass 2 — LLM)

Previously the LLM prompt contained no information about which original study was being replicated. This degraded accuracy for `mixed` and `uninformative` cases where the LLM needed to know which specific effect to look for in the abstract.

Fix: `_llm_outcome()` now accepts `original_title`, `original_authors`, `original_year` and — when `original_title` is non-empty — prepends:

```text
This paper replicates: {authors} ({year}). {title}
```

before the abstract. The fulltext excerpt was also removed from the prompt (it added noise without improving accuracy). The quote instruction was updated to ask for 2–3 verbatim sentences from the abstract.

#### New `outcome_reasoning` field

The LLM response now includes `"outcome_reasoning"`: a one-sentence note explaining the classification choice (e.g. `"Partial: valence effect replicated but arousal effect did not reach significance."`). This field is empty for keyword-matched rows (Pass 1) and for rows where the LLM call failed. Old cache entries without it return `""` — no cache invalidation needed.

#### Caller wiring (`run_extract.py`)

`_get_outcome()` now passes `resolved_title_o`, `resolved_author_o`, `resolved_year_o` from the `link` dict through to `extract_outcome()`. For multi-original rows where the multi-original LLM returned originals, outcome comes inline from `orig.get("outcome_evidence")` — these rows are unchanged.

#### Schema change (`shared/schema.py`)

`outcome_reasoning` was added to `EXTRACT_ADDED_COLS` after `out_quote_source`. Old rows in `extracted.csv` get an empty string for this column on re-read.

---

## Recent Improvements (2026-06-01)

### L — MarkItDown as 6th parse method (`shared/pdf_parsing.py`)

`parse_markitdown(pdf_path, doi_r)` converts a PDF to clean Markdown using [Microsoft MarkItDown](https://github.com/microsoft/markitdown). The raw `.md` output is cached separately at `cache/markdown/{key}.md` (human-readable). The function extracts `abstract`, `intro`, and a basic reference list using a line-by-line scanner that handles multiple academic heading styles: `# Abstract`, `**Abstract**`, `ABSTRACT` (ALL CAPS), `1. Introduction` (numbered).

`PARSE_METHODS` now includes `"markitdown"` as the 6th entry.

### M — Unified scoring formula + shared API

`score_parse_result(r)`, `best_parse_result(results)`, and `best_parse_method_name(results)` are now exported from `shared/pdf_parsing.py`. Both `link_original.py` and `_get_outcome` use `best_parse_result()` — previously `link_original.py` had its own local `_best_parse_result` with a different formula (`refs × 500`). The unified formula:

```text
score = refs × 300  +  abstract_len  +  intro_len × 2  +  min(raw_text_len ÷ 5, 1000)
```

This ensures the parse winner shown in the UI badge, used by DOI resolution, and used by outcome extraction are always the same method.

### N — Dynamic best-parser text for outcome extraction

`_get_outcome` in `run_extract.py` now calls `_best_fulltext_from_cache(doi_r)` before falling back to GROBID text. This reads `cache/parse/parse_{key}.json`, scores all methods, and uses the winner's `abstract + intro` as the LLM fulltext input. Since `link_original.py` writes the parse cache before `_get_outcome` runs, the cache is always available for newly processed rows.

### O — Extract Test sandbox

New workflow for safely testing pipeline options (multiple-originals, reproductions) before promoting results to production:

```bash
# Run with --extracted-test to write to extracted-test.csv
python -m extract.run_extract --extracted-test [--resume] [other flags]

# Promote to production when satisfied
python -m extract.promote_test --all           # promote everything
python -m extract.promote_test --doi 10.xxx/y  # promote one row
python -m extract.promote_test --all --dry-run # preview only
```

**Skip logic:** `--extracted-test` skips DOIs already resolved in `extracted.csv` (won't overwrite production data). It re-runs DOIs that are `target_pending` in `extracted.csv` and processes rows absent from `extracted.csv` entirely.

**Web app:** `/extract-test` tab mirrors the Extract tab but reads `extracted-test.csv`. Unlike the Extract tab, it shows `target_pending` rows so you can monitor unresolved DOIs during a test run. Each row has a **Promote →** button; a **Promote Selected** bulk action is available in the action bar.

**Parse winner badge:** The detail panel parse comparison table shows a **★ USED BY LLM** badge on the winning column plus each method's score. If a row's parse cache is missing the `markitdown` key (written before MarkItDown was added), the detail panel runs it lazily on first open and updates the cache.

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
