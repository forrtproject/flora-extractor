"""api_stage1.py — Stage 1 read out of the code and the artifact it produced.

Two kinds of fact live here and they are never mixed.

*What Stage 1 asks* — the stem alternation, the concept ids, the gate fingerprint,
the pool's column schema — is read from the running modules
(`filter/phrase_detection.py`, `search/snapshot_scan.py`), the same discipline
`api_docs.py` follows: edit the vocabulary and this page changes with it.

*What Stage 1 produced* — row counts, the per-arm admissions, the year spread — is
read from the artifact, and arrives with its provenance. Footer counts (`pool_totals`,
memoised) are cheap enough for a request; the per-arm and per-year breakdowns read
every partition's columns and are therefore taken from `stats.json`, never recomputed
here. A missing breakdown is `absent` with the refresh command, never zero.
"""
import json
import re
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify

from validate import sources

stage1_bp = Blueprint("stage1_api", __name__)

# `"C12590798",   # Replication (statistics) — 263k works` — the id and the note the
# maintainer wrote beside it. Read rather than restated: a concept swapped in the
# vocabulary without its comment here would otherwise be described by the old name.
_CONCEPT_NOTE = re.compile(r'"(C\d+)"\s*,\s*#\s*(.+?)\s*$', re.MULTILINE)

# What each pool column is FOR. The schema itself comes from `_POOL_SCHEMA`, so a
# column added there shows up with an empty role rather than being hidden.
_COLUMN_ROLE = {
    "id": "identity", "doi": "identity", "title": "identity",
    "display_name": "identity", "publication_year": "metadata", "type": "metadata",
    "authorships": "metadata", "primary_location": "metadata",
    "open_access": "metadata", "concepts": "metadata",
    "abstract_text": "text", "hit_token_title": "why it was kept",
    "hit_token_abstract": "why it was kept", "hit_concept": "why it was kept",
}


def _gate() -> dict:
    """The search gate's two arms, as the scanner will actually evaluate them."""
    from filter import phrase_detection as PD
    from search.snapshot_scan import search_gate_fingerprint

    # The pattern is one alternation with an inline `(?i)` flag; the stems are what a
    # reader needs, so the flag is separated rather than shown as a stem.
    pattern = PD.REPLICATION_STEM_PATTERN
    stems = [s for s in pattern.replace("(?i)", "").split("|") if s]

    source = Path(PD.__file__).read_text(encoding="utf-8")
    notes = dict(_CONCEPT_NOTE.findall(source))
    concepts = [{"id": c, "note": notes.get(c, ""),
                 "url": f"https://openalex.org/{c}"} for c in PD.CONCEPT_IDS]

    return {
        "pattern": pattern,
        "case_insensitive": pattern.startswith("(?i)"),
        "stems": stems,
        "concepts": concepts,
        "fingerprint": search_gate_fingerprint(),
        "module": "filter/phrase_detection.py",
    }


def _snapshot() -> dict:
    """The corpus Stage 1 reads, as this machine knows it.

    The manifest and the scan ledger are written by a scan; a checkout that pulled the
    pool has neither, and the honest answer there is "not on this machine" with the
    command that would fetch it — not a number copied out of a doc. `record_count` is
    OpenAlex's own per-partition count, so when the manifest IS here the corpus size
    is read rather than remembered.
    """
    from shared.config import SNAPSHOT_BASE_URL, SNAPSHOT_CACHE_DIR

    record: dict[str, Any] = {"base_url": SNAPSHOT_BASE_URL, "files": None,
                              "records": None, "bytes": None, "scanned_files": None,
                              "gate": None, "reason": None}
    try:
        manifest = json.loads((SNAPSHOT_CACHE_DIR / "manifest.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record["reason"] = ("no snapshot manifest on this machine — it is fetched by "
                            "`python -m search.run_search --scan`, which a checkout "
                            "that pulled the pool never runs")
    else:
        entries = manifest.get("entries") or manifest.get("files") or []
        stats = [(e.get("meta") or e) for e in entries if isinstance(e, dict)]
        record["files"] = len(entries)
        record["records"] = sum(s.get("record_count") or 0 for s in stats) or None
        record["bytes"] = sum(s.get("content_length") or 0 for s in stats) or None

    try:
        ledger = json.loads((SNAPSHOT_CACHE_DIR / "ledger.json")
                            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return record
    record["scanned_files"] = len(ledger.get("files") or {})
    from search.snapshot_scan import ledger_gate_fingerprint
    record["gate"] = ledger_gate_fingerprint(ledger)
    return record


def _pool_schema() -> list[dict]:
    from search.snapshot_scan import _POOL_SCHEMA

    return [{"name": f.name, "type": str(f.type),
             "role": _COLUMN_ROLE.get(f.name, "")} for f in _POOL_SCHEMA]


def _sidecar(pool_dir: str) -> dict:
    """`_pool_provenance.json` — the gate the pool's rows were ADMITTED under.

    Read as plain JSON rather than through `read_pool_provenance`, which raises on a
    damaged sidecar: that is the right behaviour for a run about to route, and the
    wrong behaviour for a page whose job is to show the reader what state they are in.
    """
    from search.snapshot_scan import POOL_PROVENANCE

    try:
        return json.loads((Path(pool_dir) / POOL_PROVENANCE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _overlay() -> dict:
    """The text overlay beside the pool: how much text arrived, from where, frozen or not.

    An unfrozen overlay is reported as such rather than as an error. It is a real and
    recoverable state — chunks appended since the last `freeze()` — and it is exactly
    what a reader needs told, because routing under it refuses.
    """
    from filter.engine import overlay as O
    from shared.config import OVERLAY_DIR

    chunks = O.chunk_paths(OVERLAY_DIR) if Path(OVERLAY_DIR).is_dir() else []
    record: dict[str, Any] = {"dir": str(OVERLAY_DIR), "chunks": len(chunks),
                              "hash": None, "rows": None, "sources": {},
                              "frozen": False, "created_at": None}
    if not chunks:
        return record
    try:
        record["hash"] = O.overlay_manifest_hash(OVERLAY_DIR)
    except Exception as exc:                      # unfrozen, or a broken pointer
        record["reason"] = str(exc)
        return record
    record["frozen"] = record["hash"] is not None
    try:
        manifest = O.read_manifest(Path(OVERLAY_DIR))
    except (OSError, ValueError, KeyError):
        return record
    record["rows"] = manifest.get("rows")
    record["sources"] = manifest.get("sources") or {}
    record["created_at"] = manifest.get("created_at")
    return record


@stage1_bp.route("/api/stage1")
def api_stage1():
    """The gate, the pool it produced, where both live, and how Stage 2 reads them."""
    from filter.engine.release import RELEASE_INPUTS
    from shared.config import FLORA_POOL_REPO, SNAPSHOT_BASE_URL

    gate = _gate()

    totals, totals_prov = sources.pool_totals_live()
    stats, stats_prov = sources.pool_stats()

    pool: dict[str, Any] = {"totals": totals, "provenance": totals_prov}
    if totals:
        sidecar = _sidecar(totals["pool_dir"])
        pool["sidecar"] = sidecar
        expected = sidecar.get("expected_files")
        pool["complete"] = not (isinstance(expected, int) and totals["files"] < expected)
        # The pool's own gate against this checkout's. Legitimate when they differ —
        # sharing a pool is the point — but it decides what the rows on disk mean.
        recorded = sidecar.get("search_gate_fingerprint")
        pool["gate_matches_checkout"] = (recorded == gate["fingerprint"]) if recorded else None

    return jsonify({
        "snapshot": _snapshot(),
        "gate": gate,
        "pool": pool,
        "breakdown": stats,               # per-arm, per-year, no_doi — may be {}
        "breakdown_provenance": stats_prov,
        "schema": _pool_schema(),
        "overlay": _overlay(),
        "remote": {"repo": FLORA_POOL_REPO or None, "snapshot": SNAPSHOT_BASE_URL},
        "release_inputs": list(RELEASE_INPUTS),
    })
