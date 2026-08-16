# Babysit brief: finish the 2026-08 campaign's re-extract and export

**Status: completed 2026-08-16.** The worklist is empty and the CSV is rendered
(`PENDING_RUNS.md` records the results; `handover.html` section 2 is read off the render). Kept as the record of how the run was shepherded.

For the agent that watches the rest of the run. Everything before the re-extract is
done; do not redo it. The campaign order and rationale are in
`docs/handover-campaign-2026-08.md`; the owed runs with done-criteria are in
`PENDING_RUNS.md`. This brief is only what remains and how to shepherd it.

## Fixed facts

- **Release: `16d370746b45`.** Every command names it. Do NOT run
  `filter.engine route` — a new route mints a new release and invalidates this one.
- **Interpreter: `.venv/bin/python`**, from the project root.
- **Stage 3 runs on `gpt-5.6-luna` since 2026-08-15** (flex tier, $0.10/M in,
  $0.60/M out). Luna is not in OpenAI's free daily allocation, so every call is
  billed and the run needs `OPENAI_DAILY_TOKEN_BUDGET=0` — otherwise the 9.5M
  free-tier cap stops it. The 1,567 works settled under `gpt-5.4-mini` stay
  settled (`_GENERATION_EQUIVALENCES` in `extract/tier.py`); the export ships both.
- **Resume state lives in Postgres**, not in any file. A killed or budget-stopped
  run loses nothing; re-running the same command continues where it stopped.
- The extract worklist is 5,928 works. As of 2026-08-15 13:00 UTC, 1,567 are
  settled; the 4,361 open works cost roughly $7–12 at luna's flex prices and take
  about nine hours at the measured ~480 works/hour.

## The loop

1. **Free any claims a dead run left** (harmless when there are none):
   `.venv/bin/python -m filter.engine release-claim`
   — if it lists active claims, end each one:
   `.venv/bin/python -m filter.engine release-claim --claim <id> --status failed --yes`
2. **Resume, in the background, with a log**:
   `OPENAI_DAILY_TOKEN_BUDGET=0 caffeinate -i .venv/bin/python -m extract.tier --release 16d370746b45 --run`
   (`caffeinate -i` stops idle sleep from killing the run — that happened once.)
3. **Watch the log** for `OpenAlexQuotaExhausted`, `ClaimConflict`, `Traceback`.
   Retry-and-continue noise (`Retrying request`, per-row `api_error`) is normal.
   `TokenBudgetExhausted` means the budget override was not passed.
4. If the run stops for any of those: release claims (step 1) and relaunch (step 2)
   once the cause is gone. Resume is the verdict row, so nothing needs repair.
5. Progress check (cheap, any time):
   `.venv/bin/python -c "import shared.config; from filter.engine.claims import ClaimsClient; from extract.tier import settled_work_ids; print(len(settled_work_ids(ClaimsClient())))"`
6. Spend check: OpenAI tokens for the day under `openai → gpt-5.6-luna` in
   `cache/token_usage.json`, priced at $0.10/M in and $0.60/M out.

## When the run completes (a batch loop that finds an empty worklist)

Run these from the project root, in order, and READ each output:

1. `.venv/bin/python -m extract.export --release 16d370746b45`
   Done-criteria, all three from the printed report:
   - It prints **no** `rows from a superseded generation:` line. If that line
     appears, works are still open — do not ship; go back to the loop.
   - `print_search_summary()` reports **≈0** verification searches (DOI
     verification replayed from cache).
   - The main-CSV row count falls versus the previous 2,602: about 165 alias-merged
     rows and 342 re-routed preregistrations leave, some recovered-text rows enter.
     A small net move is expected; a rise or a collapse (say, under 1,800 or over
     2,600) means read before shipping.
2. `.venv/bin/python -m extract.export --release 16d370746b45 --check` — expect
   zero differences.
3. `.venv/bin/python -m extract.sanity_check` — read the report.
4. `.venv/bin/python -m shared.cache_sync --push` — publish the bought answers.
   If it refuses because remote entries are missing locally, STOP and ask Lukas
   before using `--force` (a force replaces the shared cache with this machine's).
5. **Bookkeeping commit**: tick the five open entries in `PENDING_RUNS.md`
   (all are satisfied by this export; the dedup entry's proof is the row-count fall)
   and commit together with the refreshed page in step 6.
6. **Refresh `handover.html` section 2** from the new render (row counts, outcome
   table, set-aside table, the KPIs, remove the mid-flight warning box). Then:
   `~/.claude/skills/deploy-html/bin/deploy-doc.sh handover.html --slug flora-handover --domain flora-handover.surge.sh --no-comments --force`
7. `git push`.
8. Report to Lukas: rows shipped, the outcome distribution, actual OpenAI/DeepSeek
   spend for the campaign (read `cache/token_usage.json`), and the two follow-ups
   waiting in `handover.html` section 4 (the `search_confirm` grade read, the
   test-suite consolidation pass).

## Spend

The budget override is passed per invocation (step 2), never written to `.env` — the
committed cap stays the repo's cost guard for every other run. Report actual spend
against the $7–12 estimate when the run completes (step 8 above).

## Do not

- Do not run `filter.engine route`, `search.run_search`, or any screen command.
- Do not use `--redo`, `--redo-status`, `--limit` or `--only` on the live extract
  tier.
- Do not edit prompts, models or efforts in `shared/config.py`/`shared/prompts.py`
  — any such edit mints a new generation and reopens every settled work unless a
  reviewed `_GENERATION_EQUIVALENCES` entry says otherwise.
- Do not write to `data/extracted.csv` by any path except `extract.export`.
