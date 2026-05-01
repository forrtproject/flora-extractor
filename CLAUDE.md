# CLAUDE.md — FLoRA Extractor

This file is the primary instruction document for AI coding agents (Claude Code, Cursor, Copilot, etc.).
Read this fully before writing any code. No superpowers or plugins required.

---

## What This Project Does

**FLoRA Extractor** is a Python tool that discovers, extracts, and validates replication and reproduction studies for the FLoRA/FReD academic database. It takes academic paper DOIs as input and outputs structured records identifying:
1. Which original study a replication targets
2. What the replication outcome was (success / mixed / failure)

---

## Architecture — 4 Stage Pipeline

```
Stage 1: search/     → discovers candidate papers → data/candidates.csv
Stage 2: filter/     → removes false positives    → data/filtered.csv
Stage 3: extract/    → finds original + outcome   → data/extracted.csv
Stage 4: validate/   → Flask web app with voting  → data/validated.csv
```

Each stage reads one CSV and writes a richer CSV. Run each stage independently:
```bash
python search/run_search.py
python filter/run_filter.py
python extract/run_extract.py
python validate/app.py          # starts the web app on port 5001
```

---

## Module Map — What Each File Does

### `shared/` — DO NOT REWRITE. These are ported from a working pipeline.
| File | Purpose |
|------|---------|
| `shared/openalex_client.py` | OpenAlex API: author-year pattern extraction, candidate fetch, referenced works |
| `shared/llm_client.py` | Gemini + OpenAI calls with key rotation, prompt builders, JSON parsing |
| `shared/pdf_sources.py` | 11-tier PDF acquisition waterfall (arXiv → OSF → Unpaywall → ...) |
| `shared/grobid.py` | GROBID reference extraction from PDFs |
| `shared/disambiguation.py` | Same-author/year disambiguation (fast, no PDF needed) |
| `shared/utils.py` | `clean_doi()`, `cache_key()`, common helpers |
| `shared/config.py` | All paths, env var loading, model names, rate limits |
| `shared/schema.py` | CSV column definitions — the contract between pipeline stages |
| `shared/cache.py` | Cache read/write/clear helpers |

### `search/` — Stage 1
| File | Purpose |
|------|---------|
| `search/openalex_search.py` | Query OpenAlex API for papers with replication keywords |
| `search/external_lists.py` | Scrape Bob Reed list, I4R list; import SCORE CSV |
| `search/deduplicate.py` | Merge sources, deduplicate by DOI, cross-check against FLoRA entry sheet |
| `search/run_search.py` | Orchestrator: calls all sources, writes `data/candidates.csv` |

### `filter/` — Stage 2
| File | Purpose |
|------|---------|
| `filter/rule_filter.py` | Rule-based classifier: keyword patterns, author-year presence check |
| `filter/llm_filter.py` | Gemini classifier for uncertain cases only |
| `filter/run_filter.py` | Orchestrator: reads `data/candidates.csv`, writes `data/filtered.csv` |

### `extract/` — Stage 3
| File | Purpose |
|------|---------|
| `extract/run_extract.py` | Orchestrator: routes each DOI to single or multi-original path |
| `extract/link_original.py` | 7-stage single-original pipeline (ported from OpenAlexLLM) |
| `extract/multi_original.py` | Multi-original pipeline — finds all originals (NEEDS IMPROVEMENT) |
| `extract/code_outcome.py` | Keyword + LLM outcome extraction (new — not yet ported) |

### `validate/` — Stage 4 (Flask web app)
| File | Purpose |
|------|---------|
| `validate/app.py` | Flask entry point, registers blueprints, loads startup data |
| `validate/import_csv.py` | Load `extracted.csv` into SQLite (run once before starting app) |
| `validate/models.py` | SQLAlchemy models: Originals, Replications, Reproductions, Votes |
| `validate/state.py` | In-memory batch state (ported) |
| `validate/routes/review.py` | `GET /review` voting queue, `POST /vote` |
| `validate/routes/batch.py` | `GET /batch` SSE batch pipeline runner (ported) |
| `validate/routes/multi_originals.py` | `GET /multi-originals` pipeline UI (ported) |
| `validate/routes/input.py` | `GET /input` data generation page (ported) |
| `validate/routes/export.py` | `GET /export` CSV/XLSX/PDF export (ported) |
| `validate/routes/dashboard.py` | `GET /dashboard` stats overview (ported) |

### `misc/` — Reference only, do not import
| File | Purpose |
|------|---------|
| `misc/openalex_api_example.py` | Standalone example: how to call OpenAlex API |
| `misc/gemini_api_example.py` | Standalone example: how to call Gemini API |
| `misc/sample_candidates.csv` | 20-row sample for Stage 1 output testing |
| `misc/sample_filtered.csv` | 20-row sample for Stage 2 output testing |
| `misc/sample_extracted.csv` | 20-row sample for Stage 3 output testing |

---

## CSV Schema — The Contract Between Stages

**This is the most important section. Never change a column name without updating schema.py and notifying all teams.**

### `data/candidates.csv` (Stage 1 → Stage 2)
```python
doi_r           # str   — DOI, cleaned (no https://doi.org/ prefix)
title_r         # str   — paper title
abstract_r      # str   — abstract text
year_r          # int   — publication year
authors_r       # str   — semicolon-separated author list
journal_r       # str   — journal name
url_r           # str   — open access URL
openalex_id_r   # str   — OpenAlex work ID (e.g. W2741809807)
source          # str   — openalex | bob_reed | i4r | score | semantic_scholar
```

### `data/filtered.csv` (Stage 2 → Stage 3)
All columns from candidates.csv, plus:
```python
filter_status             # str   — replication | reproduction | false_positive | needs_review
filter_method             # str   — rule_based | llm | both
filter_evidence           # str   — phrase that triggered classification
filter_confidence         # float — 0.0–1.0
is_replication            # bool  — True if confirmed replication
is_reproduction           # bool  — True if confirmed reproduction
original_match_type       # str   — single_original | multiple_match | multiple_original
original_match_confidence # float — 0.0–1.0 confidence of match type classification
```

### `data/extracted.csv` (Stage 3 → Stage 4)
All columns from filtered.csv, plus:
```python
doi_o               # str   — original study DOI
title_o             # str   — original study title
year_o              # int   — original study publication year
authors_o           # str   — original study authors
link_method         # str   — author_year_match | llm_abstract | llm_fulltext | target_pending
link_evidence       # str   — quote or pattern used for linking
link_confidence     # float — 0.0–1.0
outcome             # str   — success | failure | mixed | uninformative | pending
outcome_phrase      # str   — supporting quote from the paper
outcome_confidence  # float — 0.0–1.0
out_quote_source    # str   — abstract | fulltext | title
type                # str   — replication | reproduction
original_rank       # int   — 1 for single; 1,2,3... for multi-original papers
n_originals         # int   — total originals in this paper (1 for single)
```

### `data/validated.csv` (Stage 4 output)
All columns from extracted.csv, plus:
```python
validation_status   # str  — confirmed | rejected | pending | needs_review
vote_count          # int  — total votes received
confirm_votes       # int  — confirm votes
reject_votes        # int  — reject votes
validator_notes     # str  — aggregated reviewer comments
```

---

## Stage 3 Linking Logic — Three Cases

```python
# run_extract.py decides which path per DOI:

if row['original_match_type'] == 'multiple_original':
    # Case C: study replicates N originals
    results = run_multi_original_for_doi(doi_r, ...)
    # → expand to N rows in extracted.csv (original_rank = 1, 2, 3...)
    # → if is_false_positive: treat as single_original
else:
    # Case A/B: single_original or multiple_match (shared pipeline)
    result = run_for_doi(doi_r, ...)
    # → 1 row in extracted.csv (original_rank = 1, n_originals = 1)
```

---

## LLM Models — Use Exactly These

```python
GEMINI_MODEL = "gemini-3-flash-preview"   # primary for all LLM calls
OPENAI_MODEL = "gpt-5-mini"               # fallback only
```

Never change model names. Never assume a model doesn't exist.

---

## Code Style Rules

1. **Python only.** Type hints on all function signatures.
2. **No unnecessary abstractions.** Three similar lines is fine; don't create a helper.
3. **No comments** unless the WHY is non-obvious.
4. **Error handling only at system boundaries** (API calls, file I/O). Don't wrap internal logic.
5. **All CSV writes use `utf-8-sig` encoding** (BOM, Excel-compatible).
6. **All DOIs pass through `clean_doi()`** from `shared/utils.py` before use.
7. **All API responses cached** to `cache/` using `cache_key()` from `shared/utils.py`.
8. **Rate limiting:** OpenAlex: 0.1s between calls. Gemini: 1s between calls. OpenAI: 0.5s.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in:
```
RESEARCHER_EMAIL=you@example.com      # for API politeness headers
GEMINI_API_KEY=...                    # rotate multiple keys: GEMINI_API_KEY_2, _3, etc.
OPENAI_API_KEY=...                    # fallback only
GROBID_URL=http://localhost:8070      # local GROBID server (optional)
```

---

## Git Workflow

```
main          ← protected, PR + 1 review required
  └── dev     ← integration branch
        ├── feature/search
        ├── feature/filter
        ├── feature/extract
        └── feature/validate
```

- Never commit directly to `main` or `dev`
- Always branch from `dev`, PR back to `dev`
- `data/`, `cache/` are gitignored (add sample files to `misc/` instead)

---

## What Is Already Done (Ported from OpenAlexLLM)

The `shared/` folder contains working, tested code. When implementing stages:
- **Do not rewrite** `shared/` modules
- Import them: `from shared.openalex_client import find_all_candidates`
- If you need a new shared utility, add it to `shared/utils.py` and tell all teams

## What Needs to Be Written (New Code)

- `search/openalex_search.py` — new
- `search/external_lists.py` — new (Bob Reed, I4R scrapers)
- `filter/rule_filter.py` — new
- `filter/llm_filter.py` — new
- `extract/code_outcome.py` — new
- `extract/run_extract.py` — new orchestrator
- `validate/models.py` — new SQLAlchemy schema
- `validate/import_csv.py` — new CSV→SQLite importer
- `validate/routes/review.py` — new voting UI
- `extract/multi_original.py` — ported BUT needs improvement (known flaws in detection logic)

---

## Running the Full Pipeline

```bash
# 1. Setup
cp .env.example .env          # fill in API keys
pip install -r requirements.txt

# 2. Run each stage
python search/run_search.py                    # → data/candidates.csv
python filter/run_filter.py                    # → data/filtered.csv
python extract/run_extract.py                  # → data/extracted.csv

# 3. Load into validation app
python validate/import_csv.py                  # → loads SQLite
python validate/app.py                         # → http://localhost:5001
```

---

## Seeding With Existing Data

The following CSVs from the existing `flora_search_approaches` pipeline can be used to bootstrap Stage 3 (skipping Stages 1-2):
- `openalex_candidates.csv` — already-confirmed replications with OpenAlex metadata
- `all_replications.csv` — full known replication set from all pathways
- `flora_entry_sheet.csv` — use for deduplication in Stage 1 (skip DOIs already in FLoRA)

Place these in `data/` when available. Stages 1-2 are only needed for discovering NEW replications not yet in these files.
