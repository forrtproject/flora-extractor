# FLoRA Extractor

A Python tool that discovers, extracts, and validates replication and reproduction studies for the [FLoRA](https://forrt.org) / [FReD](https://forrt.org/fred) database.

**Part of the [FORRT](https://forrt.org) project.**

---

## What It Does

Given a set of academic paper DOIs, FLoRA Extractor:
1. **Discovers** candidate replication/reproduction papers from OpenAlex and curated lists
2. **Filters** false positives using rule-based and LLM classification
3. **Extracts** the original target study and replication outcome from each paper
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
git clone https://github.com/YOUR_ORG/flora-extractor.git
cd flora-extractor
pip install -r requirements.txt
cp .env.example .env   # fill in your API keys

# 2. Run the pipeline
python search/run_search.py        # → data/candidates.csv
python filter/run_filter.py        # → data/filtered.csv
python extract/run_extract.py      # → data/extracted.csv

# 3. Start the validation web app
python validate/import_csv.py      # load into SQLite
python validate/app.py             # → http://localhost:5001
```

---

## API Keys Required

Add to your `.env` file:

```
RESEARCHER_EMAIL=you@example.com      # for OpenAlex/Crossref API politeness
GEMINI_API_KEY=...                    # primary LLM (free tier)
GEMINI_API_KEY_2=...                  # optional: rotate for higher quota
OPENAI_API_KEY=...                    # fallback LLM (optional)
GROBID_URL=http://localhost:8070      # optional: local GROBID server
```

Get a free Gemini API key at [aistudio.google.com](https://aistudio.google.com).

---

## Data Sources

| Source | Coverage |
|--------|----------|
| [OpenAlex](https://openalex.org) | Broad academic literature search |
| [Bob Reed's Replication Network](https://replicationnetwork.com/replication-studies/) | Curated economics replications |
| [I4R](https://i4replication.org/reports/) | Institute for Replication reports |
| SCORE | Curated list (contact Luke/Theresa) |

---

## Output Schema

Each extracted record contains:

| Field | Description |
|-------|-------------|
| `doi_r` | Replication paper DOI |
| `doi_o` | Original study DOI |
| `title_o` | Original study title |
| `outcome` | success / failure / mixed / uninformative |
| `outcome_phrase` | Supporting quote from the paper |
| `link_evidence` | Evidence used to identify the original |
| `validation_status` | confirmed / rejected / pending |

Full schema: [shared/schema.py](shared/schema.py)

---

## Team Guide

| Component | Branch | Docs |
|-----------|--------|------|
| Search (Stage 1) | `feature/search` | [RULEBOOK.md § Team Search](RULEBOOK.md) |
| Filter (Stage 2) | `feature/filter` | [RULEBOOK.md § Team Filter](RULEBOOK.md) |
| Extract (Stage 3) | `feature/extract` | [RULEBOOK.md § Team Extract](RULEBOOK.md) |
| Validate (Stage 4) | `feature/validate` | [RULEBOOK.md § Team Validate](RULEBOOK.md) |

**New team member?** Read [CLAUDE.md](CLAUDE.md) and [RULEBOOK.md](RULEBOOK.md) first.  
**AI coding agent?** Read [CLAUDE.md](CLAUDE.md) — it contains everything you need to start coding.

---

## Contributing

1. Branch from `dev` using your team's branch name (`feature/search`, etc.)
2. Use sample data in `misc/` to develop and test
3. Open a PR to `dev` when done (not `main`)
4. See [RULEBOOK.md](RULEBOOK.md) for coding standards

---

## Related Projects

- [flora_search_approaches](https://github.com/forrtproject/flora_search_approaches) — original R-based pathway pipeline (reference implementation)
- [FReD-data](https://github.com/forrtproject/FReD-data) — FReD database processing pipeline
- [FORRT Replication Database](https://forrt.org/fred) — the database this tool feeds into

---

## License

MIT
