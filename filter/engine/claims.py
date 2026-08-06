"""Claims client — the engine's handle on the Postgres state authority (#146 M2).

Postgres is the sole mutable authority: routing is recomputed from pool + specs,
but a CLAIM is what pins rows before money or judgment is spent, and a VERDICT is
permanent evidence. This module is the only way engine code touches either.

House style, matching `shared/supabase_client.py`: plain `requests` against
PostgREST, `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` from the environment (the same
two variables — they are not re-declared in `shared/config.py`), and paged reads
with a deterministic sort key.

Deployment: `db/migrations/0001_engine_baseline.sql` through
`0004_claim_expiry.sql` must have been run — 0004 adds the claim lease this
module sends, and without it every claim here is refused by name. Claiming
goes through the server-side `engine_claim_batch` RPC — one transaction that
verifies the release, checks conflicts and inserts, never select-then-insert from
here (#146 §4).

Interface for the M4 tier runners:

    client = ClaimsClient()                       # raises ClaimsNotConfigured if unset
    client.register_release(release_record)       # the six-input record from release.py
    claim_id = client.claim(release_id, "screen_expensive",
                            [(work_id, pile), ...], meta={"batch": "wave-1"})
    ...                                           # spend
    client.record_verdict(claim_id=claim_id, work_id=w, tier="screen_expensive",
                          verdict="replication", response_hash=h,
                          response_state=UPLOADED)
    client.release_claim(claim_id, "complete")

A run that dies mid-batch leaves an `active` claim: end it `failed` and re-claim
what it did not reach. Never re-claim under a live claim — the RPC rejects the
whole batch, by design.

A claim is also a LEASE. A run killed outright (SIGKILL, a lost host) never
reaches its completion path, and an `active` claim with no end blocks its works
from every later batch. So a claim carries `expires_at = now + CLAIM_TTL_HOURS`;
once that passes it blocks nothing, in the RPC and in `claimed_work_ids()` alike.
`python -m filter.engine release-claim` ends one on demand without waiting.
"""

import datetime
import os
from typing import Any, Iterable, Optional

import requests

# PostgREST caps a page at db-max-rows regardless of the Range header; every read
# pages until a short page comes back (same rule as shared/supabase_client.py).
_PAGE_SIZE = 1000
# Hashes per response-state PATCH. They travel in the URL (`response_hash=in.(…)`),
# and a 64-character hash each puts ~3 kB in a 50-hash request — comfortably inside
# any server's URL limit, while a whole commit's worth would not be.
_MARK_CHUNK = 50

TIERS = ("screen_cheap", "screen_expensive", "human", "measurement")
CLAIM_STATUSES = ("active", "complete", "cancelled", "failed")
END_STATUSES = ("complete", "cancelled", "failed")

# §4 ordering: the raw response blob uploads to HF BEFORE its verdict row exists.
# A row that could not wait is inserted PENDING_UPLOAD and reconciled later.
UPLOADED = "uploaded"
PENDING_UPLOAD = "response_pending_upload"
RESPONSE_STATES = (UPLOADED, PENDING_UPLOAD)

# Lineage kinds (#146 §5, migration 0002): a work re-routed between live piles, a
# work now rule-discarded, or a superseded expensive-tier verdict.
SUPERSESSION_KINDS = ("reroute", "verdict", "withdrawal")

# How long a claim holds its works before it stops blocking them (migration 0004).
# HOURS, not minutes: an LLM tier run over a full batch is measured in hours, and a
# lease that expired under a working run would let a second run buy the same rows.
# Six is comfortably longer than any run measured so far and short enough that a
# host lost overnight is claimable again by morning. Killing the lease entirely is
# not an option the client offers — that is the bug this constant exists to fix.
CLAIM_TTL_HOURS = 6

# What an un-migrated database says when the claim RPC is called with the lease
# argument (PostgREST cannot resolve the function) or when a read filters on the
# column. Recognised by substring because the two failures arrive as different
# status codes and neither is distinguishable from any other 400 by code alone.
_MISSING_EXPIRY_MARKERS = ("expires_at", "PGRST202")
MIGRATION_0004 = "db/migrations/0004_claim_expiry.sql"


class ClaimsError(RuntimeError):
    """Any failure talking to the state authority."""


class ClaimsNotConfigured(ClaimsError):
    """SUPABASE_URL is unset — the engine must not run unclaimed."""


class UnknownRelease(ClaimsError):
    """The state authority has no `engine_releases` row for this release.

    Its own class because it is repairable where it is raised: the release record
    on disk holds the six inputs the id hashes, so registering it is a restatement
    of what routing already decided rather than a new claim about the world. The
    RPC raises it with `foreign_key_violation`, which PostgREST returns as 409 —
    the same status as a claim conflict — so it is recognised by its message.
    """


class ClaimExpiryUnsupported(ClaimsError):
    """The database predates migration 0004: `engine_claims` has no lease column.

    Its own class because the fix is one named script and nothing else: the RPC
    cannot be called without the lease argument from here, and claiming without a
    lease is the failure this code exists to prevent — so it refuses loudly rather
    than falling back to an immortal claim.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"the state authority has no claim expiry: {detail}. Run "
            f"{MIGRATION_0004} in the Supabase SQL editor, then re-run. Until it "
            "is applied, a claim whose run is killed holds its works forever and "
            "every later batch silently skips them.")


class ClaimConflict(ClaimsError):
    """Some work in the batch is already held by an active claim of this tier."""

    def __init__(self, tier: str, message: str) -> None:
        super().__init__(f"claim conflict in tier '{tier}': {message}")
        self.tier = tier
        self.message = message


class ClaimsClient:
    """Reads and writes the `engine_*` tables through PostgREST."""

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None,
                 timeout: int = 30) -> None:
        self.url = (url if url is not None else os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.key = key if key is not None else os.getenv("SUPABASE_SERVICE_KEY", "")
        self.timeout = timeout
        if not self.url:
            raise ClaimsNotConfigured(
                "SUPABASE_URL is unset: the filter engine claims rows before it "
                "spends, so it cannot run against no state authority")

    # ── transport ────────────────────────────────────────────────────────────

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        headers.update(extra or {})
        return headers

    def _post(self, path: str, payload: Any, prefer: Optional[str] = None,
              tier: str = "") -> Any:
        extra = {"Prefer": prefer} if prefer else None
        resp = requests.post(f"{self.url}/rest/v1/{path}", headers=self._headers(extra),
                             json=payload, timeout=self.timeout)
        return self._parse(resp, path, tier)

    def _patch(self, path: str, params: dict, payload: dict) -> Any:
        resp = requests.patch(f"{self.url}/rest/v1/{path}", headers=self._headers(),
                              params=params, json=payload, timeout=self.timeout)
        return self._parse(resp, path)

    def _parse(self, resp, path: str, tier: str = "") -> Any:
        if resp.status_code >= 400:
            body = resp.text or ""
            if "unknown_release" in body:
                raise UnknownRelease(body.strip())
            if _missing_expiry(body):
                raise ClaimExpiryUnsupported(f"{path} → HTTP {resp.status_code}: "
                                             f"{body.strip()}")
            # The RPC raises unique_violation for a conflict, which PostgREST
            # returns as 409; the message prefix keeps it distinguishable from a
            # plain duplicate-key error on some other table. The tier is the one
            # the caller asked for, not one read back out of the error text.
            if resp.status_code == 409 or "claim_conflict" in body:
                raise ClaimConflict(tier or "unknown", body.strip())
            raise ClaimsError(f"{path} → HTTP {resp.status_code}: {body.strip()}")
        if not resp.content:
            return None
        return resp.json()

    def _get_paged(self, table: str, params: dict, order: str) -> list[dict]:
        """All rows matching *params*, paged past the server row cap.

        *order* must be deterministic: PostgREST may repeat or skip rows between
        pages of an unordered result set.
        """
        url = f"{self.url}/rest/v1/{table}"
        params = dict(params)
        params["order"] = order
        rows: list[dict] = []
        offset = 0
        while True:
            headers = self._headers({
                "Range-Unit": "items",
                "Range": f"{offset}-{offset + _PAGE_SIZE - 1}",
                "Prefer": "count=none",
            })
            resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
            if resp.status_code >= 400:
                body = resp.text or ""
                if _missing_expiry(body):
                    raise ClaimExpiryUnsupported(
                        f"{table} → HTTP {resp.status_code}: {body.strip()}")
                raise ClaimsError(f"{table} → HTTP {resp.status_code}: {body.strip()}")
            page = resp.json()
            rows.extend(page)
            if len(page) < _PAGE_SIZE:
                return rows
            offset += _PAGE_SIZE

    # ── releases ─────────────────────────────────────────────────────────────

    def register_release(self, release: dict) -> str:
        """Register a routing release so claims against it are accepted.

        *release* is the record `filter/engine/release.py` writes: `release_id`
        plus the six inputs. Re-registering is a no-op rather than an error — the
        id IS the hash of the payload, so a duplicate id carries identical content.
        """
        release_id = release.get("release_id")
        if not release_id:
            raise ValueError("release record has no release_id")
        payload = {k: v for k, v in release.items() if k != "release_id"}
        self._post("engine_releases",
                   {"release_id": release_id, "payload": payload},
                   prefer="resolution=ignore-duplicates,return=minimal")
        return release_id

    # ── claims ───────────────────────────────────────────────────────────────

    def claim(self, release_id: str, tier: str, items: Iterable[tuple[int, str]],
              meta: Optional[dict] = None,
              ttl_seconds: Optional[int] = None) -> str:
        """Claim *items* — `(work_id, pile)` pairs — for *tier*. Returns the claim id.

        One server-side transaction (`engine_claim_batch`). Rejection is
        all-or-nothing: if any work is already held by an UNEXPIRED active claim of
        the same tier, `ClaimConflict` is raised and NOTHING is claimed. Different
        tiers may hold the same work concurrently, and `measurement` claims conflict
        with nothing (issue #146 §8 decision 4, implementer default).

        The claim is a LEASE: it stops blocking its works `CLAIM_TTL_HOURS` after
        it is taken, whether or not anything ended it. The expiry is computed here
        and sent, so what a run holds and for how long is decided by the code that
        spends, not by a server default. A database without the lease column
        refuses the call (`ClaimExpiryUnsupported`, naming the migration) rather
        than taking a claim nothing can release.

        *ttl_seconds* overrides `CLAIM_TTL_HOURS` for one claim, because how long a
        run needs its works is a property of the tier and not of the client: six
        hours suits a batch of abstract screens, and a tier whose unit of work is a
        PDF download and a full-text call may need longer. `None` — every caller
        today — is the constant.
        """
        if tier not in TIERS:
            raise ValueError(f"unknown tier: {tier} (expected one of {TIERS})")
        payload_items = [{"work_id": int(w), "pile": pile} for w, pile in items]
        if not payload_items:
            raise ValueError("nothing to claim: items is empty")
        result = self._post("rpc/engine_claim_batch", {
            "p_release_id": release_id,
            "p_tier": tier,
            "p_items": payload_items,
            "p_meta": meta or {},
            "p_expires_at": _lease_end(ttl_seconds),
        }, tier=tier)
        return _scalar(result)

    def release_claim(self, claim_id: str, status: str) -> str:
        """End a claim: `complete` | `cancelled` | `failed`.

        A partially finished run completes the claim for what it did and re-claims
        the remainder as a NEW claim — there is no partial state. Ending a claim
        frees its items, because the conflict check reads active claims only.
        """
        if status not in END_STATUSES:
            raise ValueError(f"bad claim status: {status} (expected one of {END_STATUSES})")
        result = self._post("rpc/engine_release_claim",
                            {"p_claim_id": claim_id, "p_status": status})
        return _scalar(result)

    def claims(self, release_id: Optional[str] = None, tier: Optional[str] = None,
               status: Optional[str] = None) -> list[dict]:
        """Claims, optionally restricted to one release, tier and/or status.

        Ended claims are kept, not deleted, so this is also how a later run learns
        what an earlier one ran and under which `meta` (the tier runners record
        their mode there — a `screen_cheap` run whose discards took effect is a
        claim with `meta.mode == "live"`, and that is the only place that fact is
        written down).
        """
        params: dict = {"select": "id,release_id,tier,status,created_at,expires_at,meta"}
        if release_id:
            params["release_id"] = f"eq.{release_id}"
        if tier:
            params["tier"] = f"eq.{tier}"
        if status:
            params["status"] = f"eq.{status}"
        return self._get_paged("engine_claims", params, order="created_at.asc,id.asc")

    def active_claims(self, release_id: Optional[str] = None) -> list[dict]:
        """Every claim whose status is `active`, optionally restricted to one release.

        Expired ones are INCLUDED: an expired claim blocks nothing, but it is
        exactly what an operator asking "what is still open?" needs to see and end
        (`python -m filter.engine release-claim`). What must ignore expiry is the
        subtraction path, and that is `claimed_work_ids()` below.
        """
        return self.claims(release_id=release_id, status="active")

    def claimed_work_ids(self, release_id: str, tier: str) -> set[int]:
        """Work ids held by an active, UNEXPIRED claim of *tier* in *release_id*.

        What a runner asks before it builds a batch, so the common case is a clean
        claim rather than a rejected one. It is NOT the check that makes claiming
        safe — that check is inside the RPC, under a lock.

        Expired claims are filtered out here for the same reason the RPC ignores
        them: a run killed without ending its claim would otherwise subtract its
        works from every later batch forever, and nothing would say so.
        """
        rows = self._get_paged("engine_claim_items", {
            "select": "work_id,claim_id,"
                      "engine_claims!inner(release_id,tier,status,expires_at)",
            "engine_claims.status": "eq.active",
            "engine_claims.tier": f"eq.{tier}",
            "engine_claims.release_id": f"eq.{release_id}",
            "engine_claims.expires_at": f"gt.{_now_iso()}",
        }, order="work_id.asc,claim_id.asc")
        return {int(r["work_id"]) for r in rows}

    def claim_item_count(self, claim_id: str) -> int:
        """How many works one claim holds, counted by the server.

        A count rather than the rows: a claim can hold tens of thousands of items
        and `release-claim` only needs to say how big the thing it is about to end
        is. PostgREST reports it in `Content-Range` when asked for `count=exact`.
        """
        resp = requests.get(
            f"{self.url}/rest/v1/engine_claim_items",
            headers=self._headers({"Range-Unit": "items", "Range": "0-0",
                                   "Prefer": "count=exact"}),
            params={"select": "work_id", "claim_id": f"eq.{claim_id}"},
            timeout=self.timeout)
        if resp.status_code >= 400:
            raise ClaimsError(f"engine_claim_items → HTTP {resp.status_code}: "
                              f"{resp.text.strip()}")
        total = (resp.headers.get("Content-Range") or "").split("/")[-1]
        return int(total) if total.isdigit() else 0

    # ── verdicts ─────────────────────────────────────────────────────────────

    def record_verdict(self, *, claim_id: str, work_id: int, tier: str, verdict: str,
                       model: str = "", prompt_hash: str = "", confidence: str = "",
                       quote: str = "", response_hash: Optional[str] = None,
                       response_state: str = PENDING_UPLOAD,
                       cost: Optional[dict] = None) -> str:
        """Insert one permanent verdict row. Returns its id.

        `response_state` follows the §4 ordering: the caller uploads the raw blob
        to HF first and passes `UPLOADED` with its `response_hash`, or passes
        `PENDING_UPLOAD` and reconciles later. `UPLOADED` without a hash is
        rejected here rather than written as a dangling reference.
        """
        if response_state not in RESPONSE_STATES:
            raise ValueError(
                f"bad response_state: {response_state} (expected one of {RESPONSE_STATES})")
        if response_state == UPLOADED and not response_hash:
            raise ValueError("response_state 'uploaded' needs a response_hash naming the blob")
        row = {
            "claim_id": claim_id, "work_id": int(work_id), "tier": tier,
            "model": model, "prompt_hash": prompt_hash, "verdict": verdict,
            "confidence": confidence, "quote": quote,
            "response_hash": response_hash, "response_state": response_state,
            "cost": cost or {},
        }
        result = self._post("engine_verdicts", row, prefer="return=representation")
        return _first(result)["id"]

    def verdicts(self, tier: str, claim_ids: Optional[Iterable[str]] = None,
                 ) -> list[dict]:
        """Live (non-superseded) verdict rows for *tier*, one per voter vote.

        Restricting by *claim_ids* is done here rather than in the query: a run
        can hold hundreds of claims and `id=in.(…)` would put them all in a URL.
        A superseded row is excluded because it is evidence of what was believed,
        not of what is believed.

        `confidence` is selected because the expensive tier's gate reads it: the
        soft-discard branch of `screen_gate()` turns on whether a voter stood
        behind its answer, so a replay without this column would silently under-
        discard rather than fail (`filter/engine/tiers.py:_votes_from_rows`).

        `created_at` is selected because a work can hold several rows from the same
        VOTER — one screening per claim, and a run retries a work whose screen did
        not complete. `_answer_rows()` keeps the latest answer per voter, and
        "latest" is a fact only this column carries: the primary key is a uuid, so
        row order is not time order.

        `quote` is selected for the same kind of reason one step further on: it is
        that voter's justifying passage, and the handoff joins the pair's quotes onto
        the row Stage 3 reads (`screen_evidence`), so a set-aside row still names the
        evidence the screen acted on.
        """
        rows = self._get_paged("engine_verdicts", {
            "select": "id,claim_id,work_id,tier,model,verdict,confidence,quote,"
                      "response_state,created_at",
            "tier": f"eq.{tier}",
            "superseded_by": "is.null",
        }, order="work_id.asc,id.asc")
        if claim_ids is None:
            return rows
        wanted = set(claim_ids)
        return [r for r in rows if r.get("claim_id") in wanted]

    def pending_responses(self) -> list[dict]:
        """Every verdict row whose blob no commit has been shown to have accepted.

        Across all tiers, and INCLUDING superseded rows: `response_state` is a fact
        about bytes, not about what is currently believed, and a superseded
        verdict's raw response is exactly the evidence the archive exists to keep.
        What sweeps these is `filter/engine/tiers.py:reconcile_responses`.
        """
        return self._get_paged("engine_verdicts", {
            "select": "id,claim_id,work_id,tier,response_hash,response_state",
            "response_state": f"eq.{PENDING_UPLOAD}",
        }, order="id.asc")

    def mark_uploaded(self, response_hashes: Iterable[str]) -> int:
        """Flip `response_pending_upload` → `uploaded` for these blobs. Returns the count.

        The reconciliation half of §4's ordering. Blobs are committed to Hugging Face
        in batches AFTER the verdict rows naming them exist (one commit per blob is
        what put the repo into HTTP 429), so the row is inserted pending and told the
        truth here once a commit has actually accepted the bytes. Only rows still
        pending are touched, and the filter is the hash, so re-running is a no-op.
        """
        hashes = [h for h in dict.fromkeys(response_hashes) if h]
        for start in range(0, len(hashes), _MARK_CHUNK):
            chunk = hashes[start:start + _MARK_CHUNK]
            self._patch("engine_verdicts",
                        {"response_hash": f"in.({','.join(chunk)})",
                         "response_state": f"eq.{PENDING_UPLOAD}"},
                        {"response_state": UPLOADED})
        return len(hashes)

    def supersede_verdict(self, old_id: str, new_id: str) -> None:
        """Point a superseded verdict at the one that replaced it.

        The only mutation the permanence trigger allows besides response-upload
        reconciliation. The old row stays: it is what was believed when it was
        spent on.
        """
        self._patch("engine_verdicts", {"id": f"eq.{old_id}"},
                    {"superseded_by": new_id})


    # ── supersessions ────────────────────────────────────────────────────────

    def record_supersession(self, *, work_id: int, kind: str,
                            affected_record_ids: Iterable[str],
                            old_release_id: Optional[str] = None,
                            new_release_id: Optional[str] = None,
                            reason: str = "", actor: str = "") -> str:
        """Insert one insert-only lineage row (#146 §5). Returns its id.

        This is the ONLY thing an upstream change does to already-sent decisions:
        it names them. Nothing here writes to `unvalidated`, `validated` or
        `validation_queue` — those rows are immutable once sent, and the
        validation repo reads this table to decide what to do about them.
        """
        if kind not in SUPERSESSION_KINDS:
            raise ValueError(
                f"unknown supersession kind: {kind} (expected one of {SUPERSESSION_KINDS})")
        row = {
            "work_id": int(work_id),
            "old_release_id": old_release_id,
            "new_release_id": new_release_id,
            "kind": kind,
            "reason": reason,
            "affected_record_ids": [str(r) for r in affected_record_ids],
            "actor": actor,
        }
        result = self._post("engine_supersessions", row, prefer="return=representation")
        return _first(result)["id"]


def _now_iso() -> str:
    """Now, UTC, as PostgREST wants it in a filter value."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _lease_end(ttl_seconds: Optional[int] = None) -> str:
    """When a claim taken now stops blocking its works."""
    lease = (datetime.timedelta(seconds=ttl_seconds) if ttl_seconds
             else datetime.timedelta(hours=CLAIM_TTL_HOURS))
    return (datetime.datetime.now(datetime.timezone.utc) + lease).isoformat()


def _missing_expiry(body: str) -> bool:
    """Does this error body say the database predates migration 0004?

    Two shapes: PostgREST cannot resolve `engine_claim_batch` with the lease
    argument (`PGRST202`), or a read filtered on a column that is not there (the
    message names `expires_at`).
    """
    return any(marker in body for marker in _MISSING_EXPIRY_MARKERS)


def is_expired(claim: dict, now: Optional[datetime.datetime] = None) -> bool:
    """Has this claim's lease run out? A claim with no lease never expires.

    A missing `expires_at` means a row written before migration 0004 back-filled
    the column, which the migration does — so this is a display-time courtesy, not
    a path the engine claims under.
    """
    raw = claim.get("expires_at")
    if not raw:
        return False
    stamp = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp <= (now or datetime.datetime.now(datetime.timezone.utc))


def _scalar(result: Any) -> str:
    """A PostgREST RPC returning a scalar gives the bare value or a 1-element list."""
    if isinstance(result, list):
        if not result:
            raise ClaimsError("RPC returned no value")
        result = result[0]
    if isinstance(result, dict):
        raise ClaimsError(f"RPC returned an object, expected a scalar: {result}")
    return str(result)


def _first(result: Any) -> dict:
    if isinstance(result, list):
        if not result:
            raise ClaimsError("insert returned no row")
        return result[0]
    if isinstance(result, dict):
        return result
    raise ClaimsError(f"insert returned {type(result).__name__}, expected a row")
