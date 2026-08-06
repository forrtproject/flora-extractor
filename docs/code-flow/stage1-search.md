# Stage 1: Search — Code Flow

**Entry point:** `python -m search.run_search --scan`

## What it does

**Stage 1 searches. It does not filter.** It reads the OpenAlex bulk-parquet
snapshot once and writes every **search-gate** survivor to the **survivor pool**;
every precision decision — exclusions, phrase matching, vocabulary, rescues —
belongs to Stage 2's spec bundle, which is the one rule set that decides what is a
replication.

The single keyword exception is the **search gate**, described under
[the snapshot scan](#the-snapshot-scan-and-the-survivor-pool) below: a broad
token/stem alternation plus concept membership, which exists because 510M works
cannot be routed one rule bundle at a time. It admits generously and judges
nothing.

**The snapshot scan is now the whole of Stage 1.** The API discovery legs — the
OpenAlex phrase and concept searches, the Semantic Scholar leg, the external-list
scrapers, the cache-harvest phase and the `--auto-advance` job loop — are retired
to the `wip/api-harvest-sources` branch (PR #158). They wrote `data/candidates.csv`,
and nothing downstream reads it: Stage 2 routes the pool. `search/` now holds four
modules — `run_search.py`, `snapshot_scan.py`, `pool_sync.py` and
`fetch_abstracts.py` (a library, whose one caller is Stage 2's
`filter.engine.backfill`). `CANDIDATES_COLS` survives only as the column contract a
pool row is rebuilt into (see [csv-schema.md](../csv-schema.md)).

---

## Step-by-step

```text
run_search.py  (the operator front-end: --scan is required, four flags in total)
    │
    └── snapshot_scan.scan_snapshot(max_files, survivor_pool, force_gate)
            │
            ├── read the cached snapshot manifest (2,446 partitions, 725 GB, ~510M records)
            ├── refuse if cache/snapshot/ledger.json was written under a DIFFERENT
            │   search-gate fingerprint (--force-gate overrides, knowingly)
            │
            └── per manifest partition, skipping those the ledger marks done:
                    read the parquet columns the gate needs
                    _TOKEN_GATE over title + raw abstract inverted-index JSON   ─┐
                    concept membership (CONCEPT_IDS)                            ─┴ OR
                    keep survivors → one parquet file in the pool (_POOL_SCHEMA)
                    reconstruct abstract_text; record hit_token_title /
                        hit_token_abstract / hit_concept
                    mark the partition done in the ledger
            │
            ├── write _pool_provenance.json beside the parquet (gate + file count)
            └── finally: dashboard_cache.refresh(POOL_STAGE) — so an interrupted
                scan still leaves the dashboard showing what it consumed
```

The flags, in full — there are no others, and `--scan` is required so a bare
invocation cannot start a 725 GB read:

| Flag | Meaning |
| ---- | ------- |
| `--scan` | Run the ledger-backed production scan. Resumable. |
| `--survivor-pool PATH` | Where the pool is written (default `cache/snapshot_pool`, or `FLORA_POOL_DIR`). |
| `--snapshot-max-files N` | Stop after N partitions this run. |
| `--force-gate` | Scan on despite a ledger written under a different gate. |

Progress is a separate read-only command, safe against a scan in flight:
`python -m search.snapshot_scan --status [--json]`.

There is no sample or pilot mode. To scan a few partitions for a look, point the
state directory somewhere scratch — `FLORA_CACHE_DIR=/tmp/flora-sample python -m
search.run_search --scan --snapshot-max-files 3` moves both the ledger and the pool
out of the way. The retired `--snapshot-pilot` wrote real parquet into the
PRODUCTION pool while keeping no ledger, so the pool and the ledger disagreed about
what had been consumed.

---

## The snapshot scan and the survivor pool

`search/snapshot_scan.py` is Stage 1. Where a keyword API search can only find works
whose title or abstract matches a phrase someone thought to write down, the scanner
reads the **whole** OpenAlex bulk-parquet corpus once — 2,446 partitions, 725 GB, ~510M records — and keeps
what the search gate admits.

**The search gate** (vectorized, pyarrow) is the whole of Stage 1's keyword logic:

- a broad token/stem alternation over the title and the raw abstract
  inverted-index JSON (it runs against the un-reconstructed JSON, so it can test
  tokens but not phrases — word order does not exist there), **or**
- membership of a replication concept, which is the recall arm.

Either hit admits. There is no second keyword stage, no exclusion pattern and no
phrase precision test in the scan: a row the gate keeps goes into the pool and the
filter engine decides everything else about it. The gate keeps well under 1% of
the corpus.

**The survivor pool** (`--survivor-pool PATH`) is Stage 1's output: every gate
survivor as a parquet dataset, one file per manifest partition, a few GB against
~725 GB of snapshot. It is the filter engine's direct input — Stage 2 routes the
pool parquet, and nothing between the scan and the engine holds a filtered copy of
it. Progress is checkpointed per manifest file in `cache/snapshot/ledger.json`, so
an interrupted scan resumes where it stopped.

Because the pool holds everything the gate saw, a **Stage 2 rule change is a local
`filter.engine route` re-run over the pool**, not a rescan. Only a change to the
search gate itself — its token alternation or `CONCEPT_IDS` — costs the full scan,
which is why the gate has its own fingerprint and why a token added there is
doubly expensive: it also enlarges the artifact every collaborator downloads.

Pool columns (`_POOL_SCHEMA` in `search/snapshot_scan.py`) are the identity and
metadata needed to rebuild a paper row without the snapshot — `id`, `doi`, `title`,
`display_name`, `publication_year`, `type`, the nested `authorships`,
`primary_location`, `open_access` and `concepts` as JSON strings, the
already-reconstructed `abstract_text`, and the three booleans recording *why* the
gate kept the row: `hit_token_title`, `hit_token_abstract`, `hit_concept`.

`search/pool_sync.py` shares the pool through a private Hugging Face dataset repo,
so nobody has to reproduce the scan:

```bash
python -m search.pool_sync --check-access         # prove write access before a long scan
python -m search.pool_sync --push / --pull        # the ~2-3 GB survivor pool itself
```

`pool_manifest.json` at the repo root records the search gate, snapshot date and
ledger the pool was scanned under; pushing over a pool scanned under a *different*
gate fingerprint is refused, because the mixture would be complete under neither
gate and nothing downstream could tell.

Locally, the same facts live in `_pool_provenance.json` **inside the pool
directory** — written by the scan (which knows the gate it just applied) and by
`--pull` (from the remote manifest, the only authority for a pool this machine did
not scan). It holds the gate fingerprint, the file count that completes the pool
and where both came from, and it is what the Stage 2 release id hashes: the gate
in a release id is the pool's, so a shared pool fingerprints the same everywhere,
and a pool short of its file count is an interrupted transfer that gets no
fingerprint at all. A pool from before the sidecar existed can be stamped in place
with `python -m search.snapshot_scan --stamp-pool` (ledger, or `--gate` given
explicitly — it never guesses). Runbook for the full scan:
[aws-snapshot-scan.md](../aws-snapshot-scan.md).

---

## The search gate's two arms

Both arms live in `filter/phrase_detection.py`, which is Stage 1's only keyword
logic and is not called by Stage 2 at all:

- `REPLICATION_STEM_PATTERN` — imported by `snapshot_scan.py` as `_TOKEN_GATE`, a
  broad token/stem alternation run against the title and the *un-reconstructed*
  abstract inverted-index JSON. Word order does not exist there, so it tests tokens
  and never phrases.
- `CONCEPT_IDS` — the recall arm: works OpenAlex's own classifier tagged with a
  replication or reproducibility concept, which catches papers with no stored
  abstract and papers that describe a replication without ever using the word.

`search_gate_fingerprint()` hashes exactly those two, and that hash is what the
ledger, the pool sidecar and the Stage 2 release id all carry. Changing either
costs a full rescan, which is why a token added there is doubly expensive: it also
enlarges the artifact every collaborator downloads. In the filter engine the
concept arm is its own spec, so a concept-only row stays identifiable by
`route_rule`.

---

## Rate limits

The scan makes no API calls — it reads parquet. The only rate limits Stage 1 code
still touches are `fetch_abstracts.py`'s, and that module runs under Stage 2's
backfill; its per-source limits are in `shared/config.py`.

---

## Key functions

| Function | File | Description |
| --- | --- | --- |
| `main()` | `search/run_search.py` | The four-flag operator front-end; requires `--scan` |
| `scan_snapshot()` | `search/snapshot_scan.py` | The ledger-backed pass over the snapshot manifest |
| `search_gate_fingerprint()` | `search/snapshot_scan.py` | Hash of `_TOKEN_GATE` + `CONCEPT_IDS` — the value a rescan is bound to |
| `scan_status()` / `--status` | `search/snapshot_scan.py` | Read-only progress, safe against a scan in flight |
| `--stamp-pool` | `search/snapshot_scan.py` | Write `_pool_provenance.json` for a pool that predates it |
| `push_pool()` / `pull_pool()` | `search/pool_sync.py` | Share the pool through the private HF dataset repo |
