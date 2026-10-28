# FLoRA Extractor

A Python pipeline that discovers, extracts, and monitors replication and reproduction studies for the [FLoRA database](https://forrt.org/replication-hub/flora/).

**Part of the [FORRT](https://forrt.org) project.**

## Which models classify papers?

**Stage 2 uses two voters: `deepseek/deepseek-v4-flash` through OpenRouter and
`gpt-6-luna` through OpenAI, both at reasoning effort `low`.** A paper is discarded
only when both say `none`; every other complete pair proceeds to extraction.
The cheap pre-screen is dormant. Stage 3 reads the screen verdict and uses
`gpt-6-luna` for linking and outcome coding.

**[Models and screening](docs/models-and-screening.md)** explains the decision rule,
all model roles, required API keys, evaluation evidence and historical results.
Model settings live in [shared/config.py](shared/config.py).

---

## What It Does

Starting from keyword searches of academic databases, FLoRA Extractor:
1. **Discovers** candidate replication/reproduction papers by scanning the OpenAlex snapshot
2. **Filters** false positives with a declarative filter engine: rules route and discard, LLM screening tiers admit
3. **Extracts** the target study and replication outcome from each paper
4. **Monitors** extraction progress through a web dashboard; validation happens in a separate Supabase-backed repo

---

## Architecture

```
Stage 1: search/       → the survivor pool    (search only — no filtering)
Stage 2: filter/engine → routing store + screen verdicts (route the pool, screen)
Stage 3: extract/      → data/extracted.csv   (link original + code outcome)
Stage 4: validate/     → monitoring web app   (dashboard at localhost:5001)
                             ↕
                      Supabase (separate validation repo)
```

Each stage is independently runnable. The split is deliberate: **Stage 1 searches
and Stage 2 decides.** Stage 1's only keyword logic is the search gate that makes
scanning 510M works tractable (a broad token/stem alternation plus concept
membership); every precision decision — exclusions, phrases, vocabulary, rescues —
lives in Stage 2's spec bundle, so there is one rule set to reason about.

---

## Quick Start

```bash
git clone https://github.com/forrtproject/flora-extractor.git
cd flora-extractor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env   # fill in your API keys

# Run the pipeline
.venv/bin/python -m search.pool_sync --pull   # obtain the existing survivor pool
.venv/bin/python -m filter.engine route
.venv/bin/python -m filter.engine screen --tier screen_expensive --run
.venv/bin/python -m extract.tier --run
.venv/bin/python -m extract.export   # with one routed release; otherwise pass --release

# Start the monitoring web app
.venv/bin/python -m validate.app   # → http://localhost:5001
```

Stage 2's `screen` is a dry run without `--run`, printing what a tier would cost
before anything is claimed or spent. Stage 3 reads no CSV: `extract.tier` builds its
worklist from the routing store and the pool, and `extract.export` renders
`data/extracted.csv` from the stored verdicts.

See [docs/setup.md](docs/setup.md) for full setup instructions.

---

## Required environment variables

```bash
RESEARCHER_EMAIL=you@example.com   # OpenAlex/Crossref politeness headers
OPENROUTER_API_KEY=...            # Stage 2 voter 1; Stage 3 reference-list pick check
OPENAI_API_KEY=...                # Stage 2 voter 2; Stage 3 linking and outcomes
GEMINI_API_KEY=...                # Stage 3 document reference-extraction fallback
SUPABASE_SERVICE_KEY=...          # claims and permanent verdicts for --run
HF_TOKEN=...                      # access to the private survivor-pool dataset
```

The shared `SUPABASE_URL` is supplied by `.env.defaults`. Model IDs and reasoning
efforts are committed constants, not environment variables. Copy `.env.example`
for the settings catalogue; defaults and loading rules live in
[shared/config.py](shared/config.py). See [setup](docs/setup.md) for pool access,
credentials and optional services.

---

## Documentation

**[docs/README.md](docs/README.md) is the documentation index** — every guide,
reference and code-flow walkthrough is listed there.

**AI coding agent?** Read [CLAUDE.md](CLAUDE.md) first.

---

## Data Sources

| Source | Role |
| ------ | ---- |
| [OpenAlex](https://openalex.org) snapshot | **Discovery** — the whole corpus is scanned locally by Stage 1's search gate; this is the only discovery source |

The API-harvest discovery sources (Semantic Scholar search, Bob Reed's Replication
Network, I4R) are retired — nothing downstream read their output, so they were parked
on `wip/api-harvest-sources` (PR #158). Semantic Scholar survives only as one of the
abstract-backfill sources in `search/fetch_abstracts.py`. Wiring curated harvesters
back in is issue #150.

Full-text: Unpaywall, CORE, arXiv, OSF. DOI resolution: Crossref.
Abstract backfill: Europe PMC, Semantic Scholar, Crossref, Scopus.

---

## Contributing

This is a research pipeline whose screening, linking, and extraction decisions affect the resulting scientific dataset. **We welcome issues with bug reports, evidence, and suggestions. Unsolicited pull requests are unlikely to be merged.** Please discuss a proposed change with maintainers before implementing it.

See [CONTRIBUTING.md](CONTRIBUTING.md) for useful issue reports and the workflow for agreed changes, and follow the [FORRT Code of Conduct](https://forrt.org/coc/).

---

## Acknowledgements

We thank [Dan Elton](https://github.com/delton137) for open-sourcing the
[Metascience Observatory](https://github.com/The-Metascience-Observatory/mo_pipeline)
replication extraction pipeline and database. Comparing our output against it helped
us improve accuracy in several places. Its replication database also gave us
candidates to screen that our own search had missed.

---

## Related

- [FLoRA database](https://forrt.org/replication-hub/flora/) — the database this pipeline feeds
- [flora_search_approaches](https://github.com/forrtproject/flora_search_approaches) — original R-based pipeline

## License

MIT
