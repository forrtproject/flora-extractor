"""
check.py — Check tab: filter + search over Stage 3's output CSVs.

The searchable stages are the ones that HAVE a table on disk: `extracted` and
`extracted-test`. Stage 2 is not among them — its artifact is the routing store
plus the survivor pool, and answering one search over that pair means streaming
several GB of parquet per request. The Stage 2 figures live on the dashboard,
which reads the store directly, and a row-level record of a release comes from
`python -m filter.engine export-csv --out <file>`.

Routes:
  GET /check                  → check page
  GET /api/check/search       → filtered/paginated rows as JSON
  GET /api/check/download     → filtered rows as CSV attachment
"""
import datetime
from typing import Optional

import pandas as pd
import pyarrow.parquet as pq
from flask import Blueprint, jsonify, render_template, request, send_file

from shared.config import DATA_DIR
from shared.dashboard_cache import DASHBOARD_DIR

check_bp = Blueprint("check", __name__)

_MAIN_STAGES = {
    "extracted":      DATA_DIR / "extracted.csv",
    "extracted-test": DATA_DIR / "extracted-test.csv",
}


def _set_aside_stages() -> dict:
    """Every set-aside CSV as its own Check stage, keyed by its filename stem.

    The rows the export files away are NOT in `extracted.csv` — that is what setting
    them aside means — so before this they were countable on the dashboard and
    unreadable anywhere. They carry the same columns, so the whole Check surface works
    over them unchanged, and the dashboard's cards can link straight in.

    Built from `SET_ASIDE_DESTINATIONS`, the one place a destination is named, so a
    file the export learns to write cannot go missing here.
    """
    from shared.schema import SET_ASIDE_DESTINATIONS

    return {filename[:-4]: DATA_DIR / filename
            for filename in sorted(set(SET_ASIDE_DESTINATIONS.values()))}


SET_ASIDE_STAGES = _set_aside_stages()
_STAGES = {**_MAIN_STAGES, **SET_ASIDE_STAGES}

# Which column holds the "type/status" filter for each stage. Every stage here is
# rendered from EXTRACTED_COLS, so they all answer to the same column.
_TYPE_COL = {name: "type" for name in _STAGES}


def _to_year_int(y: str) -> Optional[int]:
    """Convert '2009', '2009.0' or '2009.5' to int year."""
    try:
        return int(float(str(y).strip()))
    except (ValueError, TypeError):
        return None


def _apply_filters(chunk: pd.DataFrame, stage: str, params: dict) -> pd.DataFrame:
    year_from    = params.get("year_from", "")
    year_to      = params.get("year_to", "")
    type_vals    = params.get("type_vals", [])
    outcomes     = params.get("outcomes", [])
    link_methods = params.get("link_methods", [])
    match_types  = params.get("match_types", [])
    doi_verified = params.get("doi_verified_vals", [])
    sources      = params.get("sources", [])
    q            = params.get("q", "")
    no_doi       = params.get("no_doi", False)

    if year_from and "year_r" in chunk.columns:
        yf = int(year_from)
        chunk = chunk[chunk["year_r"].apply(lambda y: (_to_year_int(y) or -1) >= yf)]
    if year_to and "year_r" in chunk.columns:
        yt = int(year_to)
        chunk = chunk[chunk["year_r"].apply(lambda y: (_to_year_int(y) or 9999) <= yt)]

    no_doi_url = params.get("no_doi_url", False)
    no_abstract = params.get("no_abstract", False)

    if no_doi and "doi_r" in chunk.columns:
        chunk = chunk[chunk["doi_r"].fillna("").str.strip() == ""]
    if no_doi_url and "doi_r" in chunk.columns:
        doi_empty = chunk["doi_r"].fillna("").str.strip() == ""
        url_empty = chunk["url_r"].fillna("").str.strip() == "" if "url_r" in chunk.columns else doi_empty
        chunk = chunk[doi_empty & url_empty]
    if no_abstract and "abstract_r" in chunk.columns:
        chunk = chunk[chunk["abstract_r"].fillna("").str.strip() == ""]

    type_col = _TYPE_COL.get(stage)
    if type_vals and type_col and type_col in chunk.columns:
        chunk = chunk[chunk[type_col].isin(type_vals)]

    for col, vals in [
        ("outcome",             outcomes),
        ("link_method",         link_methods),
        ("original_match_type", match_types),
        ("doi_o_verification",  doi_verified),
        ("link_confidence",     params.get("link_confidences", [])),
        ("source",              sources),
    ]:
        if vals and col in chunk.columns:
            chunk = chunk[chunk[col].isin(vals)]

    if q:
        mask = pd.Series(False, index=chunk.index)
        for col in ("doi_r", "title_r", "doi_o", "title_o"):
            if col in chunk.columns:
                mask |= chunk[col].str.lower().str.contains(q, na=False)
        chunk = chunk[mask]

    return chunk


def _read_one_stage(stage: str, params: dict) -> pd.DataFrame:
    path = _STAGES[stage]
    if not path.exists():
        return pd.DataFrame()

    # The Parquet mirror is a read cache for the CSV, so it may only serve while it is
    # at least as new as the file it mirrors. Without this test it was served whenever
    # it existed: on 2026-08-18 the mirror was 26 days old and held 1,881 rows against
    # the CSV's 3,147, in the SUPERSEDED outcome vocabulary ("success"/"failure" rather
    # than "successful"/"failed"). Every Check result was that stale set, and nothing
    # said so. Refresh it with
    # `python -c "from shared.dashboard_cache import refresh; refresh('extracted')"`.
    pq_path = DASHBOARD_DIR / f"{stage}.parquet"
    if pq_path.exists() and pq_path.stat().st_mtime >= path.stat().st_mtime:
        try:
            df = pq.read_table(pq_path).to_pandas().fillna("")
            return _apply_filters(df, stage, params)
        except Exception:
            pass

    chunks = []
    for chunk in pd.read_csv(
        path, encoding="utf-8-sig", dtype=str,
        chunksize=50_000, on_bad_lines="skip",
    ):
        chunk = chunk.fillna("")
        filtered = _apply_filters(chunk, stage, params)
        if not filtered.empty:
            chunks.append(filtered)
    return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()


def _read_filtered(stages: list[str], params: dict) -> pd.DataFrame:
    """Read and filter one or more stages. Adds _stage column when >1 stage."""
    multi = len(stages) > 1
    frames = []
    for stage in stages:
        df = _read_one_stage(stage, params)
        if not df.empty:
            if multi:
                df.insert(0, "_stage", stage)
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _get_list(name: str) -> list[str]:
    """Parse a request param that may be repeated or comma-separated."""
    vals = request.args.getlist(name)
    result = []
    for v in vals:
        result.extend(x.strip() for x in v.split(",") if x.strip())
    return result


def _extract_params() -> dict:
    return {
        "year_from":         request.args.get("year_from", "").strip(),
        "year_to":           request.args.get("year_to", "").strip(),
        "type_vals":         _get_list("type"),
        "outcomes":          _get_list("outcome"),
        "link_methods":      _get_list("link_method"),
        "match_types":       _get_list("match_type"),
        "doi_verified_vals": _get_list("doi_verified"),
        "link_confidences":  _get_list("link_confidence"),
        "sources":           _get_list("source"),
        "q":                 request.args.get("q", "").strip().lower(),
        "no_doi":            request.args.get("no_doi", "") == "1",
        "no_doi_url":        request.args.get("no_doi_url", "") == "1",
        "no_abstract":       request.args.get("no_abstract", "") == "1",
    }


def _get_stages() -> list[str]:
    stages = _get_list("stage")
    valid  = [s for s in stages if s in _STAGES]
    return valid if valid else ["extracted"]


@check_bp.route("/check")
def check_page():
    """The Check page, with its outcome options read from the schema.

    They used to be a hardcoded list, and six of its ten values matched no row:
    it offered `success` and `failure` where the pipeline writes `successful` and
    `failed`, so the two largest categories — 1,178 and 652 rows — filtered to
    nothing. Rendering the closed vocabulary instead means a value cannot drift
    out of the filter without moving in `shared/schema.py` first.
    """
    from shared.schema import OUTCOME_CATEGORIES, OUTCOME_VALUES

    def _label(value: str) -> str:
        return value.replace("_", " ").capitalize()

    # Replication verdicts first (the common case), then the joined reproduction
    # axes, which are numerous and read as a group.
    replication = sorted(OUTCOME_CATEGORIES)
    reproduction = sorted(set(OUTCOME_VALUES) - set(OUTCOME_CATEGORIES))
    # The link-method list was hardcoded and had drifted badly: it offered
    # `author_year_match`, which the pipeline has not written in any current row, and
    # omitted `llm_references`, `llm_title_search` and `llm_author_year_search` —
    # together 92% of the shipped rows. Rendering the closed vocabulary means a method
    # cannot drift out of the filter without moving in `shared/schema.py` first.
    from shared.schema import LINK_METHOD_VALUES, RESOLVED_LINK_METHODS

    resolved = sorted(RESOLVED_LINK_METHODS)
    other = sorted(set(LINK_METHOD_VALUES) - RESOLVED_LINK_METHODS)

    stage_options = [(k, _label(k), _STAGES[k].exists()) for k in _MAIN_STAGES]
    aside_options = [(k, _label(k), _STAGES[k].exists()) for k in SET_ASIDE_STAGES]

    return render_template(
        "check.html", active_page="check",
        outcome_options=[(v, _label(v)) for v in replication + reproduction],
        link_method_options=[(v, _label(v), True) for v in resolved]
                            + [(v, _label(v), False) for v in other],
        match_type_options=[(v, _label(v)) for v in
                            ("single_original", "multiple_original")],
        stage_options=stage_options, aside_options=aside_options,
        confidence_options=[("high", "High"), ("medium", "Medium"), ("low", "Low")])


@check_bp.route("/api/check/search")
def api_check_search():
    stages = _get_stages()
    page     = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(10, int(request.args.get("per_page", 25))))

    df    = _read_filtered(stages, _extract_params())
    total = len(df)
    pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page  = min(page, pages)
    start = (page - 1) * per_page
    rows  = df.iloc[start:start + per_page].to_dict("records") if not df.empty else []

    return jsonify({"total": total, "pages": pages, "page": page, "rows": rows})


@check_bp.route("/api/check/download")
def api_check_download():
    stages = _get_stages()
    df = _read_filtered(stages, _extract_params())

    download_dir = DATA_DIR / "dashboard" / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    date_str   = datetime.date.today().isoformat()
    stage_str  = "+".join(stages)
    filename   = f"check_{stage_str}_{date_str}.csv"
    out_path   = download_dir / filename

    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return send_file(str(out_path), as_attachment=True,
                     download_name=filename, mimetype="text/csv")
