# SciMeto Classifier — Stage 2 (filter) port

This document is a team-facing reference for the Stage-2 filter that was
ported from SciMeto's deterministic classifier in commit `4b81b10` on
2026-05-04. Read this if you are tuning a regex, adding a phrase, debugging
why a row landed in `needs_review`, or wiring up a new source.

## TL;DR

- **What is it?** A two-pass Stage-2 classifier: a rule-based phrase /
  exclusion / author-year-cite filter, followed by an optional Gemini /
  OpenAI uplift on rows the rules left as `needs_review`.
- **Why?** SciMeto's classifier had been running in production and is well-
  validated against FReD ground truth. Porting it (rather than reinventing)
  gives Stage 2 the same precision behaviour as SciMeto's Lookup tab.
- **Where did it come from?**
  `apps/worker/src/services/replication/phraseDetection.ts` and
  `classifier.ts` in the SciMeto repo. The `extractTargets` /
  `resolveTarget` / `classifyOutcome` pieces of the SciMeto classifier
  belong to *Stage 3* in flora-extractor's vocabulary, so they live in
  `extract/` (different team) — only the phrase-detection and exclusion
  pieces are ported here.

## File map

```
filter/
├── __init__.py
├── spec/
│   ├── exclusion-patterns.yaml         ← copied verbatim from SciMeto
│   └── README.md                        ← spec hand-off contract
├── phrase_detection.py                  ← REPLICATION_PHRASES, REPRODUCTION_PHRASES,
│                                          NON_SCHOLARLY_REPLICATION_CONTEXTS
├── rule_filter.py                       ← apply_rule_filter()
├── llm_filter.py                        ← apply_llm_filter()
└── run_filter.py                        ← orchestrator (candidates.csv → filtered.csv)
```

## Function reference

### `filter.phrase_detection`

Module-level constants:

| Name | Type | Purpose |
|------|------|---------|
| `REPLICATION_PHRASES` | `list[re.Pattern]` | 19 case-insensitive regexes ported line-for-line from SciMeto's `phraseDetection.ts`. |
| `REPRODUCTION_PHRASES` | `list[re.Pattern]` | Subset that should result in `filter_status == 'reproduction'` rather than `'replication'` when no other replication phrase fires. |
| `NON_SCHOLARLY_REPLICATION_CONTEXTS` | `list[(id, re.Pattern)]` | Loaded from `filter/spec/exclusion-patterns.yaml` at import time. |

Functions:

| Function | Purpose |
|----------|---------|
| `is_non_scholarly_context(text)` | Return the matched exclusion id, or `None`. |
| `has_replication_phrase(text)` | True iff a replication phrase matches AND no exclusion fires. |
| `find_replication_phrase(text)` | Lowercase first matching phrase, or `None`. |
| `is_reproduction_only(text)` | True if every phrase that matches is in `REPRODUCTION_PHRASES`. |

### `filter.rule_filter`

`apply_rule_filter(df) → pd.DataFrame` adds the four `FILTER_ADDED_COLS` to a
candidates DataFrame:

| Column | Set to | When |
|--------|--------|------|
| `filter_status` | `false_positive` (high) | Title+abstract matches an exclusion pattern. |
| | `false_positive` (high) | No replication phrase detected at all. |
| | `replication` (high) | Replication phrase + author-year cite present. |
| | `reproduction` (high) | Reproduction-only phrase + author-year cite. |
| | `needs_review` (medium) | Phrase present but no specific author-year cite. |
| `filter_method` | `rule_based` | Always (LLM filter may overwrite to `llm` or `both` later). |
| `filter_evidence` | a short string | Either `phrase:<phrase>; cite:<raw>` or `exclusion:<id>` or `phrase:<phrase>; no author-year cite`. |
| `filter_confidence` | `high`/`medium`/`low` | Categorical, per RULEBOOK §Filter. Float confidences are intentionally avoided. |

Author-year extraction reuses
`shared.openalex_client.extract_author_year_patterns` so the cite gate
matches what Stage 3 will expect downstream.

### `filter.llm_filter`

`apply_llm_filter(df) → pd.DataFrame` only touches rows whose
`filter_status == 'needs_review'`. For each such row:

1. Build a JSON-mode prompt with title + abstract.
2. Look up cached verdict at `cache_key("filter|{title}|{abstract}")` in
   `LLM_CACHE_DIR`.
3. If miss, call `call_gemini(prompt)`; on failure call `call_openai(prompt)`.
4. Validate the returned `filter_status` against `VALID_STATUSES`; coerce
   anything unknown to `needs_review` and log a warning.
5. Validate `filter_confidence` against `VALID_CONFIDENCE`; default to `low`.
6. Truncate `filter_evidence` at 240 chars.
7. Update the row in place; flip `filter_method` from `rule_based` to `both`.

If no LLM key is set, the function logs a warning and **returns the DataFrame
unchanged** rather than fabricating verdicts. This keeps offline runs honest.

### `filter.run_filter`

The orchestrator. Reads `data/candidates.csv` (`utf-8-sig`), reindexes to
`CANDIDATES_COLS`, runs `apply_rule_filter` then `apply_llm_filter`,
reindexes to `FILTERED_COLS`, writes `data/filtered.csv` (`utf-8-sig`).

```bash
python -m filter.run_filter
```

## How a row gets classified — worked examples

| Title / abstract excerpt | Outcome | Why |
|--------------------------|---------|-----|
| "We replicated Smith (2010) ..." | `replication` / `high` | Phrase + cite both present. |
| "We replicate prior findings ..." (no cite) | `needs_review` / `medium` | Phrase present, no specific target — RULEBOOK rule 4. |
| "We tested the reproducibility of Brown (2018) ..." | `reproduction` / `high` | Only reproduction-flavoured phrase fires; cite present. |
| "DNA replication forks in eukaryotes ..." | `false_positive` / `high` | `BIOLOGICAL` exclusion pattern fires before the phrase check. |
| "On consumer choice in supermarkets ..." | `false_positive` / `high` | No replication phrase detected. |

## Hand-off contract with SciMeto

The exclusion-patterns YAML is the **only** file that travels both ways
between SciMeto and this repo. The phrase-detection regexes are kept inline
in `phrase_detection.py` because the JS-style regex constructs SciMeto uses
(e.g. inline character-class alternations) don't round-trip cleanly through
YAML. If the SciMeto team updates `phraseDetection.ts`, the agreed flow is:

1. They open a PR in this repo updating `filter/phrase_detection.py` and
   `filter/spec/exclusion-patterns.yaml`.
2. The PR includes the corresponding test cases from
   `apps/worker/src/services/replication/__tests__/phraseDetection.test.ts`.
3. Stage 2 lead reviews against `tests/test_filter.py` and merges.

## Tests

`tests/test_filter.py` covers:

- Phrase detection positive / negative.
- DNA exclusion fires before phrase check.
- Code/data exclusion ditto.
- Reproduction-only detection.
- Mixed phrases NOT flagged as reproduction-only.
- Rule filter end-to-end on a 4-row DataFrame: replication / reproduction
  / needs_review / false_positive paths plus the column-emission contract.

Run them directly (the project's `tests/conftest.py` requires Flask, which
isn't needed for the filter port):

```bash
python - <<'PY'
import sys; sys.path.insert(0, '.')
from tests import test_filter as t
import inspect
for name, fn in inspect.getmembers(t, inspect.isfunction):
    if name.startswith('test_'):
        fn(); print('PASS', name)
PY
```

## Pointers

- Spec hand-off: [`filter/spec/README.md`](../filter/spec/README.md)
- Walkthrough scripts: [`examples/pipeline_example.bat`](../examples/pipeline_example.bat)
                     · [`examples/pipeline_example.sh`](../examples/pipeline_example.sh)
- Stage 1 engine port (sister branch): [`docs/scimeto_engine_port.md` on `feature/search`](../docs/scimeto_engine_port.md)
- Upstream: `apps/worker/src/services/replication/phraseDetection.ts` and `classifier.ts` in SciMeto.
