"""Materializing a pile as a Stage 3 CSV, with the routing that produced it.

The export is the engine's only writing hand, and it writes the Stage 2 contract
(`FILTERED_COLS`) plus the provenance columns issue #148 asks for
(`ENGINE_EXPORT_COLS`): what the pool knew — work type, concept hit — and what
the engine decided — winning rule, precedence, every match, release.

`pending` is never exported. A pending row is one the engine has not finished
having an opinion about; materializing it would turn "we don't know" into a
Stage 3 input, which is the exact conversion `no_text` exists to prevent.

The manifest beside the CSV is immutable: an export names a release, a pile and
a content hash, and a second export under the same name would silently change
what a downstream reader believes it read.
"""

import csv
import hashlib
import json
from pathlib import Path
from typing import Optional

import pyarrow.parquet as pq

from filter.engine.spec import FilterSpec, load_specs
from filter.engine.workids import resolve, work_id
from shared.schema import ENGINE_EXPORTED_COLS

SPEC_DIR = Path(__file__).resolve().parents[2] / "filter" / "spec"
CONVENTIONS_PATH = SPEC_DIR / "conventions.json"


def load_conventions(path: Optional[Path] = None) -> dict:
    """`filter/spec/conventions.json` — the pile→status policy, machine-read."""
    return json.loads((path or CONVENTIONS_PATH).read_text(encoding="utf-8"))


def export_pile(con, pool_dir: Path, pile: str, out_csv: Path, release_id: str,
                from_year: Optional[int] = None, to_year: Optional[int] = None,
                conventions: Optional[dict] = None,
                specs: Optional[list[FilterSpec]] = None,
                aliases: Optional[dict[int, int]] = None,
                created_at: str = "") -> dict:
    """Write *pile* of *release_id* to *out_csv* and its manifest beside it.

    *specs* supplies the winning rule's `vocabulary`, which the routing table does
    not store — the pile and the rule id do, and the vocabulary is a property of
    the rule, so it is read back from the bundle rather than duplicated per row.
    *aliases* must be the map `build_routing()` ran with, or the join misses the
    rows whose ids were canonicalised.
    """
    conventions = conventions or load_conventions()
    policy = (conventions.get("piles") or {}).get(pile)
    if policy is None:
        raise ValueError(f"unknown pile {pile!r}")
    if not policy.get("exported"):
        raise ValueError(f"pile {pile!r} is not exported: a pending row has no "
                         "settled routing to hand to Stage 3")

    out_csv = Path(out_csv)
    manifest_path = Path(str(out_csv) + ".manifest.json")
    if manifest_path.exists():
        raise FileExistsError(
            f"{manifest_path} already exists — an export manifest is immutable; "
            "write the new export under a different name")

    routing = _routing_rows(con, release_id, pile)
    vocabularies = {spec.id: spec.vocabulary
                    for spec in (specs if specs is not None else load_specs(SPEC_DIR))}
    prefix = conventions.get("filter_method_prefix", "engine:")

    rows: list[dict] = []
    for path in sorted(Path(pool_dir).glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000):
            for record in batch.to_pylist():
                routed = routing.get(resolve(work_id(record["id"]), aliases or {}))
                if routed is None:
                    continue
                year = record.get("publication_year")
                if from_year is not None and (year is None or year < from_year):
                    continue
                if to_year is not None and (year is None or year > to_year):
                    continue
                rows.append(_export_row(record, routed, policy, prefix, release_id,
                                        vocabularies.get(routed["rule_id"])))

    _write_csv(out_csv, rows)
    return _write_manifest(manifest_path, out_csv, release_id, pile, len(rows),
                           created_at)


def _routing_rows(con, release_id: str, pile: str) -> dict[int, dict]:
    columns = ["work_id", "pile", "pending_reason", "rule_id", "precedence",
               "matched_rules", "evidence"]
    result = con.execute(
        f"SELECT {', '.join(columns)} FROM routing WHERE release_id = ? AND pile = ?",
        [release_id, pile]).fetchall()
    return {row[0]: dict(zip(columns, row)) for row in result}


def _export_row(record: dict, routed: dict, policy: dict, prefix: str,
                release_id: str, vocabulary: Optional[str]) -> dict:
    from search.snapshot_scan import _row_from_snapshot  # avoids a Stage 1 import cycle

    row = _row_from_snapshot(record, abstract=record.get("abstract_text"))
    status = policy["filter_status"]
    if policy.get("vocabulary_names_status") and vocabulary:
        status = vocabulary
    evidence = routed.get("evidence") or ""
    row.update({
        "filter_status": status,
        "filter_method": prefix + release_id[:12],
        "filter_evidence": f"rule:{routed['rule_id']}"
                           + (f"; {evidence}" if evidence else ""),
        "filter_confidence": policy["filter_confidence"],
        "oa_type": record.get("type") or "",
        "hit_concept": record.get("hit_concept"),
        "route_rule": routed["rule_id"],
        "route_precedence": routed["precedence"],
        "matched_rules": "|".join(routed.get("matched_rules") or []),
        "pending_reason": routed.get("pending_reason") or "",
        "release_id": release_id,
    })
    return row


def _write_csv(out_csv: Path, rows: list[dict]) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=ENGINE_EXPORTED_COLS,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: "" if row.get(col) is None else row.get(col)
                             for col in ENGINE_EXPORTED_COLS})


def _write_manifest(manifest_path: Path, out_csv: Path, release_id: str, pile: str,
                    rows: int, created_at: str) -> dict:
    manifest = {
        "release_id": release_id,
        "pile": pile,
        "rows": rows,
        "csv": out_csv.name,
        "sha256": hashlib.sha256(out_csv.read_bytes()).hexdigest(),
        "created_at": created_at,
        "columns": ENGINE_EXPORTED_COLS,
    }
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True),
                             encoding="utf-8")
    return manifest
