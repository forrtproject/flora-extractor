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
    read_cache_migrating(cache_dir, key, legacy_keys, migrate_note) → dict | None
    write_cache(cache_dir, key, data, suffix=".json") → None
    content_key(prefix, doi, *parts) → str
    clear_cache(cache_dir, key, suffixes=None) → list[str]
    clear_content_keys(cache_dir, prefix, doi, suffix=".json") → list[str]
"""
import json
import os
import threading
from pathlib import Path
from typing import Optional, Sequence

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


def read_cache_migrating(cache_dir: Path, key: str, legacy_keys: Sequence[str],
                         migrate_note: dict) -> Optional[dict]:
    """read_cache(*key*), falling back to keys a maintainer declared equivalent.

    Keys stay strict and automatic: a changed prompt or model still writes a new key
    and re-pays for the answer. This is the opt-in escape hatch (issue #171) for the
    case where the key moved but the answer provably did not — a mislabelled key
    component, or a prompt edit reviewed and judged answer-preserving. Each
    *legacy_keys* entry is registered at its call site with a rationale comment, so
    accepting one is a commit, not a runtime guess.

    A legacy hit is re-stored under *key* with a `cache_migrated` note (whatever the
    call site declares, plus the key it came from), so any response on disk stays
    traceable to the prompt version and models that produced it. The legacy file is
    left untouched: other checkouts and the shared HF cache still hit it.
    """
    entry = read_cache(cache_dir, key)
    if entry is not None:
        return entry
    for legacy in legacy_keys:
        entry = read_cache(cache_dir, legacy)
        if entry is not None:
            migrated = {**entry, "cache_migrated": {**migrate_note, "from_key": legacy}}
            write_cache(cache_dir, key, migrated)
            return migrated
    return None


def write_json(path: Path, data: object, indent: "int | None" = None) -> None:
    """Write *data* to *path* as JSON, through a temp file and os.replace.

    What a reader can see at the path is therefore either the previous file or the
    whole new one, never half of each. Two writers of one cache file are not a
    conflict of answers — the key names its content — but they are a conflict of
    bytes: Stage 3 runs its rows in a thread pool, two rows can want the same
    original's metadata at the same moment, and two plain `open("w")` writes
    interleave into a file whose next read raises or misses. A miss re-pays for the
    call the cache existed to avoid; the raise ends a row as api_error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_cache(cache_dir: Path, key: str, data: dict, suffix: str = ".json") -> None:
    """Write *data* to cache as JSON. Creates parent directories if needed."""
    write_json(cache_dir / f"{key}{suffix}", data, indent=2)


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
