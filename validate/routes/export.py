"""
routes/export.py — Export validated records as CSV / XLSX / minimal CSV.

Routes:
  GET  /export                   → export options page
  POST /api/export/download      → stream file to browser
    body: {"format": "csv"|"xlsx"|"minimal", "include_needs_review": bool}
"""
import io

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from validate.models import Replication

export_bp = Blueprint("export", __name__)

_MINIMAL_COLS = ["doi_r", "title_r", "doi_o", "title_o", "validation_status"]

_ALL_COLS = [
    "doi_r", "title_r", "doi_o", "title_o", "year_o", "authors_o",
    "outcome", "validation_status", "vote_count", "confirm_votes",
    "reject_votes", "validator_notes", "flora_status",
]


@export_bp.route("/export")
def export_page():
    return render_template("export.html", active_page="export")


@export_bp.route("/api/export/download", methods=["POST"])
def api_export_download():
    body                 = request.get_json(force=True) or {}
    fmt                  = body.get("format", "csv")
    include_needs_review = body.get("include_needs_review", False)

    if fmt not in ("csv", "xlsx", "minimal"):
        return jsonify({"error": f"unknown format: {fmt}"}), 400

    statuses = ["confirmed"]
    if include_needs_review:
        statuses.append("needs_review")

    reps = Replication.query.filter(
        Replication.validation_status.in_(statuses)
    ).all()

    rows = [
        {
            "doi_r":             r.doi_r,
            "title_r":           r.title_r or "",
            "doi_o":             r.doi_o or "",
            "title_o":           r.title_o or "",
            "year_o":            r.year_o or "",
            "authors_o":         r.authors_o or "",
            "outcome":           r.outcome or "",
            "validation_status": r.validation_status,
            "vote_count":        r.vote_count,
            "confirm_votes":     r.confirm_votes,
            "reject_votes":      r.reject_votes,
            "validator_notes":   r.validator_notes or "",
            "flora_status":      r.flora_status or "",
        }
        for r in reps
    ]
    df = pd.DataFrame(rows, columns=_ALL_COLS)

    if fmt == "minimal":
        df = df[_MINIMAL_COLS]
        buf = io.BytesIO()
        df.to_csv(buf, index=False, encoding="utf-8-sig")
        buf.seek(0)
        return send_file(buf, download_name="validated_minimal.csv",
                         as_attachment=True, mimetype="text/csv")

    if fmt == "xlsx":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Validated")
        buf.seek(0)
        return send_file(
            buf, download_name="validated.xlsx", as_attachment=True,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    buf = io.BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    buf.seek(0)
    return send_file(buf, download_name="validated.csv",
                     as_attachment=True, mimetype="text/csv")
