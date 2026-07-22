"""
routes/dashboard.py — Read-only monitoring dashboard.

Routes:
  GET  /dashboard                        → dashboard page
  GET  /api/dashboard/csv-stats          → pipeline stats (stats.json → live compute)
  GET  /api/dashboard/search-phrases     → per-phrase OpenAlex yield from cursor files
  GET  /api/dashboard/download           → stream a raw pipeline CSV as attachment
  GET  /api/dashboard/supabase-stats     → Supabase validation KPIs (cached 5 min)
  GET  /api/dashboard/supabase-analytics → coverage, per-field validator agreement,
                                           final-vs-pipeline changes
  GET  /api/dashboard/supabase-outcomes  → outcome distribution from validated table
  GET  /api/dashboard/supabase-corrections → per-field correction frequency
  GET  /api/dashboard/supabase-drilldown → paginated incorrect-DOI table
"""
import datetime
import functools
import json
import re
import shutil

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import DATA_DIR, BASE_DIR
from shared import supabase_client as supa
from shared.dashboard_cache import compute_stage_stats, load_stats

ANALYSIS_DIR = BASE_DIR / "analysis"

dashboard_bp = Blueprint("dashboard", __name__)

_OUTCOME_KEYS = ("success", "failure", "mixed", "uninformative",
                 "cannot_be_determined", "descriptive", "pending", "api_error")
# The five rule-based resolution methods are now distinct link_method values. The
# dashboard's coarse "author_year_match" tile aggregates them (plus legacy/literal
# author_year_match rows); the granular per-method breakdown lives in stats.json's
# by_link_method.
_RULE_METHOD_KEYS = ("citation_context_match", "same_author_year_title_overlap",
                     "single_candidate_after_requery", "title_pattern_match",
                     "grobid_ref_match", "author_year_match_legacy", "author_year_match")
_METHOD_KEYS  = _RULE_METHOD_KEYS + ("llm_abstract", "llm_fulltext",
                 "no_original_found", "target_pending", "api_error")


@dashboard_bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", active_page="dashboard", set_files=SET_FILES)



def _stats_json_to_api(sj: dict, source: str = "stats_json") -> dict:
    """Translate a stats.json-shaped dict to the flat dict the dashboard JS expects.

    Missing stages degrade to a null count rather than failing — the UI renders
    those tiles as "—".
    """
    c  = sj.get("candidates")     or {}
    f  = sj.get("filtered")       or {}
    e  = sj.get("extracted")      or {}
    et = sj.get("extracted_test") or {}

    by_status = f.get("by_filter_status", {})
    by_method = e.get("by_link_method",   {})
    by_model  = e.get("by_model",         {})
    by_outcome= e.get("by_outcome",       {})
    by_mt     = e.get("by_match_type",    {})

    tbm  = et.get("by_link_method",   {})
    tmod = et.get("by_model",         {})
    toc  = et.get("by_outcome",       {})
    tmt  = et.get("by_match_type",    {})

    out: dict = {
        # Candidates
        "candidates_count":  c.get("total"),
        "candidates_source": c.get("by_source", {}),
        "candidates_by_year": c.get("by_year", {}),
        # Filtered
        "filtered_count":           f.get("total"),
        "filter_replication":       by_status.get("replication",    0),
        "filter_reproduction":      by_status.get("reproduction",   0),
        "filter_false_positive":    by_status.get("false_positive", 0),
        "filter_needs_review":      by_status.get("needs_review",   0),
        "filter_no_doi":            f.get("rep_repro_no_doi",          0),
        "filter_no_doi_or_url":     f.get("rep_repro_no_doi_or_url",   0),
        "filter_no_abstract":       f.get("rep_repro_no_abstract",     0),
        "filter_rep_repro_total":   f.get("rep_repro_total",           0),
        "filter_by_rule_exit":      f.get("by_rule_exit",     {}),
        "filter_rule_exit_status":  f.get("rule_exit_status", {}),
        "filtered_by_year":         f.get("by_year",          {}),
        # Candidates data quality
        "candidates_no_doi":        c.get("no_doi",        0),
        "candidates_no_doi_or_url": c.get("no_doi_or_url", 0),
        "candidates_no_abstract":   c.get("no_abstract",   0),
        # Extracted
        "extracted_count":           e.get("total"),
        "target_pending_count":      e.get("target_pending_count", 0),
        "match_single":              by_mt.get("single_original",   0),
        "match_multiple_match":      by_mt.get("multiple_match",    0),
        "match_multiple_original":   by_mt.get("multiple_original", 0),
        "method_author_year_match":  sum(by_method.get(k, 0) for k in _RULE_METHOD_KEYS),
        "method_llm_abstract":       by_method.get("llm_abstract",       0),
        "method_llm_fulltext":       by_method.get("llm_fulltext",       0),
        "method_no_original_found":  by_method.get("no_original_found",  0),
        "method_target_pending":     by_method.get("target_pending",     0),
        "method_api_error":          by_method.get("api_error",          0),
        "model_gemini": by_model.get("gemini", 0),
        "model_gpt":    by_model.get("gpt",    0),
        "model_qwen":   by_model.get("qwen",   0),
        "model_other":  by_model.get("other",  0),
        "model_none":   by_model.get("none",   0),
        **{f"outcome_{k}": by_outcome.get(k, 0) for k in _OUTCOME_KEYS},
        "extracted_by_type":            e.get("by_type", {}),
        "extracted_outcome_replication":  e.get("by_outcome_replication",  {}),
        "extracted_outcome_reproduction": e.get("by_outcome_reproduction", {}),
        "extracted_by_year":            e.get("by_year", {}),
        # Extracted-test
        "test_extracted_count":          et.get("total"),
        "test_target_pending_count":     et.get("target_pending_count", 0),
        "test_match_single":             tmt.get("single_original",   0),
        "test_match_multiple_match":     tmt.get("multiple_match",    0),
        "test_match_multiple_original":  tmt.get("multiple_original", 0),
        "test_method_author_year_match": sum(tbm.get(k, 0) for k in _RULE_METHOD_KEYS),
        "test_method_llm_abstract":      tbm.get("llm_abstract",       0),
        "test_method_llm_fulltext":      tbm.get("llm_fulltext",       0),
        "test_method_no_original_found": tbm.get("no_original_found",  0),
        "test_method_target_pending":    tbm.get("target_pending",     0),
        "test_method_api_error":         tbm.get("api_error",          0),
        "test_model_gemini": tmod.get("gemini", 0),
        "test_model_gpt":    tmod.get("gpt",    0),
        "test_model_qwen":   tmod.get("qwen",   0),
        "test_model_other":  tmod.get("other",  0),
        "test_model_none":   tmod.get("none",   0),
        **{f"test_outcome_{k}": toc.get(k, 0) for k in _OUTCOME_KEYS},
        "test_extracted_by_type":            et.get("by_type", {}),
        "test_extracted_outcome_replication":  et.get("by_outcome_replication",  {}),
        "test_extracted_outcome_reproduction": et.get("by_outcome_reproduction", {}),
        "test_extracted_by_year":            et.get("by_year", {}),
        "_source": source,
        "_updated_at": sj.get("updated_at"),
    }
    return out


@dashboard_bp.route("/api/dashboard/csv-stats")
def api_csv_stats():
    """Return pipeline stats. Fast path: stats.json; slow path: live Parquet/CSV.

    The slow path calls dashboard_cache.compute_stage_stats so both paths share
    one implementation of every aggregation.
    """
    sj = load_stats()
    missing = [s for s in _STAGE_FILES if not sj.get(s.replace("-", "_"))]
    if missing:
        for stage in missing:
            try:
                computed = compute_stage_stats(stage)
            except Exception:
                computed = None
            if computed is not None:
                sj[stage.replace("-", "_")] = computed
        return jsonify(_stats_json_to_api(sj, source="csv"))

    return jsonify(_stats_json_to_api(sj))


@functools.lru_cache(maxsize=1)
def _phrase_job_keys() -> "tuple[list[str], dict[str, str]]":
    """(labels, {job_key: label}) for every plausible (phrase, year-range) job."""
    from search.openalex_search import CONCEPT_IDS, SEARCH_PHRASES, _job_key

    years: list = [None, *range(1900, datetime.date.today().year + 3)]
    labels = [*SEARCH_PHRASES, *(f"concept:{c}" for c in CONCEPT_IDS)]
    return labels, {
        _job_key(label, a, b): label
        for label in labels
        for a in years for b in years
        if a is None or b is None or a <= b
    }


@dashboard_bp.route("/api/dashboard/search-phrases")
def api_search_phrases():
    """Per-phrase OpenAlex yield, recovered from the cursor checkpoint files.

    candidates.csv has no phrase column, so attribution is rebuilt by hashing
    every (phrase, from_year, to_year) job key and matching it to the cursor
    filenames in cache/openalex/. Counts are records FETCHED, before dedup — a
    paper matching three phrases is counted three times, so the column does not
    sum to candidates_count.
    """
    from shared.config import OA_CACHE_DIR

    labels, key_to_label = _phrase_job_keys()
    totals: dict[str, int] = {label: 0 for label in labels}
    jobs:   dict[str, int] = {label: 0 for label in labels}
    unattributed = 0
    for path in OA_CACHE_DIR.glob("*.cursor.json"):
        label = key_to_label.get(path.name.replace(".cursor.json", ""))
        if label is None:
            unattributed += 1
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        totals[label] += int(state.get("total_fetched") or 0)
        jobs[label]   += 1

    rows = [
        {"phrase": label, "fetched": totals[label], "jobs": jobs[label],
         "source": "concept" if label.startswith("concept:") else "phrase"}
        for label in labels
    ]
    rows.sort(key=lambda r: -r["fetched"])
    return jsonify({
        "rows": rows,
        "total_fetched": sum(totals.values()),
        "unattributed_files": unattributed,
    })


# ── Set-aside CSVs ────────────────────────────────────────────────────────────
# Rows the pipeline deliberately kept OUT of extracted.csv, each in its own file.
# Adding a new set here is all that is needed — the dashboard builds its tab,
# stats, and detail table generically from this registry.
SET_FILES: dict[str, dict] = {
    "not_a_replication": {
        "title": "Not a Replication",
        "file": "not_a_replication.csv",
        "why": "The outcome classifier judged is_genuine_attempt=false — the text does not "
               "describe a real attempt to replicate or reproduce the named original. These are "
               "Stage 2 false positives that survived the phrase gate.",
        "action": "Spot-check for classifier over-rejection; genuine misses should be promoted back.",
    },
    "unresolved_doi_mismatch": {
        "title": "Unresolved DOI Mismatch",
        "file": "unresolved_doi_mismatch.csv",
        "why": "doi_o pointed at a paper whose title/year did not match the resolved original, and "
               "re-resolution from title+author found no confident replacement. A wrong DOI is worse "
               "than a flagged one, so these are held back rather than guessed.",
        "action": "Resolve the original by hand, or confirm no original exists.",
    },
    "cannot_be_determined": {
        "title": "Cannot Be Determined",
        "file": "cannot_be_determined.csv",
        "why": "The original was linked but the text did not support any outcome verdict — usually a "
               "missing abstract, a paywalled full text, or a genuinely ambiguous result statement.",
        "action": "Recover the full text, then re-run the outcome step.",
    },
    "unresolved_self_links": {
        "title": "Unresolved Self-Links",
        "file": "unresolved_self_links.csv",
        "why": "doi_o resolved to the replication paper itself. Replication titles often echo the "
               "original's, so title search can return the replication — these could not be "
               "disentangled automatically.",
        "action": "Identify the true original manually.",
    },
    "target_pending": {
        "title": "Target Pending",
        "file": "target_pending.csv",
        "why": "The paper is a genuine replication but no candidate original was retrievable at "
               "extraction time — link_method = target_pending.",
        "action": "Retry once the reference data improves.",
    },
    "reproductions": {
        "title": "Reproductions (legacy set)",
        "file": "reproductions.csv",
        "why": "Hand-curated reproduction records carried over from earlier FLoRA work. Uses the "
               "FLoRA entry-sheet column layout, not the extracted.csv schema.",
        "action": "Merge into the main pipeline output once schemas are reconciled.",
        "encoding": "cp1252",
    },
    "pre_validation_audit": {
        "title": "Pre-Validation Audit",
        "file": "pre_validation_audit.csv",
        "why": "Per-row audit findings raised before rows are pushed to Supabase. One row per "
               "finding (check / severity / detail), so a record may appear several times.",
        "action": "Clear high-severity findings before the next csv_to_db push.",
    },
    "doi_audit_report": {
        "title": "DOI Audit Report",
        "file": "doi_audit_report.csv",
        "why": "Output of extract.audit_dois — every doi_o whose registry metadata disagreed with "
               "the extracted original, with the proposed correction.",
        "action": "Apply with `python -m extract.audit_dois --apply`.",
    },
}

_SET_PAGE_SIZE = 50

# The one column that characterises each set — rendered as the "what type are they"
# breakdown beside the row count. Everything else about a set is visible in its table.
_SET_PRIMARY_COL: dict[str, str] = {
    "pre_validation_audit": "severity",
    "doi_audit_report":     "status",
    "reproductions":        "outcome",
}
_SET_PRIMARY_DEFAULT = "type"


def _read_set(key: str) -> "pd.DataFrame | None":
    spec = SET_FILES.get(key)
    if spec is None:
        return None
    path = DATA_DIR / spec["file"]
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, encoding=spec.get("encoding", "utf-8-sig"),
                         dtype=str, on_bad_lines="skip").fillna("")
    except Exception:
        return None
    # Hand-maintained CSVs (reproductions.csv) carry hundreds of trailing commas,
    # which pandas turns into empty "Unnamed: N" columns. Named columns are kept
    # even when empty — a blank field is information; a phantom column is not.
    keep = [c for c in df.columns
            if not str(c).startswith("Unnamed:") or df[c].astype(bool).any()]
    return df[keep]


@dashboard_bp.route("/api/dashboard/set-stats")
def api_set_stats():
    """Row count plus the one breakdown that characterises this set."""
    key = request.args.get("set", "")
    if key not in SET_FILES:
        return jsonify({"error": "unknown set"}), 400

    df = _read_set(key)
    if df is None:
        return jsonify({"total": None, "missing": True, "file": SET_FILES[key]["file"]})

    col = _SET_PRIMARY_COL.get(key, _SET_PRIMARY_DEFAULT)
    primary: dict[str, int] = {}
    if col in df.columns:
        primary = {str(k): int(v) for k, v in df[col].value_counts().items() if str(k).strip()}

    return jsonify({
        "total": len(df),
        "file": SET_FILES[key]["file"],
        "columns": list(df.columns),
        "primary_col": col if primary else None,
        "primary": primary,
    })


@dashboard_bp.route("/api/dashboard/set-rows")
def api_set_rows():
    """Paginated rows for one set CSV, with a free-text search across all columns."""
    key = request.args.get("set", "")
    if key not in SET_FILES:
        return jsonify({"error": "unknown set"}), 400

    page   = max(1, int(request.args.get("page", 1)))
    search = request.args.get("search", "").strip().lower()

    df = _read_set(key)
    if df is None:
        return jsonify({"rows": [], "total": 0, "pages": 1, "page": 1, "columns": []})

    if search:
        mask = df.apply(lambda col: col.astype(str).str.lower().str.contains(
            search, regex=False, na=False)).any(axis=1)
        df = df[mask]

    total = len(df)
    pages = max(1, (total + _SET_PAGE_SIZE - 1) // _SET_PAGE_SIZE)
    page  = min(page, pages)
    start = (page - 1) * _SET_PAGE_SIZE
    return jsonify({
        "rows": df.iloc[start:start + _SET_PAGE_SIZE].to_dict("records"),
        "columns": list(df.columns),
        "total": total, "pages": pages, "page": page,
    })


@dashboard_bp.route("/api/dashboard/set-download")
def api_set_download():
    """Stream a set CSV as a download attachment."""
    key = request.args.get("set", "")
    if key not in SET_FILES:
        return jsonify({"error": "unknown set"}), 400
    src = DATA_DIR / SET_FILES[key]["file"]
    if not src.exists():
        return jsonify({"error": "file not found"}), 404
    return send_file(str(src), as_attachment=True,
                     download_name=SET_FILES[key]["file"], mimetype="text/csv")


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


# ── Supabase endpoints ────────────────────────────────────────────────────────

@dashboard_bp.route("/api/dashboard/supabase-stats")
def api_supabase_stats():
    """Validation KPIs from Supabase (cached 5 min)."""
    return jsonify(supa.get_validation_stats())


@dashboard_bp.route("/api/dashboard/supabase-outcomes")
def api_supabase_outcomes():
    """Outcome distribution from validated table."""
    return jsonify(supa.get_validated_outcomes())


@dashboard_bp.route("/api/dashboard/supabase-analytics")
def api_supabase_analytics():
    """Coverage, per-field validator agreement, and final-vs-pipeline changes."""
    return jsonify(supa.get_validation_analytics())


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
                (r"Gaps with DOI:\s*(\d+)",              "doi_gaps"),
                (r"Gaps URL-only[^:]*:\s*(\d+)",         "url_gaps"),
                (r"Fuzzy-matched[^:]*:\s*(\d+)",         "fuzzy_gaps"),
                (r"Total genuine gaps:\s*(\d+)",         "total_gaps"),
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


@dashboard_bp.route("/api/dashboard/old-pipeline-analysis")
def api_old_pipeline_analysis():
    """Comparison of old pipeline (all_replications.csv) vs new pipeline.

    Fast path: reads analysis/old_pipeline_comparison.json written by
    `python -m analysis.old_pipeline_compare`.
    Returns a stub with generation instructions if the file is missing.
    """
    try:
        from analysis.old_pipeline_compare import load_cached
        data = load_cached()
        if data is not None:
            return jsonify(data)
    except Exception:
        pass
    return jsonify({
        "error": "comparison not generated yet",
        "hint": "python -m analysis.old_pipeline_compare",
    }), 202


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
