"""
routes/filter_view.py — Read-only view of filtered.csv (Stage 2 output).

Loads data/filtered.csv; falls back to misc/sample_filtered.csv.

Routes:
  GET  /filter                  → render filter.html
  GET  /api/filter/list         → filtered summary rows as JSON
"""
import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from shared.config import BASE_DIR, DATA_DIR

filter_view_bp = Blueprint("filter_view", __name__)

_CSV_PATH    = DATA_DIR / "filtered.csv"
_SAMPLE_PATH = BASE_DIR / "misc" / "sample_filtered.csv"


def _load_csv() -> tuple[pd.DataFrame | None, str]:
    if _CSV_PATH.exists():
        return pd.read_csv(_CSV_PATH, encoding="utf-8-sig", dtype=str,
                           on_bad_lines="skip").fillna(""), "data/filtered.csv"
    if _SAMPLE_PATH.exists():
        return pd.read_csv(_SAMPLE_PATH, encoding="utf-8-sig", dtype=str,
                           on_bad_lines="skip").fillna(""), "misc/sample_filtered.csv (sample)"
    return None, ""


@filter_view_bp.route("/filter")
def filter_page():
    df, source = _load_csv()
    return render_template("filter.html", active_page="filter",
                           source=source, total=len(df) if df is not None else 0)


@filter_view_bp.route("/api/filter/list")
def api_filter_list():
    df, _ = _load_csv()
    if df is None:
        return jsonify({"error": "No filtered.csv found. Run Stage 2 first."}), 404

    q       = request.args.get("q",       "").strip().lower()
    fstatus = request.args.get("fstatus", "all")
    fmethod = request.args.get("fmethod", "all")
    fconf   = request.args.get("fconf",   "all")

    if q:
        mask = (
            df.get("doi_r",   pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("title_r", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
        )
        df = df[mask]

    if fstatus != "all" and "filter_status" in df.columns:
        df = df[df["filter_status"] == fstatus]
    if fmethod != "all" and "filter_method" in df.columns:
        df = df[df["filter_method"] == fmethod]
    if fconf != "all" and "filter_confidence" in df.columns:
        df = df[df["filter_confidence"] == fconf]

    rows = []
    for i, r in enumerate(df.to_dict("records"), start=1):
        rows.append({
            "idx":              i,
            "doi_r":            r.get("doi_r",            ""),
            "title_r":          r.get("title_r",          ""),
            "year_r":           r.get("year_r",           ""),
            "authors_r":        r.get("authors_r",        ""),
            "journal_r":        r.get("journal_r",        ""),
            "source":           r.get("source",           ""),
            "filter_status":    r.get("filter_status",    ""),
            "filter_method":    r.get("filter_method",    ""),
            "filter_confidence":r.get("filter_confidence",""),
            "filter_evidence":  r.get("filter_evidence",  ""),
            "abstract_r":       r.get("abstract_r",       ""),
        })

    return jsonify({"rows": rows, "total": len(rows)})
