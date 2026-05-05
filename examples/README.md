# `examples/` — runnable walkthroughs

Each script here exercises the pipeline end-to-end with conservative defaults
so the team can see the engine in action without burning API quota.

| Script                  | What it does                                                     |
|-------------------------|------------------------------------------------------------------|
| `discover_example.bat`  | Windows: four Stage 1 (search) demo runs. Optionally runs Stage 2. |
| `discover_example.sh`   | Bash mirror of the above; identical Python entry points.        |

Both scripts share the same env-var knobs:

| Variable                  | Default      | Effect                                             |
|---------------------------|--------------|----------------------------------------------------|
| `MAX_PER_SOURCE`          | `25`         | Stop a source after this many kept candidates.     |
| `YEAR_FROM` / `YEAR_TO`   | `2022`/`2024`| Publication-year window passed to every adapter.   |
| `SOURCES`                 | `openalex`   | Comma-separated; e.g. `openalex,crossref,semantic_scholar` |
| `OUT_DIR`                 | `data/examples` | Where the demo CSVs go (gitignored).            |
| `OPENALEX_API_KEY`        | (unset)      | **Required** since Feb 13, 2026; OpenAlex is skipped without it. |
| `RESEARCHER_EMAIL`        | (unset)      | Used for the Crossref polite pool `mailto`.        |
| `SEMANTIC_SCHOLAR_API_KEY`| (unset)      | Optional; falls back to 0.5 req/s unauthenticated. |

The four runs are progressively broader so you can see how recall changes
without changing anything else:

1. **Load example** — the same three keywords behind the SciMeto Discover UI's
   "Load example" button. Exercises every wildcard syntax (`replicat*`,
   `pre-?registered`, `(close|high-powered) replication`).
2. **Placeholder** — the four-line placeholder text shown in the UI's
   New-Run modal.
3. **Custom** — a long alternation list demonstrating that the engine
   bundles many phrase variants into ONE OpenAlex search call.
4. **Spec-only** — no `--keywords` flag, so the engine uses just the YAML
   spec at `search/spec/search-keywords.yaml`. Closest analogue to a
   production run.

After Run 1 the script stages the engine output at `data/candidates.csv`
and tries Stage 2. On `feature/search` (where Stage 2 is still a stub)
it skips with a friendly message; on branches where the filter port is
present it actually runs `python -m filter.run_filter`.

## Quick recipes

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

## Where to look next

- `docs/scimeto_engine_port.md` — what the engine modules do, line by line.
- `search/spec/README.md` — hand-off contract describing why the YAMLs are
  copied verbatim from SciMeto and how to keep them in sync.
- `search/RATE_LIMITS_VERIFIED.md` — when the rate-limit docs were last
  audited (60-day freshness gate enforced by the runner).
