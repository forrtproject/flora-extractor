
# Stage 2 — Filter

**Input:** `data/candidates.csv`
**Output:** `data/filtered.csv`
**Run:**

```bash
python -m filter.run_filter
```

Results are viewable in the **Filter** tab of the Stage 4 web app (`http://localhost:5001/filter`).

---

## What This Stage Does

Takes the raw candidate pool from Stage 1 and produces a clean, classified set of genuine replication and reproduction studies. Two steps run in sequence:

1. **Deduplicate** — remove any remaining title-level duplicates that slipped through Stage 1 (cross-source collisions where DOIs differed slightly or were missing)
2. **Classify** — label each candidate as `replication`, `reproduction`, or `false_positive` using a fast rule-based classifier, followed by an LLM pass for uncertain cases only

The goal is **high precision** — Stage 3 is expensive (PDF + LLM), so false positives waste compute.

---

## Pipeline Flow

```text
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
         │                                           │
         └── needs_review                            │
                   │                                 │
                   ▼                                 │
        LLM classifier                               │
        assign filter_status                         │
         │                                           │
         ▼                                           │
  data/filtered.csv  ◄────────────────────────────--┘
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

Applied **only** to rows where `filter_status = needs_review` from the rule pass.

Prompt provides title and abstract; asks whether the paper is a replication, reproduction, or neither. Returns:

- `filter_status` — final classification
- `filter_evidence` — a quote from the abstract supporting the decision
- `filter_confidence` — `high` / `medium` / `low`

Responses cached to `cache/llm/` using `cache_key(doi_r + "_filter")`. Rate limit: 1s between calls (`LLM_RATE_SEC`).

**Error handling:** Retry up to 3 times with exponential backoff (1s, 2s, 4s). After 3 failures, set `filter_status = needs_review` and `filter_confidence = low` so the row is not silently lost.

---

## Output Schema — `filtered.csv`

All columns from `candidates.csv`, plus:

| Column              | Type | Description                                                        |
| ------------------- | ---- | ------------------------------------------------------------------ |
| `filter_status`     | str  | `replication` / `reproduction` / `false_positive` / `needs_review` |
| `filter_method`     | str  | `rule_based` / `llm` / `both`                                      |
| `filter_evidence`   | str  | Phrase or quote that triggered classification                      |
| `filter_confidence` | str  | `high` / `medium` / `low`                                          |

False positives are **included** in `filtered.csv` with `filter_status = false_positive` so Stage 3 can skip them cleanly. They are never deleted.

---

## Files

| File                    | Status       | Description                                                 |
| ----------------------- | ------------ | ----------------------------------------------------------- |
| `filter/run_filter.py`  | Stub         | Orchestrator — reads candidates.csv, runs steps, writes CSV |
| `filter/rule_filter.py` | To implement | Deduplication, keyword classifier, author-year pattern check|
| `filter/llm_filter.py`  | To implement | LLM classifier for `needs_review` rows                      |

---

## What Needs to Be Implemented

- [ ] `deduplicate_candidates(df)` — second-pass title dedup (DOI dedup is done in Stage 1)
- [ ] `classify_with_rules(df)` — keyword pattern classifier returning `filter_status`, `filter_evidence`, `filter_confidence`
- [ ] `check_author_year_patterns(row)` — flag `needs_review` when no author-year citation pattern found
- [ ] `classify_with_llm(rows)` — LLM classifier for `needs_review` rows only

---

## Rules

- Never delete false positives — set `filter_status = false_positive` and include in output
- LLM classification pass runs only on `needs_review` rows — do not call LLM for every paper
- All LLM calls must be cached with `cache_key(doi_r + suffix)` before writing to disk
- Rate limit: 1s between LLM calls (`LLM_RATE_SEC`)
- Retry up to 3 times with exponential backoff on API errors; never silently drop a row
- `filter_confidence` must be one of: `high`, `medium`, `low`

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
