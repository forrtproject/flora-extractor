"""api_analysis.py — the breakdowns, read live from the rendered CSV.

Replication and reproduction outcomes are reported in SEPARATE maps. They are
different vocabularies — a reproduction's outcome is the join of two axes
(computation and robustness) — so one distribution over both would invent a scale that
does not exist.

Token usage is re-landed here from PR #132 (issue #115), which went stale against
main's dashboard refactor. Provider stays a level of its own because a model id does
not name its provider: gpt-5.4-mini reached through OpenRouter is not OpenAI spend.
Input and output stay apart because they are priced differently.
"""
from flask import Blueprint, jsonify

from shared.config import OPENAI_DAILY_TOKEN_BUDGET
from validate import sources

analysis_bp = Blueprint("analysis", __name__)


def _counts(df, column: str) -> dict:
    """Value counts for one column, blanks dropped. Missing column -> {}."""
    if column not in df.columns:
        return {}
    series = df[column].fillna("").astype(str).str.strip()
    return {k: int(v) for k, v in series[series != ""].value_counts().items()}


# A reproduction's outcome is the JOIN of two independent axes, so the flat
# distribution has one bar per COMBINATION and hides which axis actually failed.
# The grid is the honest shape: computation down, robustness across.
_COMPUTATION = ("computationally reproducible", "computational issues",
                "technical failure", "not checked", "cannot_be_determined")
_ROBUSTNESS = ("robust", "robustness challenges", "not checked", "cannot_be_determined")


def _repro_axes(repro) -> dict:
    """The reproduction outcomes as a computation x robustness grid."""
    if repro.empty or "outcome_computation" not in repro.columns:
        return {"rows": [], "computation": [], "robustness": [], "total": 0}
    comp = repro["outcome_computation"].fillna("").astype(str).str.strip()
    robu = repro.get("outcome_robustness")
    robu = (robu.fillna("").astype(str).str.strip() if robu is not None
            else comp.where(False, ""))
    # Values the vocabularies do not name still have to appear: an unexpected label is
    # a finding, not something to drop off the grid.
    comp_labels = list(_COMPUTATION) + sorted(set(comp) - set(_COMPUTATION) - {""})
    robu_labels = list(_ROBUSTNESS) + sorted(set(robu) - set(_ROBUSTNESS) - {""})
    grid = [[int(((comp == c) & (robu == r)).sum()) for r in robu_labels]
            for c in comp_labels]
    keep = [i for i, row in enumerate(grid) if any(row)]
    keep_c = [j for j in range(len(robu_labels)) if any(grid[i][j] for i in keep)]
    return {
        "computation": [comp_labels[i] for i in keep],
        "robustness":  [robu_labels[j] for j in keep_c],
        "rows":        [[grid[i][j] for j in keep_c] for i in keep],
        "total":       int(len(repro)),
    }


# Columns a drill-down row carries: enough to find the paper, never the whole record.
_ROW_COLS = ("doi_r", "title_r", "year_r", "doi_o", "title_o", "outcome",
             "link_method", "link_confidence", "doi_o_verification", "type",
             "outcome_computation", "outcome_robustness")


@analysis_bp.route("/api/dashboard/rows")
def api_rows():
    """The rows behind one count: `?field=doi_o_verification&value=api_error`.

    Every number in the analysis band is a filter over the same CSV, so one endpoint
    answers "which rows are these?" for all of them. Without it an `api_error` count
    states that something failed and gives no way to find WHICH row failed, which is
    the only thing that makes the count actionable.
    """
    from flask import request

    field = request.args.get("field", "")
    value = request.args.get("value", "")
    limit = max(1, min(int(request.args.get("limit", 200)), 1000))

    df, prov = sources.extracted_csv()
    if df is None:
        return jsonify({"rows": [], "total": 0, "provenance": prov})
    if field not in df.columns:
        return jsonify({"error": f"no column {field!r} in the render",
                        "rows": [], "total": 0, "provenance": prov}), 400

    series = df[field].fillna("").astype(str).str.strip()
    match = df[series.eq("") if value == "" else series.eq(value)]
    columns = [c for c in _ROW_COLS if c in match.columns]
    rows = [{c: (str(r[c])[:300] if r[c] is not None else "") for c in columns}
            for _, r in match.head(limit).iterrows()]
    return jsonify({"field": field, "value": value, "columns": columns,
                    "rows": rows, "total": int(len(match)),
                    "shown": len(rows), "provenance": prov})


@analysis_bp.route("/api/dashboard/analysis")
def api_analysis():
    """Outcome, link and verification breakdowns over the rendered rows."""
    df, prov = sources.extracted_csv()
    if df is None:
        return jsonify({"rows": 0, "provenance": prov})

    if "type" in df.columns:
        is_repro = df["type"].fillna("").astype(str).str.strip().eq("reproduction")
    else:
        is_repro = df.index != df.index          # all False, same length

    return jsonify({
        "rows": len(df),
        "repro_axes": _repro_axes(df[is_repro]),
        "by_outcome_replication":  _counts(df[~is_repro], "outcome"),
        "by_outcome_reproduction": _counts(df[is_repro],  "outcome"),
        "by_link_method":          _counts(df, "link_method"),
        "by_link_confidence":      _counts(df, "link_confidence"),
        "by_doi_verification":     _counts(df, "doi_o_verification"),
        "by_type":                 _counts(df, "type"),
        "by_year":                 _counts(df, "year_r"),
        "provenance": prov,
    })


@analysis_bp.route("/api/dashboard/token-usage")
def api_token_usage():
    """LLM token spend, cumulative per provider+model and broken down by day.

    A missing ledger yields an empty structure rather than an error: the panel then
    says nothing has been recorded on this machine.
    """
    record, prov = sources.token_usage_record()

    totals: dict = {}
    days: list[dict] = []
    for day in sorted(record, reverse=True):
        providers = record[day] or {}
        day_rows: list[dict] = []
        for provider in sorted(providers):
            for model, counts in sorted((providers[provider] or {}).items()):
                tin  = int(counts.get("in", 0) or 0)
                tout = int(counts.get("out", 0) or 0)
                day_rows.append({"provider": provider, "model": model,
                                 "in": tin, "out": tout, "total": tin + tout})
                agg = totals.setdefault((provider, model), {"in": 0, "out": 0})
                agg["in"]  += tin
                agg["out"] += tout
        days.append({"day": day, "rows": day_rows,
                     "in":    sum(r["in"]    for r in day_rows),
                     "out":   sum(r["out"]   for r in day_rows),
                     "total": sum(r["total"] for r in day_rows)})

    rows = [{"provider": p, "model": m, "in": v["in"], "out": v["out"],
             "total": v["in"] + v["out"]}
            for (p, m), v in totals.items()]
    rows.sort(key=lambda r: -r["total"])

    return jsonify({
        "rows":  rows,
        "days":  days,
        "in":    sum(r["in"]    for r in rows),
        "out":   sum(r["out"]   for r in rows),
        "total": sum(r["total"] for r in rows),
        "openai_daily_budget": OPENAI_DAILY_TOKEN_BUDGET,
        "provenance": prov,
    })
