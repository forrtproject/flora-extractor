"""
routes/flora.py — Master list of all papers.

Reads paper metadata from data/extracted.csv (same source as the Extract tab),
then joins vote counts from SQLite so the two tabs always show the same data.

Routes:
  GET  /flora                    → master list page
  GET  /api/flora/list           → filtered list JSON
  GET  /api/flora/detail/<doi_r> → full row with vote breakdown
"""
import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from shared.config import BASE_DIR, DATA_DIR
from validate.models import db, Replication, Vote

flora_bp = Blueprint("flora", __name__)

_CSV_PATH    = DATA_DIR / "extracted.csv"
_SAMPLE_PATH = BASE_DIR / "misc" / "sample_extracted.csv"


def _load_csv() -> pd.DataFrame | None:
    path = _CSV_PATH if _CSV_PATH.exists() else (_SAMPLE_PATH if _SAMPLE_PATH.exists() else None)
    if path is None:
        return None
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip").fillna("")
    # normalise title column
    if "title_r" not in df.columns and "study_r" in df.columns:
        df["title_r"] = df["study_r"]
    elif "title_r" in df.columns and "study_r" in df.columns:
        df["title_r"] = df["title_r"].where(df["title_r"] != "", df["study_r"])
    return df


def _vote_map() -> dict:
    """Return {doi_r: {confirm_votes, reject_votes, vote_count, validation_status}} from SQLite."""
    out = {}
    try:
        for rep in Replication.query.all():
            out[rep.doi_r] = {
                "confirm_votes":     rep.confirm_votes,
                "reject_votes":      rep.reject_votes,
                "vote_count":        rep.vote_count,
                "validation_status": rep.validation_status,
            }
    except Exception:
        pass
    return out


@flora_bp.route("/flora")
def flora_page():
    df = _load_csv()
    n  = len(df) if df is not None else 0

    def _count(mtype):
        if df is None:
            return 0
        col = "original_match_type"
        return int((df[col] == mtype).sum()) if col in df.columns else 0

    counts = {
        "all":               n,
        "single":            _count("single_original"),
        "multiple_match":    _count("multiple_match"),
        "multiple_original": _count("multiple_original"),
    }
    return render_template("flora.html", active_page="flora", counts=counts)


@flora_bp.route("/api/flora/list")
def api_list():
    df = _load_csv()
    if df is None:
        return jsonify({"rows": [], "total": 0})

    match_type = request.args.get("type",   "all")
    status     = request.args.get("status", "all")
    q          = request.args.get("q",      "").strip().lower()

    mtype_col = "original_match_type"
    if match_type == "single" and mtype_col in df.columns:
        df = df[df[mtype_col].isin(["single_original", ""])]
    elif match_type in ("multiple_match", "multiple_original") and mtype_col in df.columns:
        df = df[df[mtype_col] == match_type]

    if q:
        mask = (
            df.get("doi_r",   pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("title_r", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("title_o", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
        )
        df = df[mask]

    votes = _vote_map()

    def _status_label(vote_info: dict) -> str:
        vs = vote_info.get("validation_status", "pending")
        if vs in ("confirmed", "rejected"):
            return "completed"
        if vote_info.get("vote_count", 0) > 0:
            return "in_progress"
        return "pending"

    rows = []
    for r in df.to_dict("records"):
        doi_r = r.get("doi_r", "")
        vi    = votes.get(doi_r, {})
        sl    = _status_label(vi)
        if status != "all" and sl != status:
            continue
        rows.append({
            "doi_r":             doi_r,
            "title_r":           r.get("title_r", "") or r.get("study_r", ""),
            "doi_o":             r.get("doi_o", ""),
            "title_o":           r.get("title_o", ""),
            "year_r":            r.get("year_r", ""),
            "outcome":           r.get("outcome", ""),
            "link_method":       r.get("link_method", ""),
            "link_confidence":   r.get("link_confidence", ""),
            "match_type":        r.get("original_match_type", "single_original"),
            "original_rank":     r.get("original_rank", "1"),
            "n_originals":       r.get("n_originals", "1"),
            "validation_status": vi.get("validation_status", "pending"),
            "status_label":      sl,
            "vote_count":        vi.get("vote_count", 0),
            "confirm_votes":     vi.get("confirm_votes", 0),
            "reject_votes":      vi.get("reject_votes", 0),
        })

    return jsonify({"rows": rows, "total": len(rows)})


@flora_bp.route("/api/flora/detail/<path:doi_r>")
def api_detail(doi_r: str):
    df = _load_csv()
    if df is None:
        return jsonify({"error": "No extracted.csv"}), 404

    matches = df[df["doi_r"] == doi_r]
    if matches.empty:
        return jsonify({"error": "not found"}), 404

    row = matches.iloc[0].to_dict()
    votes_qs = []
    rep = Replication.query.filter_by(doi_r=doi_r).first()
    if rep:
        votes_qs = Vote.query.filter_by(replication_id=rep.id).all()

    return jsonify({
        "doi_r":   row.get("doi_r", ""),
        "title_r": row.get("title_r", "") or row.get("study_r", ""),
        "doi_o":   row.get("doi_o", ""),
        "title_o": row.get("title_o", ""),
        "outcome": row.get("outcome", ""),
        "link_method":     row.get("link_method", ""),
        "link_confidence": row.get("link_confidence", ""),
        "link_evidence":   row.get("link_evidence", ""),
        "votes": [
            {"reviewer": v.reviewer_id, "vote": v.vote,
             "comment": v.comment, "at": v.created_at}
            for v in votes_qs
        ],
    })
