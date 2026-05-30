"""
routes/dashboard.py — Validation stats dashboard.

Routes:
  GET  /dashboard               → dashboard page
  GET  /api/dashboard/stats     → JSON stats from SQLite (validation votes)
  GET  /api/dashboard/csv-stats → JSON stats read directly from pipeline CSVs
"""
import pandas as pd
from flask import Blueprint, jsonify, render_template
from sqlalchemy import func

from shared.config import DATA_DIR
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


@dashboard_bp.route("/api/dashboard/csv-stats")
def api_csv_stats():
    """Read pipeline CSVs directly and return counts + distributions."""
    stats: dict = {}

    # ── Candidates ────────────────────────────────────────────────────────────
    cand_path = DATA_DIR / "candidates.csv"
    if cand_path.exists():
        try:
            cdf = pd.read_csv(cand_path, encoding="utf-8-sig", dtype=str,
                              on_bad_lines="skip", usecols=lambda c: c in ("doi_r", "openalex_id_r"))
            stats["candidates_count"] = len(cdf)
        except Exception:
            stats["candidates_count"] = None
    else:
        stats["candidates_count"] = None

    # ── Filtered ──────────────────────────────────────────────────────────────
    filt_path = DATA_DIR / "filtered.csv"
    if filt_path.exists():
        try:
            fdf = pd.read_csv(filt_path, encoding="utf-8-sig", dtype=str,
                              on_bad_lines="skip",
                              usecols=lambda c: c in ("doi_r", "filter_status"))
            stats["filtered_count"] = len(fdf)
            if "filter_status" in fdf.columns:
                vc = fdf["filter_status"].value_counts().to_dict()
                stats["filter_replication"]    = int(vc.get("replication",    0))
                stats["filter_reproduction"]   = int(vc.get("reproduction",   0))
                stats["filter_false_positive"] = int(vc.get("false_positive", 0))
                stats["filter_needs_review"]   = int(vc.get("needs_review",   0))
            else:
                stats.update(filter_replication=0, filter_reproduction=0,
                             filter_false_positive=0, filter_needs_review=0)
        except Exception:
            stats["filtered_count"] = None
            stats.update(filter_replication=0, filter_reproduction=0,
                         filter_false_positive=0, filter_needs_review=0)
    else:
        stats["filtered_count"] = None
        stats.update(filter_replication=0, filter_reproduction=0,
                     filter_false_positive=0, filter_needs_review=0)

    # ── Extracted ─────────────────────────────────────────────────────────────
    ext_path = DATA_DIR / "extracted.csv"
    if ext_path.exists():
        try:
            edf = pd.read_csv(ext_path, encoding="utf-8-sig", dtype=str,
                              on_bad_lines="skip",
                              usecols=lambda c: c in
                              ("doi_r", "link_method", "original_match_type", "outcome"))
            stats["extracted_count"] = len(edf)

            if "link_method" in edf.columns:
                stats["target_pending_count"] = int((edf["link_method"] == "target_pending").sum())
            else:
                stats["target_pending_count"] = 0

            if "original_match_type" in edf.columns:
                vc = edf["original_match_type"].value_counts().to_dict()
                stats["match_single"]            = int(vc.get("single_original",    0))
                stats["match_multiple_match"]    = int(vc.get("multiple_match",     0))
                stats["match_multiple_original"] = int(vc.get("multiple_original",  0))
            else:
                stats.update(match_single=0, match_multiple_match=0, match_multiple_original=0)

            if "outcome" in edf.columns:
                vc = edf["outcome"].value_counts().to_dict()
                for key in ("success", "failure", "mixed", "uninformative",
                            "descriptive", "pending", "api_error"):
                    stats[f"outcome_{key}"] = int(vc.get(key, 0))
            else:
                for key in ("success", "failure", "mixed", "uninformative",
                            "descriptive", "pending", "api_error"):
                    stats[f"outcome_{key}"] = 0

        except Exception:
            stats["extracted_count"] = None
            stats["target_pending_count"] = 0
            stats.update(match_single=0, match_multiple_match=0, match_multiple_original=0)
            for key in ("success", "failure", "mixed", "uninformative",
                        "descriptive", "pending", "api_error"):
                stats[f"outcome_{key}"] = 0
    else:
        stats["extracted_count"] = None
        stats["target_pending_count"] = 0
        stats.update(match_single=0, match_multiple_match=0, match_multiple_original=0)
        for key in ("success", "failure", "mixed", "uninformative",
                    "descriptive", "pending", "api_error"):
            stats[f"outcome_{key}"] = 0

    return jsonify(stats)
