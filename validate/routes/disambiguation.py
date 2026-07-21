"""
routes/disambiguation.py — Single-DOI disambiguation page and API.

Blueprint prefix: none (mounted at /)
Routes:
  GET  /             → disambiguation page
  POST /api/lookup   → run pipeline for one DOI, return JSON
"""
from flask import Blueprint, jsonify, render_template, request

from validate import state
from shared.config import log
from extract.link_original import run_for_doi
from shared.utils import clean_doi, pdf_serve_url

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
        result["pdf_serve_url"] = pdf_serve_url(doi_r, result)
        return jsonify(result)
    except Exception as e:
        log.exception("Pipeline error for %s", doi_r)
        return jsonify({"error": str(e)}), 500
