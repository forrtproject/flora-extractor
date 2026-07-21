"""
migrate_link_methods.py — One-off migration of legacy link_method values.

Stage 3 used to collapse five internal rule-based resolution methods
(citation_context_match, same_author_year_title_overlap,
single_candidate_after_requery, title_pattern_match, grobid_ref_match) into a
single public link_method value, "author_year_match". Those methods are now kept
distinct, but rows written under the old scheme cannot be disaggregated
retroactively — the specific method was never recorded.

This helper rewrites any surviving literal "author_year_match" rows in an
extracted.csv to "author_year_match_legacy" so the ambiguous legacy value is
visibly distinct from the new granular labels. Dry-run by default, like
extract/audit_dois.py — pass --apply to write.

Do NOT run this on production data without agreement: it rewrites the
link_method column in place.

Usage:
    python -m tools.migrate_link_methods --input data/extracted.csv            # dry-run
    python -m tools.migrate_link_methods --input data/extracted.csv --apply    # write
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

_LEGACY_VALUE = "author_year_match"
_MIGRATED_VALUE = "author_year_match_legacy"


def migrate_file(csv_path: Path, apply: bool = False) -> dict:
    """Rewrite legacy author_year_match rows in *csv_path*.

    Returns {"total": int, "legacy": int, "written": bool}. When apply is False
    the file is not touched (dry-run).
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found")

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    if "link_method" not in df.columns:
        raise ValueError(f"{csv_path} has no link_method column")

    mask = df["link_method"] == _LEGACY_VALUE
    n_legacy = int(mask.sum())

    print(f"{csv_path}: {len(df)} rows, {n_legacy} legacy '{_LEGACY_VALUE}' rows")
    if n_legacy == 0:
        print("Nothing to migrate.")
        return {"total": len(df), "legacy": 0, "written": False}

    if not apply:
        print(f"[dry-run] would rewrite {n_legacy} rows "
              f"'{_LEGACY_VALUE}' → '{_MIGRATED_VALUE}' (pass --apply to write)")
        return {"total": len(df), "legacy": n_legacy, "written": False}

    df.loc[mask, "link_method"] = _MIGRATED_VALUE
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"Wrote {n_legacy} migrated rows → {csv_path}")
    return {"total": len(df), "legacy": n_legacy, "written": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate legacy author_year_match link_method values.")
    parser.add_argument(
        "--input", type=Path, default=Path("data/extracted.csv"),
        help="Path to the extracted.csv to migrate (default: data/extracted.csv)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the migration in place. Omit for a dry-run.",
    )
    args = parser.parse_args()

    migrate_file(args.input, apply=args.apply)
