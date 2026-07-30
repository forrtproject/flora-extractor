"""
cache.py — Cache read/write/clear helpers.

All cache files live under CACHE_DIR (defined in shared/config.py).
Keys are generated with cache_key() from shared/utils.py.

An LLM cache entry is named by content_key(): `<prefix>_<doi hash>_<content hash>`,
where the content hash covers everything the answer depends on — the prompt version
(shared/prompts.py), the model, and the inputs actually sent. The DOI hash sits in
the middle of the name purely so a single paper's entries can still be found and
purged with a glob; it is never the whole key.

Public API:
    read_cache(cache_dir, key, suffix=".json") → dict | None
    write_cache(cache_dir, key, data, suffix=".json") → None
    content_key(prefix, doi, *parts) → str
    clear_cache(cache_dir, key, suffixes=None) → list[str]
    clear_content_keys(cache_dir, prefix, doi, suffix=".json") → list[str]
"""
import json
from pathlib import Path
from typing import Optional

from .utils import cache_key


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


def content_key(prefix: str, doi: str, *parts: object) -> str:
    """Return `<prefix>_<hash of doi>_<hash of parts>`.

    *parts* must name everything the cached answer depends on: the prompt version,
    the model(s) that will answer, and the inputs actually sent. Values are
    stringified and joined, so a changed candidate list or reference list produces a
    different key rather than replaying the previous paper's answer.
    """
    joined = "␟".join("" if p is None else str(p) for p in parts)
    return f"{prefix}_{cache_key(doi or '')}_{cache_key(joined)}"


def clear_content_keys(cache_dir: Path, prefix: str, doi: str,
                       suffix: str = ".json") -> list[str]:
    """Delete every content-keyed entry for *doi* under *prefix*. Returns filenames."""
    deleted: list[str] = []
    for path in cache_dir.glob(f"{prefix}_{cache_key(doi or '')}_*{suffix}"):
        try:
            path.unlink()
            deleted.append(path.name)
        except OSError:
            pass
    return deleted


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
