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
python -m search.run_search         # Stage 1 → data/candidates.csv
python -m filter.run_filter         # Stage 2 → data/filtered.csv
python -m extract.run_extract       # Stage 3 → data/extracted.csv  (streamed row-by-row)
python -m validate.import_csv       # load extracted.csv into SQLite
python -m validate.app              # Stage 4 web app → http://localhost:5001
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

The web app provides tabbed views for each stage's output:

- `/search`        — Stage 1 candidates (candidates.csv)
- `/filter`        — Stage 2 filtered list (filtered.csv)
- `/extract`       — Stage 3 extraction results with model comparison tool
- `/extract-test`  — Stage 3 test sandbox (extracted-test.csv) with per-row Promote button
- `/validate`      — Stage 4 voting queue

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
| `shared/pdf_parsing.py`        | Six PDF parse methods (openalex_xml, pdfminer, GROBID, docpluck, opendataloader, markitdown); `parse_all()` orchestrator; `score_parse_result()`, `best_parse_result()`, `best_parse_method_name()` scoring API |
| `shared/grobid.py`             | GROBID reference extraction from PDFs                                       |
| `shared/disambiguation.py`     | Same-author/year candidate disambiguation — needs validation                |
| `shared/utils.py`              | `clean_doi()`, `cache_key()`, common helpers                                |
| `shared/config.py`             | All paths, env var loading, rate limits; `MARKITDOWN_CACHE_DIR = cache/markdown/` |
| `shared/schema.py`             | CSV column definitions — the contract between pipeline stages               |
| `shared/cache.py`              | Cache read/write/clear helpers                                              |

### `search/` — Stage 1

| File                         | Purpose                                                                     |
| ---------------------------- | --------------------------------------------------------------------------- |
| `search/openalex_search.py`  | Query OpenAlex API for papers with replication keywords                     |
| `search/external_lists.py`   | Bob Reed list scraper, I4R list scraper (pluggable — see Stage 1 docs)      |
| `search/deduplicate.py`      | Merge sources, deduplicate by DOI + fuzzy title, cross-check FLoRA sheet    |
| `search/run_search.py`       | Orchestrator: calls all sources, appends to `data/candidates.csv` via index |

### `filter/` — Stage 2

| File                     | Purpose                                                                        |
| ------------------------ | ------------------------------------------------------------------------------ |
| `filter/rule_filter.py`  | Rule-based classifier: keyword patterns, author-year check                     |
| `filter/llm_filter.py`   | LLM classifier for uncertain cases only                                        |
| `filter/run_filter.py`   | Orchestrator: reads candidates.csv in 50k-row chunks, streams to filtered.csv  |

### `extract/` — Stage 3

| File                       | Purpose                                                                      |
| -------------------------- | ---------------------------------------------------------------------------- |
| `extract/run_extract.py`   | Orchestrator: classifies match type, routes to single or multi-original; supports `--extracted-test` flag; `_best_fulltext_from_cache()` feeds the best-scoring parse result to the outcome LLM |
| `extract/link_original.py` | Single-original pipeline; runs `parse_all()` on the PDF, scores all methods, uses the winner's text for the DOI-resolution LLM; uses shared `best_parse_result()` scoring |
| `extract/multi_original.py`| Multi-original pipeline — finds all target studies (needs improvement)       |
| `extract/code_outcome.py`  | Keyword + LLM outcome extraction                                             |
| `extract/promote_test.py`  | CLI + library: merge rows from extracted-test.csv into extracted.csv; `--all`, `--doi`, `--dry-run`, `--force` |

### `validate/` — Stage 4 (Flask web app)

| File                           | Purpose                                                             |
| ------------------------------ | ------------------------------------------------------------------- |
| `validate/app.py`                  | Flask entry point, `create_app()` factory, blueprint registration                          |
| `validate/import_csv.py`           | Load `flora_selected.csv` into SQLite (run once before starting)                           |
| `validate/models.py`               | SQLAlchemy models: Replication, Vote tables                                                |
| `validate/routes/review.py`        | `GET /validate`, `POST /vote`, `GET /api/validate/log`                                     |
| `validate/routes/flora.py`         | `GET /flora` master list, API endpoints                                                    |
| `validate/routes/dashboard.py`     | `GET /dashboard`; CSV stats including link-method breakdown, model family breakdown (Gemini/GPT/Qwen), and full Extract Test section |
| `validate/routes/export.py`        | `GET /export`, `POST /api/export/download`                                                 |
| `validate/routes/extract_view.py`  | Blueprint factory (`make_extract_blueprint` + `add_shared_routes`) for Extract + Extract Test tabs; PDF availability column; Promote endpoint; parse winner badge via `best_parse_method_name` |

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
S2_API_KEY=...                        # optional: Semantic Scholar API key (Stage 1)
GROBID_URL=http://localhost:8070      # default; override if GROBID runs elsewhere
GEMINI_MODEL=gemini-3-flash-preview   # primary Gemini model
GEMINI_HEAVY_MODEL=gemini-3-flash-preview  # used for DOI resolution (defaults to GEMINI_MODEL)
OPENAI_MODEL=gpt-5-mini               # OpenAI fallback
FILTER_OPENAI_MODEL=gpt-5-mini        # Stage 2 filter primary model
GEMINI_USE_FLEX=true                  # 50% cost reduction; requires paid GEMINI_API_KEY
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

## PDF Parsing — How the Best Parser Is Selected

`parse_all()` in `shared/pdf_parsing.py` runs six methods and returns a dict keyed by method name. Both `link_original.py` (DOI resolution) and `_get_outcome` in `run_extract.py` (outcome extraction) call `best_parse_result()` to pick the winner:

```text
score = refs × 300  +  abstract_len  +  intro_len × 2  +  min(raw_text_len ÷ 5, 1000)
```

The winner's `abstract + intro` is fed to the LLM. Structured references (for citation pattern matching) come from whichever method the winner is — if MarkItDown wins but has sparse references, the LLM prompt's reference section will be thin; this is acceptable because citation matching runs as a rule-based step before the LLM fires.

Parse results are cached at `cache/parse/parse_{key}.json`. MarkItDown's raw `.md` output is additionally cached at `cache/markdown/{key}.md` (human-readable). The detail panel in the web app shows a **★ USED BY LLM** badge on the winning column plus each method's score.

If a row's parse cache exists but is missing the `markitdown` key (written before MarkItDown was added), the detail panel runs MarkItDown lazily on first open and updates the cache.

---

## What Is Already Done

The following are implemented and working (ported from the *OpenAlexLLM* prototype — an internal earlier-generation pipeline for the same task):

- All of `shared/` — but see caveats above about semantic validation
- `validate/` — Stage 4 app is fully implemented and running
- `validate/import_csv.py` — imports `data/flora_selected.csv` (107 rows) into SQLite

"Ported from OpenAlexLLM" means the code was adapted from a private prototype that was built for an earlier round of FLoRA extraction. It does what it claims to do but predates this project's test suite and schema definitions.

### Additional features implemented (June 2026)

- **Extract Test sandbox** — `--extracted-test` flag, `promote_test` CLI, Extract Test web tab with Promote button, target-pending rows visible in Extract Test tab only
- **PDF availability column** — Available / Not Available / Not Needed badge in both Extract and Extract Test tables, with server-side filter
- **Parse scoring + MarkItDown** — six parse methods, unified scoring formula used by both DOI resolution and outcome extraction, winner badge in UI
- **Dashboard enhancements** — link-method breakdown, LLM model family breakdown (Gemini / GPT / Qwen), and full Extract Test stats section
- **Index-based dedup for Stage 1 + 2** — `cache/candidates_index.txt` and `cache/filtered_index.txt` replace full-CSV loads; Stage 1 appends new rows only; Stage 2 reads candidates.csv in 50k-row chunks; both stages support `--rebuild-index` to force a fresh index build

## What Needs to Be Written

All core pipeline modules are now implemented. Known gaps:

- `tests/live/` directory — mentioned in `tests/test_filter.py` but not yet created; live LLM integration tests should go here, guarded by `TEST_LIVE_API=1`
- Unit tests for standalone scripts: `search/sensitivity_check.py`, `extract/mix_for_validation.py`, `validate/csv_to_db.py`
- Unit tests for orchestrators: `search/deduplicate.py`, `filter/run_filter.py` (currently tested only indirectly)
- Unit tests for `extract/promote_test.py` promote logic (currently smoke-tested only)

---

## Seeding With Existing Data

The following CSVs from prior FLoRA extraction work can be used to skip Stages 1–2:

- `data/openalex_candidates.csv` — confirmed replications with OpenAlex metadata
- `data/all_replications.csv` — full known replication set from all pathways
- `data/flora_entry_sheet.csv` — use for deduplication in Stage 1 (skip DOIs already in FLoRA)
- `data/flora_selected.csv` — 107 rows already loaded into the Stage 4 app

These files are in `data/` on the shared drive. If you are setting up from scratch and the files are not present, contact the project leads.

Stages 1–2 are only needed for discovering new replications not yet in these files.
