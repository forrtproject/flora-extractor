"""
csv_index.py — sidecar key indexes for the multi-million-row pipeline CSVs.

candidates.csv and filtered.csv both grow past a million rows, so neither stage
can load its own output to answer "have I already written this row?". Each keeps
a plain-text index of row keys next to it in cache/ and consults that instead.
Stage 1 and Stage 2 had grown separate copies of the same four functions, one of
which built the whole file as a single string in memory — the exact failure the
other had already been fixed for.

Reads and writes stream line-by-line: at 2.9M keys the index file is large enough
that materialising it whole has caused MemoryError on Windows.

A loaded index is memoised per instance (see ``KeyIndex.load``): the snapshot
scanner merges once per pyarrow batch — thousands of times per run — and each
merge starts by loading the index, which at 7M keys costs seconds and a GB of
RSS every time.
"""

import os
from pathlib import Path
from typing import Callable, Iterable, Optional

import pandas as pd

from shared.config import log

_CHUNK = 50_000


class KeyIndex:
    """A set of row keys persisted one-per-line at *path*.

    *key_fn* maps a row to the keys it should be indexed under. The candidates
    index stores every key a row has, so a duplicate is caught via any identifier;
    the filtered index stores only the strongest one, which is all a resume check
    needs.
    """

    def __init__(self, path: Path, key_fn: "Callable[[dict], list[str]]", label: str):
        self.path = path
        self.key_fn = key_fn
        self.label = label
        self._cache: Optional[set[str]] = None
        self._cache_stamp: Optional[tuple] = None

    def _stamp(self) -> tuple:
        """Identity of the file a cached set was read from: path, size, mtime.

        Anything that changes the file — our own append, a rebuild, another
        process, a test monkeypatching ``path`` — moves the stamp, so a stale
        cache is never returned.
        """
        try:
            st = os.stat(self.path)
        except OSError:
            return (str(self.path), None, None)
        return (str(self.path), st.st_size, st.st_mtime_ns)

    def _remember(self, index: set[str]) -> None:
        self._cache = index
        self._cache_stamp = self._stamp()

    def load(self) -> set[str]:
        """The index as a set, re-read from disk only when the file has changed.

        The returned set is the memoised object itself, not a copy: at 7M keys a
        copy per call would cost as much as the read it replaces. Callers that
        add a key to it must also ``append`` that key, which is what keeps the
        cached set and the file describing the same rows.
        """
        if not self.path.exists():
            return set()
        stamp = self._stamp()
        if self._cache is not None and self._cache_stamp == stamp:
            return self._cache
        index: set[str] = set()
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                key = line.strip()
                if key:
                    index.add(key)
        log.info("%s index loaded: %d keys from disk", self.label, len(index))
        self._remember(index)
        return index

    def save(self, keys: Iterable[str]) -> None:
        keys = keys if isinstance(keys, set) else set(keys)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(k + "\n" for k in keys)
        tmp.replace(self.path)
        self._remember(keys)

    def append(self, keys: Iterable[str]) -> None:
        """Append new keys — never rewrites the full file."""
        keys = list(keys)
        # Only a cache that still describes THIS file can absorb the new keys; one
        # taken from another file (or a since-rewritten one) must be dropped, or the
        # append would carry a set of foreign keys onto the file we are writing.
        coherent = self._cache is not None and self._cache_stamp == self._stamp()
        with open(self.path, "a", encoding="utf-8") as f:
            f.writelines(k + "\n" for k in keys)
        if coherent:
            # Kept in step with the file rather than dropped: the next load() would
            # otherwise re-read the whole index from disk, once per merged batch.
            self._cache.update(k for k in keys if k)
            self._cache_stamp = self._stamp()
        else:
            self._cache = self._cache_stamp = None

    def build(self, csv_path: Path) -> set[str]:
        """Rebuild the index from *csv_path* in chunks and write it to disk."""
        log.info("Building %s index from %s (reading in chunks)...", self.label, csv_path)
        index: set[str] = set()
        for chunk in pd.read_csv(
            csv_path, encoding="utf-8-sig", dtype=str, chunksize=_CHUNK, low_memory=False
        ):
            chunk = chunk.fillna("")
            for row in chunk.to_dict("records"):
                index.update(k for k in self.key_fn(row) if k)
        log.info("%s index built: %d keys — saving to disk", self.label, len(index))
        self.save(index)
        return index

    def load_or_build(self, csv_path: Path) -> set[str]:
        index = self.load()
        if index:
            return index
        if csv_path.exists():
            return self.build(csv_path)
        return set()


def dedup_csv(csv_path: Path, index: KeyIndex, dry_run: bool = False) -> tuple[int, int]:
    """Remove duplicate rows from *csv_path* in place, keeping the first occurrence.

    A row is a duplicate when ANY of its keys has already been seen — the same rule
    the incremental merge applies, so a rebuild and an append can never disagree
    about which rows are duplicates. Rows with no identifier at all are always kept. Output is streamed to a temp file and moved
    over the original only after the whole file has been read, so an interrupted run
    leaves the original intact. The index is rebuilt afterwards because the row set
    it describes has changed.

    Returns (rows_before, rows_after).
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path.name} not found at {csv_path}")

    seen_keys: set[str] = set()
    rows_before = rows_after = 0
    tmp_path = csv_path.with_suffix(".dedup.tmp")
    first_write = True

    for chunk in pd.read_csv(
        csv_path, encoding="utf-8-sig", dtype=str, chunksize=_CHUNK,
        on_bad_lines="skip", low_memory=False,
    ):
        chunk = chunk.fillna("")
        rows_before += len(chunk)

        keep_mask: list[bool] = []
        for row in chunk.to_dict("records"):
            keys = [k for k in index.key_fn(row) if k]
            if not keys or not any(k in seen_keys for k in keys):
                seen_keys.update(keys)
                keep_mask.append(True)
                rows_after += 1
            else:
                keep_mask.append(False)

        if not dry_run:
            kept = chunk[keep_mask]
            if not kept.empty:
                kept.to_csv(
                    tmp_path,
                    mode="w" if first_write else "a",
                    index=False,
                    encoding="utf-8-sig" if first_write else "utf-8",
                    header=first_write,
                )
                first_write = False

    log.info(
        "dedup %s: %d -> %d rows (%d duplicates%s)",
        csv_path.name, rows_before, rows_after, rows_before - rows_after,
        " -- dry run, no changes written" if dry_run else "",
    )

    if not dry_run and not first_write:
        tmp_path.replace(csv_path)
        log.info("%s replaced -- rebuilding %s index...", csv_path.name, index.label)
        index.build(csv_path)

    return rows_before, rows_after
