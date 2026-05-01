"""
routes/disambiguation.py — Single-DOI disambiguation page and API.

Blueprint prefix: none (mounted at /)
Routes:
  GET  /             → disambiguation page
  POST /api/lookup   → run pipeline for one DOI, return JSON
"""
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from validate import state
from shared.config import PDF_CACHE_DIR, log
from extract.link_original import run_for_doi
from shared.utils import cache_key, clean_doi

disambiguation_bp = Blueprint("disambiguation", __name__)


@disambiguation_bp.route("/")
def index():
    return render_template("disambiguation.html", active_page="disambiguation")


@disambiguation_bp.route("/api/lookup", methods=["POST"])
def api_lookup():
    body  = request.get_json(force=True) or {}
    doi_r = clean_doi(body.get("doi_r", "").strip())
    force = bool(body.get("force", False))

    if not doi_r:
        return jsonify({"error": "doi_r is required"}), 400

    log.info("=== API lookup: %s (force=%s) ===", doi_r, force)
    try:
        result = run_for_doi(doi_r, state.flora_df, state.cands_df, force=force)
        result["pdf_serve_url"] = _pdf_serve_url(doi_r, result)
        return jsonify(result)
    except Exception as e:
        log.exception("Pipeline error for %s", doi_r)
        return jsonify({"error": str(e)}), 500


def _pdf_serve_url(doi_r: str, result: dict) -> str:
    if result.get("pdf_path"):
        return f"/pdf/{Path(result['pdf_path']).name}"
    expected = PDF_CACHE_DIR / f"{cache_key(doi_r)}.pdf"
    return f"/pdf/{expected.name}" if expected.exists() else ""
