# Setup Guide

## Prerequisites

- Python 3.10+
- pip
- (Optional) Docker — for GROBID PDF reference extraction
- A Google AI Studio account — for Gemini API access

## Installation

```bash
git clone <repo-url>
cd flora-extractor
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```bash
RESEARCHER_EMAIL=you@example.com   # for OpenAlex / Crossref politeness headers
GEMINI_API_KEY=...                 # from https://aistudio.google.com
```

## GROBID (optional, recommended for Stage 3)

GROBID extracts structured references from PDFs, improving DOI resolution accuracy.

```bash
docker run -t --rm -p 8070:8070 lfoppiano/grobid:0.8.0
```

Leave `GROBID_URL=http://localhost:8070` in `.env` (the default). If GROBID is not running, the pipeline logs a warning and falls back to abstract-only processing.

## Running the pipeline

Each stage reads from the previous stage's CSV output. Run them in order:

```bash
# Stage 1 — discover candidate papers
python -m search.run_search

# Stage 2 — filter false positives
python -m filter.run_filter

# Stage 3 — extract original study + outcome
python -m extract.run_extract

# Stage 4 — monitoring web app
python -m validate.app        # → http://localhost:5001
```

## Seeding from existing data

If the shared-drive CSVs are available, you can skip Stages 1–2:

| File | Description |
|------|-------------|
| `data/candidates.csv` | Stage 1 output — start here if discovered via OpenAlex |
| `data/filtered.csv` | Stage 2 output — start here to run Stage 3 immediately |
| `data/extracted.csv` | Stage 3 output — load into web app for monitoring |
| `data/flora_selected.csv` | 107 rows already in FLoRA — used for deduplication |

### Large data files (DVC + Cloudflare R2)

`candidates.csv` (~4.7 GB) and `filtered.csv` (~4.3 GB) are far too large for git or
the free GitHub LFS tier. They are stored **zipped** in a Cloudflare R2 bucket and
versioned with [DVC](https://dvc.org); only the small `data/*.zip.dvc` pointer files
are committed to git. The unzipped CSVs are gitignored working copies.

One-time setup — put R2 credentials (from an R2 "Object Read & Write" API token) in
`.dvc/config.local`, which is gitignored so secrets never reach git:

```bash
dvc remote modify --local r2 access_key_id     <R2_ACCESS_KEY_ID>
dvc remote modify --local r2 secret_access_key <R2_SECRET_ACCESS_KEY>
```

Then fetch (or later update) the data:

```bash
./scripts/data.sh pull   # dvc pull the zips from R2, then unzip to CSVs
./scripts/data.sh pack   # after regenerating the CSVs: re-zip + dvc add (then commit + dvc push)
```

**Pruning old versions.** At ~3 GB zipped, R2's 10 GB free tier holds roughly three
versions. To keep only the last N and delete older blobs from both the local cache and
R2:

```bash
./scripts/data.sh prune 3          # dry-run: show what would be removed
./scripts/data.sh prune 3 apply    # actually delete all but the last 3 versions
```

This wraps `dvc gc --workspace --rev HEAD --num N --cloud`. `--num` counts **git
commits** back from HEAD (plus the current workspace), not data updates — if code
commits sit between data updates you may retain fewer than N distinct data versions.
For exact per-version control, tag each data snapshot and use `dvc gc --all-tags`.

## Environment Variables

See `.env.example` for the full list with descriptions. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `RESEARCHER_EMAIL` | Yes | Politeness header for APIs |
| `GEMINI_API_KEY` | Yes | Primary LLM |
| `GEMINI_API_KEY_2..N` | No | Key rotation for higher quota |
| `OPENAI_API_KEY` | No | Fallback LLM |
| `OPENROUTER_API_KEY` | No | Qwen via OpenRouter (primary for linking) |
| `SUPABASE_URL` | No | Validation monitoring tab |
| `SUPABASE_SERVICE_KEY` | No | Validation monitoring tab |
| `GROBID_URL` | No | PDF reference extraction (default: localhost:8070) |
| `GEMINI_MODEL` | No | Override Gemini model name |
| `GEMINI_HEAVY_MODEL` | No | Override for DOI resolution (defaults to GEMINI_MODEL) |

## Cache

All API results are cached in `cache/` (gitignored). The cache persists across runs — delete specific files or the whole directory to force fresh fetches.

```bash
rm -rf cache/          # clear everything
rm cache/parse/        # clear PDF parse cache only
```

## Development server

```bash
python -m validate.app
# → http://localhost:5001
```

The app auto-reloads when Flask is in debug mode (default when run directly).
