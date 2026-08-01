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
    │       'needs_review' rows are written through unchanged — Stage 3's
    │       front-door screen is the validated decider of "is this a
    │       replication at all"
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

**No LLM step.** Stage 2 is deterministic: `needs_review` rows are written through
as they are and reach Stage 3, whose two-model front-door screen decides them. When
that screen passes a row, `run_extract` overwrites `filter_status` with the screen's
paper type and sets `filter_method = "screen"`. `llm` and `both` are historical
values from the retired Stage 2 escalation.

## `filter_confidence` values

`high | medium | low` — categorical, not a float. A 3-level label is more actionable than a continuous probability from a single LLM call.

## Key functions

| Function | File | Description |
|----------|------|-------------|
| `apply_rules()` | `filter/rule_filter.py` | Rule-based classification |
| `run_filter()` | `filter/run_filter.py` | Main orchestrator, chunked read |
