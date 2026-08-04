"""Claims client — the engine's handle on the Postgres state authority (#146 M2).

Postgres is the sole mutable authority: routing is recomputed from pool + specs,
but a CLAIM is what pins rows before money or judgment is spent, and a VERDICT is
permanent evidence. This module is the only way engine code touches either.

House style, matching `shared/supabase_client.py`: plain `requests` against
PostgREST, `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` from the environment (the same
two variables — they are not re-declared in `shared/config.py`), and paged reads
with a deterministic sort key.

Deployment: `db/migrations/0001_engine_baseline.sql` must have been run. Claiming
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
"""

import os
from typing import Any, Iterable, Optional

import requests

# PostgREST caps a page at db-max-rows regardless of the Range header; every read
# pages until a short page comes back (same rule as shared/supabase_client.py).
_PAGE_SIZE = 1000

TIERS = ("screen_cheap", "screen_expensive", "human", "measurement")
CLAIM_STATUSES = ("active", "complete", "cancelled", "failed")
END_STATUSES = ("complete", "cancelled", "failed")

# §4 ordering: the raw response blob uploads to HF BEFORE its verdict row exists.
# A row that could not wait is inserted PENDING_UPLOAD and reconciled later.
UPLOADED = "uploaded"
PENDING_UPLOAD = "response_pending_upload"
RESPONSE_STATES = (UPLOADED, PENDING_UPLOAD)


class ClaimsError(RuntimeError):
    """Any failure talking to the state authority."""


class ClaimsNotConfigured(ClaimsError):
    """SUPABASE_URL is unset — the engine must not run unclaimed."""


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

    def _post(self, path: str, payload: Any, prefer: Optional[str] = None) -> Any:
        extra = {"Prefer": prefer} if prefer else None
        resp = requests.post(f"{self.url}/rest/v1/{path}", headers=self._headers(extra),
                             json=payload, timeout=self.timeout)
        return self._parse(resp, path)

    def _patch(self, path: str, params: dict, payload: dict) -> Any:
        resp = requests.patch(f"{self.url}/rest/v1/{path}", headers=self._headers(),
                              params=params, json=payload, timeout=self.timeout)
        return self._parse(resp, path)

    def _parse(self, resp, path: str) -> Any:
        if resp.status_code >= 400:
            body = resp.text or ""
            # The RPC raises unique_violation for a conflict, which PostgREST
            # returns as 409; the message prefix keeps it distinguishable from a
            # plain duplicate-key error on some other table.
            if resp.status_code == 409 or "claim_conflict" in body:
                raise ClaimConflict(_tier_of(path, body), body.strip())
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
                raise ClaimsError(f"{table} → HTTP {resp.status_code}: {resp.text.strip()}")
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
              meta: Optional[dict] = None) -> str:
        """Claim *items* — `(work_id, pile)` pairs — for *tier*. Returns the claim id.

        One server-side transaction (`engine_claim_batch`). Rejection is
        all-or-nothing: if any work is already held by an active claim of the same
        tier, `ClaimConflict` is raised and NOTHING is claimed. Different tiers may
        hold the same work concurrently, and `measurement` claims conflict with
        nothing (issue #146 §8 decision 4, implementer default).
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
        })
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

    def active_claims(self, release_id: Optional[str] = None) -> list[dict]:
        """Every active claim, optionally restricted to one release."""
        params: dict = {"select": "id,release_id,tier,status,created_at,meta",
                        "status": "eq.active"}
        if release_id:
            params["release_id"] = f"eq.{release_id}"
        return self._get_paged("engine_claims", params, order="created_at.asc,id.asc")

    def claimed_work_ids(self, release_id: str, tier: str) -> set[int]:
        """Work ids currently held by an active claim of *tier* in *release_id*.

        What a runner asks before it builds a batch, so the common case is a clean
        claim rather than a rejected one. It is NOT the check that makes claiming
        safe — that check is inside the RPC, under a lock.
        """
        rows = self._get_paged("engine_claim_items", {
            "select": "work_id,claim_id,engine_claims!inner(release_id,tier,status)",
            "engine_claims.status": "eq.active",
            "engine_claims.tier": f"eq.{tier}",
            "engine_claims.release_id": f"eq.{release_id}",
        }, order="work_id.asc,claim_id.asc")
        return {int(r["work_id"]) for r in rows}

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

    def supersede_verdict(self, old_id: str, new_id: str) -> None:
        """Point a superseded verdict at the one that replaced it.

        The only mutation the permanence trigger allows besides response-upload
        reconciliation. The old row stays: it is what was believed when it was
        spent on.
        """
        self._patch("engine_verdicts", {"id": f"eq.{old_id}"},
                    {"superseded_by": new_id})


def _tier_of(path: str, body: str) -> str:
    """Best-effort tier name for a conflict message (the RPC names it in the error)."""
    for tier in TIERS:
        if tier in body:
            return tier
    return "unknown"


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
