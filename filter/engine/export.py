"""Materializing a pile as a Stage 3 CSV, with the routing that produced it.

The export is the engine's only writing hand, and it writes the Stage 2 contract
(`FILTERED_COLS`) plus the provenance columns issue #148 asks for
(`ENGINE_EXPORT_COLS`): what the pool knew — work type, concept hit — and what
the engine decided — winning rule, precedence, every match, release.

`pending` is never exported. A pending row is one the engine has not finished
having an opinion about; materializing it would turn "we don't know" into a
Stage 3 input, which is the exact conversion `no_text` exists to prevent.

An export reads the bundle again — the winning rule's `vocabulary` and the
pile→status mapping are not in the routing table — so it must prove that the
bundle it is reading is the one the release was routed under. `expect_*` are how
the caller passes the release's own record in; a mismatch is refused outright.
There is no override: a stale client re-routes (issue #146 §4), because an
export labelled with a release id it does not match is worse than no export.

The manifest beside the CSV is immutable: an export names a release, a pile and
a content hash, and a second export under the same name would silently change
what a downstream reader believes it read.
"""

import csv
import hashlib
import json
from pathlib import Path
from typing import Iterator, Optional

from filter.engine.pool_reader import iter_pool_batches, overlay_manifest_hash
from filter.engine.spec import FilterSpec, bundle_hash, load_specs
from filter.engine.workids import alias_release, resolve, work_id
from shared.schema import ENGINE_EXPORTED_COLS

SPEC_DIR = Path(__file__).resolve().parents[2] / "filter" / "spec"
CONVENTIONS_PATH = SPEC_DIR / "conventions.json"
ALIASES_FILENAME = "aliases.json"

# "Don't check the overlay binding" — None cannot mean it, because a release
# routed with no overlay legitimately expects None. Public because every caller
# that forwards the parameter must forward THIS object: a second sentinel would
# be "unchecked" to its own module and a mismatch to this one.
UNCHECKED: object = object()


def load_conventions(path: Optional[Path] = None) -> dict:
    """`filter/spec/conventions.json` — the pile→status policy, machine-read."""
    return json.loads((path or CONVENTIONS_PATH).read_text(encoding="utf-8"))


def export_pile(con, pool_dir: Path, pile: str, out_csv: Path, release_id: str,
                from_year: Optional[int] = None, to_year: Optional[int] = None,
                conventions: Optional[dict] = None,
                specs: Optional[list[FilterSpec]] = None,
                aliases: Optional[dict[int, int]] = None,
                spec_dir: Path = SPEC_DIR,
                expect_bundle_hash: Optional[str] = None,
                expect_alias_release: Optional[str] = None,
                overlay_dir: Optional[Path] = None,
                expect_overlay_hash: object = UNCHECKED,
                created_at: str = "") -> dict:
    """Write *pile* of *release_id* to *out_csv* and its manifest beside it.

    *specs* supplies the winning rule's `vocabulary`, which the routing table does
    not store — the pile and the rule id do, and the vocabulary is a property of
    the rule, so it is read back from the bundle rather than duplicated per row.
    *aliases* must be the map `build_routing()` ran with, or the join misses the
    rows whose ids were canonicalised.

    *expect_bundle_hash* / *expect_alias_release* / *expect_overlay_hash* come from
    the release record and are checked against *spec_dir* / *overlay_dir* before
    anything is written. Overlay text is coalesced into the exported rows exactly
    as it was during routing — Stage 3 must see the text the pile decision saw.
    """
    check_release_binding(spec_dir, release_id, expect_bundle_hash,
                          expect_alias_release, overlay_dir, expect_overlay_hash)
    out_csv = Path(out_csv)
    manifest_path = Path(str(out_csv) + ".manifest.json")
    if manifest_path.exists():
        raise FileExistsError(
            f"{manifest_path} already exists — an export manifest is immutable; "
            "write the new export under a different name")

    rows = [row for _, _, row in iter_export_rows(
        con, pool_dir, [pile], release_id, from_year=from_year, to_year=to_year,
        conventions=conventions, specs=specs, aliases=aliases, spec_dir=spec_dir,
        overlay_dir=overlay_dir)]

    _write_csv(out_csv, rows)
    return _write_manifest(manifest_path, out_csv, release_id, pile, len(rows),
                           created_at)


def iter_export_rows(con, pool_dir: Path, piles: list[str], release_id: str,
                     from_year: Optional[int] = None, to_year: Optional[int] = None,
                     conventions: Optional[dict] = None,
                     specs: Optional[list[FilterSpec]] = None,
                     aliases: Optional[dict[int, int]] = None,
                     spec_dir: Path = SPEC_DIR,
                     overlay_dir: Optional[Path] = None,
                     ) -> Iterator[tuple[str, int, dict]]:
    """`(pile, work_id, row)` per work of *piles*, in pool order, ready for the CSV.

    The alias-resolved work id travels with the row because the CSV's
    `openalex_id_r` is the pool's raw id and a caller keying anything by work —
    a tier verdict, say — must key it by the id the routing table used.

    One pass over the pool for however many piles are asked for — `export_pile()`
    asks for one, `handoff.py` asks for both screen piles — because streaming
    5.1M rows once per pile is the cost this function exists to avoid. The caller
    orders the result; the pool's order is not the piles' order.

    Binding is NOT checked here: the caller checks it once, before writing
    anything, so a refusal happens before the first row rather than after the
    first million.
    """
    conventions = conventions or load_conventions()
    policies = {}
    for pile in piles:
        policy = (conventions.get("piles") or {}).get(pile)
        if policy is None:
            raise ValueError(f"unknown pile {pile!r}")
        if not policy.get("exported"):
            raise ValueError(f"pile {pile!r} is not exported: a pending row has no "
                             "settled routing to hand to Stage 3")
        policies[pile] = policy

    routing: dict[int, dict] = {}
    for pile in piles:
        routing.update(_routing_rows(con, release_id, pile))
    vocabularies = {spec.id: spec.vocabulary
                    for spec in (specs if specs is not None else load_specs(spec_dir))}
    prefix = conventions.get("filter_method_prefix", "engine:")

    seen: set[int] = set()
    for batch in iter_pool_batches(pool_dir, overlay_dir, aliases=aliases):
        for record in batch.to_pylist():
            # One row per WORK, matching the store's key: an aliased pool row
            # and its canonical row are one work and must export once.
            resolved = resolve(work_id(record["id"]), aliases or {})
            routed = routing.get(resolved)
            if routed is None or resolved in seen:
                continue
            seen.add(resolved)
            year = record.get("publication_year")
            if from_year is not None and (year is None or year < from_year):
                continue
            if to_year is not None and (year is None or year > to_year):
                continue
            yield routed["pile"], resolved, _export_row(
                record, routed, policies[routed["pile"]], prefix, release_id,
                vocabularies.get(routed["rule_id"]))


class StaleBundleError(RuntimeError):
    """The bundle on disk is not the one *release_id* was routed under."""


def check_release_binding(spec_dir: Path, release_id: str,
                          expect_bundle_hash: Optional[str],
                          expect_alias_release: Optional[str],
                          overlay_dir: Optional[Path] = None,
                          expect_overlay_hash: object = UNCHECKED) -> None:
    mismatches = []
    if expect_overlay_hash is not UNCHECKED:
        # None is a real expectation here (routed with no overlay), so the
        # unchecked case needs its own sentinel rather than reusing None.
        actual = overlay_manifest_hash(overlay_dir) if overlay_dir else None
        if actual != expect_overlay_hash:
            mismatches.append(
                f"overlay_hash: release {str(expect_overlay_hash)[:12]}, "
                f"{overlay_dir or 'no overlay'} now {str(actual)[:12]}")
    if expect_bundle_hash:
        actual = bundle_hash(spec_dir)
        if actual != expect_bundle_hash:
            mismatches.append(f"bundle_hash: release {expect_bundle_hash[:12]}, "
                              f"{spec_dir} now {actual[:12]}")
    if expect_alias_release:
        actual = alias_release(spec_dir / ALIASES_FILENAME)
        if actual != expect_alias_release:
            mismatches.append(f"alias_release: release {expect_alias_release[:12]}, "
                              f"{spec_dir} now {actual[:12]}")
    if mismatches:
        raise StaleBundleError(
            f"release {release_id[:12]} was routed under a different bundle — "
            + "; ".join(mismatches)
            + ". The export reads the bundle for vocabulary and pile policy, so it "
              "would label these rows with a release they do not match. Re-run "
              "`python -m filter.engine route` and export the new release.")


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
