"""
dedup_preprint_versions.py — Keep only the latest version of each preprint (issue #17).

Preprint servers mint a fresh DOI per version, e.g.
    10.31234/osf.io/d3x9p_v1 .. _v4
which land in the pipeline as several rows for one work. Rule (#17): keep the highest
`_v<N>`, UNLESS a version-less DOI for the same base exists, in which case keep that
(the version-less DOI is the canonical/published record) and drop the `_v` rows.

Rows whose DOI has no `_v<N>` suffix are never touched unless they are the version-less
base of a group that also has `_v` rows. Runs on any stage CSV; dry-run by default.

    python -m tools.dedup_preprint_versions --input data/extracted.csv          # dry-run
    python -m tools.dedup_preprint_versions --input data/filtered.csv --apply
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from shared.utils import clean_doi, csv_lock

_VER_RE = re.compile(r"^(.*?)_v(\d+)$")


def _base_and_version(doi: str) -> "tuple[str, int | None]":
    """('10.31234/osf.io/d3x9p', 2) for a _v DOI; (doi, None) for a version-less one."""
    doi = clean_doi(doi)
    m = _VER_RE.match(doi)
    return (m.group(1), int(m.group(2))) if m else (doi, None)


def superseded_indices(df: pd.DataFrame, doi_col: str = "doi_r") -> list:
    """Row indices to DROP: every preprint version that is not the survivor of its base
    group. A group is only touched when it has at least one `_v` DOI."""
    base, ver = {}, {}
    for i, d in df[doi_col].items():
        b, v = _base_and_version(str(d or ""))
        if not b:
            continue
        base[i], ver[i] = b, v

    groups: dict[str, list] = {}
    for i, b in base.items():
        groups.setdefault(b, []).append(i)

    drop: list = []
    for b, idxs in groups.items():
        if len(idxs) < 2 or all(ver[i] is None for i in idxs):
            continue  # nothing to collapse
        versionless = [i for i in idxs if ver[i] is None]
        if versionless:
            keep = versionless[0]          # canonical published DOI wins
        else:
            keep = max(idxs, key=lambda i: ver[i])  # highest _v
        drop.extend(i for i in idxs if i != keep)
    return drop


def dedup_file(path: Path, apply: bool = False) -> dict:
    path = Path(path)
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    drop = superseded_indices(df)
    print(f"{path}: {len(df)} rows, {len(drop)} superseded preprint versions")
    for i in drop[:10]:
        print(f"  drop {df.at[i, 'doi_r']}")
    if drop and len(drop) > 10:
        print(f"  … and {len(drop) - 10} more")
    if not apply or not drop:
        if not apply:
            print("[dry-run] pass --apply to write")
        return {"total": len(df), "dropped": len(drop), "written": False}

    with csv_lock(path):
        df.drop(index=drop).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(df) - len(drop)} rows -> {path}")
    return {"total": len(df), "dropped": len(drop), "written": True}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Drop superseded preprint versions.")
    p.add_argument("--input", type=Path, default=Path("data/extracted.csv"))
    p.add_argument("--apply", action="store_true", help="Write in place (default: dry-run).")
    a = p.parse_args()
    dedup_file(a.input, apply=a.apply)
