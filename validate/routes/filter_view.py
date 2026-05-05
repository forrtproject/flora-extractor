"""
routes/filter_view.py — Read-only view of filtered.csv (Stage 2 output).

Loads data/filtered.csv; falls back to misc/sample_filtered.csv.

Routes:
  GET  /filter                  → render filter.html
  GET  /api/filter/list         → filtered summary rows as JSON
  POST /api/filter/run-stage3   → run Stage 3 extraction for selected DOIs
"""
import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from shared.config import BASE_DIR, DATA_DIR, log
from shared.utils import clean_doi

filter_view_bp = Blueprint("filter_view", __name__)

_CSV_PATH    = DATA_DIR / "filtered.csv"
_SAMPLE_PATH = BASE_DIR / "misc" / "sample_filtered.csv"


def _load_csv() -> tuple[pd.DataFrame | None, str]:
    if _CSV_PATH.exists():
        return pd.read_csv(_CSV_PATH, encoding="utf-8-sig", dtype=str,
                           on_bad_lines="skip").fillna(""), "data/filtered.csv"
    if _SAMPLE_PATH.exists():
        return pd.read_csv(_SAMPLE_PATH, encoding="utf-8-sig", dtype=str,
                           on_bad_lines="skip").fillna(""), "misc/sample_filtered.csv (sample)"
    return None, ""


@filter_view_bp.route("/filter")
def filter_page():
    df, source = _load_csv()
    return render_template("filter.html", active_page="filter",
                           source=source, total=len(df) if df is not None else 0)


@filter_view_bp.route("/api/filter/list")
def api_filter_list():
    df, _ = _load_csv()
    if df is None:
        return jsonify({"error": "No filtered.csv found. Run Stage 2 first."}), 404

    q       = request.args.get("q",       "").strip().lower()
    fstatus = request.args.get("fstatus", "all")
    fmethod = request.args.get("fmethod", "all")
    fconf   = request.args.get("fconf",   "all")

    if q:
        mask = (
            df.get("doi_r",   pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("title_r", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
        )
        df = df[mask]

    if fstatus != "all" and "filter_status" in df.columns:
        df = df[df["filter_status"] == fstatus]
    if fmethod != "all" and "filter_method" in df.columns:
        df = df[df["filter_method"] == fmethod]
    if fconf != "all" and "filter_confidence" in df.columns:
        df = df[df["filter_confidence"] == fconf]

    rows = []
    for i, r in enumerate(df.to_dict("records"), start=1):
        rows.append({
            "idx":              i,
            "doi_r":            r.get("doi_r",            ""),
            "title_r":          r.get("title_r",          ""),
            "year_r":           r.get("year_r",           ""),
            "authors_r":        r.get("authors_r",        ""),
            "journal_r":        r.get("journal_r",        ""),
            "source":           r.get("source",           ""),
            "filter_status":    r.get("filter_status",    ""),
            "filter_method":    r.get("filter_method",    ""),
            "filter_confidence":r.get("filter_confidence",""),
            "filter_evidence":  r.get("filter_evidence",  ""),
            "abstract_r":       r.get("abstract_r",       ""),
        })

    return jsonify({"rows": rows, "total": len(rows)})


@filter_view_bp.route("/api/filter/run-stage3", methods=["POST"])
def api_run_stage3():
    """
    Run Stage 3 extraction for a list of DOIs from filtered.csv.
    Appends results to data/extracted.csv (skips DOIs already present).
    """
    body = request.get_json(force=True) or {}
    dois = [clean_doi(d) for d in body.get("dois", []) if str(d).strip()]
    if not dois:
        return jsonify({"error": "no dois provided"}), 400

    df, _ = _load_csv()
    if df is None:
        return jsonify({"error": "No filtered.csv found. Run Stage 2 first."}), 404

    extracted_path = DATA_DIR / "extracted.csv"
    existing_dois: set[str] = set()
    if extracted_path.exists():
        ex = pd.read_csv(extracted_path, encoding="utf-8-sig", dtype=str,
                         on_bad_lines="skip").fillna("")
        existing_dois = set(ex.get("doi_r", pd.Series(dtype=str)).tolist())

    first_write = not extracted_path.exists()
    results: list[dict] = []

    for doi in dois:
        if doi in existing_dois:
            results.append({"doi": doi, "status": "skipped",
                            "reason": "already in extracted.csv"})
            continue

        mask = df["doi_r"].apply(clean_doi) == doi
        if not mask.any():
            results.append({"doi": doi, "status": "error",
                            "reason": "not found in filtered.csv"})
            continue

        row = df[mask].iloc[0]
        try:
            from extract.run_extract import (
                classify_match_type, _build_cands_df, _build_rep_df,
                _merge_row, _merge_multi_row, _empty_row, _append_row,
                _get_outcome, _parse_originals,
            )
            from extract.link_original import run_for_doi
            from extract.multi_original import run_multi_original_for_doi

            match      = classify_match_type(row.to_dict())
            match_type = match["original_match_type"]
            match_conf = match["original_match_confidence"]
            log.info("[%s] filter/run-stage3: match_type=%s", doi, match_type)

            out_rows: list[dict] = []
            if match_type == "multiple_original":
                result    = run_multi_original_for_doi(doi, _build_rep_df(row))
                originals = _parse_originals(result)
                if result.get("is_false_positive") or not originals:
                    link    = run_for_doi(doi, cands_df=_build_cands_df(row))
                    outcome = _get_outcome(doi, row, link)
                    out_rows.append(
                        _merge_row(row, link, outcome, "single_original", match_conf, 1, 1)
                    )
                else:
                    for orig in originals:
                        outcome = _get_outcome(doi, row, {})
                        out_rows.append(
                            _merge_multi_row(row, orig, outcome, match_type,
                                            match_conf, len(originals))
                        )
            else:
                link    = run_for_doi(doi, cands_df=_build_cands_df(row))
                outcome = _get_outcome(doi, row, link)
                out_rows.append(
                    _merge_row(row, link, outcome, match_type, match_conf, 1, 1)
                )

            for rrow in out_rows:
                _append_row(extracted_path, rrow, first=first_write)
                first_write = False
            existing_dois.add(doi)

            results.append({
                "doi":        doi,
                "status":     "done",
                "n_rows":     len(out_rows),
                "match_type": match_type,
                "outcome":    out_rows[0].get("outcome", "") if out_rows else "",
            })
        except Exception as e:
            log.error("[%s] filter/run-stage3 failed: %s", doi, e)
            results.append({"doi": doi, "status": "error", "reason": str(e)})

    return jsonify({"results": results})
