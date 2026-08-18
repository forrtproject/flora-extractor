"""api_flow.py — the funnel as it actually is, and what the input was missing.

Counts alone hide coverage loss, so this band reports what the input LACKED before it
reports what survived. The actionable number is `no_text`: a work that matched a
screening rule but was downgraded because its abstract was empty
(`filter/engine/route.py`, `_TEXT_PILES`). Those works would have been screened had
text existed, so they are recoverable coverage rather than genuine rejections.

`no_text` lives in the routing table's `pending_reason` column, not in a pile name —
the work sits in `pending` and the reason says why. Counting it needs its own query.

A stage whose source is absent reports `count: None`, never 0. "No routing store here"
and "zero works in this pile" are different facts and must not render alike.
"""
import csv
import json
import sys
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify

from shared.config import DATA_DIR
from validate import sources

flow_bp = Blueprint("flow", __name__)

# link_evidence and abstract fields run far past csv's default 128 KB cap,
# and a field over it raises rather than truncating.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def _stage(sid: str, label: str, count: Optional[int], prov: dict) -> dict:
    return {"id": sid, "label": label, "count": count, "provenance": prov}


def _no_text_count(con: Any, release_id: str) -> int:
    """Works downgraded out of a screening pile for having no abstract."""
    row = con.execute(
        "SELECT count(*) FROM routing WHERE release_id = ? AND pending_reason = ?",
        [release_id, "no_text"]).fetchone()
    return int(row[0]) if row else 0


def _pool_sidecar(pool: dict) -> dict:
    """`_pool_provenance.json`: what the pull recorded about this pool copy."""
    path = Path(pool.get("pool_dir", "")) / "_pool_provenance.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pool_recorded_at(pool: dict) -> Optional[str]:
    return _pool_sidecar(pool).get("recorded_at")


def _pool_shortfall(pool: dict) -> Optional[str]:
    """A partial pool named as one, rather than passed off as the whole corpus.

    `pool_sync --pull` resumes, so an interrupted pull leaves a pool that reads as a
    perfectly good smaller corpus. The sidecar's `expected_files` is the only thing
    that can tell the two apart.
    """
    expected = _pool_sidecar(pool).get("expected_files")
    have = pool.get("files")
    if expected and have and have < expected:
        return (f"partial pool: {have:,} of {expected:,} partitions — "
                "`python -m search.pool_sync --pull` resumes")
    if pool.get("unreadable"):
        return f"{pool['unreadable']:,} partition(s) unreadable"
    return None


def _set_aside_counts() -> dict:
    """Rows the export filed away instead of shipping, per destination file.

    The destinations come from `shared.schema.SET_ASIDE_DESTINATIONS` — the one place
    a destination is named — so a file the export writes cannot be missing here.
    Several statuses share one file (non_article and non_article_type both land in
    not_a_replication.csv), so the files are de-duplicated before counting.

    Parsed with `csv`, NOT counted by line: abstract and evidence fields carry embedded
    newlines, so a line count over-reports badly — target_pending.csv reads 3,729 lines
    against 2,329 actual rows. Streamed a row at a time rather than loaded, so the
    memory cost stays flat.

    A file the export wrote nothing to is deleted, so absence means an empty pile —
    0, not unavailable.
    """
    from shared.schema import SET_ASIDE_DESTINATIONS

    from validate.routes.dashboard import _SET_ASIDE_COPY

    counts: dict = {}
    for filename in sorted(set(SET_ASIDE_DESTINATIONS.values())):
        path = DATA_DIR / filename
        rows = 0
        if path.exists():
            try:
                with path.open(newline="", encoding="utf-8-sig") as handle:
                    rows = max(sum(1 for _ in csv.reader(handle)) - 1, 0)  # minus header
            except (OSError, csv.Error):
                continue                 # a pile that cannot be read is not a count
        copy = _SET_ASIDE_COPY.get(filename, {})
        # Which statuses land here — several share one file, and a reader asking
        # "when does this happen?" needs the causes, not just the destination.
        statuses = sorted(k for k, v in SET_ASIDE_DESTINATIONS.items() if v == filename)
        counts[filename] = {
            "rows": rows,
            "title": copy.get("title") or filename.replace(".csv", "").replace("_", " ").title(),
            "why": copy.get("why", ""),
            "action": copy.get("action", ""),
            "statuses": statuses,
        }
    return counts


@flow_bp.route("/api/dashboard/flow")
def api_flow():
    """pool -> release piles -> screened -> rendered rows, each with its provenance."""
    stages: list[dict] = []
    completeness: dict = {}
    release_id: Optional[str] = None

    # Footer-only read (`pool_totals`), which is why the pool can be on a web request
    # at all: the breakdowns read every column of 2,232 partitions and belong in a
    # refresh, not here.
    from shared.dashboard_cache import pool_totals
    try:
        pool = pool_totals()
    except Exception as exc:                     # a panel state, never a 500
        pool = None
        stages.append(_stage("pool", "survivor pool", None,
                             sources.absent("survivor pool", str(exc))))
    else:
        if pool is None:
            stages.append(_stage("pool", "survivor pool", None,
                                 sources.absent("survivor pool",
                                                "pool not on this machine — "
                                                "`python -m search.pool_sync --pull`")))
        else:
            stages.append(_stage("pool", "survivor pool", pool.get("total"),
                                 {"source": "survivor pool", "state": "live",
                                  "release_id": None, "machine": None,
                                  "reason": _pool_shortfall(pool),
                                  "as_of": _pool_recorded_at(pool)}))

    piles_data, store_prov = sources.filtered_stats()
    if not piles_data:
        for sid, label in (("release_piles", "routed works"),
                           ("screened", "admitted for screening")):
            stages.append(_stage(sid, label, None, store_prov))
    else:
        from filter.engine.route import ADMITTED_PILES
        release_id = piles_data.get("release_id")
        piles = piles_data.get("by_pile") or {}
        stages.append(_stage("release_piles", "routed works",
                             piles_data.get("total"), store_prov))
        stages.append(_stage("screened", "admitted for screening",
                             sum(piles.get(p, 0) for p in ADMITTED_PILES), store_prov))
        completeness["by_pile"] = piles
        con, _ = sources.routing_store()
        if con is not None and release_id:
            try:
                completeness["no_text"] = _no_text_count(con, release_id)
            finally:
                con.close()

    df, csv_prov = sources.extracted_csv()
    if df is None:
        stages.append(_stage("rendered", "rendered rows", None, csv_prov))
    else:
        stages.append(_stage("rendered", "rendered rows", len(df), csv_prov))
        completeness["rendered_rows"] = len(df)
        # Both are "what the row never had". A blank abstract is the downstream face
        # of the router's `no_text`: the screen and every abstract-stage rung read it,
        # so a row without one reached the ladder already short of evidence.
        for column, key in (("doi_r", "blank_doi_r"), ("abstract_r", "blank_abstract_r")):
            if column in df.columns:
                blank = df[column].fillna("").astype(str).str.strip().eq("").sum()
                completeness[key] = int(blank)

    return jsonify({"stages": stages, "completeness": completeness,
                    "set_aside": _set_aside_counts(), "release_id": release_id})
