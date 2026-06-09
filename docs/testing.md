# Testing

## Running tests

```bash
# All unit tests (no live API calls)
python -m pytest tests/

# Verbose output
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_extract.py -v

# Run by keyword
python -m pytest tests/ -k "outcome"

# With coverage
python -m pytest tests/ --cov=. --cov-report=html
```

## Test layout

```
tests/
├── conftest.py                  — Flask app + client fixtures
├── test_a_cli_flags.py          — CLI flag behaviour (--no-llm, --extracted-test)
├── test_analysis_overlap.py     — analysis/ data loader + gap analysis
├── test_apa_resolver.py         — APA reference resolver
├── test_b_title_pattern.py      — title-pattern matching for original study linking
├── test_c_openalex_xml.py       — OpenAlex XML fulltext fetcher
├── test_d_pdf_parsing.py        — PDF parse methods + scoring
├── test_disambiguation.py       — Jaccard scoring + same-author/year resolution
├── test_extract.py              — Stage 3 orchestrator + outcome extraction
├── test_filter.py               — Stage 2 rule filter
├── test_multi_original.py       — Multi-original pipeline
├── test_openalex_client.py      — OpenAlex API wrapper + find_all_candidates
├── test_rule_analysis.py        — Filter rule analysis
├── test_search.py               — Stage 1 search + OpenAlex pagination
├── test_search_engine.py        — Search engine spec + keyword expansion
├── test_supabase_client.py      — Supabase monitoring client (all mocked)
└── test_validate.py             — Monitoring app routes (Flask test client)
```

## Writing new tests

### Mock all external calls

Never make live API calls in unit tests. Use `unittest.mock.patch`:

```python
from unittest.mock import patch

def test_classify_replication():
    with patch("filter.llm_filter.call_gemini") as mock:
        mock.return_value = {"filter_status": "replication", ...}
        result = classify_with_llm(sample_row)
    assert result["filter_status"] == "replication"
```

### Schema tests

Check that a CSV has all required columns:

```python
import pandas as pd
from shared.schema import validate_csv_columns

df = pd.read_csv("misc/sample_filtered.csv")
missing = validate_csv_columns(list(df.columns), "filtered")
assert not missing, f"Missing columns: {missing}"
```

### Live API tests

Place live tests in `tests/live/` and guard with:

```python
import os
import pytest

@pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run"
)
def test_openalex_live():
    ...
```

Run with:
```bash
TEST_LIVE_API=1 python -m pytest tests/live/
```

## Known pre-existing failures

These tests fail due to a behaviour change in `extract/code_outcome.py` (the fallback outcome was changed from `uninformative` to `cannot_be_determined`). They are pre-existing failures unrelated to the dashboard/validation refactor:

- `test_a_cli_flags.py::TestNoLlmExtractOutcome::test_no_llm_skips_llm_and_returns_uninformative_when_no_keyword`
- `test_extract.py::TestExtractOutcome::test_llm_failure_returns_uninformative`
- `test_extract.py::TestExtractOutcome::test_invalid_llm_outcome_normalised`
- `test_extract.py::TestRunExtract::test_false_positives_pass_through_unchanged`

Two `test_search.py` tests fail because the `fake_get()` mock is missing the `headers` keyword argument added to `openalex_search.py`'s `_get_page()` function. Update the mock signature to fix:

```python
def fake_get(url, params, headers=None, timeout=30):
    ...
```
