"""
cache.py — Cache read/write/clear helpers.

All cache files live under CACHE_DIR (defined in shared/config.py).
Keys are generated with cache_key() from shared/utils.py.

Public API:
    read_cache(cache_dir, key, suffix=".json") → dict | None
    write_cache(cache_dir, key, data, suffix=".json") → None
    clear_cache(cache_dir, key, suffixes=None) → list[str]
    read_dual_cache(cache_dir, legacy_key, content_key, mode, suffix=".json") → dict | None
    write_dual_cache(cache_dir, legacy_key, content_key, data, suffix=".json") → None
"""
import json
from pathlib import Path
from typing import Optional


def read_cache(cache_dir: Path, key: str, suffix: str = ".json") -> Optional[dict]:
    """Return cached dict for *key*, or None if not cached."""
    path = cache_dir / f"{key}{suffix}"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def write_cache(cache_dir: Path, key: str, data: dict, suffix: str = ".json") -> None:
    """Write *data* to cache as JSON. Creates parent directories if needed."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}{suffix}"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def read_dual_cache(cache_dir: Path, legacy_key: str, content_key: str,
                    mode: str = "accumulate", suffix: str = ".json") -> Optional[dict]:
    """Read a dual-written LLM cache entry (see write_dual_cache).

    Cached LLM results are stored under two keys: a stable *legacy_key* (e.g. keyed
    on the DOI alone) and a *content_key* that also folds in the model, prompt
    version and input hash. The read mode controls which one wins:

    - "accumulate" (default): prefer the legacy entry if present — this preserves
      previously-computed results across prompt/model changes, useful when
      experimenting so old answers are not thrown away. Falls back to the content
      key when no legacy entry exists.
    - "latest": read ONLY the content-keyed entry — this guarantees the result
      matches the current prompt/model/input, at the cost of ignoring legacy
      entries. Use in production runs where correctness-to-current-prompt matters.
    """
    if mode == "latest":
        return read_cache(cache_dir, content_key, suffix)
    legacy = read_cache(cache_dir, legacy_key, suffix)
    if legacy is not None:
        return legacy
    return read_cache(cache_dir, content_key, suffix)


def write_dual_cache(cache_dir: Path, legacy_key: str, content_key: str,
                     data: dict, suffix: str = ".json") -> None:
    """Write *data* under BOTH the legacy key and the content key.

    Doubles disk usage for the entry but lets either read mode (accumulate /
    latest) find it. See read_dual_cache for how the two keys are used.
    """
    write_cache(cache_dir, legacy_key, data, suffix)
    write_cache(cache_dir, content_key, data, suffix)


def clear_cache(cache_dir: Path, key: str,
                suffixes: Optional[list[str]] = None) -> list[str]:
    """
    Delete all cache files matching *key* + each suffix.
    Returns list of filenames actually deleted.
    """
    if suffixes is None:
        suffixes = [".json"]
    deleted: list[str] = []
    for suffix in suffixes:
        path = cache_dir / f"{key}{suffix}"
        if path.exists():
            try:
                path.unlink()
                deleted.append(path.name)
            except Exception:
                pass
    return deleted
