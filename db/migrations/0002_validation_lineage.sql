-- 0002_validation_lineage.sql — lineage between the engine and the validation
-- tables (issue #146 M5).
--
-- §5: "Decisions become immutable once sent for human validation; upstream changes
-- create superseding records with lineage." This migration supplies the two halves
-- of that sentence.
--
-- 1. record_metadata gains `work_id` + `release_id`, written by the validation repo's
--    import (flora-validation/csv_to_db.py) when a row is pushed for validation.
--    Reconciliation keys on work_id, not DOI:
--    a work is the engine's identity (alias-resolved int64), a DOI is a string a
--    row may lack, share or spell differently. Both columns are NULLABLE because
--    every row imported before the engine existed has neither — that is the shape
--    of the data, not a compatibility shim.
--
-- 2. engine_supersessions records that an upstream change affects validation
--    records that have already been sent. It NEVER touches those records: nothing
--    here updates or deletes unvalidated / validated / validation_queue. The
--    supersession row IS the lineage, and the validation repo consumes it
--    downstream and decides what to show a validator.
--
-- Idempotent, same rules as 0001: no DROP TABLE, re-running is a no-op.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ── record_metadata lineage columns ──────────────────────────────────────────
-- record_metadata belongs to the validation schema, not the engine's, so this is
-- an ADD COLUMN and nothing else: no constraint that could reject a legacy row,
-- no default that would claim a lineage a historical row does not have.
ALTER TABLE record_metadata ADD COLUMN IF NOT EXISTS work_id    bigint;
ALTER TABLE record_metadata ADD COLUMN IF NOT EXISTS release_id text;

COMMENT ON COLUMN record_metadata.work_id IS
    'Alias-resolved OpenAlex int64 of the replication (from openalex_id_r). NULL on rows imported before the engine.';
COMMENT ON COLUMN record_metadata.release_id IS
    'Routing release the row was exported under. NULL for rows that did not come through the engine.';

-- The reconciliation lookup: work_ids → the validation records that carry them.
CREATE INDEX IF NOT EXISTS record_metadata_work_id_idx ON record_metadata (work_id);


-- ── engine_supersessions ─────────────────────────────────────────────────────
-- One row per (work, upstream change) naming the validation records it affects.
--
--   kind = 'reroute'    — the work moved between non-terminal piles: the decision
--                         still stands but was routed under a superseded release.
--   kind = 'withdrawal' — the work is now rule-discarded: what was sent for
--                         validation should no longer be in the corpus.
--   kind = 'verdict'    — an expensive-tier verdict was superseded upstream
--                         (engine_verdicts.superseded_by), changing what was sent.
--
-- affected_record_ids is text[], not bigint[]: record_id in the validation schema
-- is a uuid string (the import mints `str(uuid.uuid4())`), so a bigint
-- array could not hold the ids this column exists to name.
CREATE TABLE IF NOT EXISTS engine_supersessions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id             bigint      NOT NULL,
    old_release_id      text,
    new_release_id      text,
    kind                text        NOT NULL
                        CHECK (kind IN ('reroute', 'verdict', 'withdrawal')),
    reason              text        NOT NULL DEFAULT '',
    affected_record_ids text[]      NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now(),
    actor               text        NOT NULL DEFAULT ''
);

COMMENT ON TABLE engine_supersessions IS
    'Insert-only lineage: an upstream change and the already-sent validation records it names. Never mutates the validation tables.';

CREATE INDEX IF NOT EXISTS engine_supersessions_work_idx
    ON engine_supersessions (work_id, created_at);
CREATE INDEX IF NOT EXISTS engine_supersessions_release_idx
    ON engine_supersessions (new_release_id, created_at);


-- Permanence: same rule as engine_audit. A lineage record a script can rewrite is
-- not lineage — a correction is another row, not an edit.
CREATE OR REPLACE FUNCTION engine_supersessions_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'engine_supersessions is insert-only (attempted %)', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS engine_supersessions_append_only_trg ON engine_supersessions;
CREATE TRIGGER engine_supersessions_append_only_trg
    BEFORE UPDATE OR DELETE ON engine_supersessions
    FOR EACH ROW EXECUTE FUNCTION engine_supersessions_append_only();


ALTER TABLE engine_supersessions ENABLE ROW LEVEL SECURITY;

COMMIT;
