"""
routes/dashboard.py — Validation stats dashboard.

Routes:
  GET  /dashboard           → dashboard page
  GET  /api/dashboard/stats → JSON stats from SQLite
"""
from flask import Blueprint, jsonify, render_template
from sqlalchemy import func

from validate.models import db, Replication, Vote

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", active_page="dashboard")


@dashboard_bp.route("/api/dashboard/stats")
def api_stats():
    total        = Replication.query.count()
    confirmed    = Replication.query.filter_by(validation_status="confirmed").count()
    rejected     = Replication.query.filter_by(validation_status="rejected").count()
    needs_review = Replication.query.filter_by(validation_status="needs_review").count()
    in_progress  = Replication.query.filter(
        Replication.validation_status == "pending",
        Replication.vote_count > 0,
    ).count()
    pending      = Replication.query.filter(
        Replication.validation_status == "pending",
        Replication.vote_count == 0,
    ).count()

    total_votes    = Vote.query.count()
    avg_votes      = round(total_votes / total, 2) if total > 0 else 0
    reviewer_count = db.session.query(
        func.count(func.distinct(Vote.reviewer_id))
    ).scalar() or 0

    return jsonify({
        "total":          total,
        "confirmed":      confirmed,
        "rejected":       rejected,
        "needs_review":   needs_review,
        "in_progress":    in_progress,
        "pending":        pending,
        "total_votes":    total_votes,
        "avg_votes":      avg_votes,
        "reviewer_count": reviewer_count,
    })
