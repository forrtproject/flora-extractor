# Stage 2 — Filter
**Input:** `data/candidates.csv`  
**Output:** `data/filtered.csv`  
**Run:** `python filter/run_filter.py`

---

## What This Stage Does

Takes the raw candidate pool from Stage 1 and produces a clean, classified set of genuine replication and reproduction studies. Three steps run in sequence:

1. **Deduplicate** — remove any remaining title-level duplicates that slipped through Stage 1 (cross-source collisions where DOIs differed slightly or were missing)
2. **Classify** — label each candidate as `replication`, `reproduction`, or `false_positive` using a fast rule-based classifier, followed by an LLM pass for uncertain cases only
3. **Characterise** — determine the original-study relationship type: one clear original (`single_original`), multiple OpenAlex candidates needing disambiguation (`multiple_match`), or paper genuinely targets several independent originals (`multiple_original`)

The goal is **high precision** — Stage 3 is expensive (PDF + LLM), so false positives waste compute.

---

## Pipeline Flow

```
data/candidates.csv
         │
         ▼
  Title deduplication (fuzzy, rapidfuzz ≥ 90)
         │
         ▼
  Rule-based classifier
  title_r + abstract_r → keyword patterns
         │
         ├── false_positive ─────────────────────────┐
         │                                           │
         ├── replication / reproduction              │
         │         │                                │
         └── needs_review                           │
                   │                                │
                   ▼                                │
        LLM classifier (Gemini Flash)               │
        assign filter_status                        │
         │                                          │
         ▼                                          │
  Original-match type classification                │
  abstract + OpenAlex references → Gemini Flash     │
  single_original | multiple_match | multiple_original
         │                                          │
         ▼                                          │
  data/filtered.csv  ◄────────────────────────────-┘
  (false_positives kept, filter_status = false_positive)
```

---

## Step 1 — Deduplication

Handled in `filter/rule_filter.py`. A second deduplication pass after Stage 1 catches cross-source title collisions where DOIs were missing or slightly mismatched.

**Pass 1 — DOI cleanup**  
Re-run `clean_doi()` on all rows. Any remaining exact DOI duplicates are collapsed, keeping the row with the richest metadata (most non-empty fields).

**Pass 2 — Fuzzy title match**  
For rows still without a DOI, `rapidfuzz.fuzz.token_sort_ratio` is computed between all remaining title pairs. Pairs scoring ≥ 90 are collapsed; the row with more metadata is kept.

---

## Step 2 — Classification (Replication / Reproduction / False Positive)

### Rule-based pass (`filter/rule_filter.py`)

Assigns `filter_status` using keyword patterns applied to `title_r` and `abstract_r`. Fast, no API calls.

**Replication indicators** (any match → `replication`):
- `"direct replication"`, `"close replication"`, `"replication of"`, `"replication study"`
- `"registered replication report"`, `"we replicated"`, `"attempts to replicate"`
- `"conceptual replication"`, `"pre-registered replication"`

**Reproduction indicators** (any match → `reproduction`):
- `"reproduction study"`, `"we reproduced"`, `"reproducibility of"`, `"reproduction of"`
- `"computational reproduction"`, `"reanalysis of"`, `"reproducibility check"`

**False positive indicators** (any match → `false_positive`):
- Title or abstract discusses replication as a topic without being a study: `"theory of replication"`, `"what is replication"`, `"improving replication"`, `"replication crisis"`, `"replication rate"`
- Review or meta-analysis that surveys replications without being one: `"review of replications"`, `"meta-analysis of replication"`

**Author-year pattern check:**  
If no author-year citation pattern (e.g. `Smith (2020)`) is found anywhere in `abstract_r`, the row is set to `needs_review`. Missing citation of a named original is a strong signal of a false positive.

### LLM pass (`filter/llm_filter.py`)

Applied **only** to rows where `filter_status = needs_review` from the rule pass. Uses `gemini-3-flash-preview`.

Prompt provides title and abstract; asks whether the paper is a replication, reproduction, or neither. Returns:
- `filter_status` — final classification
- `filter_evidence` — a quote from the abstract supporting the decision
- `filter_confidence` — 0.0–1.0

Responses cached to `cache/llm/` using `cache_key(doi_r + "_filter")`. Rate limit: 1s between calls (`LLM_RATE_SEC`).

---

## Step 3 — Original-Match Type Classification

For every paper that passes the filter (`filter_status` in `{replication, reproduction}`), classify how it relates to its original(s). This determines which Extract pipeline the paper enters in Stage 3.

### Three Types

| Value | Meaning |
|-------|---------|
| `single_original` | Paper clearly targets one specific original study |
| `multiple_match` | OpenAlex found 2–5 candidate originals with the same author/year — disambiguation needed in Stage 3 |
| `multiple_original` | Paper genuinely targets several distinct independent originals (a multi-replication paper) |

### How Classification Works

Uses abstract + OpenAlex referenced works via **Gemini Flash** (`gemini-3-flash-preview`).

1. Fetch the paper's referenced works from OpenAlex via `find_all_candidates()` from `shared/openalex_client.py`
2. Extract author-year citation patterns from `abstract_r` using `extract_author_year_patterns()` from `shared/openalex_client.py`
3. Call Gemini with: title, abstract, list of matched OpenAlex candidates, and count of distinct author-year patterns found
4. Model returns `original_match_type`, `original_match_confidence`, and a brief evidence phrase

**Heuristics used alongside LLM:**
- ≥ 3 distinct author-year citation patterns in abstract with no title overlap between candidates → likely `multiple_original`
- Exactly 1 candidate found in OpenAlex references and no umbrella paper guard triggered → `single_original`
- 2–5 candidates with same first-author + year → `multiple_match`

Cached to `cache/llm/` using `cache_key(doi_r + "_match_type")`.

If the OpenAlex referenced-works lookup fails, default to `single_original` (safe default for Stage 3 routing).

---

## Output Schema — `filtered.csv`

All columns from `candidates.csv`, plus:

| Column | Type | Description |
|--------|------|-------------|
| `filter_status` | str | `replication` \| `reproduction` \| `false_positive` \| `needs_review` |
| `filter_method` | str | `rule_based` \| `llm` \| `both` |
| `filter_evidence` | str | Phrase or quote that triggered classification |
| `filter_confidence` | float | 0.0–1.0 |
| `is_replication` | bool | True if filter_status == replication |
| `is_reproduction` | bool | True if filter_status == reproduction |
| `original_match_type` | str | `single_original` \| `multiple_match` \| `multiple_original` |
| `original_match_confidence` | float | 0.0–1.0 confidence of match type classification |

False positives are **included** in `filtered.csv` with `filter_status = false_positive` so Stage 3 can skip them cleanly. They are never deleted.

---

## Files

| File | Status | Description |
|------|--------|-------------|
| `filter/run_filter.py` | Stub | Orchestrator — reads candidates.csv, runs all steps, writes filtered.csv |
| `filter/rule_filter.py` | To implement | Deduplication, keyword classifier, author-year pattern check |
| `filter/llm_filter.py` | To implement | Gemini Flash classifier for `needs_review` rows + original-match type |

---

## What Needs to Be Implemented

- [ ] `deduplicate_candidates(df)` — second-pass title dedup (DOI dedup is done in Stage 1)
- [ ] `classify_with_rules(df)` — keyword pattern classifier returning `filter_status`, `filter_evidence`, `filter_confidence`
- [ ] `check_author_year_patterns(row)` — flag `needs_review` when no author-year citation pattern found
- [ ] `classify_with_llm(rows)` — Gemini Flash classifier for `needs_review` rows only
- [ ] `classify_original_match_type(row)` — OpenAlex re-query + Gemini classification returning `original_match_type` and `original_match_confidence`

---

## Rules (from RULEBOOK.md)

- Never delete false positives — set `filter_status = false_positive` and include in output
- LLM classification pass runs only on `needs_review` rows — do not call LLM for every paper
- Original-match type classification runs only on confirmed replications and reproductions
- All Gemini calls must be cached with `cache_key(doi_r + suffix)` before writing to disk
- Rate limit: 1s between Gemini calls (`LLM_RATE_SEC`)
- `original_match_type` must be one of: `single_original`, `multiple_match`, `multiple_original`
- If OpenAlex referenced-works lookup fails, set `original_match_type = single_original` as a safe default and log a warning

---

## Testing

```bash
python -c "
import pandas as pd
from shared.schema import FILTERED_COLS
df = pd.read_csv('misc/sample_filtered.csv')
missing = [c for c in FILTERED_COLS if c not in df.columns]
assert not missing, f'Missing columns: {missing}'
print('Schema OK —', len(df), 'rows')
"
```
