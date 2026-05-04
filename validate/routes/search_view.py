"""
routes/search_view.py — Read-only view of candidates.csv (Stage 1 output).

Loads data/candidates.csv; falls back to misc/sample_candidates.csv.

Routes:
  GET  /search                  → render search.html
  GET  /api/search/list         → filtered summary rows as JSON
"""
import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from shared.config import BASE_DIR, DATA_DIR

search_view_bp = Blueprint("search_view", __name__)

_CSV_PATH    = DATA_DIR / "candidates.csv"
_SAMPLE_PATH = BASE_DIR / "misc" / "sample_candidates.csv"


def _load_csv() -> tuple[pd.DataFrame | None, str]:
    if _CSV_PATH.exists():
        return pd.read_csv(_CSV_PATH, encoding="utf-8-sig", dtype=str,
                           on_bad_lines="skip").fillna(""), "data/candidates.csv"
    if _SAMPLE_PATH.exists():
        return pd.read_csv(_SAMPLE_PATH, encoding="utf-8-sig", dtype=str,
                           on_bad_lines="skip").fillna(""), "misc/sample_candidates.csv (sample)"
    return None, ""


@search_view_bp.route("/search")
def search_page():
    df, source = _load_csv()
    return render_template("search.html", active_page="search",
                           source=source, total=len(df) if df is not None else 0)


@search_view_bp.route("/api/search/list")
def api_search_list():
    df, _ = _load_csv()
    if df is None:
        return jsonify({"error": "No candidates.csv found. Run Stage 1 first."}), 404

    q      = request.args.get("q",      "").strip().lower()
    source = request.args.get("source", "all")

    if q:
        mask = (
            df.get("doi_r",   pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("title_r", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("abstract_r", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
        )
        df = df[mask]

    if source != "all" and "source" in df.columns:
        df = df[df["source"] == source]

    rows = []
    for i, r in enumerate(df.to_dict("records"), start=1):
        rows.append({
            "idx":        i,
            "doi_r":      r.get("doi_r",      ""),
            "title_r":    r.get("title_r",    ""),
            "year_r":     r.get("year_r",     ""),
            "authors_r":  r.get("authors_r",  ""),
            "journal_r":  r.get("journal_r",  ""),
            "source":     r.get("source",     ""),
            "url_r":      r.get("url_r",      ""),
            "abstract_r": r.get("abstract_r", ""),
        })

    return jsonify({"rows": rows, "total": len(rows)})
