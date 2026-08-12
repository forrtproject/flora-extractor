# FLoRA Extractor

A Python pipeline that discovers, extracts, and monitors replication and reproduction studies for the [FLoRA database](https://forrt.org/replication-hub/flora/).

**Part of the [FORRT](https://forrt.org) project.**

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
git clone <repo-url>
cd flora-extractor
pip install -r requirements.txt
cp .env.example .env   # fill in your API keys

# Run the pipeline
python -m search.run_search --scan
python -m filter.engine route
python -m filter.engine screen --tier screen_expensive --run
python -m extract.tier --run
python -m extract.export --release <id>

# Start the monitoring web app
python -m validate.app   # → http://localhost:5001
```

Stage 2's `screen` is a dry run without `--run`, printing what a tier would cost
before anything is claimed or spent. Stage 3 reads no CSV: `extract.tier` builds its
worklist from the routing store and the pool, and `extract.export` renders
`data/extracted.csv` from the stored verdicts.

See [docs/setup.md](docs/setup.md) for full setup instructions.

---

## Required environment variables

```
RESEARCHER_EMAIL=you@example.com   # for OpenAlex/Crossref API politeness
GEMINI_API_KEY=...                 # primary LLM (free at aistudio.google.com)
OPENAI_API_KEY=...                 # Stage 2's second screen voter (default SCREENING_MODEL_2) and Stage 3's linking/outcome models
```

`OPENROUTER_API_KEY` is needed only when a model id contains a `/` (e.g. a
`SCREENING_MODEL_2` or the optional pre-screen voters routed through OpenRouter).
Optional: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GROBID_URL`.

`.env.example` is the authoritative list of every variable and its default — copy it
rather than this excerpt.

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

1. Branch from `origin/main` (`feature/search`, `feature/filter`, `feature/extract`, `feature/validate`)
2. Test with sample data in `misc/`
3. Open a PR with `--base main` when a feature is stable — don't wait until the end
4. `main` is branch-protected; all merges require a PR review

(The `dev` branch is stale — do not base work on it.)

---

## Related

- [FLoRA database](https://forrt.org/replication-hub/flora/) — the database this pipeline feeds
- [flora_search_approaches](https://github.com/forrtproject/flora_search_approaches) — original R-based pipeline

## License

MIT
