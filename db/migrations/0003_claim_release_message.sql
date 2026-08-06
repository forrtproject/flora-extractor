-- 0003_claim_release_message.sql — say what an unknown release actually is.
--
-- The RPC's rejection message told the operator to "refresh and re-route", which
-- is the one thing that does not help: routing is recomputed locally and writes
-- no server row, so re-routing the same pool and bundle mints the SAME release id
-- and the claim fails again. What is missing is the `engine_releases` row, and
-- registering it is a restatement of a decision routing already made
-- (`ClaimsClient.register_release`, called by `route` and retried by the claim
-- path in `filter/engine/tiers.py:_claim`).
--
-- The function body is otherwise byte-identical to 0001's; it is repeated whole
-- because plpgsql has no way to amend one line of a live function. A database
-- created from 0001 today already carries the corrected text — this migration is
-- for the ones created before it.
--
-- Idempotent, same rules as 0001 and 0002: re-running is a no-op.

BEGIN;

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
        RAISE EXCEPTION 'unknown_release: % — no engine_releases row; register the release before claiming (route registers it, and screen --run re-registers on demand)', p_release_id
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

COMMIT;
