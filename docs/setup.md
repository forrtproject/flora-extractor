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
OPENAI_API_KEY=...                 # Stage 3's second screen voter (default SCREEN_VOTER2_MODEL)
```

**Only Stage 3 enforces anything.** `_check_screen_providers()` in
`extract/run_extract.py` refuses to start unless *both* front-door screen voters have
their key (that is `GEMINI_API_KEY` plus whichever of `OPENAI_API_KEY` /
`OPENROUTER_API_KEY` the configured `SCREEN_VOTER2_MODEL` needs) — `--no-llm`
skips the check. Nothing else validates: an unset variable simply takes the default in
`shared/config.py`, so a missing `RESEARCHER_EMAIL` silently falls back to
`research@example.com`. Check `.env` against `.env.example` rather than relying on an
error — that file lists every variable the code reads, and marks the values that are
constants in `shared/config.py` and cannot be set from the environment at all.

## GROBID (optional, recommended for Stage 3)

GROBID extracts structured references from PDFs, improving DOI resolution accuracy.

```bash
docker run -t --rm -p 8070:8070 lfoppiano/grobid:0.8.0
```

**Keep `GROBID_URL=http://localhost:8070` in your `.env`.** That line is set by
`.env.example`, but it is *not* the code default: `shared/config.py` falls back to
the **public** server `https://kermitt2-grobid.hf.space`. Deleting or blanking the
env line therefore does not disable GROBID — it starts uploading every PDF the
pipeline acquires to a third-party host. If you do not want that, either run the
local container or point `GROBID_URL` somewhere unreachable so the pipeline logs a
warning and falls back to abstract-only processing.

## Running the pipeline

Run the stages in order. Stage 1 writes the survivor pool (parquet) and Stage 2
routes it; every stage after that reads the previous stage's CSV output.

```bash
# Stage 1 — discover candidate papers (search only; it applies no filters)
python -m search.run_search

# Stage 2 — route the survivor pool, screen what the rules could not settle,
# and write the file Stage 3 reads
python -m filter.engine route
python -m filter.engine screen --tier screen_expensive --run
python -m filter.engine handoff --out data/filtered.csv
# `screen` is a dry run without --run, and --run needs SUPABASE_URL /
# SUPABASE_SERVICE_KEY: the claim is what stops two runs paying for the same works.

# Stage 3 — extract original study + outcome
python -m extract.run_extract

# Stage 4 — monitoring web app
python -m validate.app        # → http://localhost:5001
```

## Seeding from existing data

If the shared-drive CSVs are available, you can skip Stages 1–2:

| File | Description |
|------|-------------|
| the survivor pool (parquet) | Stage 1 output — pull it with `python -m search.pool_sync --pull` and run Stage 2 |
| `data/filtered.csv` | Stage 2 output — start here to run Stage 3 immediately |
| `data/extracted.csv` | Stage 3 output — load into web app for monitoring |
| `data/FLoRA entry sheet - replication list.csv`, `data/flora.csv` | Rows already in FLoRA — the two files `shared/flora_skip.py` reads for `--skip-flora-validated` |

### Large data files (DVC + Cloudflare R2)

`filtered.csv` (~4.3 GB) is far too large for git or the free GitHub LFS tier.
(The survivor pool is shared separately, through Hugging Face — see
[cli-reference.md](cli-reference.md).) It is stored **zipped** in a Cloudflare R2 bucket and
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
./scripts/data.sh pack   # after regenerating the CSVs: re-zip + dvc add (then commit + push)
./scripts/data.sh push   # dvc push — use this, not a bare `dvc push` (see caveat below)
```

**R2 push caveat.** A bare `dvc push` on a recent `botocore`/`aiobotocore` can fail on
these multi-GB files with `OSError: ... Content-Length HTTP header` /
`dvc.exceptions.UploadError` — a checksum-mode default that non-AWS S3-compatible
endpoints like R2 don't handle on multipart uploads. `./scripts/data.sh push` sets the
required env vars (`AWS_REQUEST_CHECKSUM_CALCULATION` /
`AWS_RESPONSE_CHECKSUM_VALIDATION=when_required`) for you. If you must run `dvc push`
directly, export those two first — and always confirm with `dvc status -c` afterward
("Cache and remote 'r2' are in sync" means it actually landed; don't trust the exit
code of a piped command like `dvc push | tail`, which can hide a real failure).

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
| `OPENAI_API_KEY` | Stage 3 | Second voter of the front-door screen with the default `SCREEN_VOTER2_MODEL`; also `OUTCOME_MODEL`, which codes every outcome |
| `OPENROUTER_API_KEY` | No | Only reached by a model id that names it: the pre-screen's two voters, and `SCREEN_VOTER2_MODEL` when it contains a `/` |
| `SUPABASE_URL` | No | Validation monitoring tab |
| `SUPABASE_SERVICE_KEY` | No | Validation monitoring tab |
| `GROBID_URL` | No | PDF reference extraction. **Code default is the public server `https://kermitt2-grobid.hf.space`**; `.env.example` sets `http://localhost:8070`. See the GROBID section above |
| `GEMINI_USE_FLEX` / `GEMINI_FLEX_TIMEOUT` | No | 50% cheaper Gemini calls on paid keys, at the price of queueing |
| `OPENAI_USE_FLEX` / `OPENAI_FLEX_TIMEOUT` | No | Same trade on OpenAI; a request flex will not serve falls back to standard tier |

Model ids are **constants, not env vars** (`shared/config.py`, code-style rule 8):
`GEMINI_MODEL`, `GEMINI_LIGHT_MODEL`, `GEMINI_HEAVY_MODEL`, `OUTCOME_MODEL`,
`SCREEN_VOTER2_MODEL`, `GEMINI_THINKING_LEVEL`, and the pre-screen's pair in
`shared/prescreen.py`. Each names one call site and answers it alone — a call that
fails is reported as a failure, never retried against another model.

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
