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

| Source value | What it searches |
| --- | --- |
| `openalex` | 37 keyword phrases via `title_and_abstract.search` |
| `openalex_concept` | OpenAlex concept tags (`C12590798` Replication, `C9893847` Reproducibility) |
| `semantic_scholar` | Same 37 phrases via Semantic Scholar bulk search |
| `engine` | Internal engine source (requires `FLORA_USE_ENGINE=1`) |

```bash
# Phrase-based sources — need the auto-advance loop
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 --source openalex } until ($LASTEXITCODE -eq 2)
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 --source semantic_scholar } until ($LASTEXITCODE -eq 2)

# Concept-based source — single run or auto-advance loop (large result sets)
python -m search.run_search --source openalex_concept --from-year 2011 --to-year 2026
do { python -m search.run_search --auto-advance --source openalex_concept --from-year 2011 --to-year 2026 --max-per-phrase 10000 } until ($LASTEXITCODE -eq 2)

# Curated external lists (single fetch, no loop needed)
python -m search.run_search --source bob_reed
python -m search.run_search --source i4r
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

### Skipping the cache harvest in auto-advance

The harvest step scans all cached JSON pages and can be slow on large caches. Skip it per-call with `--no-harvest` and run it separately on a schedule:

```bash
# Run auto-advance without per-cycle harvest
do { python -m search.run_search --auto-advance --from-year 2011 --to-year 2026 --max-per-phrase 10000 --no-harvest } until ($LASTEXITCODE -eq 2)

# Run harvest separately (weekly, or after a crash)
python -m search.run_search --harvest-only
```

**Output:** `data/candidates.csv`

---

## Stage 2 — Filter

```bash
# Run filter on candidates.csv
python -m filter.run_filter

# Limit to specific year range
python -m filter.run_filter --from-year 2020

# Rebuild filtered index
python -m filter.run_filter --rebuild-index

# Filter using only rule-based classifier (no LLM calls)
python -m filter.run_filter --no-llm

# Reset screening decisions for rows that were decided with an empty abstract
# and whose abstract has since been backfilled into candidates.csv (dry-run)
python -m filter.reset_backfilled

# ...and apply: drop those rows from filtered.csv, rebuild the resume index
python -m filter.reset_backfilled --apply
```

**Input:** `data/candidates.csv`  
**Output:** `data/filtered.csv`

### Resetting backfilled screening decisions

Many candidates were screened by Stage 2 with an **empty abstract** (title-only
decisions). When `search/fetch_abstracts.py` later backfills abstracts into
`candidates.csv`, the resume index (`cache/filtered_index.txt`) still makes
`run_filter` skip those already-decided rows, so the recovered abstracts never
change the screening decision.

`python -m filter.reset_backfilled` fixes this in three streamed, memory-bounded
passes: it finds filtered rows decided with an empty abstract, keeps only those
whose `candidates.csv` row now has an abstract, and — with `--apply` — deletes
exactly those rows from `filtered.csv` before rebuilding the index. It **deletes
the row** rather than just its index key: `run_filter` only ever appends, so
dropping the key alone would produce a second decision for the same paper.

Operational sequence: run the abstract backfill → `reset_backfilled --apply` →
`run_filter` (the reset rows now come through with abstracts). Dry-run by default;
`--apply` to write. Do **not** run it while `run_filter` or `fetch_abstracts` is
writing.

---

## Stage 3 — Extract

```bash
# Run extraction (streams to extracted.csv)
python -m extract.run_extract

# Write to test sandbox instead of production
python -m extract.run_extract --extracted-test

# Resume from last processed row
python -m extract.run_extract --resume

# Resume, and also re-decide rows a previous run set aside on the classification screen
python -m extract.run_extract --resume --rescreen

# Skip LLM calls (rule-based only)
python -m extract.run_extract --no-llm

# Combine flags
python -m extract.run_extract --extracted-test --resume --no-llm

# Limit to N rows
python -m extract.run_extract --limit 50

# Re-extract papers already in FLoRA (the skip is ON by default)
python -m extract.run_extract --no-skip-flora-validated
```

**Input:** `data/filtered.csv`  
**Output:** `data/extracted.csv` (or `data/extracted-test.csv` with `--extracted-test`)

### Re-screening set-aside rows

`--resume` carries every already-resolved row forward untouched, including the rows
the classification screen decided on its own (`link_method`/`outcome` of
`not_a_replication`, plus the historical `screen_disagreement`). That is right for a resumed run and
wrong after the screen changes: an old voter pair's verdicts would survive
indefinitely. `--rescreen` reopens exactly those rows — the whole paper, so a
multi-original paper is re-screened as a unit — and leaves every other resolved row
carried forward.

Rows `sanity_check` has already moved out to `data/not_a_replication.csv` or
`data/screen_disagreement.csv` are no longer in `extracted.csv` and are therefore
re-processed by any run, with or without the flag. Their verdicts are still pinned by
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
→ `provisional_title_search.csv`; `target_pending` → `target_pending.csv`; and with
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
python -m analysis.run_overlap_analysis  # also produces rule_improvement_opportunities.csv

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

# Load a plain DOI list as pipeline input
python tools/load_doi_list.py path/to/dois.txt

# Clean up duplicate sources in candidates.csv
python tools/cleanup_sources.py

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
| `/multi-originals` | Multi-original paper review (not registered when `FLORA_READONLY=1`) |
| `/pipeline` | Redirects to `/dashboard` |
| `/api/dashboard/csv-stats` | Pipeline stats JSON (3-tier cascade: stats.json → Parquet → CSV) |
| `/api/dashboard/download` | Download a full stage CSV (`?stage=candidates\|filtered\|extracted\|extracted-test`) |
| `/api/check/search` | Filtered/paginated rows as JSON |
| `/api/check/download` | Filtered rows as CSV attachment |
| `/api/dashboard/supabase-stats` | Supabase validation KPIs |
| `/api/dashboard/supabase-outcomes` | Outcome distribution from validated table |
| `/api/dashboard/supabase-corrections` | Per-field correction frequency |
| `/api/dashboard/supabase-drilldown` | Paginated incorrect-DOI table |
