"""
cache.py — Cache read/write/clear helpers.

All cache files live under CACHE_DIR (defined in shared/config.py).
Keys are generated with cache_key() from shared/utils.py.

Public API:
    read_cache(cache_dir, key, suffix=".json") → dict | None
    write_cache(cache_dir, key, data, suffix=".json") → None
    clear_cache(cache_dir, key, suffixes=None) → list[str]
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
