"""Reconciliation between engine routing and the validation tables (#146 M5).

Issue #146 §5: *decisions become immutable once sent for human validation; upstream
changes create superseding records with lineage.* This module is that sentence in
code, and it is deliberately one-directional:

- it READS the routing store to find works whose pile changed between two releases;
- it READS `record_metadata` to find which of those works have already been sent
  for validation, and whether their records are still `unvalidated` or already
  `validated`;
- it WRITES one `engine_supersessions` row per affected work, naming those records.

It never updates or deletes a row in `unvalidated`, `validated` or
`validation_queue`. A sent decision is evidence of what was believed when a human
was asked; the supersession record is the lineage on top of it, and the validation
repo decides downstream what to show. Deleting or rewriting the sent row would
destroy the only account of what the validator actually judged.

Reconciliation keys on **work_id**, not DOI — the validation repo's import writes
`work_id` (and `release_id`) into `record_metadata` at push time. Rows imported
before the engine carry a null work_id and are therefore invisible here: they have
no engine lineage to supersede, which is the truth about them.

    python -m filter.engine.supersede --old <release> --new <release> [--run]
"""

import argparse
import os
from typing import Iterable, Optional, Protocol

import duckdb
import requests

from filter.engine.claims import ClaimsClient
from filter.engine.store import DEFAULT_STORE_PATH, open_store

# Same page cap and paging rule as shared/supabase_client.py: PostgREST truncates
# at db-max-rows regardless of the Range header, and an unordered result set may
# repeat or skip rows between pages.
_PAGE_SIZE = 1000

# work_ids per `in.(...)` filter. A URL carrying every id of a large diff exceeds
# what PostgREST (and proxies in front of it) will accept, so reads are chunked.
_ID_CHUNK = 200


class SupersedeError(RuntimeError):
    """The validation tables could not be read."""


class _Recorder(Protocol):
    """What `supersede()` needs of a client — ClaimsClient satisfies it."""

    def record_supersession(self, *, work_id: int, kind: str,
                            affected_record_ids: Iterable[str],
                            old_release_id: Optional[str],
                            new_release_id: Optional[str],
                            reason: str, actor: str) -> str: ...


# ── routing diff ─────────────────────────────────────────────────────────────

def diff_releases(con: duckdb.DuckDBPyConnection, old_release_id: str,
                  new_release_id: str) -> list[dict]:
    """Works whose pile differs between two releases in the routing store.

    A self-join, not a set difference: a work must be present in BOTH releases for
    its pile to have *changed*. Works only one release routed (the pool grew, or a
    work was aliased away) are not supersessions — nothing that was sent for
    validation was decided differently about them.
    """
    rows = con.execute(
        """
        SELECT o.work_id, o.pile, n.pile, o.rule_id, n.rule_id
        FROM routing o
        JOIN routing n USING (work_id)
        WHERE o.release_id = ? AND n.release_id = ?
          AND o.pile IS DISTINCT FROM n.pile
        ORDER BY o.work_id
        """, [old_release_id, new_release_id]).fetchall()
    columns = ("work_id", "old_pile", "new_pile", "old_rule", "new_rule")
    return [dict(zip(columns, row)) for row in rows]


def supersession_kind(old_pile: str, new_pile: str) -> str:
    """`withdrawal` when the work is now rule-discarded, else `reroute`.

    A discard is the only rule-terminal pile (#146 §2): a work that lands there
    should not be in the corpus at all, which is a different message to a
    validator than "still a candidate, routed differently". `verdict` is not
    produced here — it belongs to `engine_verdicts.superseded_by`, which is a
    tier-level event with no release pair.
    """
    return "withdrawal" if new_pile == "discard" else "reroute"


# ── validation-table read ────────────────────────────────────────────────────

def _get(table: str, params: dict, order: str = "record_id") -> list[dict]:
    """Paged PostgREST read, house style (`shared/supabase_client._get`)."""
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url:
        raise SupersedeError("SUPABASE_URL is unset: cannot read the validation tables")
    headers_base = {"apikey": key, "Authorization": f"Bearer {key}"}
    params = dict(params)
    params.setdefault("order", order)
    rows: list[dict] = []
    offset = 0
    while True:
        headers = {**headers_base, "Range-Unit": "items",
                   "Range": f"{offset}-{offset + _PAGE_SIZE - 1}",
                   "Prefer": "count=none"}
        resp = requests.get(f"{url}/rest/v1/{table}", headers=headers,
                            params=params, timeout=30)
        if resp.status_code >= 400:
            raise SupersedeError(f"{table} → HTTP {resp.status_code}: {resp.text.strip()}")
        page = resp.json()
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


def _chunks(values: list[int], size: int = _ID_CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def affected_validation_records(work_ids: list[int]) -> dict[int, dict]:
    """Validation records already sent for each of *work_ids*.

    Returns `{work_id: {"record_ids": [...], "validated": [...],
    "unvalidated": [...]}}`, with work_ids that were never pushed simply absent.
    The validated/unvalidated split is not a filter but a fact the operator needs:
    a record already in `validated` is final evidence, so its supersession is
    lineage a human must reconcile, whereas an unvalidated one may still be
    withdrawn upstream before anyone spends judgment on it.
    """
    unique = sorted({int(w) for w in work_ids})
    if not unique:
        return {}

    by_work: dict[int, dict] = {}
    record_to_work: dict[str, int] = {}
    for chunk in _chunks(unique):
        ids = ",".join(str(w) for w in chunk)
        for row in _get("record_metadata",
                        {"select": "record_id,work_id,release_id",
                         "work_id": f"in.({ids})"}):
            work = row.get("work_id")
            record_id = str(row.get("record_id") or "")
            if work is None or not record_id:
                continue
            entry = by_work.setdefault(int(work), {"record_ids": [], "validated": [],
                                                   "unvalidated": []})
            entry["record_ids"].append(record_id)
            record_to_work[record_id] = int(work)

    if not record_to_work:
        return {}

    # Which of those records are FINAL. `validated` is the authority on that; a
    # record absent from it is still in flight, whatever `unvalidated` says about
    # its own status column.
    final: set[str] = set()
    all_records = sorted(record_to_work)
    for chunk_records in (all_records[i:i + _ID_CHUNK]
                          for i in range(0, len(all_records), _ID_CHUNK)):
        quoted = ",".join(f'"{r}"' for r in chunk_records)
        for row in _get("validated", {"select": "record_id",
                                      "record_id": f"in.({quoted})"}):
            final.add(str(row.get("record_id") or ""))

    for record_id, work in record_to_work.items():
        entry = by_work[work]
        (entry["validated"] if record_id in final else entry["unvalidated"]).append(record_id)
    for entry in by_work.values():
        for bucket in entry.values():
            bucket.sort()
    return by_work


# ── the supersession itself ──────────────────────────────────────────────────

def supersede(client: Optional[_Recorder], diffs: list[dict], affected: dict[int, dict],
              *, reason: str = "", actor: str = "", old_release_id: Optional[str] = None,
              new_release_id: Optional[str] = None, dry_run: bool = True) -> dict:
    """Write one lineage row per affected work. Nothing else is written, ever.

    Works in *diffs* that reached no validation record are skipped: there is no
    already-sent decision to supersede, and routing that only moved unclaimed rows
    is the normal, free case (#146 §1).

    With *dry_run* (the default) nothing is posted and the plan is printed —
    `--run` is the only way to write.
    """
    planned: list[dict] = []
    for diff in diffs:
        work = int(diff["work_id"])
        entry = affected.get(work)
        if not entry or not entry["record_ids"]:
            continue
        planned.append({
            "work_id": work,
            "kind": supersession_kind(diff.get("old_pile") or "", diff.get("new_pile") or ""),
            "old_pile": diff.get("old_pile"),
            "new_pile": diff.get("new_pile"),
            "affected_record_ids": list(entry["record_ids"]),
            "n_validated": len(entry["validated"]),
            "n_unvalidated": len(entry["unvalidated"]),
        })

    result = {
        "works_changed": len(diffs),
        "works_affected": len(planned),
        "records_named": sum(len(p["affected_record_ids"]) for p in planned),
        "validated_records": sum(p["n_validated"] for p in planned),
        "unvalidated_records": sum(p["n_unvalidated"] for p in planned),
        "kinds": {kind: sum(1 for p in planned if p["kind"] == kind)
                  for kind in ("reroute", "withdrawal")},
        "planned": planned,
        "written": [],
        "dry_run": dry_run,
    }

    if dry_run:
        print(f"[dry-run] {len(diffs)} works re-routed; "
              f"{len(planned)} touch already-sent validation records")
        for plan in planned:
            print(f"  work {plan['work_id']}: {plan['old_pile']} → {plan['new_pile']} "
                  f"[{plan['kind']}] records={len(plan['affected_record_ids'])} "
                  f"(validated={plan['n_validated']}, unvalidated={plan['n_unvalidated']})")
        print("[dry-run] nothing written; no validation row is ever modified")
        return result

    if client is None:
        raise ValueError("a live run needs a client to record supersessions")

    for plan in planned:
        supersession_id = client.record_supersession(
            work_id=plan["work_id"], kind=plan["kind"],
            affected_record_ids=plan["affected_record_ids"],
            old_release_id=old_release_id, new_release_id=new_release_id,
            reason=reason, actor=actor)
        result["written"].append(supersession_id)
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m filter.engine.supersede",
        description="Reconcile a routing change against the validation tables "
                    "(#146 M5). Writes lineage records only — never a validation row.")
    parser.add_argument("--old", required=True, help="release id routing came from")
    parser.add_argument("--new", required=True, help="release id routing moved to")
    parser.add_argument("--run", action="store_true",
                        help="actually write the supersession records (default: dry-run)")
    parser.add_argument("--reason", default="", help="why the routing changed")
    parser.add_argument("--actor", default=os.getenv("USER", ""), help="who ran this")
    parser.add_argument("--store", default=str(DEFAULT_STORE_PATH),
                        help="routing store path")
    args = parser.parse_args(argv)

    con = open_store(args.store)
    diffs = diff_releases(con, args.old, args.new)
    affected = affected_validation_records([d["work_id"] for d in diffs])
    client = ClaimsClient() if args.run else None
    result = supersede(client, diffs, affected, reason=args.reason, actor=args.actor,
                       old_release_id=args.old, new_release_id=args.new,
                       dry_run=not args.run)
    if args.run:
        print(f"Wrote {len(result['written'])} supersession records "
              f"naming {result['records_named']} validation records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
