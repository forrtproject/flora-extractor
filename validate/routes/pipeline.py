"""
routes/pipeline.py — Read-only pipeline inspection view of flora_all.csv.

Routes:
  GET  /pipeline                  → render pipeline.html
  GET  /api/pipeline/list         → filtered summary rows as JSON
  GET  /api/pipeline/detail       → full row for one doi_r as JSON
"""
import json

import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from shared.config import DATA_DIR

pipeline_bp = Blueprint("pipeline", __name__)

_CSV_PATH = DATA_DIR / "flora_all.csv"

# Columns parsed from JSON strings in the CSV
_JSON_COLS = ("all_candidates_json", "grobid_refs_json")


def _load_csv() -> pd.DataFrame | None:
    if not _CSV_PATH.exists():
        return None
    return pd.read_csv(_CSV_PATH, encoding="utf-8-sig", dtype=str).fillna("")


@pipeline_bp.route("/pipeline")
def pipeline_page():
    return render_template("pipeline.html", active_page="pipeline")


@pipeline_bp.route("/api/pipeline/list")
def api_list():
    df = _load_csv()
    if df is None:
        return jsonify({"error": "flora_all.csv not found in data/"}), 404

    q      = request.args.get("q",      "").strip().lower()
    outcome = request.args.get("outcome", "all")
    method  = request.args.get("method",  "all")
    status  = request.args.get("status",  "all")

    if q:
        mask = (
            df["doi_r"].str.lower().str.contains(q, na=False)
            | df["study_r"].str.lower().str.contains(q, na=False)
            | df["resolved_title_o"].str.lower().str.contains(q, na=False)
        )
        df = df[mask]

    if outcome != "all":
        df = df[df["outcome"] == outcome]
    if method != "all":
        df = df[df["resolution_method"] == method]
    if status != "all":
        df = df[df["match_status"] == status]

    rows = []
    for i, r in enumerate(df.to_dict("records"), start=1):
        rows.append({
            "idx":               i,
            "doi_r":             r.get("doi_r", ""),
            "study_r":           r.get("study_r", ""),
            "year_r":            r.get("year_r", ""),
            "match_status":      r.get("match_status", ""),
            "resolution_method": r.get("resolution_method", ""),
            "outcome":           r.get("outcome", ""),
            "resolved_title_o":  r.get("resolved_title_o", ""),
            "resolved":          bool(r.get("resolved_doi_o", "")),
        })

    return jsonify({"rows": rows, "total": len(rows)})


@pipeline_bp.route("/api/pipeline/detail")
def api_detail():
    doi = request.args.get("doi", "").strip()
    if not doi:
        return jsonify({"error": "missing doi parameter"}), 400

    df = _load_csv()
    if df is None:
        return jsonify({"error": "flora_all.csv not found in data/"}), 404

    matches = df[df["doi_r"] == doi]
    if matches.empty:
        return jsonify({"error": "doi not found"}), 404

    row = matches.iloc[0].to_dict()

    for col in _JSON_COLS:
        val = row.get(col, "")
        if val:
            try:
                row[col] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                row[col] = []
        else:
            row[col] = []

    return jsonify(row)
