# Parked: the `/batch` blueprint

`validate/routes/batch.py` (and its template `validate/templates/batch.html`) is
removed from `main` and preserved on this branch. It was the current-state review's
finding W5: the blueprint registers on any run where `FLORA_READONLY` is unset —
the local default — and its POST endpoints call the extraction ladder directly,
making live OpenAlex, LLM and PDF calls and writing CSVs, with no authentication.
It was inert only because the input CSVs it reads no longer exist, which is an
accident of the filesystem rather than a control.

## Open question before reviving

Is a batch-extraction UI still needed at all? Stage 3 is run from the CLI
(`python -m extract.run_extract`) and the dashboard is documented as read-only.
If nobody has missed it, delete this branch instead of reviving it.

## What a safe revival requires

1. **An actual control, not a missing file.** Registration must be opt-in
   (explicit env/flag), and the endpoints must be authenticated — the dashboard
   has no auth story today, so this means adding one, not assuming localhost.
2. **Wired into cost tracking.** Every LLM call it triggers must go through the
   normal `shared/token_usage.py` recording and the OpenAI daily budget check,
   and OpenAlex calls through `shared/openalex_keys.py` — verify, don't assume:
   the parked code predates several of those seams.
3. **Current pipeline files.** The input CSVs it reads predate the engine
   handoff; rewire it to `data/filtered.csv`'s 27-column contract (screen
   verdict read off the row, never re-screened) and the current
   `extracted.csv` schema in `shared/schema.py`.
4. **Concurrency.** It must not race a CLI Stage 3 run on the same output CSV —
   the resume/append machinery in `run_extract.py` assumes one writer.
