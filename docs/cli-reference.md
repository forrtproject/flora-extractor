# CLI Reference

All commands are run from the project root with `python -m <module>`.

---

## Stage 1 — Search

```bash
# Run full search across all sources (appends new results to candidates.csv)
python -m search.run_search

# Limit to specific year range
python -m search.run_search --from-year 2020 --to-year 2024

# Auto-advance: process one (source, phrase/concept, year) job per call; repeat until exit 2
python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 200

# Harvest cached API pages into candidates.csv without making new API calls
# (run this first after any crash or cursor deletion to recover orphaned pages)
python -m search.run_search --harvest-only

# Rebuild candidates index (if CSV was modified outside the pipeline)
python -m search.run_search --rebuild-index

# Reset all cursors to start fetching from page 1 again
python -m search.run_search --reset-cursors
```

### Backfilling missing abstracts

```bash
# Full run — resumable; already-tried identifiers are skipped
python -m search.fetch_abstracts

# Count what is missing, by identifier type, without calling any API
python -m search.fetch_abstracts --dry-run

# Skip the near-zero-yield OpenAlex phase and go straight to the DOI phases
python -m search.fetch_abstracts --skip-openalex

# Cap the Scopus phase (weekly quota ~10k) and spend it on chosen DOIs first
python -m search.fetch_abstracts --scopus-limit 9000 --scopus-priority dois.txt
```

Phases run cheapest-and-highest-yield first, each with its own checkpoint
namespace so adding or reordering one never invalidates another's progress:

| # | Phase | Key needed | Measured hit rate |
| - | ----- | ---------- | ----------------- |
| 1 | OpenAlex batch | — | ~0% (this corpus was discovered via OpenAlex) |
| 2 | **Europe PMC batch** | — | **47.7%** |
| 3 | Semantic Scholar batch | `S2_API_KEY` | 8.5% here, 14.5% corpus-wide |
| 4 | CrossRef by DOI | — | 0.3–0.6% |
| 5 | Scopus by DOI | `ELSEVIER_API_KEY` | quota-capped fallback |

Rates for phases 2–4 come from one 960-DOI sample (2026-07-29) of never-tried
rows drawn across the corpus's dominant prefixes. Europe PMC leads because 69% of
this corpus's missing abstracts are Elsevier (10.1016) and Springer (10.1007),
neither of which deposits abstracts to CrossRef — and OpenAlex's abstract index
derives from that same deposit stream. Semantic Scholar still runs after it: the
two overlap only partly, and S2 is the only source that sees SSRN (10.2139).

Dataset DOIs (`10.7910` Harvard Dataverse, `10.5281` Zenodo) are excluded from
every phase — they register data, not articles, so no abstract exists to find.
They are still counted in the "rows missing abstract" total, and reported
separately.

### Filtering by source

The `--source` flag restricts which discovery tracks run. It can be repeated.

The accepted values are `_ALL_SOURCES` in `search/run_search.py`:

| Source value | What it searches |
| --- | --- |
| `openalex` | The keyword phrases in `SEARCH_PHRASES` (`search/openalex_search.py`) via `title_and_abstract.search` |
| `openalex_concept` | OpenAlex concept tags (`C12590798` Replication, `C9893847` Reproducibility) |
| `semantic_scholar` | The same `SEARCH_PHRASES` list, which `search/semantic_scholar_search.py` imports, via S2 bulk search |
| `openalex_snapshot` | The bulk-parquet snapshot scan (opt-in; see below) |
| `engine` | Internal engine source (requires `FLORA_USE_ENGINE=1`) |

Default is every source **except** `openalex_snapshot`. `bob_reed` and `i4r` are
`source` *column* values, not `--source` values: their scrapers live in
`search/external_lists.py` and are not wired into `run_search` (issue #46).

```bash
# Phrase-based sources — need the auto-advance loop
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 --source openalex } until ($LASTEXITCODE -eq 2)
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 --source semantic_scholar } until ($LASTEXITCODE -eq 2)

# Concept-based source — single run or auto-advance loop (large result sets)
python -m search.run_search --source openalex_concept --from-year 2011 --to-year 2026
do { python -m search.run_search --auto-advance --source openalex_concept --from-year 2011 --to-year 2026 --max-per-phrase 10000 } until ($LASTEXITCODE -eq 2)
```

### Concept ID management

Concept IDs are defined in `CONCEPT_IDS` inside `search/openalex_search.py`. To look up IDs:

```bash
# Print OpenAlex concepts matching a query (live API call, then exit)
python -m search.run_search --list-concepts "replication"
python -m search.run_search --list-concepts "reproducibility"
```

Current verified IDs (as of 2026-06-23):

- `C12590798` — Replication (statistics) — ~263k works
- `C9893847` — Reproducibility — ~121k works

### Harvesting the cache separately

The harvest step scans all cached JSON pages and can be slow on large caches. Run it
on its own after a crash, or when an `--auto-advance` loop stopped on resource
exhaustion before merging what it had fetched:

```bash
python -m search.run_search --harvest-only
```

### The OpenAlex snapshot scan and its survivor pool

The bulk-parquet scan is Stage 1's main path and takes 13–21 hours over 725 GB.
`--survivor-pool` writes every **search-gate** survivor to local parquet (~2–3 GB,
one file per partition). That pool is Stage 1's output and Stage 2's input, so a
rule change is a `filter.engine route` re-run over it rather than a rescan; only a
change to the search gate itself costs the full scan.

```bash
# Full snapshot scan, writing the survivor pool
python -m search.run_search --source openalex_snapshot --survivor-pool cache/snapshot_pool

# How far along is a running scan? Read-only, safe to run concurrently with it:
# files/bytes/records consumed vs the manifest, rows kept, recent throughput, ETA.
python -m search.snapshot_scan --status
python -m search.snapshot_scan --status --json
```

Running the scan on a cloud instance in us-east-1 turns those 13–21 hours into 2–5
and costs a couple of dollars: see [aws-snapshot-scan.md](aws-snapshot-scan.md) for
the launch scripts and the runbook.

### Sharing the pool

The pool lives in one **private** Hugging Face dataset repo. Set `HF_TOKEN` and
`FLORA_POOL_REPO` in `.env`. Uploads go up in batched commits
(`FLORA_HF_COMMIT_BATCH`, default 100 files per commit) — one commit per file
would push the repo past the few-thousand-commit mark where HF says repo UX
degrades.

**Collaborator workflow — one command:**

```bash
python -m search.pool_sync --pull        # the survivor pool → Stage 2's input
```

Pool files are stored **year-sharded** on the remote
(`part-2016-06-24-part_0000.parquet` → `2016/…`) and flat locally; both
directions skip files already present at the same size, so an interrupted
transfer is resumed by re-running the command.

```bash
# Prove this machine can write to the repo BEFORE a long scan (commits preflight.json)
python -m search.pool_sync --check-access

# Upload the local pool (creates the private repo on first push)
python -m search.pool_sync --push
python -m search.pool_sync --push --dry-run

# Download the whole pool, or only some partition years
python -m search.pool_sync --pull
python -m search.pool_sync --pull --years 2019,2021-2023
python -m search.pool_sync --pull --pool-dir /mnt/big/pool --repo my-org/flora-survivor-pool
```

A push writes `pool_manifest.json` at the repo root recording the search gate,
snapshot date and ledger the pool was scanned under. Pushing over a pool scanned
under a **different** gate fingerprint is refused (`--force` overrides) — the
mixture would be complete under neither gate and nothing downstream could tell.
Pulling one only warns: taking a colleague's pool is legitimate, doing so
unknowingly is not.

The prebuilt-`candidates.csv` build commands (`--build-candidates`,
`--push-build`, `--pull-build`) belonged to the admission-gated Stage 1 and are
retired with it; the pool is the artifact to share.

**Output:** the survivor pool (`cache/snapshot_pool`)

---

## Stage 2 — Filter

Stage 2 **is** the filter engine: `python -m filter.engine <command>`. The
per-row rule classifier (`filter.run_filter`, `filter/rule_filter.py`) and its
`reset_backfilled` companion have been removed; nothing in the pipeline calls them
any more.

Issue #146's declarative routing layer: one engine applies the spec bundle in
`filter/spec/` to the survivor pool and routes every row into a pile. It reads the
pool parquet directly, and its design contract is
[filter-engine.md](filter-engine.md).

**Stage 2 is where every precision decision lives.** Stage 1 searches and admits
generously; the spec bundle is the one rule set that decides what a paper is.

**Input:** the survivor pool (`cache/snapshot_pool`), plus the text overlay in
`cache/engine/overlay` when it holds any  
**Output:** `data/filtered.csv`, written by `handoff`

The usual order is `route` → `screen` → `handoff`.

```bash
# What the bundle currently says — one line per spec, plus the bundle hash
python -m filter.engine specs

# Prove the two backends (Python re / pyarrow) agree before trusting a run
python -m filter.engine verify --pool cache/snapshot_pool --sample-files 3

# Route the pool: mints a release id, writes cache/engine/releases/<id>.json,
# fills the DuckDB store, prints the pile counts
python -m filter.engine route --pool cache/snapshot_pool

# What one rule moves, what already covered it, and whether it is measured
python -m filter.engine diagnose --spec dataset-type --pool cache/snapshot_pool

# Materialize a pile as a Stage 3 CSV, with an immutable manifest beside it
python -m filter.engine export --pile screen_expensive --out data/engine_expensive.csv \
    --pool cache/snapshot_pool --from-year 2011

# What an LLM tier would cost over its pile — claims nothing, spends nothing
python -m filter.engine screen --tier screen_expensive

# Claim a small first batch and spend on it
python -m filter.engine screen --tier screen_expensive --run --limit 500 \
    --batch-label first-expensive-batch

# The cheap tier: verdicts recorded, no effect on anything, until --live
python -m filter.engine screen --tier screen_cheap --run
python -m filter.engine screen --tier screen_cheap --run --live

# The pending/no_text rows, as a worklist for the abstract backfill
python -m filter.engine worklist --out data/no_text_worklist.csv --pool cache/snapshot_pool

# Write the file Stage 3 reads
python -m filter.engine handoff --out data/filtered.csv --from-year 2011

# Push response blobs an earlier run left pending (dry run without --run)
python -m filter.engine reconcile

# Releases on disk and the pile counts each of them routed
python -m filter.engine status
```

| Command | What it does |
| ------- | ------------ |
| `specs` | Lists the loaded bundle (id, pile, precedence, shadow, measurement levels), the bundle hash, the engine version and the export schema version. Fails loudly if any spec is invalid. |
| `verify` | Runs both backends over the first batch of up to `--sample-files` pool files and prints every (spec, row) they disagree on. **Exit 1 on any mismatch** — it is meant for CI and for the check before a long run. |
| `route` | Computes the routing release id from its six inputs, records the release, and streams the pool through the bundle into the store. Idempotent per release: re-running replaces that release's rows rather than duplicating them. |
| `diagnose` | Routes the pool with and without `--spec` and reports rows moved per (pile without → pile with), overlap against every other rule (exclusive hits vs already-covered), a seeded readable sample, the holdout state and the spec's `measured` evidence. |
| `export` | Writes one pile as `FILTERED_COLS` + `ENGINE_EXPORT_COLS`, `utf-8-sig`, plus `<out>.manifest.json` (release, pile, rows, sha256). `--pile pending` is refused, an existing manifest is never overwritten, and an export is refused outright when the spec bundle or alias file has changed since the release was routed — re-run `route` rather than looking for an override flag. `--pile needs_human` additionally prints the size of the queue it just wrote. |
| `screen` | Runs one LLM tier (`--tier screen_cheap\|screen_expensive`) over that pile. **Dry run by default**: it prints the row count, the token-length distribution of the abstracts it would send and `N rows → tier X ≈ $Y`, and claims, fetches and spends nothing. `--run` claims the batch through the Supabase claims RPC *before* the first voter is asked, records one permanent verdict row per vote, and completes the claim; a claim conflict refuses without spending anything, and an exhausted token budget fails the claim and stops with the verdicts already written intact. |
| `reconcile` | Sweeps verdict rows an EARLIER run left `response_pending_upload` — the flag off, no token, a commit that 429'd — matches them to the blobs in `cache/engine/responses/` by response hash, commits those in `FLORA_HF_COMMIT_BATCH`-sized commits and marks only what a commit accepted. **Dry run by default**; `--run` acts. A pending row whose blob has been deleted is reported, not fatal. Refuses outright when Hugging Face is unconfigured (`ENGINE_TIER_HF_UPLOAD` off, no `FLORA_POOL_REPO`, no `HF_TOKEN`) rather than sweeping nothing. |
| `worklist` | Exports the release's `pending/no_text` rows (joined back to the pool for doi/title/year) as the worklist `filter.engine.backfill` reads. |
| `handoff` | Writes the two screen piles — `screen_expensive` first, then `screen_cheap` — as the file Stage 3 reads, in `ENGINE_EXPORTED_COLS` order, with a live `screen_expensive` record type written into `filter_status`. **Only rows a live tier run reached a verdict on travel**: a discarded work is left out and counted as `dropped_by_tier_verdict`, a work no live run decided (never screened, or still short of a second vote) is left out and counted as `skipped_unscreened`. `--as-routed` exports the piles as routed instead, applying whatever verdicts exist. Unlike `export`, its manifest is rewritable: the handoff is a materialized view Stage 3 re-reads, not an immutable artifact. |
| `status` | Every release found beside the store, with its creation time and pile counts. |

**The text overlay loads itself.** `route`, `export`, `screen` and `handoff` read
the overlay in `FLORA_OVERLAY_DIR` (default `cache/engine/overlay`) whenever that
directory holds overlay chunks — no flag needed, because an overlay-only rule
(the `osf-registration-*` pair) silently matches nothing without it. `--overlay
DIR` points at another overlay, `--no-overlay` runs against the bare pool, and
each command prints one line — `overlay: <dir> (hash <12>)` or `overlay: none` —
saying which text it read. The overlay folds into the release id, so `export`,
`handoff` and `screen` refuse a release routed under a different overlay.

`--spec-dir` (before the subcommand) points at a different bundle; `--store`
defaults to `cache/engine/engine.duckdb` and is **disposable** — deleting it costs
a `route` and nothing else, because routing is a pure function of pool, specs,
aliases and engine version. `route` takes the pool's provenance from
`--pool-manifest-hash`, else the local snapshot ledger, else the literal
`unmanifested` (an honest "unknown", not a claim about which pool this was).

**`screen` modes.** `screen_cheap` runs in `validation` mode by default: its votes
are recorded and its would-be discards are reported against the piles the rules
chose, and nothing is discarded. That is issue #146 §2 — the tier's zero-miss
evidence was measured on a post-gate distribution and has to be re-validated on
this one before it discards autonomously. `--live` is what makes those discards
take effect. `screen_expensive` is Stage 3's validated front door
(`classify_replication()` + `screen_gate()`) run ahead of Stage 3, so it has no
validation mode to earn and runs live; `--limit` is how a first paid batch stays
small enough to read. `--run` needs `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`
because the claim is what stops two runs spending on the same works; a dry run
needs neither.

**`handoff` exports what was screened.** Routing says a row deserves an LLM's
attention; only the LLM says it reaches Stage 3. So a row the rules put in a
screen pile but no live tier run ever decided is held back, and the manifest
accounts for it separately from a discard (`skipped_unscreened` vs
`dropped_by_tier_verdict`). `--as-routed` is the older behaviour, and the only
one available without Supabase: with no claims client there are no verdicts, so
screened-only would write an empty file and the command refuses instead of doing
that quietly. It refuses too, like `export`, when the spec bundle, alias file or
overlay has moved since the release was routed.

### Engine modules with their own entry points

Three engine modules are run directly rather than as `filter.engine` subcommands.
Each has `--help`; the one-liners are their own `description=`:

```bash
# Fill a text overlay for the routing worklist's no_text rows (#146 M3). Dry-run by default.
python -m filter.engine.backfill --worklist W [--overlay-dir D] [--run] [--freeze]

# Reconcile a routing change against the validation tables (#146 M5).
# Writes lineage records only — never a validation row. Dry-run by default.
python -m filter.engine.supersede --old <release> --new <release> [--run] [--reason …]

# Postgres sizing for the engine state tables (#146 §8 decision 1): measures the
# bytes one claimed row and one verdict actually cost.
python -m filter.engine.sizing
```

---

## Stage 3 — Extract

```bash
# Run extraction (streams to extracted.csv)
python -m extract.run_extract

# Write to test sandbox instead of production
python -m extract.run_extract --extracted-test

# Start over: discard extracted.csv and re-extract (and re-pay for) every row
python -m extract.run_extract --fresh

# Also re-decide rows a previous run set aside on the classification screen
python -m extract.run_extract --rescreen

# Skip LLM calls (rule-based only)
python -m extract.run_extract --no-llm

# Combine flags
python -m extract.run_extract --extracted-test --no-llm

# Limit to N rows
python -m extract.run_extract --limit 50

# Re-extract papers already in FLoRA (the skip is ON by default)
python -m extract.run_extract --no-skip-flora-validated
```

**Input:** `data/filtered.csv`  
**Output:** `data/extracted.csv` (or `data/extracted-test.csv` with `--extracted-test`)

The examples above are a selection, not the flag list. For the complete, current set
run:

```bash
python -m extract.run_extract --help
```

(the `argparse` block at the bottom of `extract/run_extract.py` is its source). The
sections below explain the flags whose behaviour is not obvious from one help line.

### Two flags that do not mean what they look like

**`--limit N` counts rows *processed*, not rows scanned.** The counter increments
after `_should_skip()` has passed a row, so a run over a file whose first 10,000
rows are all skipped (already resolved, already in FLoRA, wrong year, wrong source)
still processes N fresh rows — and reads far more than N. Use it to bound *spend*,
never to bound how much of the input is touched, and never to reason about which
rows a run saw.

**`--fresh` truncates the output CSV.** The first row written opens the file with
mode `w`, and an ordinary run pre-loads the existing rows before that happens, so the
file is rewritten from what it already held. `--fresh` skips that pre-load, so the
previous run's output is discarded rather than added to — in the test sandbox as
well as in production.

### Re-screening set-aside rows

An ordinary run carries every already-resolved row forward untouched, including the rows
the classification screen decided on its own (`link_method`/`outcome` of
`not_a_replication`, plus the historical `screen_disagreement`). That is right for a resumed run and
wrong after the screen changes: an old voter pair's verdicts would survive
indefinitely. `--rescreen` reopens exactly those rows — the whole paper, so a
multi-original paper is re-screened as a unit — and leaves every other resolved row
carried forward.

Historical cheap-tier discards (`prescreen_discard`) are reopened by the same flag, and
this is the only way back: a resume treats a set-aside row as settled.

Rows `sanity_check` has already moved out to `data/not_a_replication.csv`,
`data/screen_disagreement.csv` or `data/prescreen_discard.csv` are no longer in
`extracted.csv`, but a resume reads those files and treats every key in them as settled
— so without the flag they are skipped, not re-processed. Their verdicts are also pinned by
the screen cache, but that cache is keyed on the screening prompt's version, both
voter models and the abstract itself — so changing a voter or the prompt makes a
re-screen actually re-vote, with nothing to bump by hand.

### Skipping papers already in FLoRA

`--skip-flora-validated` is **on by default**: Stage 3 will not re-extract a
replication that FLoRA already has. The skip list is the union of two sources:

| Source | Rows skipped |
| ------ | ------------ |
| `data/FLoRA entry sheet - replication list.csv` | rows whose `validation_status` is `validated - unchanged`, `validated - changed`, `validated - chosen`, or `validated - discarded` |
| `data/flora.csv` | **every** row (`doi_r` and `doi_r_alt`) — the published database, so all of it is already in FLoRA |

Statuses still in flight (`help needed`, `on hold`, `awaiting validation`, blank) are
**not** skipped — those genuinely need the pipeline. Pass `--no-skip-flora-validated`
to re-extract everything anyway.

The same skip list gates the validation hand-off, so a paper FLoRA already has cannot
reach validators even if it is already sitting in `extracted.csv`:

```bash
python -m extract.csv_to_db --input data/extracted.csv        # gate ON by default
python -m extract.csv_to_db --no-skip-flora                   # import them anyway
```

Both stages import it from `shared/flora_skip.py`, so extraction and validation can
never drift apart.

A missing or unreadable source logs a warning and contributes nothing, so one bad file
cannot silently disable the whole skip list.

### Skipping works already in the validation tables

`--skip-validated` is **on by default** and answers a different question: not "is this
already published in FLoRA" but "has someone already validated it". Supabase
`record_metadata` holds ~1,770 records seeded from the prior FLoRA pipeline, and those
works must never be extracted again.

That set is frozen — everything validated from here on flows through this pipeline and
is held out by `extracted.csv`'s own resume keys — so it is a static, committed file
rather than a query. `analysis/build_validated_skip.py` materialises it once:

```bash
python -m analysis.build_validated_skip            # dry run: prints the counts
python -m analysis.build_validated_skip --apply    # writes data/validated_skip.csv
```

Stage 3 then reads `data/validated_skip.csv` (`shared/flora_skip.load_validated_skip()`)
and skips a row whose OpenAlex work id **or** cleaned `doi_r` is in it — two
identifiers because a legacy record may carry only one of them, and so may a pool row.
A missing file logs a warning and skips nothing. The run summary counts these
separately from the FLoRA skips; pass `--no-skip-validated` to extract them anyway.

### Promoting test results

```bash
# Promote all test rows to production
python -m extract.promote_test --all

# Promote a single DOI
python -m extract.promote_test --doi 10.1234/example

# Preview without writing
python -m extract.promote_test --all --dry-run

# Force overwrite (skip conflict check)
python -m extract.promote_test --all --force
```

### Post-extraction sanity check

Runs automatically at the end of every `run_extract` (on completion and on Ctrl-C).
Also runnable standalone:

```bash
# Move problem rows to the set-aside CSVs + report integrity flags
python -m extract.sanity_check

# Check the test sandbox instead
python -m extract.sanity_check --input data/extracted-test.csv

# Report only — move nothing
python -m extract.sanity_check --report-only

# Also network-verify unregistered doi_o against doi.org and quarantine fabrications
python -m extract.sanity_check --deep
```

Rows land in the **first** bucket they match: `screen_disagreement` →
`screen_disagreement.csv` (historical rows only — the front door no longer emits that
value); `outcome == not_a_replication` and non-article `doi_r` →
`not_a_replication.csv`; self-links → `unresolved_self_links.csv`;
`doi_o_verification == mismatch` → `unresolved_doi_mismatch.csv`; `llm_title_search`
→ `provisional_title_search.csv`; `target_pending` → `target_pending.csv`;
`link_method == prescreen_discard` → `prescreen_discard.csv` (ahead of the outcome rule,
so the cheap pre-screen's discards never mix into the validated screen's file); and with
`--deep`, fabricated `doi_o` → `fabricated_original_doi.csv`. `cannot_be_determined`
rows stay in `extracted.csv`.

### Parse-cache cleanup

Deletes all-empty parse caches from `cache/parse/`. Runs before audit B4 parsed every
non-multi row, including rows that exited at the reference screen with no PDF, and the
resulting empty cache then masked the real parse on any later run that did get the PDF.

```bash
python -m extract.clean_parse_cache          # dry run: count and report
python -m extract.clean_parse_cache --apply  # delete them
```

### DOI verification audit

Retroactively verify `doi_o` values in an existing CSV. Runs automatically during extraction; use this to audit rows that predate the feature.

```bash
# Dry run: print summary + write data/doi_audit_report.csv
python -m extract.audit_dois

# Write corrections into extracted.csv
python -m extract.audit_dois --apply

# Audit a single DOI
python -m extract.audit_dois --doi 10.1234/example

# Audit extracted-test.csv instead
python -m extract.audit_dois --extracted-test
```

---

## Stage 4 — Monitoring web app

```bash
# Start the web app
python -m validate.app
# → http://localhost:5001
```

The app is read-only — it displays pipeline stats and pulls validation data from Supabase. No writes to local files.

---

## Analysis

```bash
# Overlap / recall gap analysis — compares all_replications.csv against candidates.csv
# Reports genuine gaps (papers in the reference set not found by Stage 1)
python -m analysis.run_overlap_analysis

# Rule analysis — audit filter rules and extraction link methods
# (writes analysis/rule_improvement_opportunities.csv)
python -m analysis.rule_analysis

# APA reference resolver
python -m analysis.apa_resolver
```

**Outputs:** CSV and Markdown files in `analysis/` (see [code-flow/analysis.md](code-flow/analysis.md) for what each file means)

Key output files:

- `analysis/gap_summary.md` — human-readable recall gap report
- `analysis/gap_analysis_doi_matched.csv` — gaps where the reference has a DOI
- `analysis/gap_analysis_url_matched.csv` — gaps where the reference has a URL but no DOI
- `analysis/rule_improvement_opportunities.csv` — ranked filter/extract improvement suggestions
- `analysis/extraction_audit.md` — link method and confidence breakdown

---

## Tools

```bash
# Recalibrate outcome values in extracted.csv
# Must be run as a module from the project root (not from inside tools/)
python -m tools.recalibrate_outcomes

# Only reprocess recently added rows (last N rows of the CSV, which are the newest appended entries)
python -m tools.recalibrate_outcomes --tail 50

# Only reprocess rows from a given publication year onward
python -m tools.recalibrate_outcomes --since-year 2022

# Force fresh LLM calls (clears cached outcomes for rows being reprocessed)
python -m tools.recalibrate_outcomes --tail 50 --clear-cache

# Preview without writing
python -m tools.recalibrate_outcomes --tail 50 --dry-run

# Process only first N uncertain rows (for testing a prompt change)
python -m tools.recalibrate_outcomes --limit 10 --dry-run

# Drop superseded preprint versions (keep highest _v, or the version-less DOI) — issue #17
python -m tools.dedup_preprint_versions --input data/extracted.csv            # dry-run
python -m tools.dedup_preprint_versions --input data/extracted.csv --apply

# Backfill oa_work_id_r / oa_work_id_o on rows written before those columns existed.
# New rows get them automatically from run_extract — this is only for old rows.
python -m tools.backfill_oa_work_ids                                    # dry-run
python -m tools.backfill_oa_work_ids --apply                            # write
python -m tools.backfill_oa_work_ids --input data/extracted-test.csv --apply
```

---

## Tests

```bash
# Run all unit tests
python -m pytest tests/

# Run specific test file
python -m pytest tests/test_extract.py -v

# Run with live API access (requires TEST_LIVE_API=1)
TEST_LIVE_API=1 python -m pytest tests/live/

# Run with coverage
python -m pytest tests/ --cov=. --cov-report=html
```

---

## Cache management

```bash
# Clear all caches
python -c "import shutil; shutil.rmtree('cache', ignore_errors=True)"

# Clear only parse cache (re-fetch PDFs and re-parse)
python -c "import shutil; shutil.rmtree('cache/parse', ignore_errors=True)"

# Clear only LLM result cache
python -c "import shutil; shutil.rmtree('cache/llm', ignore_errors=True)"
```

---

## Web app routes

| Route | Description |
| ----- | ----------- |
| `/` | Redirects to `/dashboard` |
| `/dashboard` | 6-tab monitoring dashboard — see [dashboard-guide.md](dashboard-guide.md) |
| `/check` | Search/filter/download across any stage — see [check-page.md](check-page.md) |
| `/batch` | Batch disambiguation for multiple-match papers (not registered when `FLORA_READONLY=1`) |
| `/pipeline` | Redirects to `/dashboard` |
| `/api/dashboard/csv-stats` | Pipeline stats JSON (3-tier cascade: stats.json → Parquet → CSV) |
| `/api/dashboard/download` | Download a full stage CSV (`?stage=candidates\|filtered\|extracted\|extracted-test`) |
| `/api/check/search` | Filtered/paginated rows as JSON |
| `/api/check/download` | Filtered rows as CSV attachment |
| `/api/dashboard/supabase-stats` | Supabase validation KPIs |
| `/api/dashboard/supabase-outcomes` | Outcome distribution from validated table |
| `/api/dashboard/supabase-corrections` | Per-field correction frequency |
| `/api/dashboard/supabase-drilldown` | Paginated incorrect-DOI table |
