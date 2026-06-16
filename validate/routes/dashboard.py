"""
routes/dashboard.py — Read-only monitoring dashboard.

Routes:
  GET  /dashboard                        → dashboard page
  GET  /api/dashboard/csv-stats          → pipeline stats (column-only CSV reads)
  GET  /api/dashboard/download           → stream a raw pipeline CSV as attachment
  GET  /api/dashboard/supabase-stats     → Supabase validation KPIs (cached 5 min)
  GET  /api/dashboard/supabase-outcomes  → outcome distribution from validated table
  GET  /api/dashboard/supabase-corrections → per-field correction frequency
  GET  /api/dashboard/supabase-drilldown → paginated incorrect-DOI table
"""
import datetime
import re
import shutil

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import DATA_DIR, BASE_DIR
from shared import supabase_client as supa

ANALYSIS_DIR = BASE_DIR / "analysis"

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", active_page="dashboard")



@dashboard_bp.route("/api/dashboard/csv-stats")
def api_csv_stats():
    """Read pipeline CSVs directly and return counts + distributions."""
    stats: dict = {}

    # ── Candidates ────────────────────────────────────────────────────────────
    cand_path = DATA_DIR / "candidates.csv"
    if cand_path.exists():
        try:
            cdf = pd.read_csv(cand_path, encoding="utf-8-sig", dtype=str,
                              on_bad_lines="skip",
                              usecols=lambda c: c in ("doi_r", "openalex_id_r", "source"))
            stats["candidates_count"] = len(cdf)
            if "source" in cdf.columns:
                stats["candidates_source"] = {
                    k: int(v) for k, v in cdf["source"].fillna("unknown").value_counts().items()
                }
            else:
                stats["candidates_source"] = {}
        except Exception:
            stats["candidates_count"] = None
            stats["candidates_source"] = {}
    else:
        stats["candidates_count"] = None
        stats["candidates_source"] = {}

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


_STAGE_FILES = {
    "candidates":     DATA_DIR / "candidates.csv",
    "filtered":       DATA_DIR / "filtered.csv",
    "extracted":      DATA_DIR / "extracted.csv",
    "extracted-test": DATA_DIR / "extracted-test.csv",
}


@dashboard_bp.route("/api/dashboard/download")
def api_dashboard_download():
    """Stream a raw pipeline CSV as a download attachment.

    Query params:
      stage — candidates | filtered | extracted | extracted-test
    """
    stage = request.args.get("stage", "extracted").strip()
    if stage not in _STAGE_FILES:
        return jsonify({"error": "invalid stage"}), 400

    src = _STAGE_FILES[stage]
    if not src.exists():
        return jsonify({"error": f"{stage} CSV not found"}), 404

    download_dir = DATA_DIR / "dashboard" / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    date_str  = datetime.date.today().isoformat()
    filename  = f"{stage}_{date_str}.csv"
    dest_path = download_dir / filename

    shutil.copy2(src, dest_path)
    return send_file(str(dest_path), as_attachment=True,
                     download_name=filename, mimetype="text/csv")


_OUTCOME_KEYS = ("success", "failure", "mixed", "uninformative",
                 "cannot_be_determined", "descriptive", "pending", "api_error")
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


# ── Supabase endpoints ────────────────────────────────────────────────────────

@dashboard_bp.route("/api/dashboard/supabase-stats")
def api_supabase_stats():
    """Validation KPIs from Supabase (cached 5 min)."""
    return jsonify(supa.get_validation_stats())


@dashboard_bp.route("/api/dashboard/supabase-outcomes")
def api_supabase_outcomes():
    """Outcome distribution from validated table."""
    return jsonify(supa.get_validated_outcomes())


@dashboard_bp.route("/api/dashboard/supabase-corrections")
def api_supabase_corrections():
    """Per-field correction frequency (type / original / outcome)."""
    return jsonify(supa.get_correction_frequency())


@dashboard_bp.route("/api/dashboard/analysis-stats")
def api_analysis_stats():
    """Summary stats for the Analysis tab — gap KPIs, audit metrics, improvement opportunities."""
    stats: dict = {}

    # ── Gap summary from gap_summary.md ──────────────────────────────────────
    summary: dict = {}
    summary_path = ANALYSIS_DIR / "gap_summary.md"
    if summary_path.exists():
        try:
            text = summary_path.read_text(encoding="utf-8")
            for label, key in [
                (r"DOI-matched gaps:\s*(\d+)",   "doi_gaps"),
                (r"URL-matched gaps:\s*(\d+)",   "url_gaps"),
                (r"Fuzzy-matched gaps:\s*(\d+)", "fuzzy_gaps"),
                (r"Total:\s*(\d+)\s*gaps",       "total_gaps"),
                (r"Filter misclassifications\**:\s*(\d+)", "filter_misclassifications"),
            ]:
                m = re.search(label, text)
                if m:
                    summary[key] = int(m.group(1))
            m = re.search(r"Generated:\s*(.+)", text)
            if m:
                summary["generated"] = m.group(1).strip()
        except Exception:
            pass
    stats["gap_summary"] = summary

    # ── Extraction audit from extraction_audit.md ─────────────────────────────
    audit: dict = {}
    audit_path = ANALYSIS_DIR / "extraction_audit.md"
    if audit_path.exists():
        try:
            text = audit_path.read_text(encoding="utf-8")
            for pattern, key in [
                (r"Total extracted rows:\s*(\d+)",      "total_extracted"),
                (r"Missing DOI[^:]*:\s*(\d+)",          "missing_doi"),
                (r"API error count:\s*(\d+)",           "api_errors"),
                (r"Target pending count:\s*(\d+)",      "target_pending"),
            ]:
                m = re.search(pattern, text)
                if m:
                    audit[key] = int(m.group(1))
            m = re.search(r"Generated:\s*(.+)", text)
            if m:
                audit["generated"] = m.group(1).strip()

            # Parse link method table (markdown table rows)
            link_methods = []
            in_link_table = False
            for line in text.splitlines():
                if "link_method" in line and "count" in line:
                    in_link_table = True
                    continue
                if in_link_table:
                    if line.startswith("|:") or line.startswith("| -"):
                        continue
                    if not line.startswith("|"):
                        break
                    parts = [p.strip() for p in line.strip("|").split("|")]
                    if len(parts) >= 3:
                        try:
                            link_methods.append({
                                "method": parts[0],
                                "count": int(parts[1]),
                                "pct": float(parts[2]),
                            })
                        except (ValueError, IndexError):
                            pass
            audit["link_methods"] = link_methods

            # Parse confidence table
            conf_rows = []
            in_conf = False
            for line in text.splitlines():
                if "link_confidence" in line and "count" in line:
                    in_conf = True
                    continue
                if in_conf:
                    if line.startswith("|:") or line.startswith("| -"):
                        continue
                    if not line.startswith("|"):
                        break
                    parts = [p.strip() for p in line.strip("|").split("|")]
                    if len(parts) >= 3:
                        try:
                            conf_rows.append({
                                "level": parts[0],
                                "count": int(parts[1]),
                                "pct": float(parts[2]),
                            })
                        except (ValueError, IndexError):
                            pass
            audit["confidence_rows"] = conf_rows
        except Exception:
            pass
    stats["audit"] = audit

    # ── Rule improvement opportunities ────────────────────────────────────────
    opp_path = ANALYSIS_DIR / "rule_improvement_opportunities.csv"
    if opp_path.exists():
        try:
            df = pd.read_csv(opp_path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip")
            stats["improvement_rows"] = df.fillna("").to_dict("records")
        except Exception:
            stats["improvement_rows"] = []
    else:
        stats["improvement_rows"] = []

    # ── URL-matched gaps (small, return all) ──────────────────────────────────
    url_path = ANALYSIS_DIR / "gap_analysis_url_matched.csv"
    if url_path.exists():
        try:
            df = pd.read_csv(url_path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
                             usecols=lambda c: c in ("doi_r", "url_r", "study_r", "year_r"))
            stats["gap_url_count"] = len(df)
            stats["gap_url_rows"] = df.fillna("").to_dict("records")
        except Exception:
            stats["gap_url_count"] = None
            stats["gap_url_rows"] = []
    else:
        stats["gap_url_count"] = None
        stats["gap_url_rows"] = []

    return jsonify(stats)


@dashboard_bp.route("/api/dashboard/analysis-gaps")
def api_analysis_gaps():
    """Paginated DOI-matched gap rows with optional title/DOI search."""
    page     = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(10, int(request.args.get("per_page", 50))))
    search   = request.args.get("search", "").strip().lower()

    doi_path = ANALYSIS_DIR / "gap_analysis_doi_matched.csv"
    if not doi_path.exists():
        return jsonify({"total": 0, "pages": 0, "rows": []})

    try:
        df = pd.read_csv(doi_path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
                         usecols=lambda c: c in ("doi_r", "url_r", "study_r", "year_r"))
        df = df.fillna("")
        if search:
            mask = (df["doi_r"].str.lower().str.contains(search, na=False) |
                    df["study_r"].str.lower().str.contains(search, na=False))
            df = df[mask]
        total = len(df)
        pages = max(1, (total + per_page - 1) // per_page)
        page  = min(page, pages)
        start = (page - 1) * per_page
        rows  = df.iloc[start:start + per_page][["doi_r", "study_r", "year_r"]].to_dict("records")
        return jsonify({"total": total, "pages": pages, "page": page, "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e), "total": 0, "pages": 0, "rows": []})


@dashboard_bp.route("/api/dashboard/supabase-drilldown")
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
