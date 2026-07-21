"""
supabase_client.py — Thin wrapper around Supabase REST API for validation stats.

All functions return {"error": "supabase_not_configured"} when SUPABASE_URL is unset.
Results are cached in-process for CACHE_TTL seconds to avoid hammering the API.

Supabase tables used:
  unvalidated       — all records sent for validation (validation_status column)
  validation_queue  — individual validator judgements (type_check / original_check /
                      outcome_check per validator_slot); is_validated bool
  validated         — final admin-approved records (outcome, doi_r, doi_o, type)
"""
import os
import time
from typing import Any

import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

CACHE_TTL = 300  # 5 minutes

_CACHE: dict[str, dict] = {}

_NOT_CONFIGURED: dict = {"error": "supabase_not_configured"}

# validator_slot values in validation_queue → flat prefix used in dashboard rows
_SLOT_PREFIX: dict[str, str] = {
    "validator_1": "val1",
    "validator_2": "val2",
    "llm_validator": "llm_val",
}


def _headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }


def _get(table: str, params: dict | None = None) -> list[dict]:
    """Fetch rows from a Supabase table (up to 10 000 rows via Range header)."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**_headers(), "Range-Unit": "items", "Range": "0-9999", "Prefer": "count=none"}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _cached(key: str, fn) -> Any:
    entry = _CACHE.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    result = fn()
    _CACHE[key] = {"ts": time.time(), "data": result}
    return result


def get_validation_stats() -> dict:
    """Return high-level KPIs from unvalidated + validation_queue tables."""
    if not SUPABASE_URL:
        return _NOT_CONFIGURED

    def _fetch():
        rows = _get("unvalidated", {"select": "validation_status"})
        total = len(rows)
        status_counts: dict[str, int] = {}
        for r in rows:
            s = r.get("validation_status", "")
            status_counts[s] = status_counts.get(s, 0) + 1

        queue = _get("validation_queue", {"select": "validator_id,is_validated"})
        total_judgements = sum(1 for r in queue if r.get("is_validated"))
        # validator_id is populated by the external validation app, not by this repo's
        # writer (csv_to_db.py leaves it unset), so this may read 0 until that app runs.
        active_validators = len({r["validator_id"] for r in queue if r.get("validator_id")})
        # completion_rate = filled validator slots / total slots. This is a progress
        # metric, NOT inter-rater agreement (which needs per-record vote comparison the
        # external validation repo owns). Do not relabel this as "agreement".
        completion_rate = (total_judgements / len(queue)) if queue else 0.0

        return {
            "total": total,
            "unvalidated": status_counts.get("unvalidated", 0),
            "validated": status_counts.get("validated", 0),
            "need_review": status_counts.get("need_review", 0),
            "in_progress": status_counts.get("validation_inprogress", 0),
            "total_judgements": total_judgements,
            "active_validators": active_validators,
            "completion_rate": round(completion_rate, 4),
        }

    try:
        return _cached("validation_stats", _fetch)
    except Exception as e:
        return {"error": str(e)}


def get_correction_frequency() -> dict:
    """
    Count how often each field (type / original / outcome) was marked incorrect,
    counting once per record even if multiple validators flagged it.
    """
    if not SUPABASE_URL:
        return _NOT_CONFIGURED

    def _fetch():
        rows = _get("validation_queue", {
            "select": "record_id,type_check,original_check,outcome_check",
            "is_validated": "eq.true",
        })

        fields = ["type", "original", "outcome"]
        per_record: dict[str, dict] = {}
        for row in rows:
            rid = row.get("record_id", "")
            if rid not in per_record:
                per_record[rid] = {f: False for f in fields}
            for f in fields:
                if row.get(f"{f}_check") == "incorrect":
                    per_record[rid][f] = True

        result: dict[str, int] = {f"{f}_incorrect": 0 for f in fields}
        for flags in per_record.values():
            for f in fields:
                if flags[f]:
                    result[f"{f}_incorrect"] += 1
        return result

    try:
        return _cached("correction_frequency", _fetch)
    except Exception as e:
        return {"error": str(e)}


def get_validated_outcomes() -> dict:
    """Return outcome distribution from the validated (admin-approved) table."""
    if not SUPABASE_URL:
        return _NOT_CONFIGURED

    def _fetch():
        rows = _get("validated", {"select": "outcome"})
        counts: dict[str, int] = {}
        for r in rows:
            o = r.get("outcome") or "unknown"
            counts[o] = counts.get(o, 0) + 1
        return {"outcomes": counts, "total": len(rows)}

    try:
        return _cached("validated_outcomes", _fetch)
    except Exception as e:
        return {"error": str(e)}


_DRILLDOWN_PAGE_SIZE = 25


def get_drilldown_page(page: int, outcome_filter: str, check_filter: str) -> dict:
    """
    Return paginated records where ≥ 1 validator marked a field incorrect.

    Data comes from validation_queue (checks) joined to unvalidated (doi/outcome/type)
    in Python. Rows are flattened to val1_*/val2_*/llm_val_* keys so the dashboard
    HTML needs no changes.
    """
    if not SUPABASE_URL:
        return _NOT_CONFIGURED

    cache_key_str = f"drilldown_{page}_{outcome_filter}_{check_filter}"
    fields = ["type", "original", "outcome"]

    def _fetch():
        # 1. All completed validator judgements with check results
        queue_rows = _get("validation_queue", {
            "select": (
                "record_id,validator_slot,validator_name,"
                "type_check,original_check,outcome_check,"
                "corrected_doi_o,corrected_outcome,corrected_type,validator_notes"
            ),
            "is_validated": "eq.true",
        })

        # Group by record_id
        by_record: dict[str, list] = {}
        for row in queue_rows:
            rid = row.get("record_id", "")
            by_record.setdefault(rid, []).append(row)

        def _has_incorrect(vlist: list) -> bool:
            for v in vlist:
                for f in fields:
                    if check_filter not in ("all", f):
                        continue
                    if v.get(f"{f}_check") == "incorrect":
                        return True
            return False

        incorrect_ids = {rid for rid, vlist in by_record.items() if _has_incorrect(vlist)}
        if not incorrect_ids:
            return {"rows": [], "total": 0, "page": page,
                    "page_size": _DRILLDOWN_PAGE_SIZE, "pages": 1}

        # 2. Fetch the source records for those IDs
        unval_rows = _get("unvalidated", {
            "select": "record_id,doi_r,doi_o,outcome,type",
            "record_id": f"in.({','.join(incorrect_ids)})",
        })

        if outcome_filter != "all":
            unval_rows = [r for r in unval_rows if r.get("outcome") == outcome_filter]

        # 3. Flatten validator checks into the row using prefix mapping
        result_rows = []
        for rec in unval_rows:
            rid = rec.get("record_id")
            row_data: dict[str, Any] = {
                "doi_r": rec.get("doi_r"),
                "doi_o": rec.get("doi_o"),
                "outcome": rec.get("outcome"),
                "type": rec.get("type"),
            }
            for v in by_record.get(rid, []):
                slot = v.get("validator_slot", "")
                prefix = _SLOT_PREFIX.get(slot, slot)
                row_data[f"{prefix}_type_check"] = v.get("type_check")
                row_data[f"{prefix}_original_check"] = v.get("original_check")
                row_data[f"{prefix}_outcome_check"] = v.get("outcome_check")
                row_data[f"{prefix}_notes"] = v.get("validator_notes")
            result_rows.append(row_data)

        total = len(result_rows)
        offset = _DRILLDOWN_PAGE_SIZE * (page - 1)
        return {
            "rows": result_rows[offset: offset + _DRILLDOWN_PAGE_SIZE],
            "total": total,
            "page": page,
            "page_size": _DRILLDOWN_PAGE_SIZE,
            "pages": max(1, (total + _DRILLDOWN_PAGE_SIZE - 1) // _DRILLDOWN_PAGE_SIZE),
        }

    try:
        return _cached(cache_key_str, _fetch)
    except Exception as e:
        return {"error": str(e)}
