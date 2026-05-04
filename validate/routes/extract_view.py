"""
routes/extract_view.py — Read-only view of extracted.csv (Stage 3 output) plus
                          a single-DOI re-run endpoint for model comparison.

Loads data/extracted.csv; falls back to misc/sample_extracted.csv.

Routes:
  GET  /extract                       → render extract.html
  GET  /api/extract/list              → filtered summary rows as JSON
  GET  /api/extract/detail            → full detail for one (doi_r, original_rank)
                                        enriched with LLM/GROBID caches
  POST /api/extract/run-doi           → run Stage 3 link step for one DOI
                                        with a selectable model (no cache write)
"""
import json
import urllib.request
import urllib.error
from pathlib import Path

import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from shared.config import BASE_DIR, DATA_DIR, GROBID_CACHE_DIR, LLM_CACHE_DIR, OA_CACHE_DIR, RESEARCHER_EMAIL
from shared.utils import cache_key, clean_doi

extract_view_bp = Blueprint("extract_view", __name__)

_CSV_PATH    = DATA_DIR / "extracted.csv"
_SAMPLE_PATH = BASE_DIR / "misc" / "sample_extracted.csv"


_FILTERED_CSV_PATH    = DATA_DIR / "filtered.csv"
_CANDIDATES_CSV_PATH  = DATA_DIR / "candidates.csv"


def _load_csv() -> tuple[pd.DataFrame | None, str]:
    """Return (dataframe, source_label). Tries real CSV first, falls back to sample.

    Also fills in missing title_r values from filtered.csv or candidates.csv
    (which may carry the title as study_r) so existing extracted.csv rows
    produced before the study_r→title_r fix are still displayed correctly.
    """
    if _CSV_PATH.exists():
        df = pd.read_csv(_CSV_PATH, encoding="utf-8-sig", dtype=str,
                         on_bad_lines="skip").fillna("")
        df = _fill_titles(df)
        return df, "data/extracted.csv"
    if _SAMPLE_PATH.exists():
        return pd.read_csv(_SAMPLE_PATH, encoding="utf-8-sig", dtype=str,
                           on_bad_lines="skip").fillna(""), "misc/sample_extracted.csv (sample)"
    return None, ""


def _fill_titles(df: pd.DataFrame) -> pd.DataFrame:
    """
    Back-fill empty title_r in extracted.csv from filtered.csv or candidates.csv.
    Those CSVs may store the replication title as study_r (old seeded data format).
    """
    missing_mask = df.get("title_r", pd.Series([""] * len(df))) == ""
    if not missing_mask.any():
        return df

    for src_path in (_FILTERED_CSV_PATH, _CANDIDATES_CSV_PATH):
        if not src_path.exists() or not missing_mask.any():
            break
        try:
            src = pd.read_csv(src_path, encoding="utf-8-sig", dtype=str,
                              on_bad_lines="skip", usecols=lambda c: c in
                              ("doi_r", "title_r", "study_r")).fillna("")
        except Exception:
            continue

        # build doi_r → title lookup from source CSV
        title_lookup: dict[str, str] = {}
        for _, r in src.iterrows():
            t = r.get("title_r", "") or r.get("study_r", "")
            if t and r.get("doi_r"):
                title_lookup[r["doi_r"]] = t

        def _lookup(row):
            if row.get("title_r", ""):
                return row["title_r"]
            return title_lookup.get(row.get("doi_r", ""), "")

        df["title_r"] = df.apply(_lookup, axis=1)
        missing_mask = df["title_r"] == ""

    return df


def _read_json_cache(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_json_list(path: Path) -> list:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _enrich_detail(row: dict) -> dict:
    """Augment a CSV row with data from LLM and GROBID cache files."""
    doi_r = clean_doi(str(row.get("doi_r", "")))
    key   = cache_key(doi_r)

    llm   = _read_json_cache(LLM_CACHE_DIR / f"llm_{key}.json")
    outcome_cache = _read_json_cache(LLM_CACHE_DIR / f"outcome_{cache_key(doi_r)}.json")
    match_cache   = _read_json_cache(
        LLM_CACHE_DIR / f"match_type_{cache_key(doi_r + '_match_type')}.json"
    )
    grobid = _read_json_cache(GROBID_CACHE_DIR / f"{key}.json")
    oa_candidates = _read_json_list(OA_CACHE_DIR / f"candidates_{key}.json")

    # Pull fields that are stored in caches but not in extracted.csv
    enriched = dict(row)
    enriched.update({
        # LLM identification
        "llm_prompt":     llm.get("llm_prompt",    ""),
        "llm_source":     llm.get("llm_source",    row.get("link_method", "")),
        "llm_confidence": llm.get("llm_confidence",""),
        "llm_evidence":   llm.get("llm_evidence",  row.get("link_evidence", "")),
        "llm_reasoning":  llm.get("llm_reasoning", ""),
        "llm_error":      llm.get("llm_error",      ""),
        "resolution_score": llm.get("resolution_score", ""),
        # Outcome LLM
        "outcome_llm_prompt": outcome_cache.get("llm_prompt", ""),
        # Match-type classification
        "match_reasoning": match_cache.get("reasoning", ""),
        # GROBID / PDF section extraction
        "grobid_status":   grobid.get("status",    ""),
        "n_grobid_refs":   grobid.get("n_refs",    len(grobid.get("refs", []))),
        "grobid_abstract": grobid.get("abstract",  ""),
        "grobid_intro":    grobid.get("intro",     ""),
        "grobid_methods":  grobid.get("methods",   ""),
        "grobid_refs":     grobid.get("refs",      [])[:30],
        # OpenAlex candidate pool
        "oa_candidates":   oa_candidates,
        "n_oa_candidates": len(oa_candidates),
    })

    # title fallback — extracted.csv may use study_r in some legacy rows
    if not enriched.get("title_r"):
        enriched["title_r"] = row.get("study_r", "")

    return enriched


# ── Routes ────────────────────────────────────────────────────────────────────

@extract_view_bp.route("/extract")
def extract_page():
    df, source = _load_csv()
    return render_template("extract.html", active_page="extract",
                           source=source, total=len(df) if df is not None else 0)


@extract_view_bp.route("/api/extract/list")
def api_list():
    df, _ = _load_csv()
    if df is None:
        return jsonify({"error": "No extracted.csv found. Run Stage 3 first."}), 404

    q       = request.args.get("q",       "").strip().lower()
    outcome = request.args.get("outcome", "all")
    method  = request.args.get("method",  "all")
    fstatus = request.args.get("fstatus", "all")
    mtype   = request.args.get("mtype",   "all")

    if q:
        mask = (
            df.get("doi_r",   pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("title_r", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            | df.get("title_o", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
        )
        df = df[mask]

    if outcome != "all" and "outcome" in df.columns:
        df = df[df["outcome"] == outcome]
    if method != "all" and "link_method" in df.columns:
        df = df[df["link_method"] == method]
    if fstatus != "all" and "filter_status" in df.columns:
        df = df[df["filter_status"] == fstatus]
    if mtype != "all" and "original_match_type" in df.columns:
        df = df[df["original_match_type"] == mtype]

    rows = []
    for i, r in enumerate(df.to_dict("records"), start=1):
        title = r.get("title_r", "") or r.get("study_r", "")
        rows.append({
            "idx":           i,
            "doi_r":         r.get("doi_r",         ""),
            "title_r":       title,
            "year_r":        r.get("year_r",        ""),
            "filter_status": r.get("filter_status", ""),
            "type":          r.get("type",          ""),
            "link_method":   r.get("link_method",   ""),
            "outcome":       r.get("outcome",       ""),
            "title_o":       r.get("title_o",       ""),
            "original_rank": r.get("original_rank", "1"),
            "n_originals":   r.get("n_originals",   "1"),
            "match_type":    r.get("original_match_type", ""),
            "link_confidence": r.get("link_confidence", ""),
        })

    return jsonify({"rows": rows, "total": len(rows)})


@extract_view_bp.route("/api/extract/detail")
def api_detail():
    doi  = request.args.get("doi",  "").strip()
    rank = request.args.get("rank", "1").strip()

    if not doi:
        return jsonify({"error": "missing doi parameter"}), 400

    df, _ = _load_csv()
    if df is None:
        return jsonify({"error": "No extracted.csv found"}), 404

    matches = df[df["doi_r"] == doi]
    if "original_rank" in df.columns and not matches.empty:
        ranked = matches[matches["original_rank"] == rank]
        if not ranked.empty:
            matches = ranked

    if matches.empty:
        return jsonify({"error": "doi not found"}), 404

    row = matches.iloc[0].to_dict()
    return jsonify(_enrich_detail(row))


@extract_view_bp.route("/api/extract/run-doi", methods=["POST"])
def api_run_doi():
    """
    Run the Stage 3 LLM identification step for one DOI with a selectable model.

    Reads context from extracted.csv + caches (no new API calls for PDF/GROBID).
    Does NOT update extracted.csv — intended for model comparison only.

    Body JSON:
      { "doi": "10.xxx/yyy", "model": "gemini-2.5-flash-lite-preview-06-17" }

    model routing:
      starts with "gemini"      → call_gemini(prompt, model=model)
      starts with "gpt" / "o1"  → call_openai(prompt, model=model)
      anything else             → call_openrouter(prompt, model=model)
    """
    data  = request.get_json(force=True) or {}
    doi   = clean_doi(data.get("doi", "").strip())
    model = data.get("model", "").strip()

    if not doi:
        return jsonify({"error": "missing doi"}), 400
    if not model:
        return jsonify({"error": "missing model"}), 400

    df, _ = _load_csv()
    if df is None:
        return jsonify({"error": "No extracted.csv found"}), 404

    matches = df[df["doi_r"] == doi]
    if matches.empty:
        return jsonify({"error": f"DOI not found in extracted.csv: {doi}"}), 404

    row = matches.iloc[0].to_dict()
    abstract_r = str(row.get("abstract_r", ""))
    title_r    = str(row.get("title_r", "") or row.get("study_r", ""))
    year_r_str = str(row.get("year_r", ""))
    oa_id_r    = str(row.get("openalex_id_r", ""))

    try:
        year_r = int(year_r_str) if year_r_str else 2099
    except ValueError:
        year_r = 2099

    # Get candidates from OpenAlex cache (does not make new API calls if cached)
    try:
        from shared.openalex_client import find_all_candidates, extract_author_year_patterns
        candidates = find_all_candidates(doi, oa_id_r, title_r, abstract_r, year_r, "")
        patterns   = extract_author_year_patterns(abstract_r, max_year=year_r)
        pattern    = "; ".join(f"{p['surname']} ({p['year']})" for p in patterns[:5])
    except Exception as e:
        candidates = []
        pattern    = ""

    # Get GROBID sections from cache
    key    = cache_key(doi)
    grobid = _read_json_cache(GROBID_CACHE_DIR / f"{key}.json")
    sections = {
        "intro":      grobid.get("intro",    ""),
        "methods":    grobid.get("methods",  ""),
        "references": grobid.get("refs",     []),
    }

    # Build prompt
    from shared.llm_client import build_identification_prompt
    prompt = build_identification_prompt(
        title_r, abstract_r, pattern, candidates, sections
    )

    # Call the selected model
    result, error = None, ""
    model_lower = model.lower()
    if model_lower.startswith("gemini"):
        from shared.llm_client import call_gemini
        result, error = call_gemini(prompt, model=model)
    elif model_lower.startswith(("gpt-", "o1", "o3", "o4")):
        from shared.llm_client import call_openai
        result, error = call_openai(prompt, model=model)
    else:
        from shared.llm_client import call_openrouter
        result, error = call_openrouter(prompt, model=model)

    return jsonify({
        "doi":         doi,
        "model":       model,
        "prompt":      prompt,
        "result":      result,
        "error":       error,
        "n_candidates": len(candidates),
        "n_refs":      len(sections.get("references", [])),
    })


@extract_view_bp.route("/api/crossref/lookup")
def api_crossref_lookup():
    """
    Fetch paper metadata from Crossref (fallback: doi.org) for a given DOI.

    GET /api/crossref/lookup?doi=10.xxxx/yyyy

    Returns: { title, authors, year, journal, doi, url, volume, issue, pages }
    """
    doi = clean_doi(request.args.get("doi", "").strip())
    if not doi:
        return jsonify({"error": "missing doi"}), 400

    headers = {
        "User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})",
        "Accept": "application/json",
    }

    def _crossref(doi: str) -> dict:
        url = f"https://api.crossref.org/works/{urllib.request.quote(doi, safe='')}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            msg = json.load(r).get("message", {})
        authors_raw = msg.get("author", [])
        authors = ", ".join(
            f"{a.get('family', '')}, {a.get('given', '')[:1]}."
            for a in authors_raw[:6] if a.get("family")
        )
        year = ""
        for field in ("published-print", "published-online", "issued"):
            parts = msg.get(field, {}).get("date-parts", [[""]])
            if parts and parts[0] and parts[0][0]:
                year = str(parts[0][0])
                break
        return {
            "title":   (msg.get("title",            [""])[0] or ""),
            "authors": authors,
            "year":    year,
            "journal": (msg.get("container-title",  [""])[0] or ""),
            "volume":  str(msg.get("volume",         "") or ""),
            "issue":   str(msg.get("issue",          "") or ""),
            "pages":   str(msg.get("page",           "") or ""),
            "doi":     msg.get("DOI", doi),
            "url":     msg.get("URL", f"https://doi.org/{doi}"),
            "source":  "crossref",
        }

    try:
        return jsonify(_crossref(doi))
    except Exception as e:
        return jsonify({"error": str(e), "doi": doi}), 502
