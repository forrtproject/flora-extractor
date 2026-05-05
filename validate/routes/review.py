"""
routes/review.py — Reviewer name prompt, voting queue, vote submission.

Routes:
  GET  /set-name              → reviewer name prompt
  POST /set-name              → store name in session, redirect
  GET  /validate              → voting queue page
  GET  /api/review/next       → next record JSON for current reviewer
  POST /vote                  → submit or update a vote
  GET  /api/validate/log      → lazy full-log detail for a record
"""
from datetime import datetime

from flask import (
    Blueprint, jsonify, redirect, render_template,
    request, session, url_for,
)

from shared.config import log
from shared.utils import clean_doi
from validate.models import db, Replication, Vote

review_bp = Blueprint("review", __name__)


@review_bp.route("/set-name", methods=["GET", "POST"])
def set_name_page():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            session["reviewer_id"] = name
            next_url = request.args.get("next") or url_for("dashboard.dashboard_page")
            return redirect(next_url)
    return render_template("set_name.html")


@review_bp.route("/validate")
def validate_page():
    return render_template("validate.html", active_page="validate")


@review_bp.route("/api/review/next")
def api_next():
    reviewer_id = session.get("reviewer_id", "anonymous")
    show_all    = request.args.get("show_all", "false").lower() == "true"
    after_id    = request.args.get("after_id", type=int, default=0)
    before_id   = request.args.get("before_id", type=int, default=0)

    query = Replication.query
    if not show_all:
        query = query.filter(
            Replication.validation_status.notin_(["confirmed", "rejected"])
        )

    recs = query.order_by(Replication.id).all()
    recs.sort(key=lambda r: (0 if r.validation_status == "needs_review" else 1, r.id))

    if not recs:
        return jsonify({"done": True})

    if after_id:
        candidates = [r for r in recs if r.id > after_id]
        rep = candidates[0] if candidates else None
    elif before_id:
        candidates = [r for r in recs if r.id < before_id]
        rep = candidates[-1] if candidates else None
    else:
        rep = recs[0]

    if not rep:
        return jsonify({"done": True})

    own_vote = Vote.query.filter_by(
        replication_id=rep.id, reviewer_id=reviewer_id
    ).first()

    return jsonify({
        "id":                  rep.id,
        "doi_r":               rep.doi_r,
        "title_r":             rep.title_r or "",
        "doi_o":               rep.doi_o or "",
        "title_o":             rep.title_o or "",
        "year_o":              rep.year_o or "",
        "authors_o":           rep.authors_o or "",
        "abstract_r":          rep.abstract_r or "",
        "outcome":             rep.outcome or "",
        "outcome_phrase":      rep.outcome_phrase or "",
        "outcome_confidence":  rep.outcome_confidence or "",
        "out_quote_source":    rep.out_quote_source or "",
        "link_method":         rep.link_method or "",
        "link_evidence":       rep.link_evidence or "",
        "link_confidence":     rep.link_confidence or "",
        "original_match_type": rep.original_match_type or "",
        "original_rank":       rep.original_rank,
        "n_originals":         rep.n_originals,
        "type":                rep.type or "replication",
        "validation_status":   rep.validation_status,
        "confirm_votes":       rep.confirm_votes,
        "reject_votes":        rep.reject_votes,
        "vote_count":          rep.vote_count,
        "your_vote":           own_vote.vote if own_vote else None,
        "your_comment":        own_vote.comment if own_vote else "",
        "total_pending":       Replication.query.filter(
            Replication.validation_status.notin_(["confirmed", "rejected"])
        ).count(),
        "total":               Replication.query.count(),
    })


@review_bp.route("/vote", methods=["POST"])
def vote():
    body          = request.get_json(force=True) or {}
    rep_id        = body.get("replication_id")
    vote_value    = body.get("vote", "")
    comment       = body.get("comment", "").strip()
    corrected_doi = clean_doi(body.get("corrected_doi_o", "") or "")
    reviewer_id   = session.get("reviewer_id", "anonymous")

    if not rep_id or vote_value not in ("confirm", "reject", "needs_review"):
        return jsonify({"error": "invalid input"}), 400

    rep = db.get_or_404(Replication, rep_id)

    existing = Vote.query.filter_by(
        replication_id=rep_id, reviewer_id=reviewer_id
    ).first()

    now = datetime.utcnow().isoformat()
    if existing:
        existing.vote       = vote_value
        existing.comment    = comment
        existing.created_at = now
    else:
        db.session.add(Vote(
            replication_id=rep_id,
            reviewer_id=reviewer_id,
            vote=vote_value,
            comment=comment,
            created_at=now,
        ))

    if corrected_doi:
        rep.doi_o = corrected_doi

    db.session.flush()
    _recompute_status(rep)
    db.session.commit()

    return jsonify({
        "ok":                True,
        "validation_status": rep.validation_status,
        "confirm_votes":     rep.confirm_votes,
        "reject_votes":      rep.reject_votes,
        "vote_count":        rep.vote_count,
    })


@review_bp.route("/api/validate/log")
def api_log():
    rep_id = request.args.get("id", type=int)
    if not rep_id:
        return jsonify({"error": "missing id"}), 400

    rep   = db.get_or_404(Replication, rep_id)
    votes = Vote.query.filter_by(replication_id=rep_id).all()

    return jsonify({
        "link_method":      rep.link_method,
        "link_evidence":    rep.link_evidence,
        "link_confidence":  rep.link_confidence,
        "outcome":          rep.outcome,
        "outcome_phrase":   rep.outcome_phrase,
        "flora_status":     rep.flora_status,
        "votes": [
            {"reviewer": v.reviewer_id, "vote": v.vote,
             "comment": v.comment, "at": v.created_at}
            for v in votes
        ],
    })


def _recompute_status(rep: Replication) -> None:
    votes             = Vote.query.filter_by(replication_id=rep.id).all()
    rep.vote_count    = len(votes)
    rep.confirm_votes = sum(1 for v in votes if v.vote == "confirm")
    rep.reject_votes  = sum(1 for v in votes if v.vote == "reject")
    has_needs_review  = any(v.vote == "needs_review" for v in votes)

    if has_needs_review:
        rep.validation_status = "needs_review"
    elif rep.confirm_votes >= 2:
        rep.validation_status = "confirmed"
    elif rep.reject_votes >= 2:
        rep.validation_status = "rejected"

    rep.validator_notes = " | ".join(v.comment for v in votes if v.comment)
