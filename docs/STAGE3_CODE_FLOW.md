# Stage 3 — Code-Level Flow

This document traces exactly what happens to a DOI when `python -m extract.run_extract` is run — which functions are called, in what order, with what thresholds. For the design-level overview see [STAGE3_EXTRACT.md](STAGE3_EXTRACT.md).

---

## Entry point: `run_extract()` in `extract/run_extract.py`

Reads `data/filtered.csv` (falls back to `misc/sample_filtered.csv`). Iterates every row.

**`--resume` mode:** if `resume=True`, reads the existing `extracted.csv` first and calls `_load_extracted_rows()`, which partitions rows into two sets: *resolved* (all rows for that DOI have `link_method != "target_pending"`) and *pending* (at least one row is `target_pending`). In the main loop, resolved DOIs are written directly to the output CSV without any processing; pending and new DOIs are processed normally.

**False positives are passed through** — written to `extracted.csv` with all extraction columns empty and `link_method = target_pending`. No classification or LLM calls are made for them.

For every non-false-positive row:

```
classify_match_type(row)
        │
        ├── multiple_original → run_multi_original_for_doi()
        │                             └── writes N rows (one per original)
        │
        └── single_original / multiple_match → run_for_doi()
                                                    └── extract_outcome()
                                                    └── writes 1 row
```

Each completed row is **immediately appended** to `data/extracted.csv`. Stage 4 validation can open the file before Stage 3 finishes.

---

## Phase 1 — Match-type classification

**Function:** `classify_match_type(row, no_llm)` in `extract/run_extract.py`

### Step 0 — Deterministic rules (run before cache and LLM)

`_rule_classify_multi_original(title_r, abstract_r)` scans title and abstract:

| Signal | Pattern |
|---|---|
| Title | `"many labs"`, `"registered replication report"`, `"many analysts"`, `"replication(s) of N"` |
| Abstract | `"replication(s) of N"`, `"replicated N original findings"`, `"N independent studies"` (N ≥ 3) |

If any rule fires → immediately returns `{original_match_type: "multiple_original", original_match_confidence: "high", rule_fired: True}`. Skips cache and LLM entirely. This cannot be overridden by a stale cached LLM result.

### Step 1 — LLM cache check

Looks for `cache/llm/match_type_<md5(doi_r+"_match_type")>.json`. Returns cached result if found.

### Step 2 — Author-year pattern extraction

`extract_author_year_patterns(title_r)` + `extract_author_year_patterns(abstract_r)` using 8 regexes ordered most-specific → least-specific:

| Pattern name | Example match |
|---|---|
| `multi_and_paren` | `"Smith, Jones, and Brown (2005)"` |
| `multi_and_bare` | `"Smith, Jones, and Brown, 2005"` |
| `etal_paren` | `"Smith et al. (2005)"` |
| `etal_bare` | `"Smith et al., 2005"` |
| `two_and_paren` | `"Smith & Jones (2005)"` |
| `two_and_bare` | `"Smith & Jones, 2005"` |
| `single_paren` | `"Smith (2005)"` |
| `single_bare` | `"Smith, 2005"` |

Overlapping spans are deduplicated. Years > `year_r` are excluded.

Returns `list[{surname, year, raw, pattern, start, end}]`.

### Step 3 — OpenAlex candidate fetch

`find_all_candidates(doi_r, openalex_id_r, title_r, abstract_r, year_r)` in `shared/openalex_client.py`:

1. Checks `cache/openalex/candidates_<md5>.json` — returns cached if found.
2. If no `openalex_id_r` → logs a warning and returns `[]` (no candidates possible).
3. Fetches the paper's `referenced_works` from OpenAlex via `GET /works/{id}`.
4. Batch-fetches metadata for all referenced works (50 IDs per request).
5. For each author-year pattern, checks every referenced work:
   - Year match: exact, or ±1 year tolerance
   - Author match: exact → prefix (≥3 chars either direction) → near-prefix (1 char difference at end)
6. Deduplicates by OpenAlex ID. Caches result.

### Step 4 — LLM classification

`_llm_classify_match_type(doi_r, title_r, abstract_r, distinct_pairs, candidates)`:

Prompt includes title, 800-char abstract, author-year patterns (as `"Smith (2005)"` lines), and up to 15 candidate titles. Uses `call_llm(prompt, gemini_model=GEMINI_LIGHT_MODEL)`.

Returns one of:
- `single_original` — targets one study
- `multiple_match` — one target but 2–5 candidates share the same author/year
- `multiple_original` — paper explicitly replicates N independent original studies

Result cached to `cache/llm/match_type_<md5>.json`.

---

## Phase 2a — Multiple-original path

**Function:** `run_multi_original_for_doi(doi_r, all_rep_df, force_multi)` in `extract/multi_original.py`

Calls `identify_all_originals_with_llm()` in `shared/llm_client.py`:
1. Acquires PDF (same 11-tier waterfall — see Phase 2b Stage 5)
2. Parses with all 5 methods, picks richest result
3. Sends GEMINI_HEAVY_MODEL prompt with 2000-char abstract, 1200-char intro, 800-char methods, reference list (up to 100 entries), all candidates
4. When `force_multi=True` (rule fired in Phase 1), the prompt includes a directive: *"CONFIRMED MULTI-TARGET — list ALL originals, do not set is_false_positive."* The cache is also bypassed so the stronger prompt always runs.

Returns `{is_false_positive, originals: [{rank, title, doi, year, evidence, confidence, outcome, outcome_evidence}]}`.

**Routing after result:**

| Condition | Action |
|---|---|
| Originals found | Write N rows to `extracted.csv` (`original_rank = 1, 2, 3…`). Outcome comes from the LLM's per-original `outcome_evidence`. |
| No originals + `rule_fired=True` | Write 1 `target_pending` row with `match_type=multiple_original`. Never downgrades to single-original. |
| No originals + `rule_fired=False` | Falls through to single-original pipeline (`run_for_doi`). |

---

## Phase 2b — Single-original path

**Function:** `run_for_doi(doi_r, flora_df, cands_df, force, no_llm)` in `extract/link_original.py`

### Stage 1 — Base data

- `_flora_row(doi_r, flora_df)` — looks up FLoRA entry sheet for this DOI; returns columns prefixed with `flora_` (ref_r, url_r, doi_o, outcome, etc.)
- `_cands_row(doi_r, cands_df)` — returns pass-through columns from the candidates DataFrame built by `run_extract.py` from the filtered.csv row

### Stage 2 — OpenAlex re-query

`find_all_candidates(doi_r, oa_id_r, study_r, abstract_r, year_r, pattern_r)` — same function as Phase 1, hits cache for free on the second call.

**FLoRA anchor injection:** If the FLoRA entry sheet has a manually validated `doi_o` for this paper (status contains `"validated"`), that DOI is fetched from OpenAlex via `fetch_openalex_by_doi()` and prepended to the candidate list. An anchor note is added to every downstream LLM prompt: *"FLoRA team verified this as the original — confirm if supported, override only on strong contradicting evidence."*

### Stage 2.5 — Title-pattern resolver

`_resolve_by_title_pattern(doi_r, study_r, candidates)` — runs before any LLM call.

`_extract_title_target(title_r)` applies 9 regexes:

```
"A Direct Replication of TARGET"
"Replicating TARGET"
"A Reproduction of TARGET"
"Reproducing TARGET"
"Revisiting / Re-examining / Reconsidering TARGET"
"Can we replicate TARGET?"
"Does TARGET replicate?"
"Testing the replicability of TARGET"
"TARGET: A Replication [and Extension]"
```

Minimum target length: 8 characters (shorter targets like "Trust" are noise).

Scores each candidate: `score = jaccard_similarity(candidate["title"], target)`.

| Score condition | Action |
|---|---|
| `best ≥ 0.4` AND `best ≥ 1.5 × second` | Resolves immediately as `resolution_method = "title_pattern_match"`. No LLM or PDF. |
| `best ≥ 0.3` but gap not met | Does NOT resolve, but injects a `TITLE PATTERN HINT` into all downstream LLM prompts with the top 3 candidate titles. |
| `best < 0.3` | Falls through unchanged. |

### Stage 3 — Rule-based citation resolver

`_resolve_rule_based(doi_r, abstract_r, candidates, year_r, study_r)` — runs before any LLM call.

**If 1 candidate (not an umbrella paper):** resolves immediately as `single_candidate_after_requery` (score 1.0).

Umbrella paper guard: `is_umbrella_paper()` checks for titles matching EEGManyLabs, ManyLabs, PSA, StudySwap, and similar project names.

**Path A — Citation context scoring:**

Parses parenthetical citations from abstract: `(Smith, 2005, Psychological Science)` using `_CITATION_RE`.

For each candidate:

| Signal | Points |
|---|---|
| Author match in citation | +2 |
| Year exact match | +2 |
| Year ±1 match | +1 |
| Journal token overlap ≥ 60% | +3 |
| Journal token overlap ≥ 30% | +1.5 |
| Title Jaccard against abstract | up to +1 |

Journal data fetched from OpenAlex and cached per candidate DOI (`cache/openalex/journal_<md5>.json`).

**Resolves if:** `best_total ≥ 4.0` AND `gap_to_second ≥ 2.0`. Returns `citation_context_match`.

**Path B — Same-author/year cluster:**

Fires when all candidates share one surname AND one year (Path A strict threshold not met).

`jaccard_similarity(candidate["title"], abstract + title)` for each. **Resolves if:** `best > 0.05` AND `best ≥ 1.5 × second`. Returns `same_author_year_title_overlap`.

### Stage 4 — Abstract-level LLM

Fires only if: `abstract_r` is non-empty **AND** `distinct_pairs` from the abstract is non-empty (papers with no author-year citation patterns in the abstract don't benefit from an abstract-only LLM call).

Calls `identify_original_with_llm(doi_r + "_abstract", ...)` with GEMINI_HEAVY_MODEL. Cached separately at `cache/llm/llm_<md5(doi+"_abstract")>.json`. If resolved → returns early.

### Stage 5 — PDF acquisition (11-tier waterfall)

**`no_pdf=True` early exit:** if the `--no-pdf` flag is set and Stages 2.5, 3, and 4 did not resolve the paper, Stage 5 returns `target_pending` immediately with `resolution_method = "needs_fulltext"`. No PDF is downloaded and Stages 6–7 are skipped entirely. `_save_parse_cache()` in `run_extract.py` is also skipped for these rows.

`acquire_pdf(doi_r, title, openalex_id)` in `shared/pdf_sources.py`:

| Tier | Source | Notes |
|---|---|---|
| 0 | OpenAlex GROBID XML | Checks `has_content.grobid_xml`; downloads pre-parsed TEI from `content.openalex.org`. No PDF file, but returns structured sections. |
| 1 | arXiv direct PDF | DOI pattern `10.48550/arXiv.*` |
| 2 | OSF preprint | DOI pattern `10.3123x/osf.io/*` |
| 3 | OpenAlex OA URL | `open_access.oa_url` field |
| 4 | Unpaywall direct PDFs | All `url_for_pdf` locations, best-first |
| 5 | SemanticScholar | `openAccessPdf.url` via Graph API |
| 6 | CORE.ac.uk | `downloadUrl` or `fullTextUrl` |
| 7 | Europe PMC | PMC full-text PDF via `pmcid` |
| 8 | Unpaywall landing pages | HTML scraper for HAL, DSpace, Pure repos |
| 9 | SerpAPI | Google Scholar search; rotates keys on 429 |
| 10 | Playwright headless Chromium | Publisher-specific CSS selectors; intercepts inline PDFs |
| 11 | HTML text extraction | Extracts visible text (up to 50 000 chars) from best available landing page |

Every tier checks its own cache before making a network call. PDFs saved to `cache/pdf/<md5>.pdf`. The OpenAlex XML result (Tier 0) is returned separately in `openalex_xml` alongside any PDF result.

### Stage 6 — PDF parsing (5 methods, pick richest)

`parse_all(doi_r, pdf_path, oa_xml)` in `shared/pdf_parsing.py` runs all five parsers:

| Method | Implementation |
|---|---|
| `openalex_xml` | Reformats the Tier 0 GROBID XML dict |
| `pdfminer` | pdfminer.six text extraction + section splitter |
| `grobid` | `run_grobid()` — pdfminer + optional GROBID server + LLM fallbacks (direct PDF, image-based) for scanned PDFs |
| `docpluck` | `docpluck.parse()` if installed; graceful error if not |
| `docling` | `DocumentConverter` if installed; graceful error if not |

`_best_parse_result()` selects the winner: `score = len(references) × 500 + len(abstract) + len(intro)`. References are weighted 500× because they are the most useful context for the LLM. If all methods errored, GROBID result is the fallback.

Parse results are saved to `cache/parse/parse_<md5>.json` immediately after computation. This ensures the `_save_parse_cache()` call in `run_extract.py` finds the file and skips re-parsing.

### Stage 7 — Full-text LLM identification

Guard: if no context at all (no abstract, no candidates, no intro, no references) → skips LLM, writes `target_pending`.

`identify_original_with_llm(doi_r, study_r, abstract_r, pattern, candidates, sections)` in `shared/llm_client.py`:

**Prompt contents:**
- Replication paper title + 700-char abstract
- Author-year cited pattern
- Numbered candidate list: title, year, all authors, DOI, OpenAlex ID
- 600-char intro from PDF (+ 400-char methods if intro < 300 chars)
- Reference list (up to 30 entries)
- FLoRA anchor note (if validated DOI exists)
- Title pattern hint (if Stage 2.5 found plausible matches)
- Validator feedback (if human reviewer corrected a prior answer)

**LLM fallback chain:** GEMINI_HEAVY_MODEL → OpenAI → OpenRouter (Qwen).

**Result:** LLM returns `selected_candidate_number`. The candidate's OpenAlex-verified DOI is used; any DOI the LLM produces itself is used only if the candidate had no DOI (prevents hallucination). Cached to `cache/llm/llm_<md5>.json` only if resolved.

**`link_method` mapping** (internal method → schema value):

| Internal | Schema `link_method` |
|---|---|
| `citation_context_match` | `author_year_match` |
| `same_author_year_title_overlap` | `author_year_match` |
| `single_candidate_after_requery` | `author_year_match` |
| `title_pattern_match` | `author_year_match` |
| `grobid_ref_match` | `author_year_match` |
| `llm_gemini` / `llm_openai` | `llm_fulltext` |
| `llm_abstract_gemini` / `llm_abstract_openai` | `llm_abstract` |
| `llm_failed` / `no_candidates_found` / `none` | `target_pending` |

**`link_confidence`:** uses `llm_confidence` directly when the LLM resolved (avoids round-tripping through a float score). For rule-based resolutions, converts `resolution_score` to high/medium/low (≥0.8 = high, ≥0.5 = medium, else low).

---

## Phase 3 — Outcome extraction

**Function:** `extract_outcome(doi_r, abstract_r, fulltext, title_r, no_llm)` in `extract/code_outcome.py`

Fulltext passed in = concatenation of `grobid_abstract` + `grobid_intro` + `grobid_methods` + `html_text` from the link result (combined up to ~3200 chars, much richer than the previous intro-only 1000 chars).

### Pass 1 — Keyword scan on title (high-confidence only)

Check order: failure → mixed → success → descriptive. Failure is checked first so `"failed to replicate"` never triggers the bare-`"replicated"` success pattern.

Returns only if `outcome_confidence == "high"`.

### Pass 2 — Keyword scan on abstract (any hit)

Same check order. Returns on first match regardless of confidence.

### Pass 3 — Keyword scan on fulltext[:3000] (high-confidence only)

Returns only if `outcome_confidence == "high"`.

**Key patterns:**

| Outcome | Example phrases |
|---|---|
| `failure` | `"failed to replicate"`, `"replication failed"`, `"null result"`, `"no evidence"` |
| `mixed` | `"partially replicated"`, `"mixed results"`, `"smaller effect"`, `"qualified support"` |
| `success` | `"successfully replicated"`, `"confirmed the original"`, bare `"replicated"` |
| `descriptive` | `"adapted the method"`, `"in a different context"`, `"not a direct test"` |

### Pass 4 — LLM fallback

`_llm_outcome(doi_r, title_r, abstract_r, fulltext)` — sends title + 1000-char abstract + 800-char fulltext to `call_llm(prompt, gemini_model=GEMINI_LIGHT_MODEL)`. Returns `{outcome, outcome_phrase, outcome_confidence, out_quote_source}`. Cached to `cache/llm/outcome_<md5>.json`.

---

## Phase 4 — Assembly and streaming write

`_merge_row(filter_row, link, outcome, match_type, match_conf, rank, n)`:

- Copies all columns from the filtered.csv row
- Propagates `study_r → title_r` for old seeded data using `study_r`
- Generates `pair_id = make_pair_id(doi_r, doi_o)` (stable identifier for the replication-original pair)
- Sets `type = "reproduction"` if `filter_status == "reproduction"`, else `"replication"`

`_append_row(out_path, result_row, first)` writes immediately:
- First row: `mode='w'` (creates / truncates file), writes header
- All subsequent: `mode='a'` (append), no header

---

## Caching summary

| Cache directory | Contents | Key |
|---|---|---|
| `cache/openalex/candidates_*.json` | OpenAlex candidate list per replication DOI | `md5(doi_r)` |
| `cache/openalex/journal_*.json` | Journal name per candidate DOI | `md5(doi_o)` |
| `cache/openalex/unpaywall_*.json` | Unpaywall full response | `md5(doi_r)` |
| `cache/openalex/ss_*.json` | SemanticScholar response | `md5(doi_r)` |
| `cache/openalex/oa_*.json` | OpenAlex OA URL | `md5(doi_r)` |
| `cache/openalex_xml/oa_xml_*.json` | OpenAlex GROBID XML sections | `md5(openalex_id)` |
| `cache/pdf/*.pdf` | Downloaded PDFs | `md5(doi_r)` |
| `cache/parse/parse_*.json` | All 5 parse results per DOI | `md5(doi_r)` |
| `cache/llm/match_type_*.json` | Match-type LLM result | `md5(doi_r+"_match_type")` |
| `cache/llm/llm_*.json` | Full-text LLM identification | `md5(doi_r)` |
| `cache/llm/llm_*_abstract.json` | Abstract-only LLM identification | `md5(doi_r+"_abstract")` |
| `cache/llm/outcome_*.json` | Outcome LLM result | `md5(doi_r)` |
| `cache/llm/multi_*.json` | Multi-original LLM result | `md5(doi_r)` |

All caches persist across runs. Clear a specific DOI's caches with `clear_pipeline_caches(doi_r)` in `link_original.py`, or clear the entire `cache/` directory to force a full re-run.

---

## Error handling

| Failure | Behaviour |
|---|---|
| OpenAlex API failure in `find_all_candidates` | Returns `[]`; `classify_match_type` defaults to `single_original` |
| Missing `openalex_id_r` | Logs a warning; `find_all_candidates` returns `[]` immediately |
| All PDF tiers fail | `pdf_ok = False`; pipeline continues using abstract-only context |
| LLM call fails (all providers) | Writes `link_method = "target_pending"` or `"api_error"` depending on where failure occurred |
| Entire row extraction raises exception | Caught in `run_extract.py`; writes `_empty_row()` with `outcome = "api_error"` |

---

## Known limitations

- **`title_pattern_match` grouped into `author_year_match`** in `link_method` — you cannot distinguish title-pattern resolution from citation-score resolution in `extracted.csv`. The internal `resolution_method` string (available in debug logs and LLM cache files) preserves the distinction.
- **`_MULTI_N_MIN = 3`** — abstract counts of 1 or 2 studies are not classified as `multiple_original` by rules. They fall through to the LLM, which may or may not classify them correctly.
- **Abstract-level LLM skipped when abstract has no author-year patterns** — papers where the only citation evidence is in footnotes or reference styles without year-in-parentheses will bypass the abstract-LLM early-exit and go straight to PDF acquisition.
