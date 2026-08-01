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
"""

from pathlib import Path
from typing import Callable, Iterable

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

    def load(self) -> set[str]:
        if not self.path.exists():
            return set()
        index: set[str] = set()
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                key = line.strip()
                if key:
                    index.add(key)
        log.info("%s index loaded: %d keys from disk", self.label, len(index))
        return index

    def save(self, keys: Iterable[str]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(k + "\n" for k in keys)
        tmp.replace(self.path)

    def append(self, keys: Iterable[str]) -> None:
        """Append new keys — never rewrites the full file."""
        with open(self.path, "a", encoding="utf-8") as f:
            f.writelines(k + "\n" for k in keys)

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

    A row is a duplicate when its strongest key has already been seen; rows with no
    identifier at all are always kept. Output is streamed to a temp file and moved
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
            keys = index.key_fn(row)
            primary = keys[0] if keys else ""
            if not primary or primary not in seen_keys:
                seen_keys.update(k for k in keys if k)
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
