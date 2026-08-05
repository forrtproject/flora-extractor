# WIP — `extract/csv_to_db.py` (parked)

This branch exists to preserve `extract/csv_to_db.py` and `tests/test_csv_to_db.py`
exactly as they stood on `fix/current-state-findings` at the moment they were removed
from the main line. Nothing else on this branch differs from that commit.

## Why it is parked

The push of resolved `data/extracted.csv` rows into the Supabase validation tables
(`unvalidated`, `record_metadata`, `validation_queue`) is performed by
[`flora-validation/csv_to_db.py`](https://github.com/forrtproject/flora-validation/blob/main/csv_to_db.py),
in the separate validation repo — confirmed by @Rohan-Tondlekar in
[flora-extractor#172](https://github.com/forrtproject/flora-extractor/issues/172) — not
by this module. `csv_to_db.py` has never
been run against the current schema — its own docstring says it is blocked on a
`title_r` / `title_o` migration in the validation repo — and nothing in this repo
imports it: the only references were tests of its own contracts. Keeping a second,
untested importer in the extractor repo means two candidate answers to "what actually
reaches the validators", which is worse than none.

Stage 3's contract is therefore: **it produces `data/extracted.csv`**. What happens
between that file and the validation tables is owned elsewhere.

## The defect it carries if it is ever revived

Review finding **F6** (2026-08-05):

> `csv_to_db` makes three independent PostgREST inserts per row with no retry;
> ordering `record_metadata` last means a mid-row failure leaves orphan
> `unvalidated` / `validation_queue` rows that the pair-id dedup will never
> reconcile — the comment's "gets completed on re-run" is not implemented.

Concretely: `run_import()` inserts into `unvalidated`, then `validation_queue` (three
slots), then `record_metadata`. `_load_existing_pair_ids()` dedups on `pair_id` read
from `unvalidated` alone, so a row that failed after its `unvalidated` insert is seen
as already imported on the next run and its missing metadata/queue rows are never
created. There is no retry and no rollback.

Do **not** fix this by adding retries around the three calls. The two things that
would make it correct:

1. **One server-side RPC that creates all three atomically** — a Postgres function
   (`SECURITY DEFINER`, one transaction) taking the record payload and creating the
   `unvalidated` row, its `record_metadata` row and its three `validation_queue`
   slots, so a row is either fully present or fully absent.
2. **Server-side idempotency on `pair_id`** — a unique constraint on the pair id plus
   `ON CONFLICT DO NOTHING` / upsert semantics inside that function, so re-running the
   import is safe by construction rather than by a client-side pre-read that can only
   ever see one of the three tables.

## Status of the live importer

Read on 2026-08-06 from `flora-validation@main`. It does **not** have the F6 problem:
psycopg2, the whole run inside one `with conn:` transaction, and `_insert_unvalidated`
uses `ON CONFLICT (pair_id) DO NOTHING` — the atomic, server-side-idempotent shape the
two fixes above describe. Two gaps were raised on #172 instead:

- it sends `title_r` / `title_o` into the `study_r` / `study_o` columns (issue #103),
  so the extracted study numbers have nowhere to land;
- it sends neither `record_metadata.work_id` nor `release_id`, so pushed records carry
  no engine lineage and `filter/engine/supersede.py` cannot see them.

## Ask — @Rohan-Tondlekar

Still open on #172: **move the importer into this repo** (or vendor a copy here) so the
output contract of Stage 3 is visible in one place, or document in `flora-validation`
how it is run (manual / cron / app action) and against which input. Once either is
done, this branch can be deleted rather than left as a second possible answer.

Once that is done, delete this branch — there is no reason to merge a corrected
version of this module back while a working importer exists elsewhere.

## Restoring

```bash
git checkout wip/csv-to-db -- extract/csv_to_db.py tests/test_csv_to_db.py
```

Note that the removal commit on the main line also stripped the contract tests that
imported this module from `tests/test_link_method_contract.py`,
`tests/test_dashboard_stats.py`, `tests/test_engine_supersede.py`,
`tests/test_audit_extracted.py` and `tests/test_extract.py`; restoring the module means
restoring those too (see that commit's diff).
