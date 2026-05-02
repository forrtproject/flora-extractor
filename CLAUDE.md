# CLAUDE.md — FLoRA Extractor

This file is the primary instruction document for AI coding agents (Claude Code, Cursor, Copilot, etc.) and for human contributors. Read it fully before writing any code.

Other agent runtimes: see [AGENTS.md](AGENTS.md), which points here.

---

## What This Project Does

**FLoRA Extractor** discovers, extracts, and validates replication and reproduction studies for the [FLoRA database](https://forrt.org/replication-hub/flora/). Starting from keyword searches of academic databases, it identifies which paper each replication targets, what the outcome was, and exports the results for entry into FLoRA.

The pipeline produces structured records identifying:

1. Which original target study a replication targets
2. What the replication result was (success / failure / mixed / uninformative / descriptive)

---

## Architecture — 4 Stage Pipeline

```text
Stage 1: search/     → discovers candidate papers → data/candidates.csv
Stage 2: filter/     → removes false positives    → data/filtered.csv
Stage 3: extract/    → finds original + outcome   → data/extracted.csv
Stage 4: validate/   → Flask web app with voting  → data/validated.csv
```

Each stage reads one CSV and writes a richer CSV. Stages are independently runnable:

```bash
python search/run_search.py
python filter/run_filter.py
python extract/run_extract.py
python -m validate.import_csv      # load into SQLite
python -m validate.app             # starts the web app on port 5001
```

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
| `shared/llm_client.py`         | Gemini + OpenAI calls with key rotation, prompt builders, JSON parsing      |
| `shared/pdf_sources.py`        | Multi-tier PDF acquisition waterfall (arXiv → OSF → Unpaywall → CORE → …)   |
| `shared/grobid.py`             | GROBID reference extraction from PDFs                                       |
| `shared/disambiguation.py`     | Same-author/year candidate disambiguation — needs validation                |
| `shared/utils.py`              | `clean_doi()`, `cache_key()`, common helpers                                |
| `shared/config.py`             | All paths, env var loading, rate limits                                     |
| `shared/schema.py`             | CSV column definitions — the contract between pipeline stages               |
| `shared/cache.py`              | Cache read/write/clear helpers                                              |

### `search/` — Stage 1

| File                         | Purpose                                                                     |
| ---------------------------- | --------------------------------------------------------------------------- |
| `search/openalex_search.py`  | Query OpenAlex API for papers with replication keywords                     |
| `search/external_lists.py`   | Bob Reed list scraper, I4R list scraper (pluggable — see Stage 1 docs)      |
| `search/deduplicate.py`      | Merge sources, deduplicate by DOI + fuzzy title, cross-check FLoRA sheet    |
| `search/run_search.py`       | Orchestrator: calls all sources, writes `data/candidates.csv`               |

### `filter/` — Stage 2

| File                     | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `filter/rule_filter.py`  | Rule-based classifier: keyword patterns, author-year check     |
| `filter/llm_filter.py`   | LLM classifier for uncertain cases only                        |
| `filter/run_filter.py`   | Orchestrator: reads candidates.csv, writes filtered.csv        |

### `extract/` — Stage 3

| File                       | Purpose                                                                      |
| -------------------------- | ---------------------------------------------------------------------------- |
| `extract/run_extract.py`   | Orchestrator: classifies match type, routes to single or multi-original      |
| `extract/link_original.py` | Single-original pipeline (ported from OpenAlexLLM)                           |
| `extract/multi_original.py`| Multi-original pipeline — finds all target studies (needs improvement)       |
| `extract/code_outcome.py`  | Keyword + LLM outcome extraction (new — not yet ported)                      |

### `validate/` — Stage 4 (Flask web app)

| File                           | Purpose                                                             |
| ------------------------------ | ------------------------------------------------------------------- |
| `validate/app.py`              | Flask entry point, `create_app()` factory, blueprint registration   |
| `validate/import_csv.py`       | Load `flora_selected.csv` into SQLite (run once before starting)    |
| `validate/models.py`           | SQLAlchemy models: Replication, Vote tables                         |
| `validate/routes/review.py`    | `GET /validate`, `POST /vote`, `GET /api/validate/log`              |
| `validate/routes/flora.py`     | `GET /flora` master list, API endpoints                             |
| `validate/routes/dashboard.py` | `GET /dashboard`, `GET /api/dashboard/stats`                        |
| `validate/routes/export.py`    | `GET /export`, `POST /api/export/download`                          |

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

The authoritative schema definition is in **`shared/schema.py`**. The summary below is for orientation; if there is any discrepancy, `schema.py` wins.

Never change a column name without updating `schema.py` and notifying all teams.

### `data/candidates.csv` (Stage 1 → Stage 2)

```text
doi_r, title_r, abstract_r, year_r, authors_r, journal_r,
url_r, openalex_id_r, source
```

`source` values: `openalex | bob_reed | i4r | semantic_scholar | ...`

### `data/filtered.csv` (Stage 2 → Stage 3)

All candidates.csv columns, plus:

```text
filter_status      — replication | reproduction | false_positive | needs_review
filter_method      — rule_based | llm | both
filter_evidence    — phrase or quote that triggered classification
filter_confidence  — high | medium | low  (categorical)
```

`filter_confidence` is categorical, not a continuous float. A single LLM call cannot reliably produce calibrated probabilities; a three-level label is more honest and easier to act on.

### `data/extracted.csv` (Stage 3 → Stage 4)

All filtered.csv columns, plus:

```text
original_match_type       — single_original | multiple_match | multiple_original
original_match_confidence — high | medium | low
doi_o, title_o, year_o, authors_o
link_method    — author_year_match | llm_abstract | llm_fulltext | target_pending | api_error
link_evidence
link_confidence           — high | medium | low
outcome        — success | failure | mixed | uninformative | descriptive | pending | api_error
outcome_phrase, outcome_confidence, out_quote_source
type           — replication | reproduction
original_rank  — 1 for single; 1,2,3... for multi-original papers
n_originals
```

`original_match_type` is determined by Stage 3 as its first routing step — not by Stage 2.

`outcome = descriptive`: paper replicated methods in a different context but does not test the original claim. Include in extraction; flag for review during validation.

`api_error`: set when extraction failed after retries. Reviewers see these in the Validate tab.

### `data/validated.csv` (Stage 4 output)

All extracted.csv columns, plus:

```text
validation_status  — confirmed | rejected | pending | needs_review
vote_count, confirm_votes, reject_votes, validator_notes
validated_doi_o    — reviewer-corrected original DOI (blank = accepted unchanged)
validated_outcome  — reviewer-corrected outcome (blank = accepted unchanged)
```

`validated_doi_o` and `validated_outcome` enable accuracy measurement by diffing against the extracted values.

---

## Stage 3 Routing Logic

Stage 3 determines `original_match_type` itself as its first step, then routes accordingly:

```python
# run_extract.py:

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

Use `cache_key()` from `shared/utils.py` to get a stable hash for a given input, then use `shared/cache.py` to read and write:

```python
from shared.utils import cache_key
from shared.cache import read_cache, write_cache

key = cache_key(doi_r + "_filter")    # unique per call type
cached = read_cache(key)
if cached is None:
    result = call_api(...)
    write_cache(key, result)
else:
    result = cached
```

Cache files are stored in `cache/` (gitignored). They persist across runs; clear manually if you need fresh data.

---

## Error Handling on API Failures

On any API call failure (LLM, OpenAlex, CrossRef):

1. Log the error with the DOI and error code.
2. Retry up to **3 times** with exponential backoff: 1 s, 2 s, 4 s.
3. After 3 failures: set the relevant field to `api_error` (e.g. `outcome = api_error`, `link_method = api_error`) and continue to the next record — do not crash the pipeline.
4. This produces an `api_error` status that reviewers can see in the Validate tab, distinct from `pending` (not yet processed).

---

## Code Style Rules

1. **Python primary.** Type hints on all function signatures.
2. **R is welcome.** Teams may implement individual stage functions in R, provided input/output CSV schemas are identical. Include equivalent test cases. We can help translate to Python later if needed.
3. **No unnecessary abstractions.** Three similar lines is fine; don't create a helper unless it's used three or more times.
4. **Comments:** Default to no comments. Add one only when the WHY is non-obvious — a hidden constraint, a threshold that was empirically chosen, a workaround for a specific API quirk. File-level docstrings should be a short paragraph explaining what the file does and why it exists, not just a list of functions.
5. **Error handling only at system boundaries** (API calls, file I/O). Don't wrap internal logic in try/except.
6. **All CSV writes use `utf-8-sig` encoding** (BOM, Excel-compatible).
7. **All DOIs pass through `clean_doi()`** from `shared/utils.py` before writing or comparing.
8. **All API responses must be cached** using the pattern above before any result is used.
9. **Rate limiting:** OpenAlex: 0.1 s between calls. Gemini: 1 s between calls. OpenAI: 0.5 s.

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
    with patch("filter.llm_filter.call_gemini") as mock_gemini:
        mock_gemini.return_value = {"filter_status": "replication", ...}
        result = classify_with_llm(sample_row)
    assert result["filter_status"] == "replication"
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
GEMINI_API_KEY_2=...                  # optional: key rotation for higher quota
OPENAI_API_KEY=...                    # optional fallback LLM
GROBID_URL=http://localhost:8070      # default; override if GROBID runs elsewhere
GEMINI_MODEL=gemini-2.0-flash         # override to use a different model
OPENAI_MODEL=gpt-4o-mini              # override to use a different model
```

GROBID is optional. If `GROBID_URL` points to a server that is not running, the PDF extraction step logs a warning and falls back to abstract-only processing. It does not crash.

---

## Git Workflow

```text
main     ← protected; PR + 1 review required; no direct commits
  └── dev     ← integration branch; protected; PR required
        ├── feature/search
        ├── feature/filter
        ├── feature/extract
        └── feature/validate
```

- Branch from `dev`, PR back to `dev`.
- **Open PRs when a feature is stable, not just at the end.** Partial, working functionality is better to merge than a giant branch at deadline.
- `data/` and `cache/` are gitignored — add sample files to `misc/` instead.
- Branch protection rules are enforced on both `main` and `dev`.

---

## What Is Already Done

The following are implemented and working (ported from the *OpenAlexLLM* prototype — an internal earlier-generation pipeline for the same task):

- All of `shared/` — but see caveats above about semantic validation
- `validate/` — Stage 4 app is fully implemented and running
- `validate/import_csv.py` — imports `data/flora_selected.csv` (107 rows) into SQLite

"Ported from OpenAlexLLM" means the code was adapted from a private prototype that was built for an earlier round of FLoRA extraction. It does what it claims to do but predates this project's test suite and schema definitions.

## What Needs to Be Written

- `search/openalex_search.py` — new
- `search/external_lists.py` — new (Bob Reed + I4R scrapers; see Stage 1 docs for pluggable pattern)
- `filter/rule_filter.py` — new
- `filter/llm_filter.py` — new
- `extract/code_outcome.py` — new
- `extract/run_extract.py` — new orchestrator (includes match-type classification as first step)
- `extract/multi_original.py` — ported but needs significant improvement

---

## Seeding With Existing Data

The following CSVs from prior FLoRA extraction work can be used to skip Stages 1–2:

- `data/openalex_candidates.csv` — confirmed replications with OpenAlex metadata
- `data/all_replications.csv` — full known replication set from all pathways
- `data/flora_entry_sheet.csv` — use for deduplication in Stage 1 (skip DOIs already in FLoRA)
- `data/flora_selected.csv` — 107 rows already loaded into the Stage 4 app

These files are in `data/` on the shared drive. If you are setting up from scratch and the files are not present, contact the project leads.

Stages 1–2 are only needed for discovering new replications not yet in these files.
