"""
routes/multi_originals.py — Multi-original study identification pipeline.

Routes:
  GET  /multi-originals                     → pipeline page
  GET  /api/multi-originals/candidates      → list of doi_r's + status
  GET  /api/multi-originals/result/<doi>    → full result dict for one DOI
  POST /api/multi-originals/run             → start pipeline (SSE stream)
  POST /api/multi-originals/stop            → stop running pipeline
  POST /api/multi-originals/export          → write resolved CSV
"""
import json
import queue
import threading
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
from flask import Blueprint, Response, jsonify, render_template, request, send_file, stream_with_context

from validate import state
from shared.config import MULTI_ORIG_CANDS_PATH, MULTI_ORIG_RESOLVED_PATH, log
from extract.multi_original import run_multi_original_for_doi
from shared.utils import clean_doi, pdf_serve_url

multi_orig_bp = Blueprint("multi_originals", __name__)

# ── Pipeline state ────────────────────────────────────────────────────────────
_lock       = threading.Lock()
_stop_event = threading.Event()
_run_state  = {"running": False, "current_doi": None, "total": 0, "done": 0}

# ── Review columns for export ─────────────────────────────────────────────────
_EXPORT_COLS = [
    "doi_r", "base_study_r", "base_year_r",
    "is_false_positive", "n_originals",
    "llm_source", "llm_reasoning",
    "pdf_source", "pdf_ok", "pdf_url",
    "n_candidates", "grobid_status", "n_grobid_refs",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_status(doi_r: str) -> str:
    if doi_r == _run_state.get("current_doi"):
        return "running"
    r = state.multi_orig_resolved.get(doi_r)
    if r is None:
        return "pending"
    if r.get("is_false_positive"):
        return "false_positive"
    return "multi_found" if r.get("n_originals", 0) > 0 else "unresolved"


def _build_candidate_row(row: pd.Series) -> dict:
    doi_r  = clean_doi(str(row.get("doi_r", "")))
    result = state.multi_orig_resolved.get(doi_r) or {}
    return {
        "doi_r"           : doi_r,
        "study_r"         : str(row.get("study_r", "")),
        "year_r"          : str(row.get("year_r",  "")),
        "status"          : _row_status(doi_r),
        "is_false_positive": bool(result.get("is_false_positive", False)),
        "n_originals"     : int(result.get("n_originals", 0)),
        "llm_source"      : result.get("llm_source",  ""),
        "pdf_source"      : result.get("pdf_source",  ""),
        "pdf_serve_url"   : pdf_serve_url(doi_r, result) if result else "",
        "pdf_url"         : result.get("pdf_url",     "") if result else "",
        "pdf_ok"          : bool(result.get("pdf_ok", False)) if result else False,
        "has_result"      : bool(result),
    }


def _upsert_csv(doi_r: str, result: dict) -> None:
    """Append/update one row in multi_original_resolved.csv (caller holds lock)."""
    originals = json.loads(result.get("originals_json", "[]"))

    if not originals:
        rows = [{**{k: result.get(k, "") for k in _EXPORT_COLS},
                 "doi_r": doi_r,
                 "original_rank": 0,
                 "resolved_title_o": "", "resolved_doi_o": "",
                 "resolved_year_o": "", "resolved_author_o": "",
                 "evidence": "", "confidence": ""}]
    else:
        rows = []
        for o in originals:
            rows.append({
                **{k: result.get(k, "") for k in _EXPORT_COLS},
                "doi_r"           : doi_r,
                "original_rank"   : o.get("rank", ""),
                "resolved_title_o": o.get("title",        ""),
                "resolved_doi_o"  : o.get("doi",          ""),
                "resolved_year_o" : o.get("year",         ""),
                "resolved_author_o": o.get("first_author", ""),
                "evidence"        : o.get("evidence",     ""),
                "confidence"      : o.get("confidence",   ""),
            })

    new_df = pd.DataFrame(rows)
    if MULTI_ORIG_RESOLVED_PATH.exists():
        existing = pd.read_csv(MULTI_ORIG_RESOLVED_PATH, dtype=str,
                               encoding="utf-8-sig").fillna("")
        existing = existing[existing["doi_r"] != clean_doi(doi_r)]
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df
    combined.to_csv(MULTI_ORIG_RESOLVED_PATH, index=False, encoding="utf-8-sig")


# ── Page route ────────────────────────────────────────────────────────────────

@multi_orig_bp.route("/multi-originals")
def multi_originals_page():
    return render_template("multi_originals.html", active_page="multi_originals")


# ── API: candidates ───────────────────────────────────────────────────────────

@multi_orig_bp.route("/api/multi-originals/candidates")
def api_candidates():
    if state.multi_orig_df.empty:
        return jsonify([])
    rows = [_build_candidate_row(row) for _, row in state.multi_orig_df.iterrows()]
    return jsonify(rows)


# ── API: single result ────────────────────────────────────────────────────────

@multi_orig_bp.route("/api/multi-originals/result/<path:doi>")
def api_result(doi: str):
    doi_r  = clean_doi(unquote(doi))
    result = state.multi_orig_resolved.get(doi_r)
    if result is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(result)


# ── API: export ───────────────────────────────────────────────────────────────

@multi_orig_bp.route("/api/multi-originals/export", methods=["POST"])
def api_export():
    with state.multi_orig_lock:
        if not state.multi_orig_resolved:
            return jsonify({"error": "no results yet"})
        for doi_r, result in state.multi_orig_resolved.items():
            _upsert_csv(doi_r, result)
    return jsonify({"path": str(MULTI_ORIG_RESOLVED_PATH),
                    "rows": len(state.multi_orig_resolved)})


@multi_orig_bp.route("/api/multi-originals/download")
def api_download():
    if not MULTI_ORIG_RESOLVED_PATH.exists():
        return jsonify({"error": "no export yet"}), 404
    return send_file(str(MULTI_ORIG_RESOLVED_PATH), as_attachment=True,
                     download_name="multi_original_resolved.csv",
                     mimetype="text/csv")


# ── API: stop ─────────────────────────────────────────────────────────────────

@multi_orig_bp.route("/api/multi-originals/stop", methods=["POST"])
def api_stop():
    with _lock:
        if not _run_state["running"]:
            return jsonify({"status": "not_running"})
        _stop_event.set()
    return jsonify({"status": "stopping"})


# ── API: run (SSE) ────────────────────────────────────────────────────────────

@multi_orig_bp.route("/api/multi-originals/run", methods=["POST"])
def api_run():
    """
    Start the multi-original pipeline. Streams SSE events.

    Request body: {"mode": "all" | "unresolved" | "selected", "dois": [...]}

    SSE event types:
      {"type": "progress", "doi": "...", "index": N, "total": N, "result": {...}}
      {"type": "error",    "doi": "...", "index": N, "total": N, "error": "..."}
      {"type": "done",     "total": N, "resolved": N}
      {"type": "stopped"}
    """
    body          = request.get_json(force=True) or {}
    mode          = body.get("mode", "all")
    selected_dois = [clean_doi(d) for d in body.get("dois", []) if d]

    with _lock:
        if _run_state["running"]:
            return jsonify({"error": "pipeline already running"}), 409
        if state.multi_orig_df.empty:
            return jsonify({"error": "no candidates loaded — generate originals input first"}), 400

        all_dois = [clean_doi(str(r["doi_r"])) for _, r in state.multi_orig_df.iterrows()]

        if mode == "selected":
            queue_list = [d for d in selected_dois if d in set(all_dois)]
        elif mode == "unresolved":
            queue_list = [d for d in all_dois
                          if not state.multi_orig_resolved.get(d)]
        else:
            queue_list = all_dois

        if not queue_list:
            return jsonify({"error": "no DOIs to process"}), 400

        _stop_event.clear()
        _run_state["running"]     = True
        _run_state["current_doi"] = None
        _run_state["total"]       = len(queue_list)
        _run_state["done"]        = 0

    result_queue: queue.Queue = queue.Queue()

    def worker():
        for i, doi_r in enumerate(queue_list):
            if _stop_event.is_set():
                result_queue.put({"type": "stopped"})
                break

            _run_state["current_doi"] = doi_r
            _run_state["done"]        = i + 1

            try:
                result = run_multi_original_for_doi(
                    doi_r, state.all_rep_df, state.cands_df
                )
                result["pdf_serve_url"] = pdf_serve_url(doi_r, result)

                with state.multi_orig_lock:
                    state.multi_orig_resolved[doi_r] = result
                    _upsert_csv(doi_r, result)

                result_queue.put({
                    "type"  : "progress",
                    "doi"   : doi_r,
                    "index" : i + 1,
                    "total" : len(queue_list),
                    "result": result,
                })
            except Exception as e:
                log.exception("Multi-original pipeline error for %s", doi_r)
                result_queue.put({
                    "type" : "error",
                    "doi"  : doi_r,
                    "index": i + 1,
                    "total": len(queue_list),
                    "error": str(e),
                })

        if not _stop_event.is_set():
            resolved_count = sum(
                1 for r in state.multi_orig_resolved.values()
                if r.get("n_originals", 0) > 0
            )
            result_queue.put({
                "type"    : "done",
                "total"   : len(queue_list),
                "resolved": resolved_count,
            })

        with _lock:
            _run_state["running"]     = False
            _run_state["current_doi"] = None

    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            try:
                event = result_queue.get(timeout=120)
            except queue.Empty:
                yield 'data: {"type":"heartbeat"}\n\n'
                continue
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event["type"] in ("done", "stopped"):
                break

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )
