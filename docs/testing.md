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

# What is actually there, right now
python -m pytest tests/ -q --co
```

Coverage needs `pytest-cov`, which is **not** in `requirements.txt` — install it
first (`pip install pytest-cov`) if you want `--cov=. --cov-report=html`.

## Test layout

44 test modules directly under `tests/`, plus 4 under `tests/live/`. The naming is
one module per seam: `test_engine_*.py` for the Stage 2 filter engine,
`test_search*.py` for Stage 1, `test_extract.py` and its neighbours for Stage 3,
`test_validate.py` for the dashboard. `ls tests/` is the list — it is not
reproduced here, because a copied listing goes stale the first time a module is
added.

As of **2026-08-04**, `python -m pytest tests/ -q --co` collects **1,105 tests** in
about half a second. A collection count far below that means an import is failing,
not that tests were deleted.

## Writing new tests

### Mock all external calls

Never make live API calls in unit tests. Use `unittest.mock.patch`:

```python
from unittest.mock import patch

def test_classify_replication():
    with patch("shared.llm_client.call_gemini") as mock:
        mock.return_value = ({"classification": "replication", "confident": True,
                              "categories": ["clearly_declared"],
                              "evidence_quote": "", "reasoning": ""}, None)
        vote = llm._classify_once("prompt", "gemini")
    assert vote["classification"] == "replication"
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

## The no-network guard

`tests/conftest.py` has an autouse fixture that patches `socket.socket.connect`
(and `connect_ex`) to raise, so any test that escapes its mocks and opens a real
connection fails immediately instead of silently calling a live API. It stands
down for tests under `tests/live/` and whenever `TEST_LIVE_API` is set.

If a test trips the guard, mock at the boundary the code actually calls
(`requests.get`, the client function) rather than exempting the test.

## Tests that need pipeline data

`tests/test_analysis_overlap.py` and one test in `tests/test_apa_resolver.py`
read the gitignored CSVs in `data/` (`candidates.csv`, `filtered.csv`,
`all_replications.csv`). They skip when those files are absent, so a fresh
checkout runs green.
