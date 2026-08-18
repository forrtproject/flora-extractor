"""api_concerns.py — only the defects the pipeline itself already defines.

No invented thresholds. A number this dashboard guessed at ("39% target_pending is
too high") would be presented with the same authority as a real defect, and the
codebase already has a source of truth: `extract.sanity_check` partitions the export,
and every non-zero bucket is drift between the file on disk and the verdicts it is
rendered from.

Two concerns have no sanity_check equivalent. `provenance_mismatch` and
`foreign_stats` both watch the cached `stats.json`: `api_csv_stats` already recomputes
the `filtered` block from the store on every request, so the pile counts were never at
risk — but the `extracted.*` block IS served from that cache, and nothing previously
said which machine wrote it or which release it describes.
"""
import contextlib
import io
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, jsonify

from extract.sanity_check import run_sanity_check
from shared.config import OPENAI_DAILY_TOKEN_BUDGET
from validate import sources

concerns_bp = Blueprint("concerns", __name__)

# Rules that need a command of their own; everything else in `flagged` is export
# drift and shares the drift command.
_FLAG_RULES = {
    "api_error": ("provider failed after retries", "high",
                  "python -m extract.tier --release <id> --run --redo <work-ids>"),
}
_DRIFT_COMMAND = "python -m extract.export --release <id> --check"


def _same_release(left: str, right: str) -> bool:
    """Release ids match when either is a prefix of the other.

    `status` and the handover quote 12-character prefixes while the store keeps the
    full sha256, so a plain equality test would report every release as a mismatch.
    """
    return bool(left and right and (left.startswith(right) or right.startswith(left)))


def _concern(cid: str, label: str, count: int, severity: str,
             command: str = "", note: str = "", check_url: str = "") -> dict:
    """One rule's finding. `check_url` opens the offending rows in the Check tab.

    A count that names a defect but cannot show which rows it means is not
    actionable, so every concern that corresponds to a filterable population
    carries the link. Concerns about the pipeline's STATE rather than its rows
    (a stale cache, a budget) have none, because there is nothing to list.
    """
    return {"id": cid, "label": label, "count": int(count), "severity": severity,
            "command": command, "note": note, "check_url": check_url}


def _chronology_dois(df) -> list:
    """The replication DOIs whose original carries a later year."""
    if df is None or not {"year_o", "year_r", "doi_r"} <= set(df.columns):
        return []
    import pandas as pd

    year_o = pd.to_numeric(df["year_o"], errors="coerce")
    year_r = pd.to_numeric(df["year_r"], errors="coerce")
    hit = df[year_o.notna() & year_r.notna() & (year_o > year_r)]
    return [d for d in hit["doi_r"].astype(str) if d and d != "nan"]


@concerns_bp.route("/api/dashboard/concerns")
def api_concerns():
    """Every rule the pipeline already enforces, with today's count for each."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):        # the checker prints its own report
        summary = run_sanity_check()

    concerns: list[dict] = []

    for name, count in (summary.get("flagged") or {}).items():
        label, severity, command = _FLAG_RULES.get(
            name, (f"rows filed as {name} that belong in a set-aside CSV",
                   "high", _DRIFT_COMMAND))
        concerns.append(_concern(name, label, count, severity, command))

    # Named, not just counted: Check has no chronology filter, but it can search a
    # DOI, and a reader has to see the row before deciding this is even wrong.
    chrono_dois = _chronology_dois(sources.extracted_csv()[0])
    concerns.append(_concern(
        "chronology", "original published after the replication that names it",
        summary.get("chronology_errors", 0), "medium",
        "python -m extract.audit_dois --apply",
        "A one-year gap is often online-first vs issue year, not a wrong link — "
        "read the row's ref_o before acting."
        + (f" Affected: {', '.join(chrono_dois[:3])}." if chrono_dois else ""),
        check_url=f"/check?q={quote(chrono_dois[0])}" if len(chrono_dois) == 1 else ""))

    concerns.append(_concern(
        "duplicate_pair_ids", "the same pair rendered twice",
        summary.get("duplicate_pair_ids", 0), "high", _DRIFT_COMMAND))

    concerns.append(_concern(
        "blank_doi_r", "rendered rows with no replication DOI",
        summary.get("blank_doi_r", 0), "low", "",
        "pair_id falls back to the OpenAlex id, so these still key — "
        "but they cannot be looked up by DOI.",
        check_url="/check?no_doi=1"))

    # `flagged["api_error"]` counts rows MISFILED as api_error. A row whose outcome IS
    # api_error is filed correctly and so never appears there — but CLAUDE.md defines
    # api_error as "failed after retries", which the next run must re-ask. Counted from
    # the render, where those rows actually live.
    df, csv_prov = sources.extracted_csv()
    outcome_errors = 0
    if df is not None and "outcome" in df.columns:
        outcome_errors = int(df["outcome"].fillna("").astype(str)
                             .str.strip().eq("api_error").sum())
    concerns.append(_concern(
        "outcome_api_error", "rendered rows whose outcome call failed after retries",
        outcome_errors, "high",
        "python -m extract.tier --release <id> --run --redo <work-ids>",
        "A transient failure is never a result — these re-ask on the next run.",
        check_url="/check?outcome=api_error"))

    _, stats_prov = sources.stats_json()
    _, store_prov = sources.filtered_stats()
    cached_release = stats_prov.get("release_id")
    live_release = store_prov.get("release_id")
    # Without a local store there is nothing to compare against. Absence of evidence
    # is not a mismatch, so this stays silent rather than firing on every machine
    # that has not routed yet.
    stale = bool(live_release and cached_release
                 and not _same_release(live_release, cached_release))
    note = ""
    if stale:
        machine = stats_prov.get("machine")
        note = (f"stats.json names release {cached_release[:12]} "
                f"(written {stats_prov.get('as_of')}"
                + (f" on {machine}'s machine" if machine else "")
                + f"); this store's newest release is {live_release[:12]}.")
    concerns.append(_concern(
        "provenance_mismatch", "cached stats describe a different release",
        1 if stale else 0, "high",
        "python -c \"from shared.dashboard_cache import refresh; refresh('filtered')\"",
        note))

    # Knowable without a store, unlike the release comparison above: cached stats
    # carrying someone else's home directory did not describe this checkout's work.
    foreign = stats_prov.get("machine")
    concerns.append(_concern(
        "foreign_stats", "cached stats were written on another machine",
        1 if foreign and foreign != Path.home().name else 0, "medium",
        "python -c \"from shared.dashboard_cache import refresh; refresh('filtered')\"",
        f"stats.json was written under {foreign}'s home directory." if foreign else ""))

    _, token_prov = sources.token_usage_record()
    concerns.append(_concern(
        "budget", "OpenAI daily token budget", 0, "low", "",
        f"cap {OPENAI_DAILY_TOKEN_BUDGET:,} tokens/day"
        if OPENAI_DAILY_TOKEN_BUDGET else "no cap set (0 disables)"))

    return jsonify({"concerns": concerns,
                    "provenance": {"sanity_check": {"source": "sanity_check",
                                                    "state": "live"},
                                   "stats_json": stats_prov,
                                   "token_usage": token_prov}})
