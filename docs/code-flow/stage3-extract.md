# Stage 3: Extract — Code Flow

**Entry point:** `python -m extract.run_extract`

## What it does

For each filtered paper, finds:
1. The original study it replicates (`doi_o`, `title_o`, `link_method`)
2. The replication outcome (`outcome`, `outcome_phrase`)

Streams results to `data/extracted.csv` one row at a time.

## Routing

```
run_extract.py
    │
    ├── load filtered.csv
    ├── skip rows already in extracted.csv (or extracted-test.csv with --extracted-test)
    ├── skip rows already in validated Supabase (if SUPABASE_URL configured)
    │
    └── for each row:
            classify_match_type(row) → original_match_type
            │
            ├── if multiple_original:
            │       run_multi_original_for_doi(doi_r) → list of originals
            │       expand to N rows (original_rank = 1, 2, 3...)
            │
            └── if single_original or multiple_match:
                    run_for_doi(doi_r) → one original study
                    extract_outcome(doi_r) → outcome
                    write one row to extracted.csv
```

## Match type classification (`classify_match_type`)

```
classify_match_type(row)
    │
    ├── count citation patterns in abstract (author-year patterns)
    ├── call OpenAlex to find candidate originals (find_all_candidates)
    │
    └── if 1 candidate: single_original
        if multiple candidates: call LLM to choose
            LLM returns: single_original | multiple_match | multiple_original
        on failure: default to single_original
```

## Original study linking (`link_original.py`)

```
run_for_doi(doi_r)
    │
    ├── fetch PDF (pdf_sources.py waterfall):
    │       arXiv → OSF → Unpaywall → CORE → direct DOI URL
    │
    ├── parse PDF (pdf_parsing.py):
    │       parse_all() → {openalex_xml, pdfminer, grobid, docpluck, opendataloader, markitdown}
    │       best_parse_result() → pick winner by score
    │
    ├── find candidates (openalex_client.py):
    │       extract_author_year_patterns(abstract)
    │       find_all_candidates(patterns) → list of candidate originals
    │
    ├── disambiguation (disambiguation.py):
    │       resolve_same_author_year(candidates, abstract + intro)
    │           if single candidate: return immediately
    │           else: Jaccard score title similarity
    │
    └── if unresolved: call LLM with abstract/fulltext
            prompt includes: title, abstract, fulltext excerpt, candidates
            LLM returns: doi_o, title_o, evidence, confidence
            cache result
```

## Outcome extraction (`code_outcome.py`)

```
extract_outcome(doi_r, abstract_r, fulltext, title_o)
    │
    ├── keyword_scan(abstract + fulltext):
    │       look for outcome phrase patterns
    │       returns: outcome, outcome_phrase, out_quote_source
    │
    └── if keyword scan inconclusive:
            call LLM with outcome prompt
                prompt includes: abstract, fulltext excerpt, original study title
                LLM returns: outcome, outcome_phrase, outcome_confidence
                cache result
            if LLM fails: outcome = cannot_be_determined
```

## PDF parse scoring

```
score = refs × 300 + abstract_len + intro_len × 2 + min(raw_text_len ÷ 5, 1000)
```

Winner's `abstract + intro` is fed to the outcome LLM. Structured references (for citation pattern matching) come from the winning parser's output.

## Test sandbox

With `--extracted-test`, all output goes to `data/extracted-test.csv`. DOIs already in `extracted.csv` are skipped (so test runs don't re-process production rows).

Promote with:
```bash
python -m extract.promote_test --all           # promote all
python -m extract.promote_test --doi 10.xxx/y  # promote one DOI
python -m extract.promote_test --all --dry-run # preview
```

## Key functions

| Function | File | Description |
|----------|------|-------------|
| `run_extract()` | `extract/run_extract.py` | Main orchestrator |
| `classify_match_type()` | `extract/run_extract.py` | Routing step |
| `run_for_doi()` | `extract/link_original.py` | Single-original pipeline |
| `run_multi_original_for_doi()` | `extract/multi_original.py` | Multi-original pipeline |
| `extract_outcome()` | `extract/code_outcome.py` | Outcome extraction |
| `find_all_candidates()` | `shared/openalex_client.py` | Candidate search |
| `resolve_same_author_year()` | `shared/disambiguation.py` | Disambiguation |
| `parse_all()` | `shared/pdf_parsing.py` | Run all PDF parsers |
| `best_parse_result()` | `shared/pdf_parsing.py` | Score and pick winner |
