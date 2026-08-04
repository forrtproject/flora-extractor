"""Routing releases: the id that names everything a routing decision depended on.

A release id is the sha256 of six inputs — the pool text, the text overlay, the
spec bundle, the engine, the alias map and the CSV schema. Anything that could
move a row between piles is in there, so two runs with the same id are the same
routing and a changed input mints a new one instead of silently overwriting.

`overlay_hash` is None until M3 (text overlays) and `pool_manifest_hash` comes
from `search.pool_sync.pool_manifest()`; both are named parameters now so a later
milestone adds a value, not a field.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from shared.config import ENGINE_CACHE_DIR

# The exact six inputs, in the order the contract names them.
RELEASE_INPUTS = ("pool_manifest_hash", "overlay_hash", "bundle_hash",
                  "engine_version", "alias_release", "schema_version")


def routing_release(pool_manifest_hash: Optional[str], overlay_hash: Optional[str],
                    bundle_hash: str, engine_version: str, alias_release: str,
                    schema_version: str) -> str:
    """The release id: sha256 over the canonical JSON of the six inputs."""
    return _release_id({
        "pool_manifest_hash": pool_manifest_hash,
        "overlay_hash": overlay_hash,
        "bundle_hash": bundle_hash,
        "engine_version": engine_version,
        "alias_release": alias_release,
        "schema_version": schema_version,
    })


def _release_id(inputs: dict) -> str:
    payload = json.dumps({k: inputs.get(k) for k in RELEASE_INPUTS},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def releases_dir(cache_dir: Optional[Path] = None) -> Path:
    return (cache_dir or ENGINE_CACHE_DIR) / "releases"


def write_release(release: dict, cache_dir: Optional[Path] = None) -> Path:
    """Persist *release* (the six inputs plus `created_at`) under its own id.

    `created_at` is passed in rather than read from the clock so the function is
    a pure mapping from its argument to a file, and so a test can assert the
    bytes it writes.
    """
    missing = [k for k in RELEASE_INPUTS if k not in release]
    if missing:
        raise ValueError(f"release is missing inputs: {', '.join(missing)}")
    if not release.get("created_at"):
        raise ValueError("release needs a created_at timestamp (ISO 8601)")
    release_id = _release_id(release)
    record = {k: release.get(k) for k in RELEASE_INPUTS}
    record["release_id"] = release_id
    record["created_at"] = release["created_at"]
    path = releases_dir(cache_dir) / f"{release_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return path


def read_release(release_id: str, cache_dir: Optional[Path] = None) -> dict:
    """The stored release record for *release_id*."""
    return json.loads((releases_dir(cache_dir) / f"{release_id}.json")
                      .read_text(encoding="utf-8"))
