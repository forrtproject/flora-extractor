-- 0004_claim_expiry.sql — a stranded claim must stop blocking its works.
--
-- A claim was released only by the run that took it (`_finish()` in
-- filter/engine/tiers.py). A SIGKILL, a lost host or a laptop closing left the
-- claim `active` forever, and every later batch subtracts actively-claimed works
-- (`ClaimsClient.claimed_work_ids`), so those rows were silently excluded from
-- every future run with nothing on screen to say so.
--
-- The fix is a lease. A claim now carries `expires_at`, and an EXPIRED claim
-- blocks nothing: the conflict check inside `engine_claim_batch` and the client's
-- subtraction query both read live claims only. Its status is untouched — the
-- claim is still a record of what a run took, and its VERDICTS are untouched too.
-- Expiry applies to claims, never to evidence.
--
-- The TTL is set by the CLIENT, per claim (`CLAIM_TTL_HOURS` in
-- filter/engine/claims.py, 6 hours: an LLM tier run is measured in hours, so a
-- lease measured in minutes would expire under a run that is still working). The
-- column default here is the same length and exists only so a row inserted by
-- something other than that client is still leased rather than immortal.
--
-- ORDER OF DEPLOYMENT: run this BEFORE the code that sends `p_expires_at`. The
-- client names this file in the error it raises when the RPC does not accept the
-- argument, so an un-migrated database fails loudly at the claim rather than
-- claiming without a lease.
--
-- Idempotent, same rules as 0001–0003: re-running is a no-op. No DROP TABLE.

BEGIN;

-- ── the lease column ─────────────────────────────────────────────────────────
ALTER TABLE engine_claims ADD COLUMN IF NOT EXISTS expires_at timestamptz;

-- Back-fill: an existing row is leased from when it was created, so a claim
-- stranded before this migration is already expired the moment it lands.
UPDATE engine_claims SET expires_at = created_at + interval '6 hours'
 WHERE expires_at IS NULL;

ALTER TABLE engine_claims ALTER COLUMN expires_at SET DEFAULT (now() + interval '6 hours');
ALTER TABLE engine_claims ALTER COLUMN expires_at SET NOT NULL;

COMMENT ON COLUMN engine_claims.expires_at IS
    'Lease end. status=active AND expires_at > now() is what blocks re-claiming.';

-- The lookup both the conflict check and claimed_work_ids() drive. The predicate
-- cannot mention now() (index predicates must be immutable), so expires_at is an
-- indexed column instead and the time comparison happens at scan time.
CREATE INDEX IF NOT EXISTS engine_claims_live_idx
    ON engine_claims (tier, release_id, expires_at) WHERE status = 'active';


-- ── engine_claim_batch: take a leased claim, ignore expired ones ─────────────
-- Body is 0003's, with two changes: the conflict check requires the blocking
-- claim to be UNEXPIRED, and the insert stores the caller's lease. A caller that
-- passes no lease (psql by hand, or a checkout from before this migration) gets
-- the column default, so it is leased too.
--
-- The 4-argument function from 0001/0003 is DROPPED rather than left beside this
-- one: adding a defaulted parameter creates an overload, and a 4-argument call
-- would then match both and fail as ambiguous. With the old one gone, a
-- 4-argument call resolves here and takes the default lease.
DROP FUNCTION IF EXISTS engine_claim_batch(text, text, jsonb, jsonb);

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


-- ── engine_release_claim: unchanged semantics, expired claims included ───────
-- Releasing is still the status flip, and an EXPIRED-but-active claim can still
-- be ended: that is what `python -m filter.engine release-claim --claim <id>`
-- does, and it is the same code path a finishing run uses. There is no second
-- release semantics.

COMMIT;
