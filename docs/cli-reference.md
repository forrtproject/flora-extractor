# CLI Reference

All commands are run from the project root with `python -m <module>`.

---

## Stage 1 — Search

Stage 1 does the OpenAlex snapshot scan and nothing else. The API-harvest sources
(phrase search, concept search, Semantic Scholar, the discovery engine) are retired
to `wip/api-harvest-sources`: they wrote `data/candidates.csv`, which nothing
downstream reads. Stage 2 reads the SURVIVOR POOL, so the pool is what a scan
produces, and `run_search` is a thin operator front-end over
`search/snapshot_scan.py`.

`--scan` is **required**. A bare `python -m search.run_search` exits 2 with usage
rather than start a 725 GB, 13–21 hour read, and the whole parser is four flags:

```bash
# Full corpus scan into the default pool (cache/snapshot_pool, or FLORA_POOL_DIR).
# Resumable: partitions the ledger already marks done are skipped.
python -m search.run_search --scan

# Write the pool somewhere else. This is Stage 2's input — a scan that writes no
# pool produces nothing.
python -m search.run_search --scan --survivor-pool /mnt/big/pool

# Stop after N snapshot partitions this run
python -m search.run_search --scan --snapshot-max-files 20

# Scan on even though the ledger was written under a DIFFERENT search gate
python -m search.run_search --scan --force-gate
```

Progress is a separate, read-only command (below), safe against a scan in flight.

### Backfilling missing abstracts

`search/fetch_abstracts.py` is a library, not a command: it owns the six sources
and their transient-vs-definitive contract, and its one caller is
`python -m filter.engine.backfill` (Stage 2), which supplies the worklist and
writes the recovered text into an overlay chunk. Run the backfill, not this
module — its CLI is in the Stage 2 section below.

```bash
# Cheap bulk pathway over a wide worklist: batched, keyless, unquota'd
python -m filter.engine.backfill --worklist wide.parquet --run --phase bulk

# Gated pathway, over the rows bulk left without text; Scopus is capped
python -m filter.engine.backfill --worklist no_text.parquet --run --phase targeted \
    --scopus-limit 9000
```

The two pathways, each source with its own checkpoint namespace so adding or
reordering one never invalidates another's progress:

| Pathway | # | Source | Key needed | Measured hit rate |
| ------- | - | ------ | ---------- | ----------------- |
| bulk | 1 | OpenAlex batch — **opt-in**, `--include-openalex` | — | 0/200 measured (this corpus was discovered via OpenAlex, and the live API's abstracts come from the same deposit stream the snapshot did) |
| bulk | 2 | **Europe PMC batch** | — | **47.7%** |
| targeted | 3 | OSF registrations (`10.17605` only) | `OSF_TOKEN` for private records | recovers a registration template, not an abstract |
| targeted | 4 | Semantic Scholar batch | `S2_API_KEY` | 8.5% here, 14.5% corpus-wide |
| targeted | 5 | CrossRef by DOI | — | 0.3–0.6% |
| targeted | 6 | Scopus by DOI | `ELSEVIER_API_KEY` | quota-capped fallback (~10k/week) |

Bulk goes first and over everything, because a batched keyless request answers
about `EPMC_BATCH_SIZE` DOIs at once; targeted sees only what bulk left without
text, because those calls are one per DOI or bought with a key, an entitlement or
a weekly quota.

OpenAlex is not in the default bulk run. `--include-openalex` (or `--source
openalex`) turns it on, and it is worth turning on only when the snapshot is old
enough that deposits made since it was cut are plausible.

Rates for sources 2, 4 and 5 come from one 960-DOI sample (2026-07-29) of never-tried
rows drawn across the corpus's dominant prefixes. Europe PMC leads because 69% of
this corpus's missing abstracts are Elsevier (10.1016) and Springer (10.1007),
neither of which deposits abstracts to CrossRef — and OpenAlex's abstract index
derives from that same deposit stream. Semantic Scholar still runs after it: the
two overlap only partly, and S2 is the only source that sees SSRN (10.2139).

Dataset DOIs (`10.7910` Harvard Dataverse, `10.5281` Zenodo) are excluded from
every phase — they register data, not articles, so no abstract exists to find.
They are still counted in the "rows missing abstract" total, and reported
separately.

### The OpenAlex snapshot scan and its survivor pool

The bulk-parquet scan is Stage 1's main path and takes 13–21 hours over 725 GB.
`--survivor-pool` writes every **search-gate** survivor to local parquet (~2–3 GB,
one file per partition). That pool is Stage 1's output and Stage 2's input, so a
rule change is a `filter.engine route` re-run over it rather than a rescan; only a
change to the search gate itself costs the full scan.

```bash
# Full snapshot scan, writing the survivor pool. --scan is required: a bare
# `python -m search.run_search` exits with usage rather than start a 725 GB read.
python -m search.run_search --scan --survivor-pool cache/snapshot_pool

# How far along is a running scan? Read-only, safe to run concurrently with it:
# files/bytes/records consumed vs the manifest, rows kept, recent throughput, ETA,
# and the pool's provenance (below).
python -m search.snapshot_scan --status
python -m search.snapshot_scan --status --json

# Stamp an existing pool that predates the sidecar, without re-scanning or
# re-pulling. The gate comes from the local ledger; with no ledger it REFUSES
# rather than guess, and the fingerprint must be given:
python -m search.snapshot_scan --stamp-pool
python -m search.snapshot_scan --stamp-pool --gate <fingerprint>   # from the remote manifest
python -m search.snapshot_scan --stamp-pool --gate local           # "this checkout scanned it"
```

**The pool's provenance sidecar** — `_pool_provenance.json`, written beside the
parquet by the scan and by `pool_sync --pull`, and readable in `--status`. It
records the search-gate fingerprint the pool's rows were **admitted** under, the
file count that completes the pool, and where the value came from (`scan`,
`pull:<repo>`, `stamp:…`). Two things depend on it, both in the routing release
id (`filter.engine route`): the gate hashed into the id is the pool's, not the
reading checkout's, so a shared pool fingerprints identically everywhere; and a
pool with fewer files than the sidecar expects is an interrupted transfer, which
routes under `unmanifested` instead of being fingerprinted as though complete. An
**unstamped** pool still routes — refusing would strand every pool that exists —
but its gate enters the id as *unknown* and every run says so loudly. The name is
underscore-prefixed so neither the `*.parquet` globs nor pyarrow's dataset
discovery ever sees it.

There is no sample or pilot mode. To scan a few partitions for a look, run the
same command against a scratch state directory — `FLORA_CACHE_DIR=/tmp/flora-sample
python -m search.run_search --scan --snapshot-max-files 3` moves the ledger and,
with it, the pool (`FLORA_POOL_DIR` defaults under the cache dir). The retired
`--snapshot-pilot` wrote real parquet into the production pool while keeping no
ledger, so the two disagreed about what had been consumed.

A scan whose ledger names a **different** search-gate fingerprint is refused: the
partitions it marks done were read under the other gate, and the rows that gate
rejected are in no pool at all, so continuing builds a pool complete under
neither. Rescan into a fresh pool directory and a fresh ledger, or pass
`--force-gate` to add to the mixture knowingly. A ledger that exists but cannot
be parsed is a hard error for the same reason — silently treating it as empty
orders a 725 GB rescan.

Running the scan on a cloud instance in us-east-1 turns those 13–21 hours into 2–5
and costs a couple of dollars: see [aws-snapshot-scan.md](aws-snapshot-scan.md) for
the launch scripts and the runbook.

### Sharing the pool

The pool lives in one **private** Hugging Face dataset repo. Set `HF_TOKEN` and
`FLORA_POOL_REPO` in `.env`. Uploads go up in batched commits
(`FLORA_HF_COMMIT_BATCH`, default 100 files per commit) — one commit per file
would push the repo past the few-thousand-commit mark where HF says repo UX
degrades. A pull fetches `FLORA_HF_PULL_WORKERS` files at a time (default 8):
each file waits on an auth and CDN round trip before its first byte, so a
one-at-a-time pull spends about half its wall clock idle and takes hours over a
link that can do the whole pool in minutes.

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
unknowingly is not. A pull also copies that fingerprint into the local
`_pool_provenance.json` — for a pulled pool the remote manifest is the only
authority on which gate admitted these rows — and records how many files complete
the pull *before* fetching any, so an interrupted transfer is visibly short rather
than fingerprintable.

The prebuilt-`candidates.csv` build commands (`--build-candidates`,
`--push-build`, `--pull-build`) belonged to the admission-gated Stage 1 and are
retired with it; the pool is the artifact to share.

**Output:** the survivor pool (`cache/snapshot_pool`)

---

## Sharing the API caches — `shared.cache_sync`

The pool is what nobody should have to re-scan; the caches are what nobody should
have to re-buy. `cache/llm` is the provider bill, and because the models are not
deterministic, re-running it does not reproduce our grading — it produces the
collaborator's own. The abstract store is six rate-limited sources over ~500k
identifiers. Both go to the same private dataset repo as the pool, under a
`cache/` prefix (`pool_sync` only ever lists `*.parquet`, so the two never collide).

```bash
python -m shared.cache_sync --pull                      # everything
python -m shared.cache_sync --pull --parts llm,abstracts
python -m shared.cache_sync --push
python -m shared.cache_sync --push --dry-run
```

Parts: `abstracts`, `llm`, `openalex`, `openalex_xml`, `parse`, `grobid`,
`doi_verify`, `pdfs`. The file parts are split into `.tar.gz` shards by the hex
prefix of `cache_key(filename)` — shard membership is fixed, so a re-push
transfers only the shards whose contents changed.

`abstracts` is the exception: it is one SQLite file
(`shared/abstract_store.py`), pushed whole and **merged** on pull rather than
replacing the local database. Merging is what keeps a puller's own abstracts, lets
an identifier already answered locally keep its local answer, and lets the
unproven-miss rule drop individual rows. The cost is granularity — a push moves the
whole store rather than one shard — and that is the one axis on which it is worse
than the directory it replaced.

`cache/engine/responses` is the one cache not shared here — `filter/engine/tiers.py`
already pushes those blobs as each tier run decides a work.

`--repo` points `cache_sync` at a dataset repo other than `FLORA_POOL_REPO`.
The abstract store has its own migration CLI: `python -m shared.abstract_store
--migrate` converts the old file-per-key cache into `cache/abstracts.sqlite`
(`--cache-dir` for a cache directory other than the configured one).

A pulled entry is safe to trust because the keys are **content-complete**:
`content_key()` folds the prompt version and the model into the key, and model ids
are constants rather than per-machine env, so a differing checkout misses instead
of reading someone else's answer as its own.

**Misses travel too, with one exception.** A cached `__none__` means "this source
definitively has no abstract for this DOI", and not re-buying a known miss is most
of the value. But a miss from a machine that lacked an entitlement is not that
fact, so the manifest records how many abstracts each source **actually
recovered**, and a pull drops a miss when the pushing machine got zero hits from a
gated source (`scopus`, `s2`, `osf`) that this machine is configured for. Because
the row IS the checkpoint, not importing it is all it takes for this machine to
fetch the DOI itself. Hits always import.

**Sharing is additive in both directions.** A pull never overwrites a local entry.
A push refuses when a shard it would replace — or the abstract store, compared on
row count — holds entries this machine does not, because a shard travels whole and
a partial cache would otherwise shrink the shared one; the pullers' recorded digest
would then stop them ever fetching the lost entries again. Pull first, then push.
`--force` on a push publishes this machine's cache anyway and drops them.

**Output:** `cache/` (and `cache/.cache_sync_pulled.json`, which records the
shards already unpacked; `--force` on a pull re-extracts them)

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
**Output:** the routing release in the DuckDB store — the pile each work landed
in, plus a permanent screen verdict per work the expensive tier judged. Stage 3
reads that store and the pool directly; nothing writes a file between the stages.
`export-csv` writes an ad-hoc CSV record of a release when a person wants one.

The usual order is `route` → `screen`.

```bash
# What the bundle currently says — one line per spec, plus the bundle hash
python -m filter.engine specs

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

# An ad-hoc CSV record of what this release admitted
python -m filter.engine export-csv --out /tmp/release-2011.csv --from-year 2011

# Push response blobs an earlier run left pending (dry run without --run)
python -m filter.engine reconcile

# Releases on disk and the pile counts each of them routed
python -m filter.engine status

# Claims still holding works, and how to end one a dead run left behind
python -m filter.engine release-claim
python -m filter.engine release-claim --claim <id> --status failed --yes
```

| Command | What it does |
| ------- | ------------ |
| `specs` | Lists the loaded bundle (id, pile, precedence, shadow, measurement levels), the bundle hash, the engine version and the export schema version. Fails loudly if any spec is invalid. |
| `route` | Computes the routing release id from its six inputs, records the release, and streams the pool through the bundle into the store. Idempotent per release: re-running replaces that release's rows rather than duplicating them. The pool is fingerprinted again after the pass and the routing is rolled back if it moved — the reader re-reads the directory, so a file that arrives mid-route would otherwise be consumed by a release id that does not name it. |
| `diagnose` | Routes the pool with and without `--spec` and reports rows moved per (pile without → pile with), overlap against every other rule (exclusive hits vs already-covered), a seeded readable sample, the holdout state and the spec's `measured` evidence. |
| `export` | Writes one pile in `ENGINE_EXPORTED_COLS` order — `FILTERED_COLS` + `ENGINE_EXPORT_COLS` + `SCREEN_COLS`, the last six blank because `export` applies no tier verdicts. Writing them blank rather than omitting them is what lets Stage 3 accept an exported pile at all: its startup check refuses any input whose header lacks `screen_verdict`, and every row then reads as unscreened and is written `target_pending`. `utf-8-sig`, plus `<out>.manifest.json` (release, pile, rows, sha256). `--pile pending` is refused, an existing manifest is never overwritten, and an export is refused outright when the spec bundle or alias file has changed since the release was routed — re-run `route` rather than looking for an override flag. `--pile needs_human` additionally prints the size of the queue it just wrote. |
| `screen` | Runs one LLM tier (`--tier screen_cheap\|screen_expensive`) over that pile. **Dry run by default**: it prints the row count, the token-length distribution of the abstracts it would send and `N rows → tier X ≈ $Y`, and claims, fetches and spends nothing. `--run` claims the batch through the Supabase claims RPC *before* the first voter is asked, records one permanent verdict row per vote, and completes the claim; a claim conflict refuses without spending anything, and an exhausted token budget fails the claim and stops with the verdicts already written intact. |
| `reconcile` | Sweeps verdict rows an EARLIER run left `response_pending_upload` — the flag off, no token, a commit that 429'd — matches them to the blobs in `cache/engine/responses/` by response hash, commits those in `FLORA_HF_COMMIT_BATCH`-sized commits and marks only what a commit accepted. **Dry run by default**; `--run` acts. A pending row whose blob has been deleted is reported, not fatal. Refuses outright when Hugging Face is unconfigured (`ENGINE_TIER_HF_UPLOAD` off, no `FLORA_POOL_REPO`, no `HF_TOKEN`) rather than sweeping nothing. |
| `worklist` | Exports the release's `pending/no_text` rows (joined back to the pool for doi/title/year) as the worklist `filter.engine.backfill` reads. |
| `export-csv` | Writes the two screen piles of one release — `screen_expensive` first, then `screen_cheap` — to the CSV named by the required `--out`, in `ENGINE_EXPORTED_COLS` order, with a live `screen_expensive` record type written into `paper_type` and its full verdict into `SCREEN_COLS`. **Only rows a live `screen_expensive` run reached a verdict on travel** — a cheap-tier verdict can drop a row but never admit one: a discarded work is left out and counted as `dropped_by_tier_verdict`, a work no live run decided (never screened, or still short of a second vote) is left out and counted as `skipped_unscreened`. `--as-routed` exports the piles as routed instead, applying whatever verdicts exist. A `<out>.manifest.json` sidecar records the release, the row count and the file's sha256; unlike `export`'s, it is rewritable — re-exporting the same release after more works were screened is a newer record of the same thing. |
| `release-claim` | With no arguments, lists every claim still `active` — id, tier, item count, when it was taken, when its lease runs out, and whether it has already expired. `--claim <id> --yes` ends one through the same `engine_release_claim` RPC a finishing run calls (`--status failed` by default; `cancelled` / `complete` also accepted), which frees its works. Verdicts are untouched. Rarely needed: a claim expires by itself `CLAIM_TTL_HOURS` (6) after it is taken, so this is for freeing works sooner than that. |
| `status` | Every release found beside the store, with its creation time and pile counts. |

**The text overlay loads itself.** `route`, `export`, `screen` and `export-csv` read
the overlay in `FLORA_OVERLAY_DIR` (default `cache/engine/overlay`) whenever that
directory holds overlay chunks — no flag needed, because an overlay-only rule
(the `osf-registration-*` pair) silently matches nothing without it. `--overlay
DIR` points at another overlay, `--no-overlay` runs against the bare pool, and
each command prints one line — `overlay: <dir> (hash <12>)` or `overlay: none` —
saying which text it read. The overlay folds into the release id, so `export`,
`export-csv` and `screen` refuse a release routed under a different overlay.

`--from-year` and `--to-year` both exist on `export` and `export-csv` — the examples
above only ever show the lower bound. `release-claim` takes `--release <id>` to
restrict its listing to one release.

`--spec-dir` (before the subcommand) points at a different bundle; `--store`
defaults to `cache/engine/engine.duckdb` and is **disposable** — deleting it costs
a `route` and nothing else, because routing is a pure function of pool, specs,
aliases and engine version. `route` takes the pool's provenance from
`--pool-manifest-hash`, else a fingerprint of the pool directory itself — the
search gate its rows were admitted under, read from `_pool_provenance.json` and
never from the local checkout, plus every parquet's name, size and footer row
count. (Size and row count do not determine file *contents*; hashing 7.6 GB per
route is not worth the case of a parquet rewritten to exactly the same size and
row count, and this is already stronger than the `content_length`-only ledger
hash it replaced.) A pool it cannot fingerprint — no parquet there, an unreadable
directory, or fewer files than the sidecar says complete it — routes under
`unmanifested:<12 hex>`, an honest "unknown" rather than a claim about which pool
this was; since routing reads the pool, that value is an anomaly worth
investigating, not a routine fallback. The suffix is a hash of the parquet names
and sizes and exists only so two different unfingerprintable pools do not mint the
same release id and share each other's claims and verdicts — it is not provenance,
which is why the `unmanifested` prefix stays visible.

**`--release` and `--sample`.** `export`, `screen`, `export-csv` and `worklist` each
take `--release <id>`: the release to read. A 12-character prefix is enough, as
long as it is unambiguous. Omitted, the store's release is used
when it holds exactly one and the command refuses when it holds several, so a
store with a re-route in it never picks one silently. `--release latest` is the
one way to ask for the newest: the release with the newest `created_at` among
those the store holds routing for, read off the release records beside the store.
A release with no record on disk has no timestamp and is not a candidate; when
none of them has one, `latest` refuses and lists what the store holds. Naming
`latest` is a choice the command line records — it is deliberately not the
default, see `extract.export` below. `diagnose --sample N`
(default 20) is how many seeded example rows the report prints for the rule under
diagnosis.

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

**`export-csv` exports what was screened.** Routing says a row deserves an LLM's
attention; only the validated pair says it reaches Stage 3. So a row the rules put
in a screen pile but no live `screen_expensive` run ever decided is held back, and
the manifest accounts for it separately from a discard (`skipped_unscreened` vs
`dropped_by_tier_verdict`) — the same population Stage 3's worklist takes. This is
what makes the export a faithful record of a release rather than a second opinion
about it. `--as-routed` exports the piles as the rules routed them, and is the only
mode available without Supabase: with no claims client there are no verdicts, so
screened-only would write an empty file and the command refuses instead of doing
that quietly. It refuses too, like `export`, when the spec bundle, alias file or
overlay has moved since the release was routed.

**`--out` is required.** The two modes write identical columns, so only the file's
name says whether every row in it was screened. Naming it is also what keeps the
export a record: a default name is how a derived file becomes a fixture that
something starts reading and nobody notices going stale.

### Engine modules with their own entry points

Three engine modules are run directly rather than as `filter.engine` subcommands.
Each has `--help`; the one-liners are their own `description=`:

```bash
# Fill a text overlay for the routing worklist's no_text rows (#146 M3). Dry-run by default.
python -m filter.engine.backfill --worklist W [--overlay-dir D] [--run] [--freeze]
#   also: --phase, --limit, --source, --batch-size, --scopus-limit,
#         --include-openalex, --dry-run

# Two pathways. Bulk is batched, keyless and unquota'd (Europe PMC; OpenAlex is
# opt-in via --include-openalex), so it is affordable over a wide worklist;
# targeted is the gated sources (OSF, Semantic Scholar, CrossRef, then Scopus)
# over the rows bulk left without text.
python -m filter.engine.backfill --worklist wide.parquet --run --phase bulk
python -m filter.engine.backfill --worklist no_text.parquet --run --phase targeted

# Reconcile a routing change against the validation tables (#146 M5).
# Writes lineage records only — never a validation row. Dry-run by default.
python -m filter.engine.supersede --old <release> --new <release> [--run] [--reason …]
#   also: --actor (defaults to $USER), --store

# Postgres sizing for the engine state tables (#146 §8 decision 1): measures the
# bytes one claimed row and one verdict actually cost.
python -m filter.engine.sizing [--rows N] [--verdict-rate R] [--json]
#   defaults: --rows 5146160 --verdict-rate 0.1
```

---

## Stage 3 — Extract

Stage 3 runs as a claimed engine tier. It has two commands: `extract.tier` decides
and records, `extract.export` renders. Design: [`filter-engine.md`](filter-engine.md),
"Milestone 6 — the extract tier".

```bash
# What would this cost? Dry run is the DEFAULT — nothing claimed, nothing spent
python -m extract.tier

# Claim the worklist and extract it
python -m extract.tier --run

# A first small batch, labelled so the claims name the campaign
python -m extract.tier --run --limit 50 --batch-label wave-1

# The sandbox: real verdicts the live export ignores
python -m extract.tier --run --mode validation

# Specific works, and works the checkpoint would otherwise skip
python -m extract.tier --run --only 2741809807,2884670852
python -m extract.tier --run --redo 2741809807

# Render data/extracted.csv from the verdicts of the works this release admits
python -m extract.export --release <id>

# Every stored verdict, whatever routing now says about its work
python -m extract.export --all-releases

# Render the sandbox's verdicts to their own CSV (set-asides go beside it)
python -m extract.export --release <id> --mode validation --out data/extracted-test.csv

# Does the file on disk match the verdicts? Writes nothing; non-zero if it differs
python -m extract.export --release <id> --check

# Drop works whose only result row is from a superseded generation
python -m extract.export --release <id> --current-generation-only
```

**Input:** the routing release and the survivor pool, read in process — the tier
builds each work's row with `iter_export_rows` + `screen_columns`, the same two
functions `filter.engine export-csv` writes with. There is no input CSV to keep in
step, and no Stage 2 file is needed for a tier run.  
**Output:** permanent verdict rows in the state authority. `data/extracted.csv` is
what `extract.export` renders from them.

`python -m extract.run_extract` is retired: it prints a pointer and exits 2. The CSV
runner it used to be — `--fresh`, `--rescreen`, `--extracted-test`, `--screen-here`,
`--filtered-csv`, the chunked read and the appending writer — is parked on the
`wip/csv-runner` branch, with a `WIP.md` recording what a revival would have to
satisfy. `extract/run_extract.py` itself stays on `main` as the per-row pipeline
library the tier's judge calls.

**`extract.tier` needs the routing store and the pool**, because the worklist is
built from the routing release and the pool text — `--store`, `--pool`,
`--spec-dir`, `--release` and `--overlay` / `--no-overlay` all default the same way
the `filter.engine` subcommands do. `--batch-size N` sets how many works are claimed
per batch (default `EXTRACT_CLAIM_BATCH`). A dry run runs without Supabase and
estimates over the whole admitted pile, saying so; `--run` without it refuses before
anything is claimed.

**The worklist is what the screen admitted, minus what is done or held.** Works with
a live current-generation `screen_expensive` PROCEED verdict, minus its discards,
minus works this tier has settled, minus works under another runner's unexpired
extract claim, minus the FLoRA and validation-table skip lists. Nothing is claimed
twice and nothing already extracted is re-bought.

**Resume is the verdict row.** There is no output file to read back, no truncation,
no carry-back: the worklist is rebuilt between batches, so a run that is killed
resumes by asking the same question again. Interrupting it costs at most the batch
in flight.

**`target_pending` and `api_error` do not settle a work.** They are the two endings
a re-run is meant to redo, so a work that ended in either comes back into the
worklist with no flag to remember. Every other ending takes it out.

**But a fresh `target_pending` RESTS.** A `target_pending` result younger than
`EXTRACT_PENDING_RETRY_DAYS` (14, in `extract/tier.py`) is subtracted from the
worklist exactly like a settled work: it reopens when the delay lapses, when a new
generation reopens everything, or when `--redo` names it. Without the delay, five
runs of one campaign re-bought the same ~830 unresolvable works' queries each time.
`api_error` has no such rest — it retries on the very next run.

**`--redo` is for a work that DID settle.** It re-admits the work despite the
checkpoint and points the previous result row at the new one
(`supersede_verdict`), so the old row stays as evidence of what was believed and
stops being read.

**A changed prompt or model reopens everything at once.** The extract GENERATION is
the hash of the ladder version, the prompts and the models at their call-site efforts
(`generation_inputs()` in `extract/tier.py`). Editing any of them means no work has a
current-generation verdict, so the whole worklist is claimable again — the equivalent
of the old `--rescreen`, without a flag and without a file to reopen.

**The dry run prices by rung, and OpenAlex in credits.** What a row costs is almost
entirely how far down the ladder it went, so the estimate is a weighted sum over
rungs rather than a per-row constant. OpenAlex is reported as credits on its own
line and never converted to dollars — it bills a daily credit budget that resets at
midnight UTC.

**`--limit N` counts works claimed, not works scanned.** The worklist subtracts skips
and settled works before the limit applies, so `--limit 50` extracts 50 fresh works.
Use it to bound spend.

**`extract.export` renders the works one routing release admits.** A verdict outlives
the routing that bought it: a stored result row is selected by kind, mode and
generation, by nothing about routing, so a work the rule book no longer admits would
keep rendering forever. On 2026-08-08 a text-overlay coverage gap let ~450 OSF
preregistrations be admitted, screened and extracted; once the rule book discards them
their verdicts stay on record, and an unfiltered export would keep shipping them to the
validation import. `--release <id>` renders only the works that release put in an
admitted pile (`screen_expensive`, `screen_cheap`, `needs_human`) and prints the count
it dropped, even when that count is zero. Omitted, the store's release is used when it
holds exactly one and the command refuses when it holds several — the same refusal, in
the same words, as `extract.tier` and the `filter.engine` subcommands. There is no
"newest release" default: the store holds seven routings of the same pool, one of them
a retired rule book that admitted 89,113 works against today's 4,650, so newest-wins
would make the file's contents depend on whichever routing anyone last ran, unstated in
the output. `--release latest` asks for it explicitly — the newest `created_at` among
the releases the store holds records for — which puts the choice in the command that
was run rather than in a default nobody typed.

**`--all-releases` renders every stored verdict**, whatever routing now says about its
work, and is the only invocation that opens no routing store. It is the pre-2026-08-08
behaviour, kept for reading the record whole; it is not what the validation import
wants. It cannot be combined with `--release`.

**The two skip lists are applied at render, not only in the worklist.** A work whose
DOI or work id entered FLoRA or the validation tables AFTER it was extracted keeps
its verdict as evidence, but its rows are dropped from the shipped CSV — the count is
reported as `already_in_flora`. Without that, a paper added to FLoRA between two
campaigns would keep reaching the validation import forever.

**Otherwise the render is pure**: no network, no cache, no pool. It
partitions rows into the set-aside CSVs on the way out, through the same
`classify_row()` `extract.sanity_check` reports with, and the set-asides belong to the
CSV being written (`--out data/extracted-test.csv` quarantines into
`data/extracted-test-set-aside/`). A work whose only result row is from a superseded
generation is carried forward and counted rather than dropped — dropping a paper
because a prompt was edited would delete a real finding — and
`--current-generation-only` is the strict view.

**It is the only writer of `data/extracted.csv`.** Each render writes the whole file,
sorted by `(work_id, original_rank)`, through a temp file and one rename. Nothing
appends to it, `sanity_check` reports rather than moves, and the two retroactive tools
correct the verdicts instead of the file. Every export therefore replaces the tracked
file whole — a re-render after a campaign is one large diff, and that is expected;
`--check` shows the difference before anything is written.

### The sandbox

`--mode validation` records real verdicts against a real claim; the mode lives in
`claim.meta.mode`, so the live export ignores them and a validation verdict does not
settle the live worklist.

```bash
python -m extract.tier --run --mode validation --limit 20 --batch-label shakedown
python -m extract.export --release <id> --mode validation --out data/extracted-test.csv
```

The sandbox render is filtered by a release like any other: `--mode` says which runs'
verdicts to read, not which works routing admits.

There is no promotion step. Re-running the same work live is the promotion, and it is
near-free because every LLM answer is already cached under a content-complete key.

### Skipping papers already in FLoRA

The extract tier's worklist subtracts every replication FLoRA already has; there is
no flag, because extracting one is never right. The skip list is the union of two
sources:

| Source | Rows skipped |
| ------ | ------------ |
| `data/FLoRA entry sheet - replication list.csv` | rows whose `validation_status` is `validated - unchanged`, `validated - changed`, `validated - chosen`, or `validated - discarded` |
| `data/flora.csv` | **every** row (`doi_r` and `alt_identifier_r`) — the published database, so all of it is already in FLoRA |

Statuses still in flight (`help needed`, `on hold`, `awaiting validation`, blank) are
**not** skipped — those genuinely need the pipeline.

The list lives in `shared/flora_skip.py` rather than inside the tier, so the
validation hand-off in the `flora-validation` repo can read the same contract and a
paper FLoRA already has cannot reach validators.

A missing or unreadable source logs a warning and contributes nothing, so one bad file
cannot silently disable the whole skip list.

### Skipping works already in the validation tables

The second skip list answers a different question: not "is this already published in
FLoRA" but "has someone already validated it". Supabase
`record_metadata` holds ~1,770 records seeded from the prior FLoRA pipeline, and those
works must never be extracted again.

That set is frozen — everything validated from here on flows through this pipeline and
is held out by the tier's own checkpoint — so it is a static, committed file rather
than a query. `analysis/build_validated_skip.py` materialises it once:

```bash
python -m analysis.build_validated_skip            # dry run: prints the counts
python -m analysis.build_validated_skip --apply    # writes data/validated_skip.csv
```

`--out` writes somewhere other than `data/validated_skip.csv`; `--dry-run` is the
explicit spelling of the default.

The tier then reads `data/validated_skip.csv` (`shared/flora_skip.load_validated_skip()`)
and holds back a work whose OpenAlex work id **or** cleaned `doi_r` is in it — two
identifiers because a legacy record may carry only one of them, and so may a pool row.
A missing file logs a warning and skips nothing.

### The set-aside partition, and the sanity report

Rows that do not belong in `extracted.csv` are written to a set-aside CSV instead, by
`extract.export`, as it writes them. `extract.sanity_check` applies the same rules to
the exported file and REPORTS: after an export every bucket reads zero, and a non-zero
count means the file and the verdicts have drifted apart.

```bash
# Report over data/extracted.csv — writes nothing
python -m extract.sanity_check

# The sandbox render instead
python -m extract.sanity_check --input data/extracted-test.csv

# Also network-verify unregistered doi_o against doi.org and flag fabrications
python -m extract.sanity_check --deep
```

The two `--deep` buckets are the reason the pass still exists: each needs a network
lookup per row, so neither can be decided as a row is written, and both name rows that
ARE in extracted.csv and should not be.

Rows land in the **first** bucket they match, and the order is load-bearing — the
`link_method` rules come first and the outcome rule last of the discard buckets,
because where a row stands in the pipeline decides which file it belongs in. The list
below is `classify_row()` in `extract/sanity_check.py`, in order:

| # | Condition | Destination |
| - | --------- | ----------- |
| 1 | `link_method == screen_disagreement` | `screen_disagreement.csv` (historical rows only — the front door no longer emits the value) |
| 2 | non-article `doi_r` (figshare data record, peer-review object) | `not_a_replication.csv` |
| 3 | `link_method == unidentified_original` | `unidentified_original.csv` |
| 4 | `link_method == keyed_link_disputed` | `keyed_link_disputed.csv` |
| 5 | `link_method == target_pending` | `target_pending.csv` |
| 6 | `link_method == prescreen_discard` | `prescreen_discard.csv` — ahead of the outcome rule, so the cheap pre-screen's discards never mix into the validated screen's file |
| 7 | `outcome == not_a_replication` | `not_a_replication.csv` |
| 8 | `link_method == api_error` | `api_error.csv` |
| 9 | `link_method == no_original_found` | `no_original_found.csv` |
| 10 | `doi_o` non-blank and equal to `doi_r` | `unresolved_self_links.csv` |
| 11 | `doi_o_verification == mismatch` | `unresolved_doi_mismatch.csv` |

With `--deep`, a `doi_o` that is registered nowhere additionally goes to
`unregistered_original_doi.csv`. `cannot_be_determined` rows stay in `extracted.csv`.

The order matters in cases that are easy to get backwards: a `keyed_link_disputed`
row whose `doi_o_verification` is `mismatch` lands in `keyed_link_disputed.csv`
(rule 4), not in `unresolved_doi_mismatch.csv`. A `llm_title_search` row with the
same mismatch does land in `unresolved_doi_mismatch.csv` — that method is resolved
now, so no earlier rule claims it.

### Parse-cache cleanup

Deletes all-empty parse caches from `cache/parse/`. Runs before audit B4 parsed every
non-multi row, including rows that exited at the reference screen with no PDF, and the
resulting empty cache then masked the real parse on any later run that did get the PDF.

```bash
python -m extract.clean_parse_cache          # dry run: count and report
python -m extract.clean_parse_cache --apply  # delete them
```

### Pre-validation audit

Read-only. Checks `extracted.csv` for rows that are not ready for a validator —
non-canonical `outcome` values (against `schema.OUTCOME_VALUES`), a `no_doi` row
with no `oa_work_id_o` to show, and the rest.

```bash
python -m extract.audit_extracted                       # audit data/extracted.csv
python -m extract.audit_extracted --input data/extracted-test.csv
python -m extract.audit_extracted --report data/pre_validation_audit.csv
python -m extract.audit_extracted --doi 10.1234/example  # repeatable
```

### Backfilling authors

Fills `authors_o` / `ref_o` retroactively from OpenAlex on rows that resolved before
those columns were written. Like the DOI audit below, it reads the exported CSV and
CORRECTS THE VERDICTS it is rendered from; render the result afterwards.

```bash
python -m extract.backfill_authors                   # dry run
python -m extract.backfill_authors --apply
python -m extract.export --release <id>              # render the correction
python -m extract.backfill_authors --mode validation
python -m extract.backfill_authors --doi 10.1234/example
```

### DOI verification audit

Retroactively verify `doi_o` values. It READS the exported CSV and, under `--apply`,
WRITES the verdicts that CSV is rendered from: it claims the affected works, records a
corrected result verdict per work and supersedes the previous one. Editing the file
would be undone by the next render, so rendering is the third step and it is yours.

```bash
# Dry run: print summary + write data/doi_audit_report.csv
python -m extract.audit_dois

# Claim, correct, supersede
python -m extract.audit_dois --apply

# Render the corrected CSV
python -m extract.export --release <id>

# Audit a single DOI
python -m extract.audit_dois --doi 10.1234/example

# Re-ask only the rows whose verification could not be completed last time
python -m extract.audit_dois --status api_error --apply

# The sandbox render, and the validation-mode verdicts behind it
python -m extract.audit_dois --mode validation
```

Verification runs once, inside the tier's judge, and its answer is stored on the row —
each re-verification costs up to three OpenAlex free-text searches (10× a filter
query), so an export that re-verified would pay that bill on every render. This tool is
therefore the only way to re-verify a settled row: after a threshold change in
`shared/doi_verify.py`, or as a spot check. `--status` is repeatable and accepts `''`
for rows that were never verified.

A correction is matched to its stored row by `pair_id`, and the work id comes off the
verdict rows themselves — never from an OpenAlex lookup. A pair id the live payloads do
not hold is reported, not guessed at.

Each Stage 3 run prints its free-text OpenAlex search count next to the token summary.

---

## Stage 4 — Monitoring web app

```bash
# Start the web app
python -m validate.app
# → http://localhost:5001
```

The app is read-only with respect to the pipeline: it displays pipeline stats and
pulls validation data from Supabase, and no route writes to any pipeline artifact.
It is not literally write-free — `/api/dashboard/download` copies the requested CSV
into `data/dashboard/download/` before streaming it, and `/set-name` stores the
reviewer's name in the Flask session.

---

## Analysis

The overlap / recall-gap analysis (`analysis.run_overlap_analysis`) is gone with the
corpus it read: it compared `all_replications.csv` against `data/candidates.csv`, and
Stage 1's corpus is the survivor pool now. The per-release recall monitor that
replaces it is specified in `analysis/gold/README.md` and not yet implemented.

`analysis/apa_resolver.py` is a library, not a command — it defines `resolve_all()`
and `run_apa_resolution()` and has no `__main__` block, so `python -m
analysis.apa_resolver` does nothing. Import it.

**Outputs:** CSV and Markdown files in `analysis/` (see [code-flow/analysis.md](code-flow/analysis.md) for what each file means)

The `gap_summary.md` / `gap_analysis_*.csv` outputs belonged to the retired overlap
analysis and are no longer produced.

---

## Tools

```bash
# Drop superseded preprint versions (keep highest _v, or the version-less DOI) — issue #17
python -m tools.dedup_preprint_versions --input data/extracted.csv            # dry-run
python -m tools.dedup_preprint_versions --input data/extracted.csv --apply

# Corrections to stored rows go through superseding verdicts, never a CSV edit:
#   python -m extract.audit_dois --apply        (doi_o corrections)
#   python -m extract.backfill_authors          (authors_o / ref_o)
# and a re-code of outcomes is a re-run: python -m extract.tier --redo <work_ids>
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
| `/dashboard` | Monitoring dashboard: five fixed tabs (Search, Filter, Extract, Extract-Test, Supabase) plus one per set-aside file — see [dashboard-guide.md](dashboard-guide.md) |
| `/check` | Search/filter/download across any stage — see [check-page.md](check-page.md) |
| `/pipeline` | Redirects to `/dashboard` |
| `/api/dashboard/csv-stats` | Pipeline stats JSON (3-tier cascade: stats.json → Parquet → CSV) |
| `/api/dashboard/download` | Download a full stage CSV (`?stage=filtered\|extracted\|extracted-test`; anything else is a 400). Copies the file to `data/dashboard/download/<stage>_<date>.csv` before streaming it |
| `/api/dashboard/set-stats`, `/set-rows`, `/set-download` | Per-set-aside-file stats, rows and CSV download |
| `/api/dashboard/supabase-analytics`, `/supabase-confusion` | Validation analytics and the human/LLM confusion matrix |
| `/pdf/<filename>` | Serves a cached PDF from `cache/pdfs/` |
| `/set-name` | Stores the reviewer's name in the session |
| `/api/check/search` | Filtered/paginated rows as JSON |
| `/api/check/download` | Filtered rows as CSV attachment |
| `/api/dashboard/supabase-stats` | Supabase validation KPIs |
| `/api/dashboard/supabase-outcomes` | Outcome distribution from validated table |
| `/api/dashboard/supabase-corrections` | Per-field correction frequency |
| `/api/dashboard/supabase-drilldown` | Paginated incorrect-DOI table |
