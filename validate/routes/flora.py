"""
routes/flora.py — Master list of all papers.

Routes:
  GET  /flora                    → master list page
  GET  /api/flora/list           → paginated list JSON (type/status/q filters)
  GET  /api/flora/detail/<id>    → full row detail with vote breakdown
"""
from flask import Blueprint, jsonify, render_template, request

from validate.models import db, Replication, Vote

flora_bp = Blueprint("flora", __name__)


@flora_bp.route("/flora")
def flora_page():
    counts = {
        "all":               Replication.query.count(),
        "single":            Replication.query.filter_by(original_match_type="single_original").count(),
        "multiple_match":    Replication.query.filter_by(original_match_type="multiple_match").count(),
        "multiple_original": Replication.query.filter_by(original_match_type="multiple_original").count(),
    }
    return render_template("flora.html", active_page="flora", counts=counts)


@flora_bp.route("/api/flora/list")
def api_list():
    match_type = request.args.get("type", "all")
    status     = request.args.get("status", "all")
    q          = request.args.get("q", "").strip().lower()

    query = Replication.query

    if match_type == "single":
        query = query.filter(
            db.or_(
                Replication.original_match_type == "single_original",
                Replication.original_match_type == "",
                Replication.original_match_type.is_(None),
            )
        )
    elif match_type in ("multiple_match", "multiple_original"):
        query = query.filter(Replication.original_match_type == match_type)

    reps = query.order_by(Replication.id).all()

    def _status_label(r: Replication) -> str:
        if r.validation_status in ("confirmed", "rejected"):
            return "completed"
        if r.vote_count > 0:
            return "in_progress"
        return "pending"

    if status != "all":
        reps = [r for r in reps if _status_label(r) == status]

    if q:
        reps = [
            r for r in reps
            if q in (r.title_r or "").lower()
            or q in (r.doi_r or "").lower()
            or q in (r.title_o or "").lower()
        ]

    return jsonify({
        "rows": [
            {
                "id":                r.id,
                "doi_r":             r.doi_r,
                "title_r":           r.title_r or "",
                "doi_o":             r.doi_o or "",
                "title_o":           r.title_o or "",
                "match_type":        r.original_match_type or "single_original",
                "validation_status": r.validation_status,
                "status_label":      _status_label(r),
                "vote_count":        r.vote_count,
                "confirm_votes":     r.confirm_votes,
                "reject_votes":      r.reject_votes,
            }
            for r in reps
        ],
        "total": Replication.query.count(),
    })


@flora_bp.route("/api/flora/detail/<int:rep_id>")
def api_detail(rep_id: int):
    rep   = db.get_or_404(Replication, rep_id)
    votes = Vote.query.filter_by(replication_id=rep_id).all()

    return jsonify({
        "id":                rep.id,
        "doi_r":             rep.doi_r,
        "title_r":           rep.title_r or "",
        "doi_o":             rep.doi_o or "",
        "title_o":           rep.title_o or "",
        "flora_status":      rep.flora_status or "",
        "validation_status": rep.validation_status,
        "confirm_votes":     rep.confirm_votes,
        "reject_votes":      rep.reject_votes,
        "votes": [
            {"reviewer": v.reviewer_id, "vote": v.vote,
             "comment": v.comment, "at": v.created_at}
            for v in votes
        ],
    })
