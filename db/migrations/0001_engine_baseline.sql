-- 0001_engine_baseline.sql — the filter engine's Postgres state authority (issue #146 M2).
--
-- Run in the Supabase SQL editor (or psql) against the project that already hosts
-- the validation tables (unvalidated / validation_queue / validated). Everything
-- here is prefixed `engine_` so the two schemas never collide.
--
-- What lives here and why (issue #146 §4): Postgres is the SOLE MUTABLE AUTHORITY.
-- Routing is derived data and is recomputed from pool + specs (local DuckDB, §4);
-- bulk immutable artifacts live on HF. What Postgres holds is exactly what cannot
-- be recomputed: which rows were CLAIMED before money or judgment was spent, what
-- verdicts came back, what humans said, and an audit trail of both.
--
-- The file is idempotent: re-running it is a no-op on an existing database.
-- No DROP TABLE, ever — verdicts and labels are permanent evidence.

BEGIN;

-- gen_random_uuid() is built in from PG13; the extension keeps the file runnable
-- on older instances and is a no-op on Supabase.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ── engine_releases ──────────────────────────────────────────────────────────
-- The server-side registry of routing releases. A release id (filter/engine/
-- release.py) is the sha256 of six inputs: pool manifest hash, overlay hash,
-- bundle hash, engine version, alias release, schema version. Claims verify
-- against this table: the server REJECTS a claim naming an unknown release, so a
-- client on a stale bundle must refresh and re-route before it can spend (§4).
-- `payload` stores those six inputs verbatim, so a release id can be explained
-- without the client that minted it.
CREATE TABLE IF NOT EXISTS engine_releases (
    release_id  text PRIMARY KEY,
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE engine_releases IS
    'Routing releases (six-input sha256). Claims are rejected against unknown ids.';


-- ── engine_claims ────────────────────────────────────────────────────────────
-- A claim pins a batch of works to one tier before spend. Claimed membership is
-- no longer recomputable: spec edits re-route unclaimed rows only (§1).
--
-- Lifecycle (§1), all of it expressed by `status`:
--   active     — the batch is pinned and blocks re-claiming in the same tier.
--   complete   — the run finished. PARTIAL COMPLETION is not a state: complete
--                the claim for what was done and re-claim the remainder as a new
--                claim. Items with no verdict are simply items with no verdict.
--   cancelled  — the batch was abandoned before spend. Flipping the status is
--                the whole of "freeing" the items: the conflict check below looks
--                at ACTIVE claims only, so a cancelled claim blocks nothing.
--   failed     — the run broke (provider down, budget exhausted). Same effect as
--                cancelled for routing; kept distinct so retries are countable.
-- Retry = a new claim over the same works once the old one is no longer active.
-- SUPERSESSION IS NOT A CLAIM OPERATION: a wrong verdict is superseded at the
-- verdict level (engine_verdicts.superseded_by); the claim that produced it stays
-- exactly as it was, because it is a record of what was spent.
CREATE TABLE IF NOT EXISTS engine_claims (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id    text        NOT NULL REFERENCES engine_releases (release_id),
    tier          text        NOT NULL
                  CHECK (tier IN ('screen_cheap', 'screen_expensive', 'human', 'measurement')),
    status        text        NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'complete', 'cancelled', 'failed')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,
    meta          jsonb       NOT NULL DEFAULT '{}'::jsonb   -- batch label, budget note, actor
);

COMMENT ON TABLE engine_claims IS
    'One claimed batch, one tier. status=active is what blocks re-claiming.';

-- The lookup the claim RPC and claimed_work_ids() both drive.
CREATE INDEX IF NOT EXISTS engine_claims_active_idx
    ON engine_claims (release_id, tier) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS engine_claims_tier_status_idx
    ON engine_claims (tier, status);


-- ── engine_claim_items ───────────────────────────────────────────────────────
-- The works in a claim, each with the pile it sat in AT CLAIM TIME. That pile is
-- pinned routing evidence: routing is recomputable and will drift under spec
-- edits, but what this batch was routed as when it was paid for must not.
--
-- CONCURRENCY RULE (issue #146 §8 open decision 4 — implementer default, flagged
-- for maintainer review): one work may be held by at most ONE ACTIVE claim PER
-- TIER, but different tiers may hold it concurrently (a human label and an LLM
-- screen of the same work are independent evidence, not a conflict), and
-- `measurement` claims neither take nor respect the lock — measurement must never
-- be able to starve production, and two measurement tasks over the same work is
-- the normal case, not an error.
--
-- This cannot be a plain unique index: the condition ("active", "same tier")
-- lives in engine_claims, and Postgres index predicates cannot cross tables.
-- So it is enforced inside engine_claim_batch() instead, under a per-tier
-- advisory lock, and the index below is the lookup that check needs.
CREATE TABLE IF NOT EXISTS engine_claim_items (
    claim_id  uuid   NOT NULL REFERENCES engine_claims (id) ON DELETE CASCADE,
    work_id   bigint NOT NULL,
    -- Deliberately un-CHECKed: the pile vocabulary lives in git (filter/spec/
    -- conventions.json) and gains members without a migration.
    pile      text   NOT NULL,
    PRIMARY KEY (claim_id, work_id)
);

COMMENT ON TABLE engine_claim_items IS
    'Works in a claim + the pile they were routed to at claim time (pinned evidence).';

CREATE INDEX IF NOT EXISTS engine_claim_items_work_idx ON engine_claim_items (work_id);


-- ── engine_verdicts ──────────────────────────────────────────────────────────
-- Evidence. Permanent by construction: a recorded verdict is never re-litigated,
-- only superseded (§1). `response_hash` names the raw response blob on HF; the
-- ordering rule (§4) is that the blob uploads BEFORE the verdict row, and a row
-- that could not wait is inserted `response_pending_upload` and reconciled later.
-- Lean on purpose: verdict, confidence, a short quote, model + prompt hashes,
-- cost. The full response lives on HF, not here.
CREATE TABLE IF NOT EXISTS engine_verdicts (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id       uuid        NOT NULL REFERENCES engine_claims (id),
    work_id        bigint      NOT NULL,
    tier           text        NOT NULL,
    model          text        NOT NULL DEFAULT '',
    prompt_hash    text        NOT NULL DEFAULT '',
    verdict        text        NOT NULL,
    confidence     text        NOT NULL DEFAULT '',
    quote          text        NOT NULL DEFAULT '',
    response_hash  text,
    response_state text        NOT NULL DEFAULT 'response_pending_upload'
                   CHECK (response_state IN ('uploaded', 'response_pending_upload')),
    cost           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    superseded_by  uuid        REFERENCES engine_verdicts (id)
);

COMMENT ON TABLE engine_verdicts IS
    'Permanent LLM/human tier evidence. UPDATE only of superseded_by/response_state; no DELETE.';

CREATE INDEX IF NOT EXISTS engine_verdicts_work_idx  ON engine_verdicts (work_id);
CREATE INDEX IF NOT EXISTS engine_verdicts_claim_idx ON engine_verdicts (claim_id);
-- The reconciliation queue: rows whose blob never made it to HF.
CREATE INDEX IF NOT EXISTS engine_verdicts_pending_upload_idx
    ON engine_verdicts (created_at) WHERE response_state = 'response_pending_upload';


-- ── engine_human_labels ──────────────────────────────────────────────────────
-- Human judgments, under the same permanence rule as verdicts. Kept separate
-- because a human label is not produced by a claim run: it can arrive from the
-- validation app, a gold set, or a holdout labelling session (`source`).
CREATE TABLE IF NOT EXISTS engine_human_labels (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id        bigint      NOT NULL,
    label          text        NOT NULL,
    labeler        text        NOT NULL DEFAULT '',
    source         text        NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now(),
    superseded_by  uuid        REFERENCES engine_human_labels (id)
);

COMMENT ON TABLE engine_human_labels IS
    'Permanent human labels. UPDATE only of superseded_by; no DELETE.';

CREATE INDEX IF NOT EXISTS engine_human_labels_work_idx ON engine_human_labels (work_id);


-- ── engine_alias_exceptions ──────────────────────────────────────────────────
-- Alias-merge exceptions: works whose OpenAlex merge the engine must not follow
-- (or must follow differently) when it resolves ids. The bulk alias map is a git
-- file with its own release input; this table holds the hand-made exceptions,
-- which are mutable and so belong to the mutable authority.
CREATE TABLE IF NOT EXISTS engine_alias_exceptions (
    work_id       bigint PRIMARY KEY,
    canonical_id  bigint      NOT NULL,
    reason        text        NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);


-- ── engine_holdout ───────────────────────────────────────────────────────────
-- Registry only. The holdout is for evaluation, never calibration (§3); how it
-- is CONSTRUCTED (size, gold/random mix, refresh policy) is issue #146 open
-- decision 2 and is deliberately not decided here. A row records that a frozen
-- manifest exists and when it froze.
CREATE TABLE IF NOT EXISTS engine_holdout (
    holdout_id  text PRIMARY KEY,
    manifest    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    frozen_at   timestamptz NOT NULL DEFAULT now()
);


-- ── engine_audit ─────────────────────────────────────────────────────────────
-- Insert-only event log. Every claim, release and supersession writes one row, so
-- "who spent what, against which release" is answerable without reading state
-- that has since changed.
CREATE TABLE IF NOT EXISTS engine_audit (
    id          bigserial PRIMARY KEY,
    actor       text        NOT NULL DEFAULT '',
    action      text        NOT NULL,
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS engine_audit_action_idx ON engine_audit (action, created_at);


-- ── Permanence triggers ──────────────────────────────────────────────────────
-- The permanence rule is enforced by the database, not by client discipline: a
-- verdict that a script can quietly rewrite is not evidence.

CREATE OR REPLACE FUNCTION engine_verdicts_permanence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'engine_verdicts is append-only: supersede the row, do not delete it (id=%)', OLD.id;
    END IF;
    -- response_hash may be filled in ONCE (the response_pending_upload
    -- reconciliation path, §4); it may never be changed to a different blob.
    IF NEW.response_hash IS DISTINCT FROM OLD.response_hash
       AND OLD.response_hash IS NOT NULL THEN
        RAISE EXCEPTION 'engine_verdicts.response_hash is write-once (id=%)', OLD.id;
    END IF;
    IF (to_jsonb(NEW) - 'superseded_by' - 'response_state' - 'response_hash')
       IS DISTINCT FROM
       (to_jsonb(OLD) - 'superseded_by' - 'response_state' - 'response_hash') THEN
        RAISE EXCEPTION 'engine_verdicts is immutable except superseded_by/response_state/response_hash (id=%)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS engine_verdicts_permanence_trg ON engine_verdicts;
CREATE TRIGGER engine_verdicts_permanence_trg
    BEFORE UPDATE OR DELETE ON engine_verdicts
    FOR EACH ROW EXECUTE FUNCTION engine_verdicts_permanence();


CREATE OR REPLACE FUNCTION engine_human_labels_permanence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'engine_human_labels is append-only: supersede the row (id=%)', OLD.id;
    END IF;
    IF (to_jsonb(NEW) - 'superseded_by') IS DISTINCT FROM (to_jsonb(OLD) - 'superseded_by') THEN
        RAISE EXCEPTION 'engine_human_labels is immutable except superseded_by (id=%)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS engine_human_labels_permanence_trg ON engine_human_labels;
CREATE TRIGGER engine_human_labels_permanence_trg
    BEFORE UPDATE OR DELETE ON engine_human_labels
    FOR EACH ROW EXECUTE FUNCTION engine_human_labels_permanence();


CREATE OR REPLACE FUNCTION engine_audit_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'engine_audit is insert-only (attempted %)', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS engine_audit_append_only_trg ON engine_audit;
CREATE TRIGGER engine_audit_append_only_trg
    BEFORE UPDATE OR DELETE ON engine_audit
    FOR EACH ROW EXECUTE FUNCTION engine_audit_append_only();


-- ── engine_claim_batch: the claim RPC ────────────────────────────────────────
-- ONE transaction, server-side, per issue #146 §4: "verify release, insert claim
-- + items atomically, reject the batch on any conflict — never select-then-insert
-- from the client."
--
--   p_items — jsonb array of {"work_id": <bigint>, "pile": "<pile at route time>"}
--   p_meta  — batch label / budget note / actor; `actor` is lifted into the audit row
--   returns — the new claim id
--
-- REJECTION IS ALL-OR-NOTHING. One already-claimed work fails the whole batch;
-- the caller re-routes (its release is stale) or re-claims the remainder. A
-- partial insert would leave the caller believing it holds works it does not.
--
-- The advisory lock is what makes the conflict check sound: two concurrent
-- claimers could each read "no conflict" and both insert. It is taken per TIER,
-- so screen_cheap and screen_expensive claim in parallel, and never for
-- `measurement`, which by rule conflicts with nothing.
CREATE OR REPLACE FUNCTION engine_claim_batch(
    p_release_id text,
    p_tier       text,
    p_items      jsonb,
    p_meta       jsonb DEFAULT '{}'::jsonb
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_claim_id  uuid;
    v_conflicts bigint;
    v_n         bigint;
BEGIN
    IF p_tier NOT IN ('screen_cheap', 'screen_expensive', 'human', 'measurement') THEN
        RAISE EXCEPTION 'unknown_tier: %', p_tier USING ERRCODE = 'check_violation';
    END IF;

    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_items must be a jsonb array of {work_id, pile}'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT count(*) INTO v_n FROM jsonb_array_elements(p_items);
    IF v_n = 0 THEN
        RAISE EXCEPTION 'empty_claim: nothing to claim'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM engine_releases WHERE release_id = p_release_id) THEN
        RAISE EXCEPTION 'unknown_release: % — refresh and re-route before claiming', p_release_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF p_tier <> 'measurement' THEN
        PERFORM pg_advisory_xact_lock(hashtext('engine_claim_batch:' || p_tier));

        SELECT count(*) INTO v_conflicts
        FROM jsonb_to_recordset(p_items) AS it(work_id bigint, pile text)
        JOIN engine_claim_items ci ON ci.work_id = it.work_id
        JOIN engine_claims       c  ON c.id = ci.claim_id
        WHERE c.status = 'active'
          AND c.tier   = p_tier;

        IF v_conflicts > 0 THEN
            RAISE EXCEPTION 'claim_conflict: % of % works already held by an active % claim',
                v_conflicts, v_n, p_tier
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;

    INSERT INTO engine_claims (release_id, tier, meta)
    VALUES (p_release_id, p_tier, coalesce(p_meta, '{}'::jsonb))
    RETURNING id INTO v_claim_id;

    -- DISTINCT ON: a batch that names the same work twice is a client bug, not a
    -- reason to fail — the claim means the same thing either way.
    INSERT INTO engine_claim_items (claim_id, work_id, pile)
    SELECT DISTINCT ON (it.work_id) v_claim_id, it.work_id, coalesce(it.pile, '')
    FROM jsonb_to_recordset(p_items) AS it(work_id bigint, pile text)
    ORDER BY it.work_id;

    INSERT INTO engine_audit (actor, action, payload)
    VALUES (coalesce(p_meta ->> 'actor', current_user),
            'claim',
            jsonb_build_object('claim_id', v_claim_id, 'release_id', p_release_id,
                               'tier', p_tier, 'n_items', v_n, 'meta', coalesce(p_meta, '{}'::jsonb)));

    RETURN v_claim_id;
END;
$$;


-- ── engine_release_claim: end a claim ────────────────────────────────────────
-- complete / cancelled / failed. The status flip is the whole operation: the
-- conflict check above reads ACTIVE claims only, so ending a claim frees its
-- items with no second write. Only an active claim can be ended, so a late
-- runner cannot re-open or re-decide a settled batch.
CREATE OR REPLACE FUNCTION engine_release_claim(
    p_claim_id uuid,
    p_status   text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_prev text;
BEGIN
    IF p_status NOT IN ('complete', 'cancelled', 'failed') THEN
        RAISE EXCEPTION 'bad_status: % (complete|cancelled|failed)', p_status
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT status INTO v_prev FROM engine_claims WHERE id = p_claim_id FOR UPDATE;
    IF v_prev IS NULL THEN
        RAISE EXCEPTION 'unknown_claim: %', p_claim_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF v_prev <> 'active' THEN
        RAISE EXCEPTION 'claim_not_active: % is already %', p_claim_id, v_prev
            USING ERRCODE = 'check_violation';
    END IF;

    UPDATE engine_claims
       SET status = p_status, completed_at = now()
     WHERE id = p_claim_id;

    INSERT INTO engine_audit (actor, action, payload)
    VALUES (current_user, 'release_claim',
            jsonb_build_object('claim_id', p_claim_id, 'status', p_status));

    RETURN p_claim_id;
END;
$$;


-- ── Access ───────────────────────────────────────────────────────────────────
-- RLS on with no policies: the anon/authenticated keys see nothing. Engine
-- runners hold the service key, which bypasses RLS, and the two RPCs are
-- SECURITY DEFINER so they work either way.
ALTER TABLE engine_releases         ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_claims           ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_claim_items      ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_verdicts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_human_labels     ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_alias_exceptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_holdout          ENABLE ROW LEVEL SECURITY;
ALTER TABLE engine_audit            ENABLE ROW LEVEL SECURITY;

COMMIT;
