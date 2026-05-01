# Stage 3 — Extract
**Input:** `data/filtered.csv`  
**Output:** `data/extracted.csv`  
**Run:** `python extract/run_extract.py`

---

## What This Stage Does

For every confirmed replication or reproduction in `filtered.csv`, this stage resolves two questions:

1. **Which original study does this paper replicate?** (linking)
2. **What was the outcome?** (success / failure / mixed / uninformative)

The stage routes each paper through one of two pipelines based on the `original_match_type` column set by Stage 2. Both pipelines start with an early LLM resolution step designed to resolve ~60% of papers before any PDF is needed.

False positives (`filter_status = false_positive`) are passed through unchanged — no extraction is run.

---

## Routing by `original_match_type`

```
data/filtered.csv
         │
         ├── filter_status == false_positive → pass through, no extraction
         │
         ├── original_match_type == single_original ──┐
         │                                            ├─→ Shared Pipeline (A/B)
         └── original_match_type == multiple_match ───┘
                                                       
         └── original_match_type == multiple_original ──→ Multi-Original Pipeline (C)
```

`run_extract.py` reads `filtered.csv`, routes each row, and writes `extracted.csv`. For `multiple_original` papers, the output expands to N rows (one per original), distinguished by `original_rank`.

---

## Shared Pipeline — Single Original & Multiple Match (Pipeline A/B)

Used for both `single_original` and `multiple_match`. The only difference is that `multiple_match` starts with 2–5 pre-fetched OpenAlex candidates; `single_original` may have 0–1.

### Stage 1 — LLM Abstract + Reference Matching (Early Exit)

**This is the first step, not the last.** It targets ~60% early resolution, avoiding the expensive PDF stages for most papers.

Input:
- `title_r`, `abstract_r` from `filtered.csv`
- Referenced works fetched from OpenAlex (via `shared/openalex_client.py`)

Process:
1. Extract author-year citation patterns from the abstract using `extract_author_year_patterns()`
2. Fetch referenced works via OpenAlex API and match against patterns
3. Call `gemini-3-flash-preview` with: title, abstract, candidate list

If the model returns `confidence = high` → resolve immediately. Set `link_method = llm_abstract`, skip Stages 2–6.

If `confidence = medium` or `low`, or if no candidates were found → continue to Stage 2.

Cached to `cache/llm/` using `cache_key(doi_r + "_abstract_link")`.

### Stage 2 — OpenAlex Author-Year Re-query

For papers not resolved in Stage 1.

1. Extract author-year citation patterns from title → abstract (8 regex patterns, Unicode surname support, name prefixes)
2. Fetch the paper's full referenced works from OpenAlex, batching 50 IDs per request
3. Match patterns against references: year tolerance ±1, fuzzy surname prefix matching (3+ chars)

Produces a candidate list: `{openalex_id, doi, title, year, all_authors, cited_pattern}`. Cached per `doi_r`.

### Stage 3 — Same-Author/Year Disambiguation (Fast Heuristic)

If exactly one non-umbrella candidate remains after Stage 2, resolve immediately. No LLM or PDF needed.

Umbrella paper guard: titles matching EEGManyLabs, ManyLabs, PSA, StudySwap, or similar framework patterns are excluded from fast-path resolution and continue to Stage 4.

If disambiguation resolves: set `link_method = author_year_match`, skip Stages 4–6.

### Stage 4 — PDF Acquisition

If still unresolved. 11-tier waterfall — each tier is skipped once a PDF is downloaded. PDFs saved to `cache/pdf/<cache_key(doi_r)>.pdf`.

| Tier | Source |
|------|--------|
| 1 | arXiv (DOI pattern `10.48550/arXiv.*`) |
| 2 | OSF (DOI pattern `10.3123*/osf.io/*`) |
| 3 | OpenAlex OA URL |
| 4 | Unpaywall direct PDFs (all `url_for_pdf` locations) |
| 5 | Semantic Scholar Graph API |
| 6 | CORE.ac.uk |
| 7 | Europe PMC |
| 8 | Unpaywall landing page scraper (DSpace, HAL, Pure repos) |
| 9 | SerpAPI / Google Scholar (multi-key rotation on 429) |
| 10 | Playwright headless Chromium (publisher-specific CSS selectors) |
| 11 | HTML text extraction (up to 50 000 chars as full-text substitute) |

### Stage 5 — Reference Extraction

Uses **pdfminer.six** locally — no GROBID server required. Extracts four sections:

| Section | Content |
|---------|---------|
| `abstract` | Paper abstract |
| `intro` | Introduction text |
| `methods` | Methods section text |
| `references` | Parsed `{authors, year, title, raw_ref}` structs |

**Fallback 1 — Direct PDF to Gemini** (`success_direct_llm`): if pdfminer extracts 0 references, the full PDF is sent to `gemini-3-flash-preview` as inline `application/pdf` data. Efficient for native-text PDFs.

**Fallback 2 — PyMuPDF image rendering** (`success_image_llm`): if direct-PDF also returns nothing, the last ~20% of pages (max 6) are rendered as 1.5× grayscale PNGs and sent to Gemini. Used for scanned PDFs.

GROBID fast-path: after extraction, if one candidate matches the reference list exactly by DOI or author+year (Jaccard similarity), resolve immediately. Set `link_method = author_year_match`.

### Stage 6 — Full LLM Identification

Builds a structured prompt:
- Replication title + abstract
- Numbered candidate list with full author lists and OpenAlex IDs
- PDF intro (1 000 chars), methods (700 chars), up to 80 reference entries
- If PDF failed but URL available: URL passed for Gemini URL grounding
- If HTML text extracted: used as intro substitute

**Model order**: `gemini-3-flash-preview` (primary, key rotation on 429) → `gpt-5-mini` (fallback). Cached as `llm_<hash>.json`.

Returns: `doi_o`, `title_o`, `link_evidence`, `link_confidence`. Sets `link_method = llm_fulltext`.

If both models fail: `link_method = target_pending`, `link_confidence = 0.0`.

### Stage 7 — Outcome Extraction

Run after linking, regardless of which stage resolved the link.

Implemented in `extract/code_outcome.py`.

**Pass 1 — Keyword matching:**
- Scans abstract and title for outcome phrases
- `success`: `"replicated"`, `"consistent with"`, `"confirmed"`, `"effect was reproduced"`
- `failure`: `"failed to replicate"`, `"no evidence"`, `"could not replicate"`, `"null result"`
- `mixed`: `"partial replication"`, `"mixed results"`, `"some but not all"`
- `uninformative`: no clear outcome phrase found

**Pass 2 — LLM outcome extraction** (for `uninformative` or low-confidence keyword matches):
- Sends title + abstract (+ fulltext intro if available) to `gemini-3-flash-preview`
- Returns `outcome`, `outcome_phrase` (supporting quote), `outcome_confidence`, `out_quote_source`
- Cached to `cache/llm/` using `cache_key(doi_r + "_outcome")`

---

## Multi-Original Pipeline (Pipeline C)

Used when `original_match_type = multiple_original`. The paper genuinely replicates several independent original studies and must produce N rows in `extracted.csv`.

### Stage 1 — LLM Abstract + Reference Matching (Early Exit)

Same as Shared Pipeline Stage 1, but with a multi-original prompt. If the model identifies all originals with `confidence = high` → resolve all immediately. Set `link_method = llm_abstract` for each row.

If not resolved → continue to Stage 2.

### Stage 2 — PDF Acquisition

Same 11-tier waterfall as Shared Pipeline Stage 4. No same-author/year disambiguation (not applicable for multi-original papers).

### Stage 3 — Reference Extraction

Same pdfminer / Gemini fallback process as Shared Pipeline Stage 5.

### Stage 4 — Multi-Original LLM

Different prompt from the single-original case. Asks the model to:
- Determine if the paper is truly multi-original or a false positive (only 1 original)
- List **all** original studies being replicated with evidence and confidence for each

Returns:
- `is_false_positive` — if true, treat as `single_original` and pass back through Shared Pipeline
- `originals[]` — one entry per original: `rank`, `doi`, `title`, `year`, `evidence`, `confidence`

Results expanded to N rows in `extracted.csv`. `original_rank` distinguishes each row (1, 2, 3...). `n_originals` is set to the total count on all rows.

Cached as `multi_<hash>.json`.

### Stage 5 — Outcome Extraction

Same keyword + LLM process as Shared Pipeline Stage 7. Run once per original (each row gets its own `outcome`).

---

## Pipeline Runner (`extract/pipeline_runner.py`)

The SSE batch pipeline runner — renamed from `batch.py` to make its purpose clear. Handles the server-sent events stream that drives the validation UI's "Run Batch" feature, sequencing each DOI through the appropriate pipeline and streaming progress updates back to the browser.

This file is a port from the working pipeline and is the main interface between Stage 3 and Stage 4 (Flask validate app).

---

## Output Schema — `extracted.csv`

All columns from `filtered.csv`, plus:

| Column | Type | Description |
|--------|------|-------------|
| `doi_o` | str | Cleaned DOI of the original study |
| `title_o` | str | Original study title |
| `year_o` | int | Original study publication year |
| `authors_o` | str | Original study authors |
| `link_method` | str | `llm_abstract` \| `author_year_match` \| `llm_fulltext` \| `target_pending` |
| `link_evidence` | str | Quote or pattern used for linking |
| `link_confidence` | float | 0.0–1.0 |
| `outcome` | str | `success` \| `failure` \| `mixed` \| `uninformative` \| `pending` |
| `outcome_phrase` | str | Supporting quote from the paper |
| `outcome_confidence` | float | 0.0–1.0 |
| `out_quote_source` | str | `abstract` \| `fulltext` \| `title` |
| `type` | str | `replication` \| `reproduction` |
| `original_rank` | int | 1 for single; 1, 2, 3… for multi-original |
| `n_originals` | int | Total originals in this paper (1 for single) |

---

## `link_method` Values

| Value | Resolving Stage | Description |
|-------|----------------|-------------|
| `llm_abstract` | Stage 1 | Resolved by LLM using abstract + OpenAlex references — no PDF needed |
| `author_year_match` | Stage 3 / Stage 5 (GROBID fast-path) | Resolved by same-author/year heuristic or GROBID reference match |
| `llm_fulltext` | Stage 6 | Resolved by LLM using full PDF context (intro, methods, references) |
| `target_pending` | Stage 6 failed | Both Gemini and OpenAI failed or returned no usable result |

---

## Files

| File | Status | Description |
|------|--------|-------------|
| `extract/run_extract.py` | Stub | Orchestrator — routes by `original_match_type`, writes extracted.csv |
| `extract/link_original.py` | Ported | Shared Pipeline (A/B) — 7-stage single/multiple-match pipeline |
| `extract/multi_original.py` | Ported (needs improvement) | Multi-Original Pipeline (C) — detection logic has known flaws |
| `extract/code_outcome.py` | Stub | Keyword + LLM outcome extraction (new — not yet ported) |
| `extract/pipeline_runner.py` | Ported (renamed from batch.py) | SSE batch runner for the validation app |

---

## What Needs to Be Implemented

- [ ] `run_extract.py` — read `filtered.csv`, route by `original_match_type`, call appropriate pipeline, write `extracted.csv`
- [ ] `code_outcome.py` — keyword pass then LLM pass for `outcome`, `outcome_phrase`, `outcome_confidence`
- [ ] Update `link_original.py` — move LLM abstract+reference matching to Stage 1 (currently Stage 4); add early-exit path
- [ ] Update `multi_original.py` — fix detection logic flaws; add LLM abstract early exit
- [ ] Update `extract/pipeline_runner.py` to use `original_match_type` routing instead of `is_multi_original`

---

## Rules (from RULEBOOK.md)

- False positives pass through unchanged — do not run extraction on them
- `run_extract.py` must route by `original_match_type`, not `is_multi_original` (old column, removed)
- LLM abstract + reference matching must be Stage 1 (the first step), not a fallback
- All LLM responses must be cached before writing to disk
- Rate limits: OpenAlex 0.1s (`OPENALEX_RATE_SEC`), Gemini 1s (`LLM_RATE_SEC`)
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
