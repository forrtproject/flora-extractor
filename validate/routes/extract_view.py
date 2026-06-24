"""
routes/extract_view.py — Factory for Extract and Extract Test blueprint views.

Exports:
  make_extract_blueprint(name, csv_path, url_prefix, active_page, test_mode=False)
  add_shared_routes(bp)   — crossref lookup + PDF serve; call on main blueprint only

Routes registered per blueprint:
  GET  <url_prefix>                    → render extract.html
  GET  /api<url_prefix>/list           → filtered summary rows as JSON
  GET  /api<url_prefix>/detail         → full detail for one (doi_r, original_rank)
  POST /api<url_prefix>/run-doi        → model comparison (no cache write)
  POST /api<url_prefix>/rerun-doi      → full pipeline rerun, writes to this CSV
  POST /api<url_prefix>/promote        → (test_mode only) promote row(s) to extracted.csv

Shared routes (registered on main blueprint only via add_shared_routes):
  GET  /api/crossref/lookup
  GET  /api/pdf/<doi>
"""
import json
import urllib.request
import urllib.error
from pathlib import Path

import pandas as pd
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import (
    BASE_DIR, DATA_DIR, GROBID_CACHE_DIR, LLM_CACHE_DIR,
    OA_CACHE_DIR, PARSE_CACHE_DIR, PDF_CACHE_DIR, RESEARCHER_EMAIL,
)
from shared.utils import cache_key, clean_doi


# ── Module-level helpers (don't depend on csv_path) ──────────────────────────

def _fill_titles(df: pd.DataFrame,
                 filtered_path: Path,
                 candidates_path: Path) -> pd.DataFrame:
    """Back-fill empty title_r from filtered.csv or candidates.csv."""
    missing_mask = df.get("title_r", pd.Series([""] * len(df))) == ""
    if not missing_mask.any():
        return df
    for src_path in (filtered_path, candidates_path):
        if not src_path.exists() or not missing_mask.any():
            break
        try:
            src = pd.read_csv(
                src_path, encoding="utf-8-sig", dtype=str, on_bad_lines="skip",
                usecols=lambda c: c in ("doi_r", "title_r", "study_r"),
            ).fillna("")
        except Exception:
            continue
        title_lookup: dict[str, str] = {}
        for _, r in src.iterrows():
            t = r.get("title_r", "") or r.get("study_r", "")
            if t and r.get("doi_r"):
                title_lookup[r["doi_r"]] = t
        def _lookup(row, tl=title_lookup):
            if row.get("title_r", ""):
                return row["title_r"]
            return tl.get(row.get("doi_r", ""), "")
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
    """Augment a CSV row with data from LLM, GROBID, and parse cache files."""
    doi_r = clean_doi(str(row.get("doi_r", "")))
    key   = cache_key(doi_r)

    llm           = _read_json_cache(LLM_CACHE_DIR / f"llm_{key}.json")
    outcome_cache = _read_json_cache(LLM_CACHE_DIR / f"outcome_{cache_key(doi_r)}.json")
    match_cache   = _read_json_cache(
        LLM_CACHE_DIR / f"match_type_{cache_key(doi_r + '_match_type')}.json"
    )
    grobid        = _read_json_cache(GROBID_CACHE_DIR / f"{key}.json")
    oa_candidates = _read_json_list(OA_CACHE_DIR / f"candidates_{key}.json")
    parse_cache_path = PARSE_CACHE_DIR / f"parse_{key}.json"
    parse_results    = _read_json_cache(parse_cache_path)
    has_pdf          = (PDF_CACHE_DIR / f"{key}.pdf").exists()

    # If parse cache exists but is missing "markitdown" (added after the cache was written),
    # run MarkItDown now and update the cache file so future opens are instant.
    if parse_results and "markitdown" not in parse_results and has_pdf:
        pdf_path = PDF_CACHE_DIR / f"{key}.pdf"
        try:
            from shared.pdf_parsing import parse_markitdown
            md_result = parse_markitdown(pdf_path, doi_r=doi_r)
            parse_results["markitdown"] = md_result
            with parse_cache_path.open("w", encoding="utf-8") as _fh:
                json.dump(parse_results, _fh, ensure_ascii=False, indent=2)
        except Exception:
            pass  # non-fatal — UI will still show other methods

    # Compute the winning parse method and its score for display in the detail panel.
    best_method = ""
    parse_scores: dict[str, int] = {}
    if parse_results:
        try:
            from shared.pdf_parsing import score_parse_result, best_parse_method_name
            parse_scores = {k: score_parse_result(v) for k, v in parse_results.items()}
            best_method  = best_parse_method_name(parse_results)
        except Exception:
            pass

    enriched = dict(row)
    enriched.update({
        "llm_prompt":     llm.get("llm_prompt",    ""),
        "llm_response":   llm.get("llm_response",  ""),
        "llm_source":     llm.get("llm_source",    row.get("link_method", "")),
        "llm_model":      llm.get("llm_model",     row.get("link_llm_model", "")),
        "llm_confidence": llm.get("llm_confidence",""),
        "llm_evidence":   llm.get("llm_evidence",  row.get("link_evidence", "")),
        "llm_reasoning":  llm.get("llm_reasoning", ""),
        "llm_error":      llm.get("llm_error",      ""),
        "resolution_score": llm.get("resolution_score", ""),
        "outcome_llm_prompt":   outcome_cache.get("llm_prompt",   ""),
        "outcome_llm_response": outcome_cache.get("llm_response", ""),
        "outcome_llm_model":    outcome_cache.get("llm_model",    ""),
        "match_reasoning":    match_cache.get("reasoning",         ""),
        "classify_llm_model": match_cache.get("classify_llm_model",""),
        "grobid_status":   grobid.get("status",    ""),
        "n_grobid_refs":   grobid.get("n_refs",    len(grobid.get("refs", []))),
        "grobid_abstract": grobid.get("abstract",  ""),
        "grobid_intro":    grobid.get("intro",     ""),
        "grobid_methods":  grobid.get("methods",   ""),
        "grobid_refs":     grobid.get("refs",      [])[:30],
        "oa_candidates":   oa_candidates,
        "n_oa_candidates": len(oa_candidates),
        "parse_results":    parse_results,
        "parse_scores":     parse_scores,
        "best_parse_source": best_method,
        "has_pdf":          has_pdf,
        "pdf_cache_key":    key,
    })
    if not enriched.get("title_r"):
        enriched["title_r"] = row.get("study_r", "")
    return enriched


def _pdf_status(doi_r: str, link_method: str) -> str:
    """Return 'available', 'not_available', or 'not_needed' for the PDF column."""
    if link_method in {"author_year_match", "llm_abstract", "no_original_found"}:
        return "not_needed"
    key = cache_key(clean_doi(doi_r))
    return "available" if (PDF_CACHE_DIR / f"{key}.pdf").exists() else "not_available"


def _call_model(prompt: str, model: str) -> tuple:
    """Route a prompt to the right provider based on model name prefix."""
    model_lower = model.lower()
    if model_lower.startswith("gemini"):
        from shared.llm_client import call_gemini
        return call_gemini(prompt, model=model)
    if model_lower.startswith(("gpt-", "o1", "o3", "o4")):
        from shared.llm_client import call_openai
        return call_openai(prompt, model=model)
    from shared.llm_client import call_openrouter
    return call_openrouter(prompt, model=model)


def _handle_run_doi(req, load_csv_fn):
    """
    Model-comparison run — reads context from CSV + caches, does NOT write.
    Body JSON: { "doi": "...", "model": "..." }
    """
    data  = req.get_json(force=True) or {}
    doi   = clean_doi(data.get("doi", "").strip())
    model = data.get("model", "").strip()

    if not doi:
        return jsonify({"error": "missing doi"}), 400
    if not model:
        return jsonify({"error": "missing model"}), 400

    df, _ = load_csv_fn()
    if df is None:
        return jsonify({"error": "No CSV found"}), 404

    matches = df[df["doi_r"] == doi]
    if matches.empty:
        return jsonify({"error": f"DOI not found: {doi}"}), 404

    row        = matches.iloc[0].to_dict()
    abstract_r = str(row.get("abstract_r", ""))
    title_r    = str(row.get("title_r", "") or row.get("study_r", ""))
    year_r_str = str(row.get("year_r", ""))
    oa_id_r    = str(row.get("openalex_id_r", ""))

    try:
        year_r = int(year_r_str) if year_r_str else 2099
    except ValueError:
        year_r = 2099

    try:
        from shared.openalex_client import find_all_candidates, extract_author_year_patterns
        candidates = find_all_candidates(doi, oa_id_r, title_r, abstract_r, year_r, "")
        patterns   = extract_author_year_patterns(abstract_r, max_year=year_r)
        pattern    = "; ".join(f"{p['surname']} ({p['year']})" for p in patterns[:5])
    except Exception:
        candidates = []
        pattern    = ""
        patterns   = []

    key         = cache_key(doi)
    parse_cache = _read_json_cache(PARSE_CACHE_DIR / f"parse_{key}.json")
    if parse_cache:
        def _score(r: dict) -> int:
            if r.get("error"):
                return -1
            return (len(r.get("references") or []) * 500
                    + len(r.get("abstract") or "")
                    + len(r.get("intro") or ""))
        valid    = [r for r in parse_cache.values() if _score(r) >= 0]
        best     = max(valid, key=_score) if valid else {}
        sections = {
            "intro":      best.get("intro",      ""),
            "methods":    "",
            "references": best.get("references", []),
        }
    else:
        grobid   = _read_json_cache(GROBID_CACHE_DIR / f"{key}.json")
        sections = {
            "intro":      grobid.get("intro",   ""),
            "methods":    grobid.get("methods", ""),
            "references": grobid.get("refs",    []),
        }

    html_text      = str(row.get("html_text", "") or "")
    distinct_pairs = {(p["surname"], p["year"]) for p in patterns}
    abstract_snip  = (abstract_r[:800] + "…") if len(abstract_r) > 800 else abstract_r
    pattern_lines  = "\n".join(f"- {s} ({y})" for s, y in sorted(distinct_pairs)) or "(none found)"
    cand_lines     = "\n".join(
        f"{i+1}. \"{c.get('title','?')}\" ({c.get('year','?')}) — {c.get('first_author','?')}"
        for i, c in enumerate(candidates[:15])
    ) or "(none found)"

    classify_prompt = (
        "Classify how many original studies this replication paper targets.\n\n"
        f"TITLE: {title_r}\n"
        f"ABSTRACT: {abstract_snip or '(not available)'}\n\n"
        f"CITED AUTHOR-YEAR PATTERNS IN ABSTRACT ({len(distinct_pairs)} distinct):\n"
        f"{pattern_lines}\n\n"
        f"CANDIDATE ORIGINALS FROM OPENALEX ({len(candidates)} found):\n"
        f"{cand_lines}\n\n"
        "Classify as ONE of: single_original | multiple_match | multiple_original\n"
        "Key rules: numeric counts ('replications of 28') and project names "
        "(Many Labs, RRR) signal multiple_original. A large candidate list alone does NOT.\n\n"
        '{"original_match_type":"<single_original|multiple_match|multiple_original>",'
        '"original_match_confidence":"<high|medium|low>","reasoning":"<brief>"}'
    )
    classify_result, classify_error = _call_model(classify_prompt, model)

    from shared.llm_client import build_identification_prompt
    link_prompt = build_identification_prompt(
        title_r, abstract_r, pattern, candidates, sections, html_text=html_text,
    )
    link_result, link_error = _call_model(link_prompt, model)

    fulltext          = str(sections.get("intro", "") or html_text or "")
    abstract_snip_out = (abstract_r[:1000] + "…") if len(abstract_r) > 1000 else abstract_r
    text_snip         = (fulltext[:800] + "…") if len(fulltext) > 800 else fulltext
    outcome_prompt = (
        "You are a research methodology expert. Classify the replication outcome.\n\n"
        f"TITLE: {title_r}\n"
        f"ABSTRACT: {abstract_snip_out or '(not available)'}\n"
        f"FULLTEXT EXCERPT: {text_snip or '(not available)'}\n\n"
        "Outcome values:\n"
        "- success: replication confirmed the original finding\n"
        "- failure: replication failed to find the original effect\n"
        "- mixed: some aspects replicated, others did not\n"
        "- uninformative: cannot determine from available text\n"
        "- descriptive: adapted methods in a new context without testing the original claim\n\n"
        'Respond with ONLY this JSON:\n'
        '{"outcome":"<value>","outcome_phrase":"<supporting quote, max 2 sentences>",'
        '"outcome_confidence":"<high|medium|low>","out_quote_source":"<abstract|fulltext|title>"}'
    )
    outcome_result, outcome_error = _call_model(outcome_prompt, model)

    return jsonify({
        "doi":   doi,
        "model": model,
        "classify": {"prompt": classify_prompt, "result": classify_result, "error": classify_error},
        "link":     {"prompt": link_prompt, "result": link_result, "error": link_error,
                     "n_candidates": len(candidates), "n_refs": len(sections.get("references", []))},
        "outcome":  {"prompt": outcome_prompt, "result": outcome_result, "error": outcome_error},
    })


def _handle_rerun_doi(req, out_csv_path: Path):
    """
    Full pipeline rerun for one DOI — clears LLM cache, re-extracts, writes to out_csv_path.
    Body JSON: { "doi": "10.xxx/yyy" }
    """
    data = req.get_json(force=True) or {}
    doi  = clean_doi(data.get("doi", "").strip())
    if not doi:
        return jsonify({"error": "missing doi"}), 400

    filtered_path = DATA_DIR / "filtered.csv"
    if not filtered_path.exists():
        return jsonify({"error": "filtered.csv not found — run Stage 2 first"}), 404
    fdf = pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig").fillna("")
    matches = fdf[fdf["doi_r"].apply(clean_doi) == doi]
    if matches.empty:
        return jsonify({"error": f"{doi} not found in filtered.csv"}), 404
    row = matches.iloc[0]

    key = cache_key(doi)
    for stale in [
        LLM_CACHE_DIR / f"llm_{key}.json",
        LLM_CACHE_DIR / f"outcome_{cache_key(doi)}.json",
        LLM_CACHE_DIR / f"match_type_{cache_key(doi + '_match_type')}.json",
    ]:
        if stale.exists():
            stale.unlink()

    try:
        from extract.run_extract import (
            classify_match_type, _build_cands_df, _merge_row,
            _get_outcome, _save_parse_cache,
        )
        from extract.link_original import run_for_doi
        from shared.schema import EXTRACTED_COLS

        match      = classify_match_type(row.to_dict())
        match_type = match["original_match_type"]
        match_conf = match["original_match_confidence"]
        link       = run_for_doi(doi, cands_df=_build_cands_df(row))
        _save_parse_cache(doi)
        outcome    = _get_outcome(doi, row, link)
        result_row = _merge_row(row, link, outcome, match_type, match_conf, 1, 1)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    try:
        from shared.schema import EXTRACTED_COLS
        if out_csv_path.exists():
            edf = pd.read_csv(out_csv_path, dtype=str, encoding="utf-8-sig").fillna("")
            edf = edf[edf["doi_r"].apply(clean_doi) != doi]
        else:
            edf = pd.DataFrame(columns=EXTRACTED_COLS)
        new_row_df = pd.DataFrame([result_row])
        for col in EXTRACTED_COLS:
            if col not in new_row_df.columns:
                new_row_df[col] = ""
        edf = pd.concat([edf, new_row_df[EXTRACTED_COLS]], ignore_index=True)
        edf.to_csv(out_csv_path, index=False, encoding="utf-8-sig")
    except Exception as exc:
        return jsonify({"error": f"pipeline ok but csv write failed: {exc}"}), 500

    return jsonify(result_row)


# ── Blueprint factory ─────────────────────────────────────────────────────────

def make_extract_blueprint(
    name: str,
    csv_path: Path,
    url_prefix: str,
    active_page: str,
    test_mode: bool = False,
) -> Blueprint:
    """
    Create an Extract or Extract Test blueprint.

    name        — Blueprint name ('extract_view' or 'extract_test_view').
    csv_path    — CSV this blueprint reads (extracted.csv or extracted-test.csv).
    url_prefix  — Page route prefix ('/extract' or '/extract-test').
    active_page — Passed to templates for nav highlighting.
    test_mode   — Adds the /promote endpoint; passes test_mode=True to templates.
    """
    bp          = Blueprint(name, __name__)
    sample_path = BASE_DIR / "misc" / "sample_extracted.csv"
    filtered_p  = DATA_DIR / "filtered.csv"
    cands_p     = DATA_DIR / "candidates.csv"

    def _load_csv() -> "tuple[pd.DataFrame | None, str]":
        if csv_path.exists():
            df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str,
                             on_bad_lines="skip").fillna("")
            df = _fill_titles(df, filtered_p, cands_p)
            return df, str(csv_path.name)
        if sample_path.exists():
            return (pd.read_csv(sample_path, encoding="utf-8-sig", dtype=str,
                                on_bad_lines="skip").fillna(""),
                    "misc/sample_extracted.csv (sample)")
        return None, ""

    # ── Page ──────────────────────────────────────────────────────────────────

    @bp.route(url_prefix)
    def extract_page():
        df, source = _load_csv()
        return render_template(
            "extract.html",
            active_page=active_page,
            source=source,
            total=len(df) if df is not None else 0,
            api_prefix=f"/api{url_prefix}",
            test_mode=test_mode,
        )

    # ── List API ──────────────────────────────────────────────────────────────

    @bp.route(f"/api{url_prefix}/list")
    def api_list():
        df, _ = _load_csv()
        if df is None:
            return jsonify({"error": f"No {csv_path.name} found. Run Stage 3 first."}), 404

        q       = request.args.get("q",          "").strip().lower()
        outcome = request.args.get("outcome",    "all")
        method  = request.args.get("method",     "all")
        fstatus = request.args.get("fstatus",    "all")
        mtype   = request.args.get("mtype",      "all")
        pdf_st  = request.args.get("pdf_status", "all")

        if q:
            mask = (
                df.get("doi_r",   pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
                | df.get("title_r", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
                | df.get("title_o", pd.Series([""] * len(df))).str.lower().str.contains(q, na=False)
            )
            df = df[mask]

        # Hide target_pending on the main Extract tab (reviewers don't need them).
        # Keep them visible on Extract Test so you can see what's still stuck.
        if not test_mode and "link_method" in df.columns:
            df = df[df["link_method"] != "target_pending"]

        if outcome != "all" and "outcome" in df.columns:
            df = df[df["outcome"] == outcome]
        if method != "all" and "link_method" in df.columns:
            df = df[df["link_method"] == method]
        if fstatus != "all" and "filter_status" in df.columns:
            df = df[df["filter_status"] == fstatus]
        if mtype != "all" and "original_match_type" in df.columns:
            df = df[df["original_match_type"] == mtype]

        # Apply pdf_status filter before pagination for accurate totals
        if pdf_st != "all":
            df = df[df.apply(
                lambda r: _pdf_status(r.get("doi_r", ""), r.get("link_method", "")) == pdf_st,
                axis=1,
            )]

        total   = len(df)
        page    = max(1, int(request.args.get("page",     1)))
        per_pg  = max(1, min(500, int(request.args.get("per_page", 200))))
        offset  = (page - 1) * per_pg
        page_df = df.iloc[offset : offset + per_pg]

        rows = []
        for i, r in enumerate(page_df.to_dict("records"), start=offset + 1):
            title = r.get("title_r", "") or r.get("study_r", "")
            rows.append({
                "idx":             i,
                "doi_r":           r.get("doi_r",           ""),
                "title_r":         title,
                "year_r":          r.get("year_r",          ""),
                "filter_status":   r.get("filter_status",   ""),
                "type":            r.get("type",            ""),
                "link_method":     r.get("link_method",     ""),
                "outcome":         r.get("outcome",         ""),
                "title_o":         r.get("title_o",         ""),
                "original_rank":   r.get("original_rank",   "1"),
                "n_originals":     r.get("n_originals",     "1"),
                "match_type":      r.get("original_match_type", ""),
                "link_confidence": r.get("link_confidence", ""),
                "link_llm_model":  r.get("link_llm_model",  ""),
                "pair_id":         r.get("pair_id",         ""),
                "ref_r":           r.get("ref_r",           ""),
                "ref_o":           r.get("ref_o",           ""),
                "pdf_status":      _pdf_status(r.get("doi_r", ""), r.get("link_method", "")),
            })

        return jsonify({
            "rows":     rows,
            "total":    total,
            "page":     page,
            "per_page": per_pg,
            "pages":    max(1, -(-total // per_pg)),
        })

    # ── Detail API ────────────────────────────────────────────────────────────

    @bp.route(f"/api{url_prefix}/detail")
    def api_detail():
        doi  = request.args.get("doi",  "").strip()
        rank = request.args.get("rank", "1").strip()
        if not doi:
            return jsonify({"error": "missing doi parameter"}), 400
        df, _ = _load_csv()
        if df is None:
            return jsonify({"error": f"No {csv_path.name} found"}), 404
        matches = df[df["doi_r"] == doi]
        if "original_rank" in df.columns and not matches.empty:
            ranked = matches[matches["original_rank"] == rank]
            if not ranked.empty:
                matches = ranked
        if matches.empty:
            return jsonify({"error": "doi not found"}), 404
        row = matches.iloc[0].to_dict()
        return jsonify(_enrich_detail(row))

    # ── Run DOI (model comparison, no cache write) ────────────────────────────

    @bp.route(f"/api{url_prefix}/run-doi", methods=["POST"])
    def api_run_doi():
        return _handle_run_doi(request, _load_csv)

    # ── Rerun DOI (full pipeline, writes to this blueprint's CSV) ────────────

    @bp.route(f"/api{url_prefix}/rerun-doi", methods=["POST"])
    def api_rerun_doi():
        return _handle_rerun_doi(request, csv_path)

    # ── Promote (test_mode only) ──────────────────────────────────────────────

    if test_mode:
        @bp.route(f"/api{url_prefix}/promote", methods=["POST"])
        def api_promote():
            from extract.promote_test import promote_rows
            data = request.get_json(force=True) or {}
            if data.get("promote_all"):
                result = promote_rows(all_rows=True)
            elif data.get("doi"):
                result = promote_rows(dois=[data["doi"]])
            else:
                return jsonify({"error": "missing doi or promote_all"}), 400
            return jsonify(result)

    return bp


# ── Shared utility routes (main blueprint only) ───────────────────────────────

def add_shared_routes(bp: Blueprint) -> None:
    """
    Register crossref-lookup and PDF-serve routes on the main extract blueprint.

    These are shared utilities called by both extract.html and extract_test.html.
    Must only be called once (on the main blueprint), not on the test blueprint.
    """

    @bp.route("/api/crossref/lookup")
    def api_crossref_lookup():
        """Fetch paper metadata from Crossref for a given DOI."""
        doi = clean_doi(request.args.get("doi", "").strip())
        if not doi:
            return jsonify({"error": "missing doi"}), 400

        headers = {
            "User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})",
            "Accept":     "application/json",
        }

        def _crossref(d: str) -> dict:
            url = f"https://api.crossref.org/works/{urllib.request.quote(d, safe='')}"
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
                "title":   (msg.get("title",           [""])[0] or ""),
                "authors": authors,
                "year":    year,
                "journal": (msg.get("container-title", [""])[0] or ""),
                "volume":  str(msg.get("volume",  "") or ""),
                "issue":   str(msg.get("issue",   "") or ""),
                "pages":   str(msg.get("page",    "") or ""),
                "doi":     msg.get("DOI", d),
                "url":     msg.get("URL", f"https://doi.org/{d}"),
                "source":  "crossref",
            }

        try:
            return jsonify(_crossref(doi))
        except Exception as e:
            return jsonify({"error": str(e), "doi": doi}), 502

    @bp.route("/api/pdf/<path:doi_raw>")
    def api_serve_pdf(doi_raw: str):
        """Serve a cached PDF from PDF_CACHE_DIR for a given DOI."""
        doi  = clean_doi(doi_raw)
        key  = cache_key(doi)
        path = PDF_CACHE_DIR / f"{key}.pdf"
        if not path.exists():
            return jsonify({"error": "PDF not in cache"}), 404
        return send_file(path, mimetype="application/pdf",
                         download_name=f"{key}.pdf", as_attachment=False)
