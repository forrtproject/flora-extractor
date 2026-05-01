# FLoRA Disambiguation Pipeline — Backend Overview

Two pipelines run through the same core infrastructure (OpenAlex re-query → PDF → GROBID → LLM) but differ in their input selection and LLM objective.

---

## Pipeline 1 — Multiple Matches

**Goal:** For replication studies where OpenAlex found multiple candidate originals but couldn't pick one, identify the single correct original.

### Input selection (`app.py`)

From `all_replications.csv`, take DOIs where `prep_notes` starts with `"openalex:"` in the FLoRA sheet, then intersect with `openalex_candidates.csv` rows where:
- `match_source = openalex_references`
- `match_status = multiple_matches`

These are studies OpenAlex already found *some* candidate for, but couldn't confidently disambiguate.

---

### Stage 1 — Base data (`lib/pipeline.py`)

For each `doi_r`, pull:
- FLoRA sheet row → `flora_*` prefixed fields (`flora_ref_r`, `flora_doi_o`, `flora_outcome`, etc.)
- `openalex_candidates.csv` row → passthrough columns (`study_r`, `abstract_r`, `year_r`, `openalex_id_r`, etc.)

---

### Stage 2 — OpenAlex re-query (`lib/openalex.py`)

1. **Extract author-year citation patterns** from the replication title → abstract → stored pattern (priority order). Regex covers:
   - `Smith (2020)`, `Smith & Jones, 2020`, `Smith et al. (2020)`, multi-author variants
   - Name prefixes: "van der", "de la", "du", "le", etc.
   - Unicode surnames (Latin Extended, Latin Extended Additional)

2. **Fetch referenced works** for the replication paper from OpenAlex using its OpenAlex ID, batching 50 IDs per API call. Result cached per `doi_r`.

3. **Match patterns against references**: for each pattern, scan referenced works for year match (exact ±1 year tolerance) + author surname prefix match (3+ chars, near-prefix with 1-char difference allowed). Every hit becomes a candidate.

Result: list of `{openalex_id, doi, title, year, first_author, match_year_exact, cited_pattern}`.

---

### Stage 3 — Same-author/year disambiguation (`lib/disambiguation.py`)

Fast heuristic pass. If only one candidate exactly matches the cited year and author, resolve immediately — no LLM or PDF needed.

---

### Stage 4 — Early abstract LLM (`lib/pipeline.py`)

If the abstract contains 2+ distinct author-year patterns *and* candidates exist, call the LLM using only abstract-level context (no PDF). Returns early if resolved — avoids the slower PDF stages.

---

### Stage 5 — PDF acquisition (`lib/pdf_sources.py`)

11-tier waterfall. Each tier is skipped once a PDF is successfully downloaded. PDFs saved to `PDF_CACHE_DIR/<md5(doi_r)>.pdf`. All API responses are cached.

| Tier | Source | Notes |
|------|--------|-------|
| 1 | arXiv | DOI pattern `10.48550/arXiv.*` or title hint |
| 2 | OSF | DOI pattern `10.3123*/osf.io/*` |
| 3 | OpenAlex OA URL | `open_access.oa_url` field |
| 4 | Unpaywall direct PDFs | All `url_for_pdf` locations, best-first |
| 5 | Semantic Scholar | Graph API `openAccessPdf` field |
| 6 | CORE.ac.uk | `downloadUrl` or `fullTextUrl` |
| 7 | Europe PMC | Reconstructed PMC PDF URL |
| 8 | Unpaywall landing page scraper | Regex-scrapes DSpace, HAL, Pure repos |
| 9 | SerpAPI / Google Scholar | Quota-limited; rotates through multiple API keys on 429 |
| 10 | Playwright headless Chromium | Intercepts PDF network responses; clicks download buttons using publisher-specific CSS selectors (Elsevier, Wiley, Springer, Taylor & Francis, APA, Cambridge, OUP, SAGE) |
| 11 | HTML text extraction | If all PDF tiers fail: downloads landing page, strips nav/footer/scripts via lxml, returns up to 50 000 chars of visible text as a full-text substitute |

---

### Stage 6 — Reference extraction (`lib/grobid.py`)

Extracts 4 sections from the PDF using **pdfminer.six** (local, no external server):

| Section | Content |
|---------|---------|
| `abstract` | Paper abstract |
| `intro` | Introduction text |
| `methods` | Methods section text |
| `references` | Parsed list of `{authors, year, title, raw_ref}` structs |

References are parsed with a multi-strategy regex: numbered entries, author-year format, DOI lines.

**Fallback 1 — Direct PDF to Gemini** (`success_direct_llm`): if pdfminer extracts 0 references, the full PDF is sent to Gemini as inline `application/pdf` data with `mediaResolution: MEDIA_RESOLUTION_LOW`. For native-text PDFs, Gemini reads embedded text directly (not billed as image tokens), making this highly efficient. Falls back gracefully if the PDF exceeds 45 MB. Results cached as `<stem>_direct_refs.json`.

**Fallback 2 — PyMuPDF image rendering** (`success_image_llm`): if direct-PDF also returns nothing, the last ~20% of pages (max 6) are rendered as 1.5× grayscale PNGs and sent to Gemini as inline image parts. Used for scanned or non-standard layouts where direct PDF fails. Results cached as `<stem>_img_refs.json`.

---

### Stage 7 — LLM identification (`lib/llm.py`)

Builds a structured prompt containing:
- Replication title + abstract
- Numbered candidate list with DOIs and OpenAlex IDs
- PDF abstract, intro (1 000 chars), methods (700 chars), up to 80 reference entries
- If PDF failed but a URL exists: URL passed for Gemini URL grounding
- If HTML text was extracted: used as intro substitute

**Model order**: Gemini `gemini-3-flash-preview` (primary, rotates API keys on quota errors) → OpenAI `gpt-5-mini` (fallback). Successful results cached as `llm_<hash>.json`.

LLM returns:

| Field | Description |
|-------|-------------|
| `selected_candidate_number` | Index into candidate list (or null) |
| `selected_doi` | DOI of the identified original |
| `selected_title` | Full title of the original |
| `confidence` | `high` / `medium` / `low` → mapped to 1.0 / 0.6 / 0.3 |
| `evidence` | 1–3 sentence quote confirming identification |
| `reasoning` | Why other candidates were ruled out |

---

## Pipeline 2 — Multiple Originals

**Goal:** For replication studies flagged as potentially targeting *multiple* original studies, identify all originals or flag as false positive.

### Input selection (`routes/input_bp.py`)

Random sample of N unique `doi_r`s from `all_replications.csv` where `multi_target = True`. N is configurable (default 20, clamped 1–500). The candidate list is overwritten on regenerate; resolved results accumulate separately in `multi_original_resolved.csv`.

---

### Stages 1–4 — Same as Pipeline 1

Base data pulled from `all_replications.csv` (not the FLoRA sheet). Same OpenAlex re-query, PDF acquisition (11 tiers), and pdfminer/PyMuPDF reference extraction. No same-author/year or early abstract disambiguation steps.

---

### Stage 5 — Multi-original LLM (`lib/llm.py`)

Different prompt and output shape. The LLM is asked to:
- Determine if the paper is truly multi-target or a false positive (only 1 original)
- List **all** original studies being replicated with evidence and confidence for each

**Model order**: same as Pipeline 1 (Gemini → OpenAI). Cached as `multi_<hash>.json`.

LLM returns:

| Field | Description |
|-------|-------------|
| `is_false_positive` | `true` if only 1 original found despite multi-target flag |
| `reasoning` | Explanation of multi-target vs. false positive decision |
| `originals[]` | One entry per original: `rank`, `title`, `doi`, `first_author`, `year`, `evidence`, `confidence` |

Results saved to `multi_original_resolved.csv` in **long format** — one row per original study per `doi_r` (a 3-original paper writes 3 rows), with `original_rank` as the distinguishing column.

---

---

## Resolution Method Reference

The `resolution_method` field in every output row records **how** the original study was identified. Values, in pipeline order:

| `resolution_method` | Stage | Description |
| ------------------- | ----- | ----------- |
| `single_candidate_after_requery` | 2 | OpenAlex re-query returned exactly one candidate matching the author-year pattern — no disambiguation needed |
| `same_author_year_title_overlap` | 3 | Fast heuristic: only one candidate matched the cited year + author surname, and its title overlaps with the citation context (Jaccard similarity) |
| `llm_abstract_gemini` | 4 | Abstract-only LLM pass (Gemini) resolved the paper before a PDF was needed — triggered when 2+ distinct author-year patterns appear in the abstract |
| `llm_abstract_openai` | 4 | Same as above, but Gemini quota was exhausted so OpenAI was the model used |
| `llm_gemini` | 7 | Full-context LLM (Gemini) resolved the paper using GROBID-extracted sections + reference list |
| `llm_openai` | 7 | Same as above, but OpenAI was used as fallback |
| `llm_failed` | 7 | LLM was called but returned no usable result (both Gemini and OpenAI failed or returned empty) |
| `no_candidates_found` | 2 | OpenAlex re-query returned zero matching candidates; cannot proceed to disambiguation |
| `needs_fulltext` | — | Placeholder: resolved cannot be determined from abstract alone and no PDF was obtained |
| `grobid_ref_match` | 6 | Reference list from GROBID exactly matched one candidate by DOI or author+year (fast-path before LLM) |
| `grobid_ref_no_match` | 6 | GROBID extracted references but none matched any candidate |
| `none` | — | Pipeline was not run for this DOI (e.g. loaded from a prior result or input error) |

### GROBID extraction status values

| `grobid_status` | Meaning |
| --------------- | ------- |
| `success` | pdfminer extracted text and ≥1 reference successfully |
| `success_direct_llm` | pdfminer found text but 0 refs → Gemini direct-PDF call extracted references |
| `success_image_llm` | pdfminer found text but 0 refs and direct PDF failed → page-image Gemini call extracted references |
| `pdfminer_failed` | pdfminer extracted no text at all (e.g. encrypted or image-only scanned PDF) |
| `no_pdf` | No PDF was available (all acquisition tiers failed or returned HTML only) |
| `not_attempted` | GROBID stage was skipped (resolved before reaching Stage 6) |

---

## Shared Infrastructure

| Component | Location | Details |
|-----------|----------|---------|
| DOI normalisation | `lib/utils.py` | `clean_doi()` strips `https://doi.org/` prefix, lowercases |
| Cache keys | `lib/utils.py` | `cache_key(doi)` → MD5 hex digest; used for all file-based caches |
| OpenAlex cache | `cache/openalex/` | `refs_<id>.json`, `candidates_<hash>.json`, `unpaywall_<hash>.json`, etc. |
| PDF cache | `cache/pdf/` | `<hash>.pdf`, `<hash>.txt` (HTML text), `<stem>_img_refs.json` (PyMuPDF) |
| LLM cache | `cache/llm/` | `llm_<hash>.json` (Pipeline 1), `multi_<hash>.json` (Pipeline 2) |
| GROBID cache | `cache/grobid/` | `<stem>.json` (pdfminer sections), `<stem>_img_refs.json` (image fallback) |
| Rate limiting | `lib/config.py` | `OPENALEX_RATE_SEC`, `UNPAYWALL_RATE_SEC`, `LLM_RATE_SEC` |
| State | `state.py` | Module-level DataFrames and result dicts shared across Flask requests |
| Results storage | `data/` | `filtered_candidates.csv`, `final_output.csv`, `multi_original_candidates.csv`, `multi_original_resolved.csv` |

---

## Changelog

### 2026-04-28 — Human validation, LLM author fix, umbrella guard, UI upgrades

#### Bug fixes

**Umbrella/framework paper auto-resolution guard (`lib/disambiguation.py`)**
Stage 3 previously auto-resolved any DOI with exactly one OpenAlex candidate as `single_candidate_after_requery`, even when that candidate was a framework paper (e.g. the EEGManyLabs design paper, ManyLabs protocol, PSA or StudySwap overview). These papers cite hundreds of studies and should never be resolved as "the" original. A new `_is_umbrella_paper()` function checks the candidate title against a regex of known multi-study framework keywords; if matched, Stage 3 returns `resolution_method = needs_fulltext` and continues to the full PDF+LLM pipeline.

**Full author list in LLM prompt (`lib/openalex.py`, `lib/llm.py`)**
The candidate list sent to the LLM previously showed only `first_author`. This made it impossible to distinguish papers with the same first author but different co-authors (e.g. Stahlberg & Sczesny 2001 vs. Stahlberg, Sczesny & Braun 2001). `find_all_candidates()` now stores `all_authors: list[str]` on each candidate dict. The prompt builder uses `_authors_str()` to render the full list as `authors: A, B, C`.

#### New feature — Human validation (`routes/batch.py`, `state.py`, `lib/pipeline.py`, `lib/llm.py`)

A per-DOI human validation workflow lets reviewers mark each resolved result as **Successful**, **Failed**, or **Recheck** and attach a free-text comment.

- **Persistence**: validations are stored in `cache/validations.json` (loaded at startup, written on every save) as `doi_r → {status, comment, timestamp}`. Survives server restarts.
- **API endpoints**:
  - `POST /api/batch/validate` — save status + comment for a DOI.
  - `POST /api/batch/run_doi` — force-rerun a single DOI, accepting an optional `validation_comment` that is prepended to the LLM prompt as a `⚠️ VALIDATOR FEEDBACK` block so the model knows the previous answer was wrong.
- **LLM integration**: `identify_original_with_llm()` and `build_identification_prompt()` accept a `validator_note` parameter. When non-empty, the prompt opens with the feedback block before the main instruction, steering the model away from the incorrect prior answer.
- **`flora_validation_status`** (raw FLoRA sheet value) added to `_FLORA_COLS` in `pipeline.py` and now flows through to all output dicts.

#### New feature — Export Minimal (`templates/batch.html`)

An "Export Minimal ▾" dropdown in the export bar produces a 6-column extract of pipeline results: `doi_r`, `study_r`, `resolved_doi_o`, `resolved_title_o`, `user_val_status`, `flora_validation_status`. Three formats are supported:

| Format | Mechanism |
| ------ | --------- |
| CSV | Client-side Blob with UTF-8 BOM |
| Excel (.xlsx) | Client-side SheetJS (`xlsx.full.min.js` from CDN) |
| PDF | Opens a print window with a styled HTML table; triggers `window.print()` |

Only rows that have been processed (have a result or resolved DOI) are included.

#### UI improvements (`templates/batch.html`)

- **FLoRA Status column filter**: changed from a free-text input to a `<select>` dropdown dynamically populated from unique `flora_validation_status` values present in the loaded data. A "— (blank)" option (value `_blank`) allows filtering for rows with no FLoRA status. Filter logic uses exact-match instead of substring-contains.
- **Val. column**: new table column showing the human validation status as a coloured pill (green ✓ OK / red ✗ Fail / orange ↺ Check). Filterable via the `f-userval` dropdown (All / ✓ OK / ✗ Failed / ↺ Recheck / Not validated).
- **Validation panel**: each detail row has a ✓ Validate button that opens an inline panel with radio buttons (Successful / Failed / Recheck), a comment textarea, a Save button, and a Re-run button (disabled for Successful). On save, the Val. cell updates in place without a full re-render. On re-run, the pipeline runs with `force=True` (clears LLM/GROBID/OA caches, keeps PDF) and the panel refreshes with the new result.
- **`escHtml` bug fix**: removed a call to an undefined `escHtml()` helper; replaced with inline `.replace(/&/g,'&amp;').replace(/</g,'&lt;')`.
- **FLoRA STATUS column**: now displays the raw string from the FLoRA sheet instead of mapping it through a label dict.

---

## Code File Reference

### `lib/pipeline.py`

The single-DOI orchestrator for Pipeline 1 (Multiple Matches). Its public function `run_for_doi(doi_r, flora_df, cands_df, force, validation_comment)` sequences all seven pipeline stages and returns a flat dict with output columns prefixed by source (`flora_*`, `pdf_*`, `grobid_*`, `resolved_*`, `llm_*`). It pulls base data from both the FLoRA sheet and `openalex_candidates.csv`, injects FLoRA-validated originals as anchor candidates for manually verified DOIs, and assembles the final output via `_build_output()`. The `clear_pipeline_caches()` helper deletes LLM, GROBID, and OpenAlex candidate caches for a DOI while preserving the downloaded PDF, enabling force-reruns without repeat downloads.

### `lib/openalex.py`

Handles author-year citation pattern extraction and OpenAlex API queries. `extract_author_year_patterns(text, max_year)` applies eight ordered regex patterns (covering single, two-author, et-al, and multi-author forms with Unicode surname support and name prefixes) to extract cited author-year pairs from any text. `find_all_candidates(doi_r, openalex_id_r, ...)` fetches the replication paper's full referenced-works list from OpenAlex (batching 50 IDs per request, cached per paper), then matches every extracted pattern against those references using year tolerance (±1) and fuzzy surname prefix matching. `fetch_openalex_by_doi(doi)` looks up a single DOI in OpenAlex and returns a candidate dict in the same format, used to inject FLoRA-verified originals into the candidate pool.

### `lib/disambiguation.py`

Provides the Stage 3 fast-path heuristic and GROBID reference matching. `resolve_same_author_year(doi_r, study_r, abstract_r, candidates)` resolves immediately when there is a single non-umbrella candidate, or when all candidates share the same first-author surname and year and one title has a clear Jaccard similarity advantage (≥1.5× margin over the runner-up). The `_is_umbrella_paper(title)` guard prevents auto-resolution for EEGManyLabs, ManyLabs, PSA, StudySwap, and similar framework papers, routing them to the full PDF+LLM pipeline instead. `resolve_by_grobid_refs(doi_r, candidates, sections)` performs a post-GROBID fast-path, matching candidates against the parsed reference list by title Jaccard similarity with author-surname and year gating.

### `lib/grobid.py`

Extracts structured sections from downloaded PDFs using pdfminer.six locally (no external server). `parse_pdf_sections(pdf_path)` extracts raw text, splits it into abstract/intro/methods/references blocks using section-header regexes, parses the reference block into structured `{authors, year, title, raw_ref}` dicts, and caches the result as JSON. `run_grobid(doi_r, pdf_path)` orchestrates extraction and, when pdfminer yields zero references, escalates through two Gemini fallbacks: direct-PDF submission (`success_direct_llm`, efficient for native-text PDFs) and PyMuPDF page-image rendering (`success_image_llm`, for scanned or non-standard layouts). Legacy wrappers `process_pdf_with_grobid()` and `parse_tei_sections()` are retained for import compatibility but are superseded by the local pdfminer approach.

### `lib/llm.py`

The LLM layer for both pipelines. For Pipeline 1, `identify_original_with_llm(doi_r, study_r, abstract_r, pattern, candidates, sections, ...)` builds a structured identification prompt via `build_identification_prompt()` — including replication title/abstract, the numbered candidate list with full author lists, GROBID-extracted intro and methods, up to 50 reference entries, and optional validator feedback or FLoRA anchor notes — then calls Gemini (`gemini-3-flash-preview`) with key rotation on 429 errors, falling back to OpenAI (`gpt-5-mini`). For Pipeline 2, `identify_all_originals_with_llm()` uses a different prompt from `build_multi_original_prompt()` that asks the model to list all replicated originals and flag false positives, returning a structured list of originals with rank, DOI, evidence, and confidence. Successful responses are cached as JSON; `call_gemini_with_pdf()` and `call_gemini_with_images()` are helper functions used by `lib/grobid.py` for its Gemini-based reference extraction fallbacks.

### `lib/pdf_sources.py`

Implements the 11-tier PDF acquisition waterfall. `acquire_pdf(doi_r, title)` tries sources in priority order: arXiv (DOI/title pattern), OSF (DOI pattern), OpenAlex OA URL, Unpaywall direct PDFs, Semantic Scholar, CORE.ac.uk, Europe PMC, Unpaywall landing-page scraping, SerpAPI/Google Scholar (with multi-key rotation), Playwright headless Chromium (intercepting PDF responses and clicking publisher-specific download buttons), and finally HTML text extraction as a full-text substitute when all PDF tiers fail. All API responses are cached per DOI in `cache/openalex/`; downloaded PDFs are cached in `cache/pdfs/` keyed by MD5 of the DOI. The function returns a dict with `pdf_url`, `pdf_source`, `pdf_path`, `pdf_ok`, `pdf_url_tried`, and `html_text`.

### `lib/config.py`

Centralised configuration module loaded at import time. It defines all directory paths (`BASE_DIR`, `DATA_DIR`, `CACHE_DIR`, and subdirectories), creates them on startup, declares input/output file paths for both pipelines, loads API keys from the `.env` file (supporting up to four Gemini keys and two SerpAPI keys for rotation), sets model identifiers (`GEMINI_MODEL = "gemini-3-flash-preview"`, `OPENAI_MODEL = "gpt-5-mini"`), and configures rate limits (`OPENALEX_RATE_SEC = 0.1`, `UNPAYWALL_RATE_SEC = 0.5`, `GROBID_RATE_SEC = 3.0`, `LLM_RATE_SEC = 1.0`). All other modules import constants from `config` rather than reading environment variables directly.

### `lib/utils.py`

Provides two shared low-level helpers used throughout the codebase. `clean_doi(doi)` strips the `https://doi.org/` (or `http://doi.org/`) URL prefix from a DOI string using a regex substitution, returning the bare DOI for consistent comparison and cache-key generation. `cache_key(text)` computes an MD5 hex digest of the input string encoded as UTF-8, returning a fixed-length string safe for use as a filesystem filename component; it is the basis for all file-based caches across the pipeline (OpenAlex, PDF, LLM, GROBID).

### `state.py`

Holds shared mutable application state for the Flask server, imported by all route blueprints. It declares module-level DataFrames (`flora_df`, `cands_df`, `filtered_df` for Pipeline 1; `all_rep_df`, `multi_orig_df` for Pipeline 2), result dicts (`resolved` for Pipeline 1, `multi_orig_resolved` for Pipeline 2) each protected by a `threading.Lock()`, and a `validations` dict that maps `doi_r` to human validation records (`{status, comment, timestamp}`) persisted to `cache/validations.json`. All DataFrames are populated at startup by `app.py` and refreshed via API endpoints; blueprints read and write state through this module rather than through Flask's application context.

### `lib/multi_original.py`

The single-DOI orchestrator specifically for Pipeline 2 (Multiple Originals). Its public function `run_multi_original_for_doi(doi_r, all_rep_df, cands_df)` runs a condensed four-stage pipeline: loading base data from `all_replications.csv` (rather than the FLoRA sheet), re-querying OpenAlex for candidate originals, acquiring the PDF through the full 11-tier waterfall, extracting references via GROBID/pdfminer, and calling `identify_all_originals_with_llm()` to identify every replicated original study. Unlike Pipeline 1 there is no same-author/year or early abstract fast-path. The returned flat dict includes `is_false_positive`, `n_originals`, and `originals_json` (a JSON array of per-original dicts with rank, title, DOI, evidence, and confidence), which the disambiguation route saves in long format to `multi_original_resolved.csv`.
