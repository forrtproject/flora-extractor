"""api_validation.py — the Supabase validation views, moved unchanged from dashboard.py.

These read the separate flora-validation app's tables. The redesign deliberately does
not touch their logic, their paths or their query-parameter names: only their home
changed. Anything already calling these endpoints keeps working.
"""
from flask import Blueprint, jsonify, request

from shared import supabase_client as supa

validation_bp = Blueprint("validation", __name__)


@validation_bp.route("/api/dashboard/supabase-stats")
def api_supabase_stats():
    """Validation KPIs from Supabase (cached 5 min), plus the per-RECORD vote counts.

    `total_judgements` counts filled validator SLOTS, not records: every record has
    three slots (two humans and the LLM), so 1,055 judgements over 469 records reads
    as if far more work were done than has been. `slots_filled` — already computed by
    `get_validation_analytics` — is the honest per-record answer, and is merged in
    here so the panel can say how many records have all three votes.
    """
    stats = dict(supa.get_validation_stats())
    analytics = supa.get_validation_analytics()
    if isinstance(analytics, dict) and "slots_filled" in analytics:
        slots = {str(k): v for k, v in (analytics.get("slots_filled") or {}).items()}
        stats["records_by_votes"] = slots
        stats["records_fully_voted"] = int(slots.get("3", 0))
        stats["records_both_humans"] = analytics.get("both_humans")
    return jsonify(stats)


@validation_bp.route("/api/dashboard/supabase-outcomes")
def api_supabase_outcomes():
    """Outcome distribution from validated table."""
    return jsonify(supa.get_validated_outcomes())


@validation_bp.route("/api/dashboard/supabase-analytics")
def api_supabase_analytics():
    """Coverage, per-field validator agreement, and final-vs-pipeline changes."""
    return jsonify(supa.get_validation_analytics())


@validation_bp.route("/api/dashboard/supabase-corrections")
def api_supabase_corrections():
    """Per-field correction frequency (type / original / outcome)."""
    return jsonify(supa.get_correction_frequency())


@validation_bp.route("/api/dashboard/supabase-confusion")
def api_supabase_confusion():
    """Pipeline-coded vs human-final confusion matrices (outcome, type) — #72."""
    return jsonify(supa.get_confusion_matrices())


@validation_bp.route("/api/dashboard/supabase-drilldown")
def api_supabase_drilldown():
    """Paginated table of DOIs where at least one field was corrected.

    Query params:
      page           — 1-based page (default 1)
      outcome_filter — "all" or a specific outcome value (default "all")
      check_filter   — "all" | "type" | "original" | "outcome" (default "all")
    """
    page = max(1, int(request.args.get("page", 1)))
    outcome_filter = request.args.get("outcome_filter", "all")
    check_filter = request.args.get("check_filter", "all")
    return jsonify(supa.get_drilldown_page(page, outcome_filter, check_filter))
