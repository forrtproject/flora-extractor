# Analysis Module Documentation

## Overview

The `analysis/` module provides non-breaking, read-only diagnostic tools for understanding pipeline performance and identifying improvement opportunities. All outputs are isolated to the `analysis/` directory.

---

## Task 4: Rule-Based Filtering & Original Linking Analysis

### Phase 1: Audit & Comparison

**Purpose:** Analyze current rule-based filtering and original linking performance, identify gaps, and rank improvement opportunities.

**Output Files:**
- `extraction_audit.md` — Current extraction state by link method and confidence
- `gap_vs_extracted_comparison.csv` — Comparison of Task 2 gaps vs. extracted.csv
- `rule_improvement_opportunities.csv` — Ranked improvement recommendations

**Usage:**
```python
from analysis.rule_analysis import run_phase1_analysis

results = run_phase1_analysis()
# Generates:
# - analysis/extraction_audit.md
# - analysis/gap_vs_extracted_comparison.csv
# - analysis/rule_improvement_opportunities.csv
```

**Key Functions:**
- `audit_extracted_csv()` — Returns dict with extraction statistics
- `analyze_link_method_distribution()` — Breakdown by link method
- `analyze_confidence_distribution()` — Breakdown by confidence level
- `find_missing_doi_rows()` — Identify rows where doi_o is missing
- `compare_task2_gaps_with_extracted()` — Cross-check Task 2 findings
- `generate_improvement_opportunities()` — Ranked improvement list

### Phase 2: Improvements (Standalone Work)

Based on Phase 1 outputs, improvements can be made to:
- `filter/rule_filter.py` — Enhanced author-year extraction
- `extract/link_original.py` — URL-based DOI recovery, citation scoring
- `extract/run_extract.py` — Match-type classification refinement

Phase 2 work should be guided by `rule_improvement_opportunities.csv`.

---

## Task 6: APA Reference Resolver

### Purpose

Resolve replications without DOI by:
1. Querying CrossRef API (title + first author + year)
2. Falling back to manual CSV for unresolved papers
3. Formatting results as APA-style references

### Output Files

- `missing_dois_resolved.csv` — All replications without DOI + resolved metadata + APA references
- `apa_resolver_report.md` — Summary of resolution success rates
- `apa_reference_fallback.csv` — Manual reference entries (user-populated)

### Usage

```python
from analysis.apa_resolver import run_apa_resolution

output_path = run_apa_resolution(email="your-email@example.com")
# Generates:
# - analysis/missing_dois_resolved.csv
# - analysis/apa_resolver_report.md
```

**Key Functions:**
- `load_missing_dois()` — Load replications without DOI from filtered.csv + extracted.csv
- `query_crossref()` — Query CrossRef API for a paper
- `format_apa_reference()` — Format metadata as APA reference
- `load_fallback_csv()` — Load manual reference CSV
- `fuzzy_match_csv()` — Fuzzy match against fallback CSV
- `resolve_all()` — Run full tiered resolution
- `run_apa_resolution()` — Orchestrator function

### Fallback CSV Format

File: `analysis/apa_reference_fallback.csv`

```csv
title_r,authors_r,year_r,doi_r,apa_reference,source
"Example Study Title","Smith, A., & Jones, B.",2023,10.xxxx/yyyy,"Smith, A., & Jones, B. (2023). Example study title. Journal Name.",manual
```

Users can manually add entries for papers that CrossRef doesn't find.

### CrossRef Integration

- **Endpoint:** `https://api.crossref.org/works`
- **Rate limit:** 1 query per 0.1 seconds (to be respectful)
- **No API key required** (but email required in User-Agent)
- **Retry:** Automatic fallback if API unavailable

---

## Testing

### Unit Tests

```bash
# Test rule analysis
python -m pytest tests/test_rule_analysis.py -v

# Test APA resolver
python -m pytest tests/test_apa_resolver.py -v

# Run both
python -m pytest tests/test_rule_analysis.py tests/test_apa_resolver.py -v
```

### Test Coverage

- ✅ Data loading (filtered.csv, extracted.csv, all_replications.csv)
- ✅ Audit functions (by method, confidence, missing DOI)
- ✅ Comparison logic (gaps vs. extraction)
- ✅ APA formatting (single author, multiple authors, no journal)
- ✅ Fallback CSV loading and matching

---

## Integration with Main Pipeline

**Non-Breaking:** All analysis is read-only:
- ✅ No modifications to `data/` directory files
- ✅ No modifications to pipeline code
- ✅ No modifications to existing test suites
- ✅ All outputs isolated to `analysis/` directory

**Dependencies:**
- Task 4 Phase 1: Requires `extracted.csv`, `filtered.csv`, `all_replications.csv` (from pipeline)
- Task 6: Requires `filtered.csv`, `extracted.csv` (from pipeline)

**Outputs feed into:**
- Task 4 Phase 2: Improvements to filter/extract rules
- Task 3: Stage 1 redesign (informed by gap analysis)
- Task 5: Model benchmarking (after improvements)

---

## Environment

**Required Libraries:**
- pandas (for CSV processing)
- requests (for CrossRef API)
- fuzzywuzzy (optional, for fuzzy matching fallback)

**Configuration:**
- Uses `shared.config.DATA_DIR` for input files
- Logs via `shared.config.log`

---

## Notes

- CrossRef API is free and widely used. No authentication required beyond a User-Agent.
- Fallback CSV allows gradual population of manual references for edge cases.
- All functions are cached-friendly (use same utilities as main pipeline).
- Rate limiting prevents API overload and is respectful to CrossRef infrastructure.
