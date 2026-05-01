"""
routes/review.py — Voting queue and vote submission.

Routes:
  GET  /review       → voting queue page (shows next unvoted record)
  POST /vote         → submit a vote for a replication record
  GET  /api/review/next  → next unvoted record JSON
"""
from flask import Blueprint, jsonify, render_template, request, session

from shared.config import log
from shared.utils import clean_doi
from validate.models import db, Replication, Vote

review_bp = Blueprint("review", __name__)


@review_bp.route("/review")
def review_page():
    """Show the voting queue page."""
    return render_template("review.html", active_page="review")


@review_bp.route("/api/review/next")
def api_next():
    """Return the next replication record pending a vote from this reviewer."""
    reviewer_id = session.get("reviewer_id", "anonymous")

    # TODO: implement next-record query
    raise NotImplementedError("api_next is not yet implemented")


@review_bp.route("/vote", methods=["POST"])
def vote():
    """Submit a vote (confirm | reject | unsure) for a replication record."""
    body           = request.get_json(force=True) or {}
    replication_id = body.get("replication_id")
    vote_value     = body.get("vote", "")
    comment        = body.get("comment", "")
    reviewer_id    = session.get("reviewer_id", "anonymous")

    if not replication_id or vote_value not in ("confirm", "reject", "unsure"):
        return jsonify({"error": "invalid input"}), 400

    # TODO: implement vote submission, update validation_status when thresholds met
    raise NotImplementedError("vote is not yet implemented")
