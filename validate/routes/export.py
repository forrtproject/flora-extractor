"""
routes/export.py — CSV/XLSX/PDF export of validated records.

Routes:
  GET  /export              → export page
  POST /api/export/download → build and send validated.csv / .xlsx / .pdf
"""
import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import DATA_DIR, log
from shared.schema import VALIDATED_COLS
from validate.models import db, Replication, Vote

export_bp = Blueprint("export", __name__)


@export_bp.route("/export")
def export_page():
    return render_template("export.html", active_page="export")


@export_bp.route("/api/export/download", methods=["POST"])
def api_export_download():
    """
    Build validated.csv from the database and send it to the browser.

    Body: {"format": "csv"|"xlsx"|"pdf"}
    """
    body = request.get_json(force=True) or {}
    fmt  = body.get("format", "csv")

    if fmt not in ("csv", "xlsx", "pdf"):
        return jsonify({"error": f"unknown format: {fmt}"}), 400

    # TODO: implement export — query all Replication rows + vote aggregation,
    # build VALIDATED_COLS DataFrame, write to file, return send_file()
    raise NotImplementedError("api_export_download is not yet implemented")
