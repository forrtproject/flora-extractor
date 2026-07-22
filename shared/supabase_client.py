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

# validator_slot values written by extract/csv_to_db.py → flat prefix used in
# dashboard rows. These MUST match _VALIDATOR_SLOTS in csv_to_db.py.
_SLOT_PREFIX: dict[str, str] = {
    "human_1": "val1",
    "human_2": "val2",
    "llm": "llm_val",
}

# PostgREST caps a single response at db-max-rows (1000 on this project) regardless
# of the Range header, so every table read must page until a short page comes back.
_PAGE_SIZE = 1000

# Paging across an unordered result set is not stable — PostgREST may repeat or skip
# rows between pages. Every paged table needs a deterministic sort key.
_PAGE_ORDER: dict[str, str] = {
    "unvalidated":      "record_id",
    "validation_queue": "queue_id",
    "validated":        "record_id",
    "record_metadata":  "record_id",
}


def _headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }


def _get(table: str, params: dict | None = None) -> list[dict]:
    """Fetch all rows from a Supabase table, paging past the server row cap."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = dict(params or {})
    params.setdefault("order", _PAGE_ORDER.get(table, "record_id"))
    rows: list[dict] = []
    offset = 0
    while True:
        headers = {
            **_headers(), "Range-Unit": "items",
            "Range": f"{offset}-{offset + _PAGE_SIZE - 1}",
            "Prefer": "count=none",
        }
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


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


# Per-field mapping: dashboard field name → (check key inside a validator blob,
# correction key inside that blob, pipeline column, admin-final column).
_FIELD_MAP: dict[str, tuple[str, str, str, str]] = {
    "type":     ("type_check",     "corrected_type",    "type",    "final_type"),
    "original": ("original_check", "corrected_doi_o",   "doi_o",   "final_doi_o"),
    "outcome":  ("outcome_check",  "corrected_outcome", "outcome", "final_outcome"),
}

_ANALYTICS_COLS = (
    "record_id,validation_status,is_tiebreaker,admin_checked,admin_override,"
    "type,outcome,doi_o,final_type,final_outcome,final_doi_o,"
    "validator_1,validator_2,llm_validator"
)


_BUCKETS = ("both_kept", "both_changed", "one_changed")

# Terminal states for a record, per field. "reviewer_took_correction" means the final
# value matches what a validator proposed; for a one_changed bucket that is exactly
# "the reviewer sided with the validator who changed it", and "reviewer_kept_pipeline"
# is "sided with the one who kept it".
_EMPTY_RESOLUTION = {
    "reviewer_kept_pipeline": 0,
    "reviewer_took_correction": 0,
    "reviewer_took_other": 0,
    "excluded": 0,
    "pending": 0,
}


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


def _resolve_state(record: dict, final_col: str, pipe_col: str,
                   proposed: set[str]) -> str:
    """How this record was finally settled for one field.

    `proposed` is the set of replacement values the human validators supplied.
    """
    if (_norm(record.get("validation_status")) == "rejected"
            or _norm(record.get("final_type")) == "not_validation"):
        return "excluded"
    final_val = record.get(final_col)
    if final_val in (None, ""):
        return "pending"
    fv = _norm(final_val)
    if fv == _norm(record.get(pipe_col)):
        return "reviewer_kept_pipeline"
    if fv in proposed:
        return "reviewer_took_correction"
    return "reviewer_took_other"


def get_validation_analytics() -> dict:
    """Interpretable validation results, all derived from the `unvalidated` table.

    validator_1 / validator_2 / llm_validator are JSONB blobs holding each slot's
    full judgement, so one read answers coverage, per-field agreement, and how the
    admin's final value compares to what the pipeline proposed.
    """
    if not SUPABASE_URL:
        return _NOT_CONFIGURED

    def _fetch():
        rows = _get("unvalidated", {"select": _ANALYTICS_COLS})

        status_counts: dict[str, int] = {}
        slots_filled = {0: 0, 1: 0, 2: 0, 3: 0}
        both_humans = llm_present = 0
        tiebreakers = admin_checked = admin_override = 0

        agreement = {
            f: {"n": 0, "both_kept": 0, "both_changed": 0,
                "both_changed_same": 0, "both_changed_differ": 0,
                "one_changed": 0, "llm_with_changer": 0,
                "llm_with_keeper": 0, "llm_absent": 0,
                # How each bucket was finally settled. Only records that have left
                # the queue (finalised or rejected) can be resolved; the rest are pending.
                "resolution": {b: dict(_EMPTY_RESOLUTION) for b in _BUCKETS}}
            for f in _FIELD_MAP
        }
        final_vs_pipeline = {f: {"n": 0, "kept": 0, "changed": 0} for f in _FIELD_MAP}

        for r in rows:
            status_counts[r.get("validation_status") or ""] = \
                status_counts.get(r.get("validation_status") or "", 0) + 1
            if r.get("is_tiebreaker"):  tiebreakers += 1
            if r.get("admin_checked"):  admin_checked += 1
            if r.get("admin_override"): admin_override += 1

            v1  = r.get("validator_1")   if isinstance(r.get("validator_1"), dict)   else None
            v2  = r.get("validator_2")   if isinstance(r.get("validator_2"), dict)   else None
            llm = r.get("llm_validator") if isinstance(r.get("llm_validator"), dict) else None
            slots_filled[sum(x is not None for x in (v1, v2, llm))] += 1
            if v1 and v2: both_humans += 1
            if llm:       llm_present += 1

            for field, (check_key, corr_key, pipe_col, final_col) in _FIELD_MAP.items():
                # ── Human-vs-human agreement (needs both slots on this field) ──
                if v1 and v2:
                    c1, c2 = _norm(v1.get(check_key)), _norm(v2.get(check_key))
                    if c1 in ("correct", "incorrect") and c2 in ("correct", "incorrect"):
                        a = agreement[field]
                        a["n"] += 1
                        if c1 == "correct" and c2 == "correct":
                            bucket = "both_kept"
                            a["both_kept"] += 1
                        elif c1 == "incorrect" and c2 == "incorrect":
                            bucket = "both_changed"
                            a["both_changed"] += 1
                            x, y = _norm(v1.get(corr_key)), _norm(v2.get(corr_key))
                            # Both rejected the pipeline value, but did they land on
                            # the same replacement? Blank corrections count as differing.
                            if x and y and x == y:
                                a["both_changed_same"] += 1
                            else:
                                a["both_changed_differ"] += 1
                        else:
                            bucket = "one_changed"
                            a["one_changed"] += 1
                            lc = _norm(llm.get(check_key)) if llm else ""
                            if lc == "incorrect":
                                a["llm_with_changer"] += 1
                            elif lc == "correct":
                                a["llm_with_keeper"] += 1
                            else:
                                a["llm_absent"] += 1

                        proposed = {_norm(v.get(corr_key))
                                    for v in (v1, v2) if _norm(v.get(corr_key))}
                        a["resolution"][bucket][
                            _resolve_state(r, final_col, pipe_col, proposed)] += 1

                # ── Admin's final value vs what the pipeline produced ──
                final_val = r.get(final_col)
                if final_val not in (None, ""):
                    fvp = final_vs_pipeline[field]
                    fvp["n"] += 1
                    if _norm(final_val) == _norm(r.get(pipe_col)):
                        fvp["kept"] += 1
                    else:
                        fvp["changed"] += 1

        return {
            "total": len(rows),
            "by_status": status_counts,
            "slots_filled": {str(k): v for k, v in slots_filled.items()},
            "at_least_one_vote": len(rows) - slots_filled[0],
            "both_humans": both_humans,
            "llm_present": llm_present,
            "tiebreakers": tiebreakers,
            "admin_checked": admin_checked,
            "admin_override": admin_override,
            "agreement": agreement,
            "final_vs_pipeline": final_vs_pipeline,
        }

    try:
        return _cached("validation_analytics", _fetch)
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
