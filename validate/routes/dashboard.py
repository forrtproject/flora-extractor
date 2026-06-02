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

    # ── Extracted + Extracted Test ────────────────────────────────────────────
    for prefix, path in [("", DATA_DIR / "extracted.csv"),
                         ("test_", DATA_DIR / "extracted-test.csv")]:
        _add_extracted_stats(stats, prefix, path)

    return jsonify(stats)


_OUTCOME_KEYS = ("success", "failure", "mixed", "uninformative",
                 "descriptive", "pending", "api_error")
_METHOD_KEYS  = ("author_year_match", "llm_abstract", "llm_fulltext",
                 "no_original_found", "target_pending", "api_error")


def _model_family(model_str: str) -> str:
    """Bucket a model identifier into gemini / gpt / qwen / none / other."""
    m = str(model_str or "").lower().strip()
    if not m:
        return "none"
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith(("gpt-", "o1", "o3", "o4")):
        return "gpt"
    if "qwen" in m:
        return "qwen"
    return "other"


def _add_extracted_stats(stats: dict, prefix: str, path) -> None:
    """Populate stats dict with extracted-CSV metrics under the given prefix."""
    zero_defaults = {
        f"{prefix}target_pending_count": 0,
        f"{prefix}match_single":            0,
        f"{prefix}match_multiple_match":     0,
        f"{prefix}match_multiple_original":  0,
        **{f"{prefix}method_{k}": 0 for k in _METHOD_KEYS},
        **{f"{prefix}model_gemini": 0, f"{prefix}model_gpt": 0,
           f"{prefix}model_qwen":  0, f"{prefix}model_none": 0,
           f"{prefix}model_other": 0},
        **{f"{prefix}outcome_{k}": 0 for k in _OUTCOME_KEYS},
    }

    if not path.exists():
        stats[f"{prefix}extracted_count"] = None
        stats.update(zero_defaults)
        return

    try:
        edf = pd.read_csv(
            path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
            usecols=lambda c: c in ("doi_r", "link_method", "link_llm_model",
                                    "original_match_type", "outcome"),
        ).fillna("")

        stats[f"{prefix}extracted_count"] = len(edf)

        # target_pending shortcut
        if "link_method" in edf.columns:
            stats[f"{prefix}target_pending_count"] = int(
                (edf["link_method"] == "target_pending").sum()
            )
        else:
            stats[f"{prefix}target_pending_count"] = 0

        # match type
        if "original_match_type" in edf.columns:
            vc = edf["original_match_type"].value_counts().to_dict()
            stats[f"{prefix}match_single"]            = int(vc.get("single_original",   0))
            stats[f"{prefix}match_multiple_match"]    = int(vc.get("multiple_match",    0))
            stats[f"{prefix}match_multiple_original"] = int(vc.get("multiple_original", 0))
        else:
            stats.update({f"{prefix}match_single": 0,
                          f"{prefix}match_multiple_match": 0,
                          f"{prefix}match_multiple_original": 0})

        # link method
        if "link_method" in edf.columns:
            vc = edf["link_method"].value_counts().to_dict()
            for k in _METHOD_KEYS:
                stats[f"{prefix}method_{k}"] = int(vc.get(k, 0))
        else:
            for k in _METHOD_KEYS:
                stats[f"{prefix}method_{k}"] = 0

        # model family (only for LLM-resolved rows)
        if "link_llm_model" in edf.columns:
            families = edf["link_llm_model"].apply(_model_family).value_counts().to_dict()
            for fam in ("gemini", "gpt", "qwen", "none", "other"):
                stats[f"{prefix}model_{fam}"] = int(families.get(fam, 0))
        else:
            for fam in ("gemini", "gpt", "qwen", "none", "other"):
                stats[f"{prefix}model_{fam}"] = 0

        # outcome
        if "outcome" in edf.columns:
            vc = edf["outcome"].value_counts().to_dict()
            for k in _OUTCOME_KEYS:
                stats[f"{prefix}outcome_{k}"] = int(vc.get(k, 0))
        else:
            for k in _OUTCOME_KEYS:
                stats[f"{prefix}outcome_{k}"] = 0

    except Exception:
        stats[f"{prefix}extracted_count"] = None
        stats.update(zero_defaults)
