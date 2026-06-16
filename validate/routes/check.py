"""
check.py — Check tab: filter + search over any pipeline CSV.

Routes:
  GET /check                  → check page
  GET /api/check/search       → filtered/paginated rows as JSON
  GET /api/check/download     → filtered rows as CSV attachment
"""
import datetime
import re

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import DATA_DIR

check_bp = Blueprint("check", __name__)

_STAGES = {
    "candidates":     DATA_DIR / "candidates.csv",
    "filtered":       DATA_DIR / "filtered.csv",
    "extracted":      DATA_DIR / "extracted.csv",
    "extracted-test": DATA_DIR / "extracted-test.csv",
}

_TYPE_COL = {
    "candidates":     None,
    "filtered":       "filter_status",
    "extracted":      "type",
    "extracted-test": "type",
}


def _apply_filters(chunk: pd.DataFrame, stage: str, params: dict) -> pd.DataFrame:
    year_from    = params.get("year_from",    "")
    year_to      = params.get("year_to",      "")
    outcome      = params.get("outcome",      "")
    link_method  = params.get("link_method",  "")
    match_type   = params.get("match_type",   "")
    doi_verified = params.get("doi_verified", "")
    source       = params.get("source",       "")
    type_val     = params.get("type_val",     "")
    q            = params.get("q",            "")

    if year_from and "year_r" in chunk.columns:
        chunk = chunk[chunk["year_r"].apply(
            lambda y: y.isdigit() and int(y) >= int(year_from)
        )]
    if year_to and "year_r" in chunk.columns:
        chunk = chunk[chunk["year_r"].apply(
            lambda y: y.isdigit() and int(y) <= int(year_to)
        )]

    type_col = _TYPE_COL.get(stage)
    if type_val and type_col and type_col in chunk.columns:
        chunk = chunk[chunk[type_col] == type_val]

    for col, val in [
        ("outcome",             outcome),
        ("link_method",         link_method),
        ("original_match_type", match_type),
        ("doi_o_verification",  doi_verified),
        ("source",              source),
    ]:
        if val and col in chunk.columns:
            chunk = chunk[chunk[col] == val]

    if q:
        mask = pd.Series(False, index=chunk.index)
        if "doi_r" in chunk.columns:
            mask |= chunk["doi_r"].str.lower().str.contains(q, na=False)
        if "title_r" in chunk.columns:
            mask |= chunk["title_r"].str.lower().str.contains(q, na=False)
        chunk = chunk[mask]

    return chunk


def _read_filtered(stage: str, params: dict) -> pd.DataFrame:
    path = _STAGES[stage]
    if not path.exists():
        return pd.DataFrame()

    chunks = []
    for chunk in pd.read_csv(
        path, encoding="utf-8-sig", dtype=str,
        chunksize=50_000, on_bad_lines="skip",
    ):
        chunk = chunk.fillna("")
        filtered = _apply_filters(chunk, stage, params)
        if not filtered.empty:
            chunks.append(filtered)

    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


def _extract_params() -> dict:
    return {
        "year_from":    request.args.get("year_from",    "").strip(),
        "year_to":      request.args.get("year_to",      "").strip(),
        "type_val":     request.args.get("type",         "").strip(),
        "outcome":      request.args.get("outcome",      "").strip(),
        "link_method":  request.args.get("link_method",  "").strip(),
        "match_type":   request.args.get("match_type",   "").strip(),
        "doi_verified": request.args.get("doi_verified", "").strip(),
        "source":       request.args.get("source",       "").strip(),
        "q":            request.args.get("q",            "").strip().lower(),
    }


@check_bp.route("/check")
def check_page():
    return render_template("check.html", active_page="check")


@check_bp.route("/api/check/search")
def api_check_search():
    stage = request.args.get("stage", "extracted").strip()
    if stage not in _STAGES:
        return jsonify({"error": "invalid stage"}), 400

    page     = max(1, int(request.args.get("page",     1)))
    per_page = min(100, max(10, int(request.args.get("per_page", 25))))

    df = _read_filtered(stage, _extract_params())
    total  = len(df)
    pages  = max(1, (total + per_page - 1) // per_page) if total else 1
    page   = min(page, pages)
    start  = (page - 1) * per_page
    rows   = df.iloc[start:start + per_page].to_dict("records") if not df.empty else []

    return jsonify({"total": total, "pages": pages, "page": page, "rows": rows})


@check_bp.route("/api/check/download")
def api_check_download():
    stage = request.args.get("stage", "extracted").strip()
    if stage not in _STAGES:
        return jsonify({"error": "invalid stage"}), 400

    df = _read_filtered(stage, _extract_params())

    download_dir = DATA_DIR / "dashboard" / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    filename = f"check_{stage}_{date_str}.csv"
    out_path = download_dir / filename

    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return send_file(str(out_path), as_attachment=True,
                     download_name=filename, mimetype="text/csv")
