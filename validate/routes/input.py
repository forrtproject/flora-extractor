"""
routes/input.py — Input data generation for both pipelines.

Routes:
  GET  /input                          → Input page
  GET  /api/input/counts               → current row counts for both pipelines
  POST /api/input/generate-matches     → filter FLoRA+OpenAlex → multiple_match_candidates.csv
  POST /api/input/generate-originals   → sample all_replications where multi_target=True AND validation_status in ('llm_confirmed','needs_review')
  GET  /api/input/download-matches     → send multiple_match_candidates.csv
  GET  /api/input/download-originals   → send multi_original_candidates.csv
"""
import random

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from validate import state
from shared.config import (
    ALL_REPLICATIONS_PATH, FILTERED_CSV_PATH, FLORA_SHEET_PATH,
    MULTI_ORIG_CANDS_PATH, OPENALEX_CANDS_PATH, log,
)
from shared.utils import clean_doi

input_bp = Blueprint("input", __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_csv(path, label):
    if not path.exists():
        log.warning("%s not found at %s", label, path)
        return pd.DataFrame()
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Loaded %s: %d rows", label, len(df))
    return df


# ── Page route ────────────────────────────────────────────────────────────────

@input_bp.route("/input")
def input_page():
    return render_template("input.html", active_page="input")


# ── API: counts ───────────────────────────────────────────────────────────────

@input_bp.route("/api/input/counts")
def api_counts():
    return jsonify({
        "matches_count"  : len(state.filtered_df),
        "originals_count": len(state.multi_orig_df),
    })


# ── API: generate matches input (pipeline 1) ──────────────────────────────────

@input_bp.route("/api/input/generate-matches", methods=["POST"])
def api_generate_matches():
    """Reload FLoRA sheet + OpenAlex candidates, re-filter for multiple-match cases."""
    state.flora_df = _load_csv(FLORA_SHEET_PATH,    "FLoRA entry sheet")
    state.cands_df = _load_csv(OPENALEX_CANDS_PATH, "openalex_candidates")

    if "doi_r" in state.flora_df.columns:
        state.flora_df["doi_r"] = state.flora_df["doi_r"].apply(clean_doi)
    if "doi_r" in state.cands_df.columns:
        state.cands_df["doi_r"] = state.cands_df["doi_r"].apply(clean_doi)

    if state.flora_df.empty or state.cands_df.empty:
        return jsonify({"error": "source CSV not found", "count": 0})

    if "prep_notes" in state.flora_df.columns:
        flora_dois = set(
            state.flora_df[
                state.flora_df["prep_notes"].str.startswith("openalex:", na=False)
            ]["doi_r"]
        ) - {""}
    else:
        flora_dois = set()

    multi_mask = (
        state.cands_df["doi_r"].isin(flora_dois)
        & (state.cands_df["match_source"] == "openalex_references")
        & (state.cands_df["match_status"] == "multiple_matches")
    )
    state.filtered_df = state.cands_df[multi_mask].copy().reset_index(drop=True)

    if not state.filtered_df.empty:
        state.filtered_df.to_csv(FILTERED_CSV_PATH, index=False, encoding="utf-8-sig")

    log.info("Input generate-matches: %d candidates", len(state.filtered_df))
    return jsonify({"count": len(state.filtered_df)})


# ── API: generate originals input (pipeline 2) ────────────────────────────────

@input_bp.route("/api/input/generate-originals", methods=["POST"])
def api_generate_originals():
    """
    Sample N unique random doi_r's from all_replications.csv where multi_target=True
    AND validation_status is 'llm_confirmed' or 'needs_review'.
    N is passed in request body as {"threshold": 20}.
    """
    body      = request.get_json(force=True) or {}
    threshold = int(body.get("threshold", 20))
    threshold = max(1, min(threshold, 500))   # clamp to sensible range

    # Load source if not already in state
    if state.all_rep_df.empty:
        state.all_rep_df = _load_csv(ALL_REPLICATIONS_PATH, "all_replications")

    if state.all_rep_df.empty:
        return jsonify({"error": "all_replications.csv not found", "count": 0})

    df = state.all_rep_df.copy()
    if "doi_r" in df.columns:
        df["doi_r"] = df["doi_r"].apply(clean_doi)

    if "multi_target" not in df.columns:
        return jsonify({"error": "multi_target column not found", "count": 0})

    if "validation_status" not in df.columns:
        return jsonify({"error": "validation_status column not found", "count": 0})

    # Filter multi_target==True AND validation_status is plausibly a replication
    mask = (
        df["multi_target"].apply(
            lambda v: str(v).strip().lower() in ("true", "1", "yes")
        )
        & df["validation_status"].isin(["llm_confirmed", "needs_review"])
    )
    multi_df = df[mask].copy()

    # Unique doi_r's only
    multi_df = multi_df.drop_duplicates(subset=["doi_r"]).reset_index(drop=True)

    # Random sample
    if len(multi_df) > threshold:
        multi_df = multi_df.sample(n=threshold, random_state=None).reset_index(drop=True)

    state.multi_orig_df = multi_df

    if not multi_df.empty:
        multi_df.to_csv(MULTI_ORIG_CANDS_PATH, index=False, encoding="utf-8-sig")

    log.info("Input generate-originals: %d candidates (threshold=%d)",
             len(multi_df), threshold)
    return jsonify({
        "count"         : len(multi_df),
        "total_available": int(mask.sum()),
        "threshold"     : threshold,
    })


# ── API: download CSVs ────────────────────────────────────────────────────────

@input_bp.route("/api/input/download-matches")
def api_download_matches():
    if not FILTERED_CSV_PATH.exists():
        return jsonify({"error": "matches CSV not generated yet"}), 404
    return send_file(str(FILTERED_CSV_PATH), as_attachment=True,
                     download_name="multiple_match_candidates.csv",
                     mimetype="text/csv")


@input_bp.route("/api/input/download-originals")
def api_download_originals():
    if not MULTI_ORIG_CANDS_PATH.exists():
        return jsonify({"error": "originals CSV not generated yet"}), 404
    return send_file(str(MULTI_ORIG_CANDS_PATH), as_attachment=True,
                     download_name="multi_original_candidates.csv",
                     mimetype="text/csv")
