# FLoRA Extractor

A Python tool that discovers, extracts, and validates replication and reproduction studies for the [FLoRA database](https://forrt.org/replication-hub/flora/).

**Part of the [FORRT](https://forrt.org) project.**

---

## What It Does

Starting from keyword searches of academic databases, FLoRA Extractor:
1. **Discovers** candidate replication/reproduction papers from OpenAlex and curated lists
2. **Filters** false positives using rule-based and LLM classification
3. **Extracts** the target study and replication outcome from each paper
4. **Validates** results through a crowdsourced voting web interface

---

## Architecture

```
Stage 1: search/      → data/candidates.csv   (discover candidates)
Stage 2: filter/      → data/filtered.csv     (remove false positives)
Stage 3: extract/     → data/extracted.csv    (link original + code outcome)
Stage 4: validate/    → Flask web app         (human voting, export)
```

Each stage is independently runnable. See [CLAUDE.md](CLAUDE.md) for full technical details.

---

## Quick Start

```bash
# 1. Clone and setup
git clone https://github.com/forrtproject/flora-extractor.git
cd flora-extractor
pip install -r requirements.txt
cp .env.example .env   # fill in your API keys

# 2. Run the pipeline
python -m search.run_search --auto-advance --from-year 2011 --to-year 2021 --max-per-phrase 200
python -m filter.run_filter           # → data/filtered.csv
python -m extract.run_extract --skip-flora-validated  # → data/extracted.csv

# 3. Start the validation web app
python -m validate.import_csv      # load into SQLite
python -m validate.app             # → http://localhost:5001
```

---

## API Keys Required

Add to your `.env` file (copy from `.env.example`):

```
RESEARCHER_EMAIL=you@example.com      # for OpenAlex/Crossref API politeness
GEMINI_API_KEY=...                    # primary LLM
GEMINI_API_KEY_2=...                  # optional: rotate for higher quota
OPENAI_API_KEY=...                    # fallback LLM (optional)
GROBID_URL=http://localhost:8070      # local GROBID server (optional, for full-text extraction)
```

Get a free Gemini API key at [aistudio.google.com](https://aistudio.google.com).

---

## Data Sources

**Bibliographic databases (primary):**

| Source                                             | Coverage                                         |
| -------------------------------------------------- | ------------------------------------------------ |
| [OpenAlex](https://openalex.org)                   | Broad academic literature, free API              |
| [Semantic Scholar](https://www.semanticscholar.org)| Supplementary coverage                           |
| [Crossref](https://www.crossref.org)               | DOI resolution and reference lists               |
| [OpenCitations](https://opencitations.net)         | Reference lists (where OpenAlex coverage is thin)|

**Curated lists (secondary, pluggable):**

| Source                                                                                | Coverage                            |
| ------------------------------------------------------------------------------------- | ----------------------------------- |
| [Bob Reed's Replication Network](https://replicationnetwork.com/replication-studies/) | Economics                           |
| [I4R](https://i4replication.org/reports/)                                             | Institute for Replication reports   |

Full-text acquisition (for Stage 3): [Unpaywall](https://unpaywall.org), [CORE](https://core.ac.uk), arXiv, OSF.

---

## Output Schema

Each extracted record contains:

| Field | Description |
|-------|-------------|
| `doi_r` | Replication paper DOI |
| `doi_o` | Original target study DOI |
| `title_o` | Original target study title |
| `outcome` | success / failure / mixed / uninformative / descriptive |
| `outcome_phrase` | Supporting quote from the paper |
| `link_evidence` | Evidence used to identify the original |
| `validation_status` | confirmed / rejected / pending / needs_review |

Full schema: [shared/schema.py](shared/schema.py)

---

## Team Guide

| Team | Stage | Branch | Docs |
|------|-------|--------|------|
| Team Search | Stage 1 | `feature/search` | [docs/STAGE1_SEARCH.md](docs/STAGE1_SEARCH.md) |
| Team Filter | Stage 2 | `feature/filter` | [docs/STAGE2_FILTER.md](docs/STAGE2_FILTER.md) |
| Team Extract | Stage 3 | `feature/extract` | [docs/STAGE3_EXTRACT.md](docs/STAGE3_EXTRACT.md) |
| Team Validate | Stage 4 | `feature/validate` | [docs/STAGE4_VALIDATE.md](docs/STAGE4_VALIDATE.md) |

**New team member?** Read [CLAUDE.md](CLAUDE.md) first — it contains architecture, schema, and coding rules.  
**AI coding agent?** Read [CLAUDE.md](CLAUDE.md) (Claude Code) or [AGENTS.md](AGENTS.md) (all others).  
**Working in R?** See the R note in [CLAUDE.md](CLAUDE.md#r-support).

---

## Contributing

1. Branch from `dev` using your team's branch name (`feature/search`, etc.)
2. Use sample data in `misc/` to develop and test independently
3. Open a PR to `dev` when a feature is stable — don't wait until the end
4. `main` and `dev` are branch-protected; all merges require a PR review

---

## Related Projects

- [flora_search_approaches](https://github.com/forrtproject/flora_search_approaches) — original R-based pathway pipeline (reference implementation)
- [FLoRA database](https://forrt.org/replication-hub/flora/) — the database this tool feeds into

---

## License

MIT
