-- 0005_extract_tier.sql — Stage 3 becomes a claimed tier.
--
-- Three things the extract tier needs that the screen tiers did not:
--
--   1. `extract` in the tier vocabulary — the CHECK on engine_claims and the
--      RPC's own list.
--   2. A verdict row that can carry a TARGET LIST. A screen verdict is one
--      label; an extract verdict is one work's targets and their outcomes, and
--      `python -m extract.export` rebuilds data/extracted.csv from it — so it
--      must be readable back out of Postgres, not only out of a response blob
--      nothing reads.
--   3. A lease that can be RENEWED. A PDF-heavy ladder run is not bounded by
--      the six hours the screen's lease assumed, and shrinking the batch to fit
--      a lease makes batch size a function of the clock rather than of the work.
--
-- ORDER OF DEPLOYMENT: run this BEFORE the code that claims tier 'extract' or
-- calls engine_renew_claim. Idempotent, same rules as 0001–0004: re-running is
-- a no-op. No DROP TABLE.

BEGIN;

-- ── 1. the tier vocabulary ───────────────────────────────────────────────────
-- 0001 declared the CHECK inline, so Postgres named it engine_claims_tier_check.
ALTER TABLE engine_claims DROP CONSTRAINT IF EXISTS engine_claims_tier_check;
ALTER TABLE engine_claims ADD CONSTRAINT engine_claims_tier_check
    CHECK (tier IN ('screen_cheap', 'screen_expensive', 'extract',
                    'human', 'measurement'));

-- engine_verdicts.tier is deliberately un-CHECKed (0001) — like
-- engine_claim_items.pile, its vocabulary lives in git. Nothing to widen there.


-- ── 2. engine_verdicts.payload ───────────────────────────────────────────────
ALTER TABLE engine_verdicts
    ADD COLUMN IF NOT EXISTS payload jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN engine_verdicts.payload IS
    'The structured answer the lean columns cannot hold: for tier=extract, the '
    'per-target link + outcome rows and the input snapshot they were derived '
    'from. Immutable like every column but the three the permanence trigger '
    'names.';

-- NO TRIGGER CHANGE IS NEEDED, and that is worth stating rather than
-- rediscovering: engine_verdicts_permanence() (0001) compares
--     to_jsonb(NEW) - 'superseded_by' - 'response_state' - 'response_hash'
-- against the same projection of OLD, so a column added here is covered the
-- moment it exists — payload is write-once by construction. A corrected extract
-- result is a NEW row pointed at by the old row's superseded_by, never an edit.


-- ── 3. the read path the export drives ───────────────────────────────────────
-- extract.export reads every live row of one tier; the existing work_id index
-- does not cover the tier predicate.
CREATE INDEX IF NOT EXISTS engine_verdicts_tier_work_idx
    ON engine_verdicts (tier, work_id)
    WHERE superseded_by IS NULL;


-- ── 4. engine_claim_batch, re-declared whole (the 0003/0004 pattern) ─────────
-- 0004's body verbatim, with ONE change: the tier list gains 'extract'. Same
-- signature, so CREATE OR REPLACE is enough — no DROP, no overload ambiguity.
CREATE OR REPLACE FUNCTION engine_claim_batch(
    p_release_id text,
    p_tier       text,
    p_items      jsonb,
    p_meta       jsonb DEFAULT '{}'::jsonb,
    p_expires_at timestamptz DEFAULT NULL
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
    IF p_tier NOT IN ('screen_cheap', 'screen_expensive', 'extract',
                      'human', 'measurement') THEN
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

    IF p_expires_at IS NOT NULL AND p_expires_at <= now() THEN
        RAISE EXCEPTION 'bad_expiry: % is not in the future', p_expires_at
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM engine_releases WHERE release_id = p_release_id) THEN
        RAISE EXCEPTION 'unknown_release: % — no engine_releases row; register the release before claiming (route registers it, and screen --run re-registers on demand)', p_release_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF p_tier <> 'measurement' THEN
        PERFORM pg_advisory_xact_lock(hashtext('engine_claim_batch:' || p_tier));

        SELECT count(*) INTO v_conflicts
        FROM jsonb_to_recordset(p_items) AS it(work_id bigint, pile text)
        JOIN engine_claim_items ci ON ci.work_id = it.work_id
        JOIN engine_claims       c  ON c.id = ci.claim_id
        WHERE c.status     = 'active'
          AND c.tier       = p_tier
          AND c.expires_at > now();

        IF v_conflicts > 0 THEN
            RAISE EXCEPTION 'claim_conflict: % of % works already held by an active % claim',
                v_conflicts, v_n, p_tier
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;

    INSERT INTO engine_claims (release_id, tier, meta, expires_at)
    VALUES (p_release_id, p_tier, coalesce(p_meta, '{}'::jsonb),
            coalesce(p_expires_at, now() + interval '6 hours'))
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


-- ── 5. engine_renew_claim: a heartbeat, not a second claim ───────────────────
-- Extends a LIVE claim's lease. Three refusals, each a real state:
--
--   unknown_claim     — nothing to renew.
--   claim_not_active  — the run already ended this claim; renewing would
--                       resurrect a lease over works nobody is working on.
--   claim_lease_lost  — THE IMPORTANT ONE. The lease already ran out, so the
--                       works are re-claimable and another machine may be
--                       spending on them right now. Re-extending would recreate
--                       exactly the double-claim the lease exists to prevent,
--                       so the RPC refuses and the client must stop the batch.
--                       Its verdicts stand: expiry frees works, it never
--                       retracts evidence (0004).
--
-- A renewal writes NO engine_audit row. The audit log answers "who spent what";
-- a heartbeat spends nothing, and one row every fifteen minutes per run would
-- bury the claims and releases that log exists for. The claim's expires_at IS
-- the record.
--
-- Never shortens: a caller passing an earlier time gets the current lease back
-- unchanged, so a clock-skewed client cannot hand its works to somebody else.
CREATE OR REPLACE FUNCTION engine_renew_claim(
    p_claim_id   uuid,
    p_expires_at timestamptz
) RETURNS timestamptz
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_status  text;
    v_current timestamptz;
BEGIN
    SELECT status, expires_at INTO v_status, v_current
      FROM engine_claims WHERE id = p_claim_id FOR UPDATE;

    IF v_status IS NULL THEN
        RAISE EXCEPTION 'unknown_claim: %', p_claim_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF v_status <> 'active' THEN
        RAISE EXCEPTION 'claim_not_active: % is already %', p_claim_id, v_status
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_current <= now() THEN
        RAISE EXCEPTION 'claim_lease_lost: % expired at %; its works are claimable again and may already be held elsewhere',
            p_claim_id, v_current
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_expires_at <= v_current THEN
        RETURN v_current;
    END IF;

    UPDATE engine_claims SET expires_at = p_expires_at WHERE id = p_claim_id;
    RETURN p_expires_at;
END;
$$;

COMMIT;
