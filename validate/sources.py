"""sources.py — every dashboard number arrives with its origin attached.

The dashboard previously rendered `stats.json`'s `filtered.*` block as today's
numbers. That block is written by whichever machine last ran `dashboard_cache.refresh`
and names the release IT routed — on 2026-08-16 that was release f7e4667b from a
different machine, while the live release was 16d370746b45. Nothing on screen said so.

Every reader here therefore returns `(data, provenance)`. A number cannot reach a
template without stating where it came from, which release it belongs to, and how old
it is. `state` is the three-way distinction the old dashboard collapsed:

  live    — read from this machine, this request
  cached  — read from a stored artifact; `as_of` says when it was written
  absent  — not available here; `reason` says why and names the command that fixes it

`absent` is never rendered as zero. "No routing store on this machine" and "zero works
in the discard pile" are different facts and must not look the same.
"""
import datetime
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any, Optional, TypedDict

import pandas as pd

from shared.config import CACHE_DIR, DATA_DIR


class Provenance(TypedDict):
    source: str
    state: str                    # "live" | "cached" | "absent"
    release_id: Optional[str]
    as_of: Optional[str]
    machine: Optional[str]
    reason: Optional[str]


EXTRACTED_PATH = DATA_DIR / "extracted.csv"
STATS_PATH     = DATA_DIR / "dashboard" / "stats.json"
TOKEN_PATH     = CACHE_DIR / "token_usage.json"

# A store path like /Users/<name>/... (or the Windows spelling, separators folded to
# "/" before matching) names the machine that wrote the artifact. Used only to TELL
# the reader; nothing branches on it.
_HOME_RE = re.compile(r"/(?:Users|home)/([^/]+)")


def _prov(source: str, state: str, *, release_id: str = None, as_of: str = None,
          machine: str = None, reason: str = None) -> Provenance:
    return {"source": source, "state": state, "release_id": release_id,
            "as_of": as_of, "machine": machine, "reason": reason}


def absent(source: str, reason: str) -> Provenance:
    """Not available here — with the command that would make it available."""
    return _prov(source, "absent", reason=reason)


def _mtime(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return datetime.datetime.fromtimestamp(
        path.stat().st_mtime).isoformat(timespec="seconds")


def _machine_of(path: str) -> Optional[str]:
    match = _HOME_RE.search(str(path or "").replace(os.sep, "/"))
    return match.group(1) if match else None


def extracted_csv() -> "tuple[pd.DataFrame | None, Provenance]":
    """The rendered verdicts. Small enough (12 MB) to read live on every request."""
    if not EXTRACTED_PATH.exists():
        return None, absent("extracted.csv",
                            "not rendered yet — `python -m extract.export --release <id>`")
    df = pd.read_csv(EXTRACTED_PATH, dtype=str, keep_default_na=False, low_memory=False)
    return df, _prov("extracted.csv", "live", as_of=_mtime(EXTRACTED_PATH))


def stats_json() -> "tuple[dict, Provenance]":
    """The cached stage stats. May have been written by another machine."""
    if not STATS_PATH.exists():
        return {}, absent("stats.json", "no cached stats on this machine")
    data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    filtered = data.get("filtered") or {}
    return data, _prov("stats.json", "cached",
                       release_id=filtered.get("release_id"),
                       as_of=data.get("updated_at") or _mtime(STATS_PATH),
                       machine=_machine_of(filtered.get("store", "")))


def _duckdb_available() -> bool:
    """Guarded: duckdb is declared in requirements.txt but may not be installed."""
    return importlib.util.find_spec("duckdb") is not None


def filtered_stats() -> "tuple[dict, Provenance]":
    """Pile counts for the newest routed release, as a provenance pair.

    `shared.dashboard_cache.compute_filtered_stats` already reads the store live,
    resolves `latest` the way the CLI does, and turns both an unopenable store and an
    unresolvable release into a state rather than an exception — including catching
    the SystemExit that `resolve_release` raises, which Flask would not catch. This
    only translates its `{available, reason, ...}` shape into the provenance contract;
    it does not re-implement any of it.
    """
    from shared.dashboard_cache import compute_filtered_stats

    stats = compute_filtered_stats()
    if not stats.get("available"):
        return {}, absent("routing store", stats.get("reason")
                          or "no routing store on this machine")
    return stats, _prov("routing store", "live",
                        release_id=stats.get("release_id"),
                        as_of=stats.get("release_created_at") or None)


def routing_store() -> "tuple[Any | None, Provenance]":
    """A read-only connection to the local routing store, or an explained absence.

    Read-only because DuckDB gives a read-write connection an exclusive lock: a
    dashboard that opened read-write would block a running screen or extract tier,
    and be blocked by one.
    """
    if not _duckdb_available():
        return None, absent("routing store",
                            "duckdb not installed — `pip install -r requirements.txt`")
    from filter.engine.store import DEFAULT_STORE_PATH, StoreUnavailable, open_store
    if not Path(DEFAULT_STORE_PATH).exists():
        return None, absent("routing store",
                            "no routing store on this machine — "
                            "`python -m search.pool_sync --pull` then "
                            "`python -m filter.engine route`")
    try:
        con = open_store(Path(DEFAULT_STORE_PATH), read_only=True)
    except StoreUnavailable as exc:
        return None, absent("routing store", f"store unreadable: {exc}")
    return con, _prov("routing store", "live",
                      as_of=_mtime(Path(DEFAULT_STORE_PATH)))


def token_usage_record() -> "tuple[dict, Provenance]":
    """The per-day token ledger this checkout has written."""
    if not TOKEN_PATH.exists():
        return {}, absent("token_usage.json", "nothing recorded on this machine yet")
    from shared import token_usage
    return token_usage.all_usage(), _prov("token_usage.json", "live",
                                          as_of=_mtime(TOKEN_PATH))


def pool_totals_live() -> "tuple[dict | None, Provenance]":
    """Survivor-pool row/file/byte counts, read from parquet footers on this request.

    Footer-only and memoised for a minute (`dashboard_cache.pool_totals`), which is why
    the pool can be on a request path at all — the per-column breakdowns cannot, and
    come from `pool_stats` instead.
    """
    from shared.dashboard_cache import pool_totals

    try:
        totals = pool_totals()
    except Exception as exc:                       # a panel state, never a 500
        return None, absent("survivor pool", str(exc))
    if totals is None:
        return None, absent("survivor pool", "no pool on this machine — "
                                             "`python -m search.pool_sync --pull`")
    return totals, _prov("survivor pool", "live", as_of=None)


def pool_stats() -> "tuple[dict, Provenance]":
    """The pool's per-arm and per-year breakdowns, from stats.json.

    Computed by a refresh, never by a request: it reads five columns off every
    partition. The machine is named from the recorded `pool_dir` for the same reason
    `stats_json` names it from the store path — a breakdown someone else's pool
    produced must not read as this one's.
    """
    data, prov = stats_json()
    pool = data.get("pool") or {}
    if not pool:
        return {}, absent("stats.json",
                          "pool breakdowns not computed here — `python -c \"from "
                          "shared.dashboard_cache import refresh, POOL_STAGE; "
                          "refresh(POOL_STAGE)\"`")
    return pool, _prov("stats.json", "cached", as_of=prov.get("as_of"),
                       machine=_machine_of(pool.get("pool_dir", "")))
