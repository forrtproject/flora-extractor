# Babysit brief: finish the 2026-08 campaign's re-extract and export

For the agent that watches the rest of the run. Everything before the re-extract is
done; do not redo it. The campaign order and rationale are in
`docs/handover-campaign-2026-08.md`; the owed runs with done-criteria are in
`PENDING_RUNS.md`. This brief is only what remains and how to shepherd it.

## Fixed facts

- **Release: `16d370746b45`.** Every command names it. Do NOT run
  `filter.engine route` — a new route mints a new release and invalidates this one.
- **Interpreter: `.venv/bin/python`**, from the project root.
- **The machine is on CEST (UTC+2).** The OpenAI free budget (9.5M tokens/day)
  resets at midnight UTC = **02:00 local**.
- **Resume state lives in Postgres**, not in any file. A killed or budget-stopped
  run loses nothing; re-running the same command continues where it stopped.
- The extract worklist is 5,928 works. As of 2026-08-15 ~05:00 UTC, 890 are settled.
  Expect roughly 1,000 works per free budget window; completion around 2026-08-19
  unless Lukas raises the budget (see "The one open decision").

## The loop

Each day shortly after 02:17 local:

1. **Free any claims a dead run left** (harmless when there are none):
   `.venv/bin/python -m filter.engine release-claim`
   — if it lists active claims, end each one:
   `.venv/bin/python -m filter.engine release-claim --claim <id> --status failed --yes`
2. **Resume, in the background, with a log**:
   `caffeinate -i .venv/bin/python -m extract.tier --release 16d370746b45 --run`
   (`caffeinate -i` stops idle sleep from killing the run — that happened once.)
3. **Watch the log** for `TokenBudgetExhausted`, `OpenAlexQuotaExhausted`,
   `ClaimConflict`, `Traceback`. Retry-and-continue noise (`Retrying request`,
   per-row `api_error`) is normal.
4. When the run stops on `TokenBudgetExhausted`: release claims (step 1) and wait
   for the next 02:17. That stop is clean by design; nothing needs repair.
5. Progress check (cheap, any time):
   `.venv/bin/python -c "import shared.config; from filter.engine.claims import ClaimsClient; from extract.tier import settled_work_ids; print(len(settled_work_ids(ClaimsClient())))"`

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

## The one open decision (Lukas's, not yours)

Raising `OPENAI_DAILY_TOKEN_BUDGET` in `.env` (or setting `0` to disable) finishes
the run in one go at roughly $15–30 of paid OpenAI spend. Do not change it on your
own — the cap is the repo's cost guard. If Lukas says to raise it, do so, relaunch,
and report actual spend against that estimate.

## Do not

- Do not run `filter.engine route`, `search.run_search`, or any screen command.
- Do not use `--redo`, `--redo-status`, `--limit` or `--only` on the extract tier.
- Do not edit prompts or models in `shared/config.py`/`shared/prompts.py` — any
  such edit mints a new generation and reopens every settled work.
- Do not write to `data/extracted.csv` by any path except `extract.export`.
