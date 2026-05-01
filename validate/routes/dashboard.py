"""
routes/dashboard.py — Stats overview dashboard.

Routes:
  GET  /dashboard           → dashboard page
  GET  /api/dashboard/stats → JSON stats summary
"""
from flask import Blueprint, jsonify, render_template

from shared.config import log
from validate.models import db, Replication, Vote

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", active_page="dashboard")


@dashboard_bp.route("/api/dashboard/stats")
def api_stats():
    """Return summary counts for the dashboard. Implemented in routes/batch.py."""
    from validate.routes.batch import api_dashboard_stats
    return api_dashboard_stats()
