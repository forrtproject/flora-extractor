# RULEBOOK — FLoRA Extractor

**For all team members and AI coding agents.**  
Read this before writing any code. These rules ensure the four pipeline stages integrate cleanly.

---

## Golden Rules (All Teams)

1. **Python only.** No R, no JavaScript in pipeline stages.
2. **CSV is the contract.** Every stage reads one CSV and writes one CSV. Never skip this.
3. **Column names are frozen.** Adding a new column is fine. Renaming or removing a column breaks downstream stages — coordinate with all teams first.
4. **Never modify `shared/`.** `shared/` is owned by Team Extract. If you need a shared function, ask Team Extract to add it to `shared/utils.py`.
5. **Never commit to `main` or `dev` directly.** Always use your feature branch and open a PR.
6. **Always use `clean_doi()`** from `shared/utils.py` when reading or writing DOI values.
7. **Always cache API responses** using `cache_key()` from `shared/utils.py`. Never call an API twice for the same input.
8. **Use sample CSVs** from `misc/` to develop and test your stage. Do not wait for another team's output.

---

## Team Responsibilities

### Team Search — `feature/search`
**Owns:** `search/` folder only  
**Input:** None (fetches from external APIs and websites)  
**Output:** `data/candidates.csv`

Rules:
- Never add columns to `candidates.csv` that aren't in `shared/schema.py:CANDIDATES_COLS`
- Always clean DOIs with `clean_doi()` before writing
- Cross-check against `data/flora_entry_sheet.csv` — skip DOIs already in FLoRA
- Deduplication: match on `doi_r` first, then fuzzy title match (threshold 0.9, use `rapidfuzz`)
- Respect API rate limits: OpenAlex = 10 requests/second max (use 0.1s sleep between calls)
- Bob Reed and I4R scrapers must handle HTTP errors gracefully (try/except, log and continue)

### Team Filter — `feature/filter`
**Owns:** `filter/` folder only  
**Input:** `data/candidates.csv`  
**Output:** `data/filtered.csv`

Rules:
- Never call the LLM for papers that are clearly `false_positive` from rules — save API quota
- Rule filter runs FIRST. LLM filter only for `filter_status = needs_review`
- Always populate `filter_evidence` — never leave it empty if you set a status
- A paper must have an explicit replication phrase AND a specific author-year citation to pass as `replication`
- Vague phrases like "we replicate prior findings on X" are `false_positive` — no specific target named
- `original_match_type = multiple_original` only when abstract contains 2+ distinct cited author-year patterns AND explicit multi-study language ("Study 1", "Study 2", "Experiments 1–3")
- `original_match_type = multiple_match` when OpenAlex finds 2–5 candidates with the same author/year requiring disambiguation
- `original_match_type = single_original` in all other cases (also the safe default when OpenAlex lookup fails)

### Team Extract — `feature/extract`
**Owns:** `extract/` folder + `shared/` folder  
**Input:** `data/filtered.csv`  
**Output:** `data/extracted.csv`

Rules:
- `shared/` modules must not change their public API (function signatures, return types)
- `extract/multi_original.py` is ported but has known flaws — improve detection and false-positive logic
- Multi-original case: expand to N rows. Each row has `original_rank` = 1, 2, 3... and `n_originals` = N
- Single-original case: `original_rank = 1`, `n_originals = 1`
- `link_method = target_pending` means no original was found — do NOT leave `doi_o` empty without setting this
- Always populate `link_evidence` — the quote or pattern used to make the decision
- `outcome = pending` when outcome extraction failed, not when you didn't try

### Team Validate — `feature/validate`
**Owns:** `validate/` folder only  
**Input:** `data/extracted.csv` (via `import_csv.py`)  
**Output:** `data/validated.csv`

Rules:
- `import_csv.py` must handle missing columns gracefully (use `.get()`, not direct dict access)
- Reviewer identity uses session cookie username — no login system required for hackathon
- One vote per reviewer per record (enforce in the database with a unique constraint on reviewer_id + record_id)
- A record becomes `confirmed` when ≥2 votes AND majority are `confirm`
- A record becomes `rejected` when ≥2 votes AND majority are `reject`
- The `validated.csv` export must include ALL columns from `extracted.csv` plus the validation columns
- The `/batch` and `/multi-originals` routes are ported from the existing pipeline — do not rewrite the pipeline logic inside them

---

## CSV Schema — Quick Reference

### `candidates.csv` columns
```
doi_r, title_r, abstract_r, year_r, authors_r, journal_r, url_r, openalex_id_r, source
```

### `filtered.csv` adds
```
filter_status, filter_method, filter_evidence, filter_confidence,
is_replication, is_reproduction, original_match_type, original_match_confidence
```

### `extracted.csv` adds
```
doi_o, title_o, year_o, authors_o,
link_method, link_evidence, link_confidence,
outcome, outcome_phrase, outcome_confidence, out_quote_source,
type, original_rank, n_originals
```

### `validated.csv` adds
```
validation_status, vote_count, confirm_votes, reject_votes, validator_notes
```

Full column definitions with types: see `shared/schema.py`

---

## API Keys and Models

```
GEMINI_MODEL  = "gemini-3-flash-preview"   # primary LLM — free tier
OPENAI_MODEL  = "gpt-5-mini"               # fallback only
```

- Use **Gemini first** for all LLM calls (free tier, generous quota)
- Use **multiple Gemini API keys** (GEMINI_API_KEY, GEMINI_API_KEY_2, etc.) — the `llm_client.py` rotates them automatically
- OpenAI is fallback only — do not call it unless Gemini fails
- Never hardcode API keys — always use `os.environ.get()` or load from `.env`

---

## Testing Rules

- Every stage orchestrator (`run_search.py`, `run_filter.py`, `run_extract.py`) must run successfully on `misc/sample_*.csv`
- Tests go in `tests/test_<stage>.py`
- Test at minimum: happy path with 5 rows from sample CSV
- Do not test external APIs in unit tests — mock them
- Before opening a PR: run your stage end-to-end with the sample CSV and confirm the output CSV is valid

---

## Adding a New CSV Column

1. Add the column to `shared/schema.py` with type and description
2. Update your stage's orchestrator to write the new column
3. Update `misc/sample_*.csv` to include the new column
4. Post in your team's PR description: "Added column `X` to `filtered.csv`"
5. Downstream teams must update their code to handle the new column before merging

---

## Error Handling Rules

- Catch exceptions at **API call boundaries** only
- On API failure: log the error, set confidence to 0.0, set status to `needs_review`, continue
- Never silently swallow exceptions inside business logic
- Always log which DOI caused a failure — use `log.error("[%s] failed: %s", doi_r, e)`

---

## Git Commit Message Format

```
feat(search): add Bob Reed list scraper
fix(filter): handle missing abstract in rule classifier
port(shared): move openalex_client from OpenAlexLLM
improve(extract): fix multi-original false positive detection
```

---

## What "Done" Means

A task is done when:
1. Your stage runs end-to-end on `misc/sample_*.csv` without errors
2. The output CSV has the correct columns (validate against `shared/schema.py`)
3. At least one test passes in `tests/`
4. The PR is open against `dev` with a description of what was built
