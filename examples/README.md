# `examples/` — runnable walkthroughs

| Script                  | What it does                                                         |
|-------------------------|----------------------------------------------------------------------|
| `pipeline_example.bat`  | Windows: Stage 1 (fixture or live search) → Stage 2 (the filter engine: spec bundle, and a route + dry-run screen when a survivor pool is present), plus pointers for Stages 3 and 4. |
| `pipeline_example.sh`   | Bash mirror of the above; identical Python entry points.            |
| `discover_example.bat`  | Windows: four Stage 1 (search) demo runs using the SciMeto engine, then prints the Stage 2 spec bundle. |
| `discover_example.sh`   | Bash mirror of the above; identical Python entry points.            |

---

## `pipeline_example` — Stage 1 + Stage 2 walkthrough

Env-var knobs:

| Variable                            | Default              | Effect                                                        |
|-------------------------------------|----------------------|---------------------------------------------------------------|
| `LIVE_SEARCH`                       | `0`                  | `1` → call OpenAlex / S2 / I4R live; `0` → synthetic fixture.  |
| `YEAR_FROM` / `YEAR_TO`             | `2023`/`2024`        | Forwarded to the per-source scripts on live runs.             |
| `OUT_DIR`                           | `data/examples`      | Where the demo's outputs go (gitignored). Deliberately not `data/`: the demo must never overwrite a real `data/candidates.csv`. A **live** run still merges into `data/candidates.csv`, because that is where `search.run_search` writes. |
| `POOL_DIR` / `FLORA_POOL_DIR`       | `cache/snapshot_pool`| The survivor pool Stage 2 routes, if you have one.            |
| `OPENALEX_API_KEY`                  | (unset)              | Required for live search.                                     |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | (unset)              | Needed only to actually run a screen tier (`--run`).          |

**Stage 1** is intentionally small — by default it writes a synthetic five-row
fixture (`examples/_make_fixture.py`) to `data/examples/candidates.csv`.

**Stage 2 is the filter engine** (`python -m filter.engine`). It routes the
**survivor pool** — parquet under `cache/snapshot_pool` — through the declarative
spec bundle in `filter/spec/`, and does **not** read `candidates.csv`, so Stage 1's
output above cannot be chained into it. The script therefore:

1. Always runs `python -m filter.engine specs` — the loaded bundle and its hash.
   Offline: no pool, no keys, no spend.
2. If a pool is present, runs `python -m filter.engine route --pool <POOL_DIR>`
   (routes every row into a pile) and then
   `python -m filter.engine screen --tier screen_expensive --pool <POOL_DIR>`,
   which is a **dry run** without `--run`: it prints the pile size and a cost
   estimate and claims, fetches and spends nothing.
3. Otherwise prints the commands to fetch a pool and run the three steps.

The live sequence, for reference:

```bash
python -m search.pool_sync --pull
python -m filter.engine route
python -m filter.engine screen --tier screen_expensive --run --limit 500
python -m filter.engine handoff --out data/filtered.csv    # Stage 3's input
```

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

The script closes by printing the Stage 2 spec bundle and the engine commands;
it runs no filtering itself, because Stage 2 routes the pool rather than these CSVs.

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

- `docs/filter-engine.md` — Stage 2's design and module contracts.
- `filter/spec/CONVENTIONS.md` — precedence bands, pile → `paper_type` mapping.
- `docs/cli-reference.md` — every command and flag, all four stages.
- `docs/csv-schema.md` — the column contract between stages.
- `search/spec/search-keywords.yaml` — the Stage 1 keyword spec the demos read.
