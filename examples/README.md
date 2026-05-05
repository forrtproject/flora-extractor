# `examples/` — runnable walkthroughs

| Script                  | What it does                                                         |
|-------------------------|----------------------------------------------------------------------|
| `pipeline_example.bat`  | Windows: Stage 1 (sample or live) → Stage 2 (rule + LLM filter), with detailed progress and a final breakdown of `filter_status` counts. |
| `pipeline_example.sh`   | Bash mirror of the above; identical Python entry points.            |
| `discover_example.bat`  | Windows: four Stage 1 (search) demo runs using the SciMeto engine. Optionally runs Stage 2. |
| `discover_example.sh`   | Bash mirror of the above; identical Python entry points.            |

---

## `pipeline_example` — Stage 1 + Stage 2 walkthrough

Env-var knobs:

| Variable                            | Default       | Effect                                                        |
|-------------------------------------|---------------|---------------------------------------------------------------|
| `LIVE_SEARCH`                       | `0`           | `1` → call OpenAlex / S2 / I4R live; `0` → use bundled sample. |
| `YEAR_FROM` / `YEAR_TO`             | `2023`/`2024` | Forwarded to the per-source scripts on live runs.             |
| `OUT_DIR`                           | `data`        | Where Stage 1/2 outputs go (gitignored).                      |
| `OPENALEX_API_KEY`                  | (unset)       | Required for live search.                                     |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | (unset)       | LLM uplift is a no-op without one.                            |

**Stage 1** is intentionally small — by default it copies
`misc/sample_candidates.csv` to `data/candidates.csv`. The sample contains
four rows that hit every Stage-2 path: a clear replication, a
reproducibility study, an obvious DNA-replication false positive, and a
multi-original case.

**Stage 2** runs `python -m filter.run_filter`, which:

1. Loads `filter/spec/exclusion-patterns.yaml`.
2. Applies `apply_rule_filter` (phrase detection + author-year cite gate + non-scholarly exclusion).
3. Applies `apply_llm_filter` to anything left as `needs_review`. With no API key the LLM step is a no-op.

The script then prints the `filter_status` breakdown and the first five rows of the result.

### Live mode

```bash
export LIVE_SEARCH=1
export YEAR_FROM=2023
export YEAR_TO=2023
export OPENALEX_API_KEY=...
bash examples/pipeline_example.sh
```

---

## `discover_example` — Stage 1 engine demo

Env-var knobs:

| Variable                   | Default       | Effect                                                          |
|----------------------------|---------------|-----------------------------------------------------------------|
| `MAX_PER_SOURCE`           | `25`          | Stop a source after this many kept candidates.                  |
| `YEAR_FROM` / `YEAR_TO`    | `2022`/`2024` | Publication-year window passed to every adapter.                |
| `SOURCES`                  | `openalex`    | Comma-separated; e.g. `openalex,crossref,semantic_scholar`      |
| `OUT_DIR`                  | `data/examples` | Where the demo CSVs go (gitignored).                          |
| `OPENALEX_API_KEY`         | (unset)       | **Required** since Feb 13, 2026; OpenAlex is skipped without it. |
| `RESEARCHER_EMAIL`         | (unset)       | Used for the Crossref polite pool `mailto`.                     |
| `SEMANTIC_SCHOLAR_API_KEY` | (unset)       | Optional; falls back to 0.5 req/s unauthenticated.             |

The four runs are progressively broader so you can see how recall changes without changing anything else:

1. **Load example** — the same three keywords behind the SciMeto Discover UI's "Load example" button.
2. **Placeholder** — the four-line placeholder text shown in the UI's New-Run modal.
3. **Custom** — a long alternation list demonstrating that the engine bundles many phrase variants into ONE OpenAlex search call.
4. **Spec-only** — no `--keywords` flag; uses just `search/spec/search-keywords.yaml`. Closest analogue to a production run.

### Quick recipes

Run with three sources and slightly larger caps:

```bat
set SOURCES=openalex,crossref,semantic_scholar
set MAX_PER_SOURCE=100
examples\discover_example.bat
```

Run a single year for tight rate-limit control:

```bash
YEAR_FROM=2023 YEAR_TO=2023 MAX_PER_SOURCE=50 \
  bash examples/discover_example.sh
```

---

## Where to look next

- `docs/scimeto_filter_port.md` — function-level reference for the filter port.
- `docs/scimeto_engine_port.md` — what the engine modules do, line by line.
- `filter/spec/README.md` — exclusion-patterns YAML hand-off contract.
- `search/spec/README.md` — search-keywords YAML hand-off contract.
- `search/RATE_LIMITS_VERIFIED.md` — when the rate-limit docs were last audited.
- `RULEBOOK.md` §Filter — the team's policy for `replication` vs `needs_review` vs `false_positive`.
