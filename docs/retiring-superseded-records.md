# Retiring superseded records in the validation tables

Status: design + prototype, 2026-09-23. Nothing has been written to Supabase. The
flora-validation half is on a LOCAL branch `retire-superseded-records` (commit
`82c48da`), not pushed.

## The problem

This repo stops writing at `data/extracted.csv`. flora-validation's `csv_to_db.py`
imports it into Supabase, keyed on `pair_id` = `md5(doi_r|doi_o)` (`make_pair_id()`
in `shared/schema.py`). The import never deletes anything. So when a new export stops
shipping a row, the old record stays in the validation queue. That happens when a
work's original changes, when the work is set aside (`no_evidence`,
`prospective_registration`, …), when routing or the screen drops it, or when it now
has fewer originals. The next case is the `llm_references` redo (handover step 3):
about 1,570 works re-extracted with a fixed prompt, many getting a different `doi_o`.

## What exists already (flora-validation, read 2026-09-23)

- **Import** (`csv_to_db.py run_import`). A `pair_id` already in the database gets its
  extractor-owned fields refreshed. A new `pair_id` whose `(work_id, original_rank)`
  matches exactly ONE existing record **re-keys that record in place**. It keeps
  `record_id` and the queue slots and overwrites `doi_o`, title, outcome, etc. If a
  validator had touched the record (and it is not `rejected`), the status becomes
  `need_review` and a note is added. Anything else is inserted as a new record with
  three queue slots.
- **Nightly maintenance** (`extractor_maintenance.py`). `sync_csv.py` downloads
  `data/extracted.csv` **from GitHub `main`**, refuses a snapshot that removes more
  than 10% of the previous one's resolved `pair_id`s (`EXTRACTOR_MAX_REMOVAL_PERCENT`),
  imports it and promotes it to `extracted_latest.csv`. Then `find_orphans.py` writes a
  read-only report. `cleanup_orphans.py --apply` is a separate manual stage. It HARD
  deletes every record whose `pair_id` the current CSV lacks, unless the record is
  `rejected`/`validated`, has a submitted judgement, or has a `validated` row. It
  leaves a JSON receipt of ids only.
- `validation_status` ∈ `unvalidated`, `validation_inprogress`, `consensus_reached`,
  `need_review`, `validated`, `rejected` (CHECK constraint). `validation_queue`,
  `record_metadata`, `validated`, `validation_skips`, `assignments`, … all reference
  `unvalidated(record_id)` with no `ON DELETE CASCADE` (only `submission_failure_releases`
  cascades). A delete therefore has to remove children first, as `cleanup_orphans` does.

## Facts, measured 2026-09-23 (GET-only reads of Supabase)

**The import is not running.** Every nightly sync since 2026-09-13 has been blocked
with `baseline_snapshot_unavailable`: the pod lost the archive of the last verified
sync. The last import was 2026-09-12 14:26 UTC, sha256 `8c33403f…`. That is exactly
commit **`d7f55d9`** (2026-09-07) of `data/extracted.csv`. The three later exports
(`71c2ff9`, `b5810d9`, `61aaeb9`, including the Observatory campaign's 1,101 new
rows) were downloaded and refused. So "the CSV on disk is what `csv_to_db` imported"
is **not** true today. What gets imported is a commit on `origin/main`, and only when
the sync is not blocked. All of this is on `origin/main`.

| | |
| --- | ---: |
| `unvalidated` records | 3,706 — unvalidated 2,959 · validated 351 · need_review 187 · rejected 86 · validation_inprogress 64 · consensus_reached 59 |
| records with `work_id` / `original_rank` in `record_metadata` | 3,706 / 3,706 (the re-key can match every record) |
| today's CSV (`HEAD` = `61aaeb9`) | 4,141 rows, 4,100 resolved, 3,607 works |
| importing today's CSV would | refresh 2,999 · insert 1,101 · re-key 0 |
| records whose `pair_id` today's CSV does not ship | **707** |
| … on the "already in the validation tables" skip list (`data/validated_skip.csv`) | 588: the legacy records the export withholds BECAUSE they are already there. Validated 285, need_review 172, rejected 80, consensus 44, in progress 7 |
| … genuinely withdrawn by the extractor | **119**, all imported in one batch on 2026-08-19. They are now in prospective_registration 65, no_evidence 37, search_link_unconfirmed 7, target_pending 4, no_original_found 3, api_error 2, not_a_replication 1 |
| … of which untouched (`unvalidated`, nothing shown or judged) | **113**. Servable to validators right now |
| works with >1 record | 182 (491 records; nearly all legitimate multi-original works) |
| `(work, rank)` slots holding 2 records | 1 |
| works where a stale record sits beside a current one | 2 (3 stale records: 2 unvalidated, 1 rejected) |
| `llm_references` records | 1,159 — unvalidated 1,111 · in progress 18 · validated 19 · consensus 7 · need_review 3 · rejected 1 |

**What this means for the redo.** A redone work whose original changes **at the same
rank** will not leave the wrong record beside the corrected one. The import re-keys it
in place: same `record_id`, new `doi_o`, and nothing lost, because the redo only
reopens untouched works. What the import cannot reach:

- a work the redo no longer resolves (the new prompt declines, the full-text rung
  finds nothing, so the work lands in `no_original_found.csv` / `target_pending.csv`);
- a work that ships fewer originals than before;
- a rank shuffle that leaves one old slot unmatched.

Those become orphans, which is the same class as the 119 above. Also note: re-keyed
`pair_id`s still count as REMOVED in the sync's 10% guard. With ~1,570 works redone
the guard may trip, and the export now warns when it will.

## The mechanism

Lukas's idea — record removed ids and delete those specifically — with three changes:
the reason is recorded; only untouched records are removed; and removal is
archive-then-delete.

### 1. The extractor names what it withdrew: `data/retired_pairs.csv`

`python -m extract.export` (live mode, production path only) compares its render
against the **committed** `data/extracted.csv` at `HEAD`. For every `pair_id` that
file shipped (resolved and typed, i.e. the rows `csv_to_db` imports) and this render
does not, it appends one row to `data/retired_pairs.csv`. Rows are appended after the
CSV is written, never duplicated per `pair_id`, and the file is tracked in git:

`retired_at, baseline, release, generation, reason, detail, pair_id, work_id, doi_r,
original_rank, doi_o, title_o, link_method, superseded_by, doi_o_now`

| `reason` | Meaning | `detail` / successor |
| --- | --- | --- |
| `superseded` | the work still ships, under other pair id(s) | `superseded_by`, `doi_o_now` |
| `set_aside` | the work's rows are in a set-aside file | the file(s) |
| `unresolved` | in `extracted.csv` but no longer importable | |
| `not_admitted` | the routing release dropped the work | the release |
| `screen_discarded` | the current screen discards it | |
| `in_flora` | its DOI is now in FLoRA | |
| `unexplained` | no current verdict speaks for the work | held on the validation side |

Works on the already-in-validation skip list are **never written** (they are counted
as "held"). Their records are the reason they are suppressed.

Why `HEAD` rather than the file on disk: the sync downloads a commit, so a commit is
what can have been imported, and a render nobody committed was never offered to
anyone. Why not Supabase: the extractor must not depend on reading the validation
tables to write its own artifact, and the validation side re-checks everything against
the database anyway. `--retired-baseline REV` (repeatable) replaces `HEAD`, which is
how the history is backfilled. `--check` prints the counts and the guard warning and
writes nothing.

### 2. The validation side retires exactly those: `csv_to_db.py --retire`

```bash
python csv_to_db.py --input data/extracted_latest.csv --retire retired_pairs.csv \
    --retire-report retire_plan.csv                       # dry run: read-only session
python csv_to_db.py --input data/extracted_latest.csv --retire retired_pairs.csv \
    --apply --expect-retire N                              # after approval
```

Each manifest `pair_id` gets one action (`plan_retirements()`, which is pure):

| action | when | writes |
| --- | --- | --- |
| `still_shipped` | the imported CSV (`--input`) carries it again | nothing |
| `absent` | no record has it (never imported, already retired, or re-keyed) | nothing |
| `held_unexplained` | reason `unexplained` (unless `--include-unexplained`) | nothing |
| `already_excluded` | status `rejected` | nothing |
| `retire` | status `unvalidated`, no queue slot shown or judged, no assignment, no `validated` row | archive the record whole in `retired_records`, then delete it and its children |
| `flag` | anything a person touched (in progress, consensus, need_review, validated, assigned) | append `⚠ Retired upstream: …` to `admin_notes`; status and `validated` untouched |

Safety properties:

- The dry run is a Postgres read-only session that runs only SELECTs. A test asserts
  this.
- `--apply` needs `--expect-retire N`, the count from the reviewed dry run. It refuses
  if the plan has changed since that review. It takes the maintenance advisory lock,
  so it cannot overlap the nightly sync, and it requires `retired_records` to exist.
- Each batch of 200 runs in one transaction under the same `LOCK TABLE … NOWAIT` as
  `cleanup_orphans`, and the plan is re-read under the lock.
- It is idempotent: a retired `pair_id` is `absent` next time.

**Archive-then-delete, not a new status.** A soft-retire status (`retired`) would need
the CHECK constraint changed. It would also need every status reader in `app.py`
audited (about 40 queries). Several are `NOT IN ('validated','rejected')` counts that
would silently report a retired record as open work. The `retired_records` table keeps
the full row (`unvalidated`, `record_metadata`, queue and skip rows as JSONB, plus the
manifest's reason and successor), so a retirement is auditable and reversible without
changing any reader. `cleanup_orphans` today keeps only ids. The migration is appended
to `db_schema.sql` (`CREATE TABLE IF NOT EXISTS retired_records`). `app.py` applies
that file at startup, so it would take effect on the first deploy of the branch. It has
not been applied.

**Why not just `cleanup_orphans`?** Today it would delete 115: the same 113, plus 2
in-progress records that were shown but never judged. `--retire` flags those 2
instead. Its retention rule also protects the 588 legacy records. The difference is
that `cleanup_orphans` INFERS a retirement from absence. A truncated or wrongly routed
CSV reads as "delete these" up to the 10% guard, and there is no reason attached.
`--retire` acts only on what the extractor stated, with why and what replaced it.
`delete_source_records()` and `WRITE_SURFACE_LOCK_SQL` are now shared by both.

### Measured against the live tables

Manifest backfilled from every committed `extracted.csv` since 2026-05 (the widest
possible seed), planned against
a GET-only snapshot, with today's CSV as `--input`:

| action | pairs |
| --- | ---: |
| retire | **113** (set_aside 111, superseded 2 — the two stale-beside records) |
| flag | 5 (in progress 3, validated 2) |
| already_excluded | 1 |
| absent (never imported) | 1,691 |
| held — legacy skip list, not written | 578 |

This matches the independent orphan count exactly (119 = 113 + 5 + 1).

## Order of operations for the `llm_references` redo

0. **Unblock the sync** (Lukas / flora-validation admin). It has refused every snapshot
   since 2026-09-13. Once it runs, the next import brings in the campaign's 1,101 rows
   (`61aaeb9`). Their removal share against `d7f55d9` is 0%.
1. **Seed the manifest**, once, from the two snapshots known to have been imported, and
   commit it. `ad0750a` holds 3,105 resolved pairs: the "previous" of the 2026-09-12
   sync, whose records were created on 2026-08-19. `d7f55d9` matches the sha256 of
   that sync.
   ```bash
   .venv/bin/python -m extract.export --release c048ab6483d3 \
       --retired-baseline ad0750a --retired-baseline d7f55d9
   ```
   This yields 123 entries (set_aside 116, superseded 7), exactly the 123 removals
   that sync reported. They contain all 113 retirable records. The re-rendered CSV is
   byte-identical today (`--check`: 0 differences).
2. The redo itself, as handover step 3 describes: sandbox pilot, then live.
3. `.venv/bin/python -m extract.export --release <id> --check` prints the retirement
   counts by reason, the removal share against `HEAD`, and a warning above 10%.
4. `.venv/bin/python -m extract.export --release <id>` writes the CSV and appends the
   manifest. **Review the manifest.** Any `unexplained`? Are the `superseded` successors
   plausible? Commit `extracted.csv` and `retired_pairs.csv` together and push.
5. **Import first.** Use the nightly sync, or an admin sync with
   `EXTRACTOR_MAX_REMOVAL_PERCENT` raised for that one run if the guard trips. Importing
   first lets the re-key correct same-rank originals in place.
6. On the validation host: `csv_to_db.py --retire <manifest> --input
   data/extracted_latest.csv --retire-report plan.csv`, the dry run. **Lukas approves
   the plan.**
7. `--apply --expect-retire N`.
8. Admins work through the flagged records (`admin_notes` containing `⚠ Retired
   upstream`).

Retiring before importing is also safe. The import would then insert fresh records
instead of re-keying, and touched records are only flagged either way.

## Open questions for Lukas

1. **The blocked sync.** Re-baseline it (admin sync with the baseline archive
   restored, or accept `d7f55d9` as the baseline). Should the archive live on durable
   storage, since the pod lost it?
2. **Approve archive-then-delete** over a `retired` status, and the `retired_records`
   migration.
3. **Wire `--retire` into maintenance?** Today it is a manual CLI. The sync could fetch
   `data/retired_pairs.csv` alongside the CSV, bind it to the same sha256, and make
   `find_orphans` report the plan nightly.
4. **Should the removal guard discount explained removals?** A `pair_id` the manifest
   names as `superseded` (and re-keyed) is not a loss. As written, a large redo trips
   the guard and needs a manual override.
5. **Flagged records.** Should a retired-upstream record that is in progress be pulled
   from serving, or only noted? Should a validated one whose original the extractor now
   says was wrong reopen for review? The validated record may reflect a validator's
   correction, so nothing touches it today.
6. **Seed choice.** The 123-entry seed from `ad0750a` + `d7f55d9` (step 1) assumes no
   other snapshot was ever imported. The table above ("Measured against the live
   tables") used every commit since 2026-05. That adds 1,691 never-imported pair ids,
   all no-ops, and found nothing more to retire, so the small seed loses nothing today.
7. **`clean_doi` change (handover step 4) — made 2026-09-25.** `clean_doi()` now
   collapses `10.1037//` and decodes `%xx` (when the result is still a DOI), and
   `render_payload()` re-spells stored DOIs through `_canonical_dois()`, re-deriving
   a `pair_id` only when the stored one provably hashes the old spelling. Measured on
   release 236bc39c2263: 24 shipped pair ids retire as `superseded` (21 of them queued
   in `unvalidated`), 13 set-aside rows re-key, and 2 works newly match the validated
   skip list and stop shipping (1 held, 1 from `target_pending.csv`).
8. This doc is not yet listed in `docs/README.md`.

## 2026-09-24 check (flora-validation side)

Read-only throughout: REST GETs of the live tables, nothing written.

**Where the import runs.** In flora-validation's web process. `app.py` starts an
APScheduler job at 02:00 UTC (`run_scheduled` → `extractor_maintenance.run_pipeline`
→ subprocess `sync_csv.py` → `csv_to_db.run_import`). It downloads
`raw.githubusercontent.com/forrtproject/flora-extractor/main/data/extracted.csv`
(`GITHUB_REPO`/`GITHUB_BRANCH`). The archives live in `EXTRACTOR_DATA_DIR`, which
defaults to `/app/data` inside the container. On Railway that directory is not
durable. Admins can queue the same pipeline from the Extractor Pipeline tab
(`POST /api/admin/maintenance/run`, stage `full`/`sync`), or with
`python sync_csv.py` on the host.

**Why it is blocked.** `extractor_maintenance_runs` holds 12 rows. The 2026-09-12
14:26 admin sync imported `extracted_20260912T142657Z_88d4c0c8.csv` (sha256
`8c33403f…`, which is `d7f55d9`). flora-validation redeployed at 16:22 that day, and
every nightly run from 09-13 to 09-24 (none on 09-19) then blocked with
`baseline_snapshot_unavailable`. `_resolve_baseline` accepts only that archive name
or `extracted_latest.csv`, and the latter is the stale copy bundled in git. The runs
of 09-13 to 09-21 downloaded exactly the baseline's bytes, but under new archive
names. There is no admin action or CLI to re-baseline.

**Fix (flora-validation branch `retire-superseded-records`, commit `2292e45`, local
only).** When the baseline archive is missing, the sync now recovers it by digest:
first from any archive on the host, then from the extractor's git history (the
newest commits touching the file up to the archive's timestamp). Only the recorded
sha256 is accepted. Checked live: the first candidate is `d7f55d9`.

**The import after that.** Measured against the `d7f55d9` baseline with the sync's
own code, current main removes 463 of 2,999 resolved pairs, which is **15.44%**. All
463 are in `data/retired_pairs.csv`. A one-run `EXTRACTOR_MAX_REMOVAL_PERCENT=16` is
needed. Simulated against the live tables, the import does 2,536 refreshes, 34
re-keys and 1,092 inserts.

**Import bug found and fixed on the branch.** A rank shuffle re-keyed the record of a
pair the same CSV still ships. That hit 11 records in this import, and 2 shipped pairs
would have ended with no record. With the fix, all 34 re-keys are manifest
`superseded` pairs.

**Retire plan** (`plan_retirements()` over a REST snapshot; the real dry run needs
`DATABASE_URL`, which no local checkout has):

| `--input` | retire | flag | held (unexplained) | already rejected | still shipped | absent |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `d7f55d9` (the CSV imported today) | 113 | 5 | 0 | 1 | 463 | 105 |
| current main, after the import | **539** | 5 | 3 | 1 | 0 | 139 |

After the import, the 539 break down as:
- set_aside 512: prospective_registration 277, target_pending 143, no_evidence 36,
  search_link_unconfirmed(+unidentified) 35, unidentified_original 7,
  no_original_found 7, api_error 4, not_a_replication 2, keyed_link_disputed 1
- superseded 24
- not_admitted 3

The 5 flags are 3 in progress and 2 validated, all set_aside. The 3 unexplained
entries are all one work (`10.1186/1471-2350-13-27`). I spot-checked 10 retire
records. All were `unvalidated`, created 2026-08-19, with 3 unshown and unjudged
slots, no validator and no validated, assignment or skip row. Their works either ship
other originals or sit in the named set-aside file.

**Worth deciding.** 143 retirements are `target_pending` works, which are not final
verdicts. They are untouched, so nothing is lost, and a work that later resolves is
inserted fresh.

**Open question 3: decided 2026-09-24 (Lukas). Retiring is automatic.** The branch
(PR on forrtproject/flora-validation) adds `retire_superseded`, the last stage of the
nightly and admin full routine. `EXTRACTOR_AUTO_RETIRE` turns it on and defaults to
on. It runs only after `sync_csv` and `find_orphans` succeed in the same run. It
applies `csv_to_db.py --retire` against the snapshot that run imported, with
`data/retired_pairs.csv` read at the same flora-extractor commit: the sync now
resolves the branch to a commit, downloads at it, and records `source_commit`.

Safeguards:
- Only the pair ids the manifest names, and only if the imported CSV no longer
  ships them.
- Untouched `unvalidated` records are archived in `retired_records`, then deleted.
  Touched records are flagged and never deleted.
- It is idempotent.
- The child does not re-take the advisory lock, which the parent holds. It passes
  cleanup's maintenance gate instead (run `running`, Parts 1 and 2 verified, same
  snapshot sha256), plus a commit match.
- A fail-closed cap, `EXTRACTOR_MAX_RETIRE_PERCENT`, defaults to 15% of
  `unvalidated`. The first run is ~539 of ~4,800, about 11%. The cap is re-checked
  per batch.
- The outcome goes to `stage_status.retire_superseded` and `safety_report.retire`.
  A retire problem is a warning, because the import stands.

The manual CLI stays, as a dry run or as `--apply --expect-retire N`. `retired_records`
is created by `db_schema.sql` at app startup on the first deploy.

**Consequence for this repo.** From the deploy on, committing `data/retired_pairs.csv`
together with `data/extracted.csv` is what retires records, the next night, with no
review step. Review the manifest before pushing, as step 4 above says. Commit the two
files together, because the stage reads both at one commit.
