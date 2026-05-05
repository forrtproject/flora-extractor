# `examples/` — runnable walkthroughs

| Script                | What it does                                                         |
|-----------------------|----------------------------------------------------------------------|
| `pipeline_example.bat`| Windows: Stage 1 (sample or live) → Stage 2 (rule + LLM filter), with detailed progress and a final breakdown of `filter_status` counts. Stages 3 + 4 are described but not auto-run. |
| `pipeline_example.sh` | Bash mirror of the above; identical Python entry points.            |

Env-var knobs:

| Variable          | Default | Effect                                                  |
|-------------------|---------|---------------------------------------------------------|
| `LIVE_SEARCH`     | `0`     | `1` → call OpenAlex / S2 / I4R live; `0` → use bundled sample. |
| `YEAR_FROM`/`YEAR_TO` | `2023`/`2024` | Forwarded to Amy's per-source scripts on live runs. |
| `OUT_DIR`         | `data`  | Where Stage 1/2 outputs go (gitignored).                |
| `OPENALEX_API_KEY`| (unset) | Required for live search.                              |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | (unset) | LLM uplift is a no-op without one.       |

## What's exercised

**Stage 1** is intentionally small — by default it just copies
`misc/sample_candidates.csv` to `data/candidates.csv`. The sample contains
four rows that hit every Stage-2 path: a clear replication, a
reproducibility study, an obvious DNA-replication false positive, and a
multi-original case.

**Stage 2** runs `python -m filter.run_filter`, which:

1. Loads `filter/spec/exclusion-patterns.yaml`.
2. Applies `apply_rule_filter` (phrase detection + author-year cite gate
   + non-scholarly exclusion).
3. Applies `apply_llm_filter` to anything left as `needs_review`. With no
   API key the LLM step is a no-op.

The script then prints the `filter_status` breakdown and the first five
rows of the result so you can see what the rules and LLM actually decided.

## Live mode

```bash
export LIVE_SEARCH=1
export YEAR_FROM=2023
export YEAR_TO=2023        # tight window for rate-limit safety
export OPENALEX_API_KEY=...
bash examples/pipeline_example.sh
```

This calls Amy's per-source scripts (post-`cde352c` they accept
`--from-year`/`--to-year`). For the OR-bundled SciMeto-engine version,
switch to `feature/search` and run `examples/discover_example.bat`.

## Where to look next

- `docs/scimeto_filter_port.md` — function-level reference for the filter port.
- `filter/spec/README.md` — exclusion-patterns YAML hand-off contract.
- `RULEBOOK.md` §Filter — the team's policy for what counts as
  `replication` vs `needs_review` vs `false_positive`.
