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
OPENAI_API_KEY=...                 # Stage 2's second screen voter (default SCREENING_MODEL_2) and Stage 3's linking/outcome models
```

**Nothing is validated up front any more.** The key each call needs follows its model
id through `provider_for()`, and a call whose provider has no key fails that row —
`api_error` — rather than the run. Stage 3 needs the key `OUTCOME_MODEL` and
`LINKING_MODEL` require (`OPENAI_API_KEY`, or `OPENROUTER_API_KEY` when the id
contains a `/`), because no call falls back to another provider. The two screen
voters' keys belong to Stage 2, which is where the screen runs; Stage 3 reads its
verdict off the row and calls neither voter. An unset variable simply takes the default in
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
routes it. Stage 3 reads no CSV: it builds its worklist in process from the routing
store and the pool, and `extract.export` renders `data/extracted.csv` from the stored
verdicts. Stage 4 reads that file.

```bash
# Stage 1 — scan the OpenAlex snapshot into the survivor pool (search only; it
# applies no filters). --scan is required; the scan takes 13-21 hours over 725 GB,
# so most collaborators pull the pool instead: python -m search.pool_sync --pull
python -m search.run_search --scan

# Stage 2 — route the survivor pool, screen what the rules could not settle,
# and write the handoff record (Stage 3 does not read it — see above)
python -m filter.engine route
python -m filter.engine screen --tier screen_expensive --run
python -m filter.engine handoff --out data/filtered.csv
# `screen` is a dry run without --run, and --run needs SUPABASE_URL /
# SUPABASE_SERVICE_KEY: the claim is what stops two runs paying for the same works.

# Stage 3 — extract original study + outcome, then render the CSV
python -m extract.tier --run    # dry run without --run; --run needs the same keys
# The export renders the works the named release admits; with one release in the
# store the id may be omitted, and a store holding several refuses without it.
python -m extract.export --release <id>   # the verdicts → data/extracted.csv

# Stage 4 — monitoring web app
python -m validate.app        # → http://localhost:5001
```

## Seeding from existing data

If the shared-drive CSVs are available, you can skip Stages 1–2:

| File | Description |
|------|-------------|
| the survivor pool (parquet) | Stage 1 output — pull it with `python -m search.pool_sync --pull` and run Stage 2 |
| `data/filtered.csv` | Stage 2 output — a reviewable record of what the screen admitted. Stage 3 does not read it; running Stage 3 needs the routing store and the pool, not this file |
| `data/extracted.csv` | Stage 3 output — load into web app for monitoring |
| `data/FLoRA entry sheet - replication list.csv`, `data/flora.csv` | Rows already in FLoRA — the two files `shared/flora_skip.py` reads. The skip is unconditional: it applies in the extract tier's worklist and again in the export, with no flag to turn it on |

### Large data files (DVC + Cloudflare R2)

This is for the two RETIRED pre-engine corpora, `candidates.csv` and the old
`filtered.csv`, which were far too large for git or the free GitHub LFS tier — the
DVC pointers hold them zipped at 1.67 GB and 1.68 GB (`data/candidates.zip.dvc`,
`data/filtered.zip.dvc`). Neither is written any more: Stage 1's corpus is the
survivor pool (shared through Hugging Face — see
[cli-reference.md](cli-reference.md)) and the current `data/filtered.csv` is Stage 2's
handoff at a couple of thousand rows. Set this up only if you need the historical
corpora.

They are stored **zipped** in a Cloudflare R2 bucket and versioned with
[DVC](https://dvc.org); only the small `data/*.zip.dvc` pointer files are committed
to git. The unzipped CSVs are gitignored working copies.

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
| `OPENAI_API_KEY` | Stages 2 and 3 | Second voter of Stage 2's front-door screen with the default `SCREENING_MODEL_2`; also Stage 3's `LINKING_MODEL` and `OUTCOME_MODEL`, which codes every outcome |
| `OPENROUTER_API_KEY` | No | Only reached by a model id that names it: the pre-screen's two voters, and `SCREENING_MODEL_2` when it contains a `/` |
| `SUPABASE_URL` | No | Validation monitoring tab |
| `SUPABASE_SERVICE_KEY` | No | Validation monitoring tab |
| `GROBID_URL` | No | PDF reference extraction. **Code default is the public server `https://kermitt2-grobid.hf.space`**; `.env.example` sets `http://localhost:8070`. See the GROBID section above |
| `GEMINI_USE_FLEX` / `GEMINI_FLEX_TIMEOUT` | No | 50% cheaper Gemini calls on paid keys, at the price of queueing |
| `OPENAI_USE_FLEX` / `OPENAI_FLEX_TIMEOUT` | No | Same trade on OpenAI; a request flex will not serve falls back to standard tier |

Model ids are **constants, not env vars** (`shared/config.py`, code-style rule 8):
`PRESCREEN_MODEL_1`/`_2`, `SCREENING_MODEL_1`/`_2`, `LINKING_MODEL`,
`OUTCOME_MODEL`, `PDF_PARSE_MODEL`, `LINKING_EFFORT`. Each is named for the
QUESTION it answers rather than the vendor that serves it, and answers its call site
alone — a call that fails is reported as a failure, never retried against another
model. The provider follows the model id through `provider_for()`, so swapping one
across vendors is a one-line change; `PDF_PARSE_MODEL` is the exception, because the
document calls build a Gemini request body that has no OpenAI-compatible equivalent.

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
