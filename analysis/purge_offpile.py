"""scratch_purge_offpile.py — take the pre-engine corpus out of the Stage 3 outputs.

extracted.csv and its set-aside CSVs were built against a filtered.csv that no longer
exists: the #146 engine handoff replaced a multi-hundred-thousand-row input with the
1,614-row screened pile. Every row whose paper is not in the current pile is a decision
about a paper this pipeline no longer processes — it cannot be re-run, refreshed or
reconciled, and it is what makes the file counts unreadable.

Rows are ARCHIVED, not deleted: each output's off-pile rows move to
data/legacy_pre_engine/<name>. The screen set-aside files are evidence for the
screening evaluations (analysis/screening_eval), so destroying them would cost more
than the tidiness is worth.

Membership is the paper, matched on doi_r first and the OpenAlex work id second — a
pool row can carry either.

    python scratch_purge_offpile.py            # dry run: counts only
    python scratch_purge_offpile.py --apply
"""
import argparse
import shutil
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data"
ARCHIVE = DATA / "legacy_pre_engine"

# target_pending.csv is omitted deliberately: it is 100% on-pile, and it is the file a
# re-run consumes. candidates/flora/entry-sheet files are corpora, not Stage 3 outputs.
OUTPUTS = [
    "extracted.csv",
    "not_a_replication.csv",
    "screen_disagreement.csv",
    "provisional_title_search.csv",
    "provisional_title_search_reviewed.csv",
    "cannot_be_determined.csv",
    "unresolved_doi_mismatch.csv",
    "unresolved_self_links.csv",
    "fabricated_original_doi.csv",
    "target_pending_archived_2026-08-05.csv",
]


def _norm(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.strip().str.lower()


def _work_ids(s: pd.Series) -> pd.Series:
    """Bare numeric OpenAlex work id, from a full URL, a W-prefixed id or a number.
    Both sides must normalise identically or the id match silently never fires."""
    return (_norm(s).str.rsplit("/", n=1).str[-1]
            .str.replace(r"^w", "", regex=True))


def pile() -> "tuple[set[str], set[str]]":
    df = pd.read_csv(DATA / "filtered.csv", dtype=str, keep_default_na=False)
    dois = set(_norm(df["doi_r"])) - {""}
    works = set(_work_ids(df.get("openalex_id_r", pd.Series(dtype=str)))) - {""}
    return dois, works


def on_pile(df: pd.DataFrame, dois: set, works: set) -> pd.Series:
    keep = _norm(df["doi_r"]).isin(dois) & (_norm(df["doi_r"]) != "")
    for col in ("oa_work_id_r", "openalex_id_r"):
        if col in df.columns:
            ids = _work_ids(df[col])
            keep |= ids.isin(works) & (ids != "")
    return keep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes")
    args = ap.parse_args()

    dois, works = pile()
    print(f"current pile: {len(dois)} DOIs / {len(works)} work ids "
          f"(data/filtered.csv)\n")
    print(f"{'file':<40} {'rows':>6} {'keep':>6} {'archive':>8}")
    total = 0
    for name in OUTPUTS:
        path = DATA / name
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        if "doi_r" not in df.columns:
            continue
        keep = on_pile(df, dois, works)
        n_off = int((~keep).sum())
        total += n_off
        print(f"{name:<40} {len(df):>6} {int(keep.sum()):>6} {n_off:>8}")
        if not args.apply or n_off == 0:
            continue
        ARCHIVE.mkdir(exist_ok=True)
        out = ARCHIVE / name
        off = df[~keep]
        if out.exists():
            off = pd.concat([pd.read_csv(out, dtype=str, keep_default_na=False), off])
        off.to_csv(out, index=False, encoding="utf-8-sig")
        shutil.copy(path, path.with_suffix(".csv.bak"))
        df[keep].to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n{'ARCHIVED' if args.apply else 'would archive'}: {total} rows"
          f" → {ARCHIVE.relative_to(DATA.parent)}/")
    if not args.apply:
        print("dry run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
