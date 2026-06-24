"""
routes/target_pending.py — View for target_pending rows in extracted.csv.

Shows rows where Stage 3 could not identify the original study
(link_method == "target_pending").  Once a row is resolved by re-running
Stage 3 (via the Extract tab's re-run button), it moves out of this view
automatically — no changes are made to extracted.csv here.

Routes:
  GET  /target-pending            → render target_pending.html
  GET  /api/target-pending/list   → target_pending rows as JSON
"""
import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from shared.config import DATA_DIR

target_pending_bp = Blueprint("target_pending", __name__)

_CSV_PATH = DATA_DIR / "extracted.csv"


def _load_pending() -> tuple[pd.DataFrame | None, str]:
    if not _CSV_PATH.exists():
        return None, ""
    df = pd.read_csv(_CSV_PATH, encoding="utf-8-sig", dtype=str,
                     on_bad_lines="skip").fillna("")
    if "link_method" in df.columns:
        df = df[df["link_method"] == "target_pending"].copy()
    return df, "data/extracted.csv"


@target_pending_bp.route("/target-pending")
def target_pending_page():
    df, source = _load_pending()
    return render_template(
        "target_pending.html",
        active_page="target_pending",
        source=source,
        total=len(df) if df is not None else 0,
    )


@target_pending_bp.route("/api/target-pending/list")
def api_list():
    df, _ = _load_pending()
    if df is None:
        return jsonify({"error": "No extracted.csv found. Run Stage 3 first."}), 404

    q = request.args.get("q", "").strip().lower()
    if q:
        mask = (
            df.get("doi_r",   pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("title_r", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
        )
        df = df[mask]

    total  = len(df)
    page   = max(1, int(request.args.get("page",     1)))
    per_pg = max(1, min(500, int(request.args.get("per_page", 200))))
    offset = (page - 1) * per_pg
    page_df = df.iloc[offset : offset + per_pg]

    rows = []
    for i, r in enumerate(page_df.to_dict("records"), start=offset + 1):
        rows.append({
            "idx":             i,
            "doi_r":           r.get("doi_r",           ""),
            "title_r":         r.get("title_r",         "") or r.get("study_r", ""),
            "year_r":          r.get("year_r",          ""),
            "authors_r":       r.get("authors_r",       ""),
            "journal_r":       r.get("journal_r",       ""),
            "abstract_r":      r.get("abstract_r",      ""),
            "filter_status":   r.get("filter_status",   ""),
            "filter_evidence": r.get("filter_evidence", ""),
            "link_evidence":   r.get("link_evidence",   ""),
            "link_method":     r.get("link_method",     ""),
        })

    return jsonify({
        "rows":     rows,
        "total":    total,
        "page":     page,
        "per_page": per_pg,
        "pages":    max(1, -(-total // per_pg)),
    })
