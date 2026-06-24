# Stage 2: Filter — Code Flow

**Entry point:** `python -m filter.run_filter`

## What it does

Classifies each candidate paper as `replication`, `reproduction`, `false_positive`, or `needs_review`. Streams results to `data/filtered.csv`.

## Step-by-step

```
run_filter.py
    │
    ├── load filtered index (cache/filtered_index.txt)
    │       If missing: build from existing filtered.csv in 50k-row chunks
    │
    ├── read candidates.csv in 50k-row chunks
    │       apply year filter (--from-year, --to-year)
    │       apply source filter
    │
    ├── for each chunk:
    │       rule_filter.apply_rules(row) → (status, method, evidence, confidence)
    │           check replication keyword patterns (see filter/rule_filter.py)
    │           check for citation (author-year pattern in abstract/title)
    │           check exclusion patterns (dna, source code, etc.)
    │           → 'replication' / 'reproduction' / 'false_positive' with high confidence
    │           → 'needs_review' with medium/low confidence for uncertain cases
    │
    │       If 'needs_review' (and --no-llm not set):
    │           llm_filter.classify_with_llm(row) → updates status + confidence
    │               call_llm() with filter prompt
    │               cache result by DOI
    │               merge LLM result with rule result
    │
    │       skip rows already in filtered index
    │       write to filtered.csv (append mode after first write)
    │       update filtered index
    │
    └── summary stats
```

## Classification logic

**Rule-based classifier** (`filter/rule_filter.py`):

1. Check title + abstract for replication/reproduction keyword phrases
2. Exclude papers with exclusion patterns (dna, computer code, etc.)
3. Check for at least one author-year citation (e.g. "Smith (2018)") → `high` confidence
4. Without citation: `medium` or `low` confidence → `needs_review`

**LLM classifier** (`filter/llm_filter.py`):

Only called for `needs_review` rows. Sends title + abstract to an LLM with a binary prompt. Result cached by DOI. Sets `filter_method = "llm"` (or `"both"` when rule also fired).

## `filter_confidence` values

`high | medium | low` — categorical, not a float. A 3-level label is more actionable than a continuous probability from a single LLM call.

## Key functions

| Function | File | Description |
|----------|------|-------------|
| `apply_rules()` | `filter/rule_filter.py` | Rule-based classification |
| `classify_with_llm()` | `filter/llm_filter.py` | LLM classification for uncertain rows |
| `run_filter()` | `filter/run_filter.py` | Main orchestrator, chunked read |
