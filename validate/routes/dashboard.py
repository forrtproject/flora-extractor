"""
routes/dashboard.py — Read-only monitoring dashboard.

Routes:
  GET  /dashboard                        → dashboard page
  GET  /api/dashboard/csv-stats          → pipeline stats (Parquet→stats.json→CSV cascade)
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
import pyarrow.parquet as pq
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import DATA_DIR, BASE_DIR
from shared import supabase_client as supa
from shared.dashboard_cache import DASHBOARD_DIR, load_stats

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
    return render_template("dashboard.html", active_page="dashboard")



def _stats_json_to_api(sj: dict) -> "dict | None":
    """Translate stats.json to the flat dict the dashboard JS expects.

    Returns None if the json is missing any required stage key, so the
    caller can fall back to the CSV read path.
    """
    c  = sj.get("candidates")
    f  = sj.get("filtered")
    e  = sj.get("extracted")
    et = sj.get("extracted_test")
    if not (c and f and e and et):
        return None

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
        "_source": "stats_json",
        "_updated_at": sj.get("updated_at"),
    }
    return out


def _read_parquet_or_csv(stage: str, cols: list[str]) -> "pd.DataFrame | None":
    """Read only the listed columns from Parquet if it exists, else from CSV."""
    pq_path = DASHBOARD_DIR / f"{stage}.parquet"
    if pq_path.exists():
        try:
            existing = pq.read_schema(pq_path).names
            read_cols = [c for c in cols if c in existing]
            return pq.read_table(pq_path, columns=read_cols).to_pandas().fillna("")
        except Exception:
            pass
    csv_path = DATA_DIR / (f"{stage}.csv" if stage != "extracted-test" else "extracted-test.csv")
    if csv_path.exists():
        try:
            return pd.read_csv(
                csv_path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
                usecols=lambda c: c in cols,
            ).fillna("")
        except Exception:
            pass
    return None


@dashboard_bp.route("/api/dashboard/csv-stats")
def api_csv_stats():
    """Return pipeline stats. Fast path: stats.json; mid path: Parquet; slow: CSV."""
    # ── Fast path: stats.json written by pipeline runners ─────────────────────
    sj = load_stats()
    if sj:
        api_out = _stats_json_to_api(sj)
        if api_out is not None:
            return jsonify(api_out)

    # ── Slow path: read from Parquet or CSV ────────────────────────────────────
    stats: dict = {"_source": "csv"}

    # Candidates
    cdf = _read_parquet_or_csv("candidates", ["doi_r", "url_r", "abstract_r", "openalex_id_r", "source"])
    if cdf is not None:
        stats["candidates_count"] = len(cdf)
        doi_c = cdf["doi_r"].fillna("") if "doi_r" in cdf.columns else pd.Series([""] * len(cdf))
        url_c = cdf["url_r"].fillna("") if "url_r" in cdf.columns else pd.Series([""] * len(cdf))
        abs_c = cdf["abstract_r"].fillna("") if "abstract_r" in cdf.columns else pd.Series([""] * len(cdf))
        stats["candidates_no_doi"]        = int((doi_c == "").sum())
        stats["candidates_no_doi_or_url"] = int(((doi_c == "") & (url_c == "")).sum())
        stats["candidates_no_abstract"]   = int((abs_c == "").sum())
        if "source" in cdf.columns:
            stats["candidates_source"] = {
                k: int(v) for k, v in cdf["source"].fillna("unknown").value_counts().items()
            }
        else:
            stats["candidates_source"] = {}
    else:
        stats["candidates_count"] = None
        stats.update(candidates_no_doi=0, candidates_no_doi_or_url=0,
                     candidates_no_abstract=0, candidates_source={})

    # Filtered
    fdf = _read_parquet_or_csv("filtered", ["doi_r", "url_r", "abstract_r", "filter_status"])
    if fdf is not None:
        stats["filtered_count"] = len(fdf)
        if "filter_status" in fdf.columns:
            vc = fdf["filter_status"].value_counts().to_dict()
            stats["filter_replication"]    = int(vc.get("replication",    0))
            stats["filter_reproduction"]   = int(vc.get("reproduction",   0))
            stats["filter_false_positive"] = int(vc.get("false_positive", 0))
            stats["filter_needs_review"]   = int(vc.get("needs_review",   0))
            # Data quality for rep+repro subset
            rr = fdf[fdf["filter_status"].isin(["replication", "reproduction"])]
            doi_f = rr["doi_r"].fillna("") if "doi_r" in rr.columns else pd.Series([""] * len(rr))
            url_f = rr["url_r"].fillna("") if "url_r" in rr.columns else pd.Series([""] * len(rr))
            abs_f = rr["abstract_r"].fillna("") if "abstract_r" in rr.columns else pd.Series([""] * len(rr))
            stats["filter_rep_repro_total"]   = len(rr)
            stats["filter_no_doi"]            = int((doi_f == "").sum())
            stats["filter_no_doi_or_url"]     = int(((doi_f == "") & (url_f == "")).sum())
            stats["filter_no_abstract"]       = int((abs_f == "").sum())
        else:
            stats.update(filter_replication=0, filter_reproduction=0,
                         filter_false_positive=0, filter_needs_review=0,
                         filter_rep_repro_total=0, filter_no_doi=0,
                         filter_no_doi_or_url=0, filter_no_abstract=0)
    else:
        stats["filtered_count"] = None
        stats.update(filter_replication=0, filter_reproduction=0,
                     filter_false_positive=0, filter_needs_review=0,
                     filter_rep_repro_total=0, filter_no_doi=0,
                     filter_no_doi_or_url=0, filter_no_abstract=0)

    # Extracted + Extracted Test
    for prefix, stage in [("", "extracted"), ("test_", "extracted-test")]:
        _add_extracted_stats(stats, prefix,
                             DATA_DIR / f"{stage}.csv",
                             pq_path=DASHBOARD_DIR / f"{stage}.parquet")

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


def _add_extracted_stats(stats: dict, prefix: str, path,
                         pq_path=None) -> None:
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

    _NEED = ("doi_r", "link_method", "link_llm_model", "original_match_type", "outcome")
    try:
        # Try Parquet first if path provided
        edf: pd.DataFrame | None = None
        if pq_path is not None and pq_path.exists():
            try:
                existing = pq.read_schema(pq_path).names
                read_cols = [c for c in _NEED if c in existing]
                edf = pq.read_table(pq_path, columns=read_cols).to_pandas().fillna("")
            except Exception:
                edf = None

        if edf is None:
            edf = pd.read_csv(
                path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
                usecols=lambda c: c in _NEED,
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
            # Coarse tile aggregates all rule-based resolution methods.
            stats[f"{prefix}method_author_year_match"] = int(
                sum(vc.get(k, 0) for k in _RULE_METHOD_KEYS)
            )
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
