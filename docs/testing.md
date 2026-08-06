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

50 test modules directly under `tests/`, plus 3 under `tests/live/`. The naming is
one module per seam: `test_engine_*.py` for the Stage 2 filter engine,
`test_snapshot_scan.py` / `test_pool_sync.py` / `test_fetch_abstracts.py` for
Stage 1, `test_extract.py` and its neighbours for Stage 3, `test_validate.py` for
the dashboard. `ls tests/` is the list — it is not reproduced here, because a
copied listing goes stale the first time a module is added.

There is no `pytest.ini`, `pyproject.toml` or `setup.cfg`, and no custom markers.
Selection is by path, `-k`, and the `skipif` guard on the live tests.

As of **2026-08-06**, `python -m pytest tests/ -q --co` collects **1,348 tests** in
under a second. A collection count far below that means an import is failing,
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
        vote = llm._classify_once("prompt", SCREENING_MODEL_1, SCREENING_EFFORT_1)
    assert vote["classification"] == "replication"
```

`_classify_once(prompt, model, effort)` takes no provider: the provider follows the
model id through `call_model`, and the vote is labelled with whichever one served it.

### Schema tests

Check that a CSV has all required columns:

```python
import pandas as pd
from shared.schema import validate_csv_columns

df = pd.read_csv("misc/sample_filtered.csv", encoding="utf-8-sig")
missing = validate_csv_columns(list(df.columns), "filtered")
assert not missing, f"Missing columns: {missing}"
```

`tests/test_schema_roundtrip.py` is where this lives for the checked-in samples in
`misc/`. It also holds each sample's categorical columns against the value sets in
`shared/schema.py`, which is what makes those sets a contract rather than a comment:
a category the pipeline starts writing has to be added to its set or the sample
carrying it fails.

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

## The rest of `tests/conftest.py`

Four more autouse fixtures, all of which a new test inherits without asking:

| Fixture | What it does |
| ------- | ------------ |
| `_no_retry_backoff` | Patches `time.sleep`, so a test that exercises the 1s/2s/4s retry ladder does not actually wait |
| `_token_usage_state_in_tmp` | Redirects `cache/token_usage.json` into a tmp dir, so a test run cannot spend a real day's budget on paper |
| `_abstract_store_in_tmp` | Same for `cache/abstracts.sqlite` |
| `_no_provider_throttle` | Removes the per-provider rate limit |

Plus the `app` / `client` fixtures for the Flask dashboard.

**A Stage 3 CSV fixture must carry `SCREEN_COLS`.** Stage 3 refuses an input whose
header has no `screen_verdict`, so a hand-written `filtered.csv` fixture is refused
unless it carries the screen block. `conftest.py` provides `SCREEN_PROCEED`,
`screen_cells(screen=None)` and `with_screen(csv_text, screen=SCREEN_PROCEED)` for
exactly that — use them rather than pasting six columns into every fixture.

## Tests that need pipeline data

None. `tests/test_analysis_overlap.py` and the `tests/test_apa_resolver.py` case
that read the gitignored `data/` CSVs (`candidates.csv`, `filtered.csv`,
`all_replications.csv`) went with the overlap analysis and the retired Stage 1
corpus. Every test now builds the files it reads under `tmp_path`, so the suite runs
on a fresh checkout with an empty `data/`.

One known failure as of 2026-08-06:
`tests/test_extract.py::TestRunExtract::test_rows_are_streamed_in_chunks_abstract_bearing_ones_first`
asserts an exact row order across chunk boundaries and does not get it. It fails on
`main` too — do not read it as a regression from whatever you just changed.
