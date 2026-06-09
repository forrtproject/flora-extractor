# FLoRA Extractor

A Python pipeline that discovers, extracts, and monitors replication and reproduction studies for the [FLoRA database](https://forrt.org/replication-hub/flora/).

**Part of the [FORRT](https://forrt.org) project.**

---

## What It Does

Starting from keyword searches of academic databases, FLoRA Extractor:
1. **Discovers** candidate replication/reproduction papers from OpenAlex and curated lists
2. **Filters** false positives using rule-based and LLM classification
3. **Extracts** the target study and replication outcome from each paper
4. **Monitors** extraction progress through a web dashboard; validation happens in a separate Supabase-backed repo

---

## Architecture

```
Stage 1: search/      → data/candidates.csv   (discover candidates)
Stage 2: filter/      → data/filtered.csv     (remove false positives)
Stage 3: extract/     → data/extracted.csv    (link original + code outcome)
Stage 4: validate/    → monitoring web app    (dashboard at localhost:5001)
                             ↕
                      Supabase (separate validation repo)
```

Each stage is independently runnable.

---

## Quick Start

```bash
git clone <repo-url>
cd flora-extractor
pip install -r requirements.txt
cp .env.example .env   # fill in your API keys

# Run the pipeline
python -m search.run_search
python -m filter.run_filter
python -m extract.run_extract

# Start the monitoring web app
python -m validate.app   # → http://localhost:5001
```

See [docs/setup.md](docs/setup.md) for full setup instructions.

---

## Required environment variables

```
RESEARCHER_EMAIL=you@example.com   # for OpenAlex/Crossref API politeness
GEMINI_API_KEY=...                 # primary LLM (free at aistudio.google.com)
```

Optional: `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GROBID_URL`. See `.env.example`.

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/setup.md](docs/setup.md) | Installation and running the pipeline |
| [docs/architecture.md](docs/architecture.md) | Module map and design decisions |
| [docs/cli-reference.md](docs/cli-reference.md) | All CLI commands and flags |
| [docs/csv-schema.md](docs/csv-schema.md) | CSV column definitions |
| [docs/dashboard-guide.md](docs/dashboard-guide.md) | Dashboard user guide |
| [docs/supabase-schema.md](docs/supabase-schema.md) | Supabase validation table schemas |
| [docs/testing.md](docs/testing.md) | Running and writing tests |
| [docs/README.md](docs/README.md) | Full documentation index |

**AI coding agent?** Read [CLAUDE.md](CLAUDE.md) first.

---

## Data Sources

| Source | Coverage |
| ------ | -------- |
| [OpenAlex](https://openalex.org) | Broad academic literature, free API |
| [Semantic Scholar](https://www.semanticscholar.org) | Supplementary coverage |
| [Bob Reed's Replication Network](https://replicationnetwork.com) | Economics replications |
| [I4R](https://i4replication.org/reports/) | Institute for Replication reports |

Full-text: Unpaywall, CORE, arXiv, OSF. DOI resolution: Crossref.

---

## Contributing

1. Branch from `dev` (`feature/search`, `feature/filter`, `feature/extract`, `feature/validate`)
2. Test with sample data in `misc/`
3. Open a PR to `dev` when a feature is stable — don't wait until the end
4. `main` and `dev` are branch-protected; all merges require a PR review

---

## Related

- [FLoRA database](https://forrt.org/replication-hub/flora/) — the database this pipeline feeds
- [flora_search_approaches](https://github.com/forrtproject/flora_search_approaches) — original R-based pipeline

## License

MIT
