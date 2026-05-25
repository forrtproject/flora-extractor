"""
sensitivity_check.py — Compare all_replications.csv against candidates.csv
to measure Stage 1 recall and diagnose success/failure phrase bias.

Usage:
    python -m search.sensitivity_check

Requires data/all_replications.csv to be present (copy from shared drive).
Reads data/candidates.csv as the set of papers Stage 1 found.

Output:
    - Overall recall (% of known replications found)
    - Breakdown of missed papers
    - Success vs failure recall comparison (if outcome column present)
    - Which of the 24 phrases appear in missed papers' abstracts/titles
      (to identify phrases that would recover them)
"""

import re
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

from shared.utils import clean_doi
from search.openalex_search import SEARCH_PHRASES

DATA_DIR = Path(__file__).parent.parent / "data"

# Column names to try for outcome in all_replications — adjust if your CSV differs
_OUTCOME_COLS = ["outcome", "result", "replication_outcome", "replication_result"]

# Fuzzy title match threshold (0–100); papers without DOIs fall back to this
_TITLE_THRESHOLD = 88


def _load(path: Path, label: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            df = pd.read_csv(path, dtype=str, encoding=enc).fillna("")
            print(f"Loaded {label}: {len(df):,} rows  ({enc})")
            return df
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not read {path}")


def _clean_doi_col(df: pd.DataFrame, col: str) -> pd.Series:
    return df[col].apply(lambda x: clean_doi(str(x)) if x else "")


def _phrase_hits(text: str) -> list[str]:
    """Return which SEARCH_PHRASES appear in text (case-insensitive)."""
    text_l = text.lower()
    return [p for p in SEARCH_PHRASES if p in text_l]


def run_sensitivity_check() -> None:
    all_rep_path = DATA_DIR / "all_replications.csv"
    cands_path   = DATA_DIR / "candidates.csv"

    if not all_rep_path.exists():
        print(
            f"\nERROR: {all_rep_path} not found.\n"
            "Copy it from the shared drive to data/ before running this script.\n"
        )
        return
    if not cands_path.exists():
        print(f"\nERROR: {cands_path} not found. Run Stage 1 first.\n")
        return

    all_rep = _load(all_rep_path, "all_replications")
    cands   = _load(cands_path,   "candidates")

    # ── Normalise DOIs ────────────────────────────────────────────────────────
    # all_replications.csv uses doi_r / study_r / abstract_r (same schema as pipeline)
    doi_col_rep   = "doi_r"   if "doi_r"   in all_rep.columns else next((c for c in all_rep.columns if "doi"   in c.lower()), None)
    title_col_rep = "study_r" if "study_r" in all_rep.columns else next((c for c in all_rep.columns if "title" in c.lower()), None)
    abs_col_rep   = "abstract_r" if "abstract_r" in all_rep.columns else next((c for c in all_rep.columns if "abstract" in c.lower()), None)

    if doi_col_rep is None:
        print("WARNING: no DOI column found in all_replications — title-only matching")
        all_rep["_doi"] = ""
    else:
        all_rep["_doi"] = _clean_doi_col(all_rep, doi_col_rep)

    print(f"Schema detected — doi: '{doi_col_rep}', title: '{title_col_rep}', abstract: '{abs_col_rep}'")

    cands["_doi"] = _clean_doi_col(cands, "doi_r")
    cands_dois    = set(cands["_doi"]) - {""}
    cands_titles  = {
        str(t).lower().strip(): doi
        for t, doi in zip(cands["title_r"], cands["_doi"])
        if t
    }

    # ── Match each known replication against candidates ───────────────────────
    found, missed = [], []

    for _, row in all_rep.iterrows():
        doi   = str(row.get("_doi", ""))
        title = str(row.get(title_col_rep, "")) if title_col_rep else ""

        # Pass 1: exact DOI match
        if doi and doi in cands_dois:
            found.append({"doi": doi, "title": title, "match": "doi"})
            continue

        # Pass 2: fuzzy title match (for papers without DOI or DOI mismatch)
        if title:
            title_l = title.lower().strip()
            best_score, best_doi = 0, ""
            for cand_title, cand_doi in cands_titles.items():
                score = fuzz.token_sort_ratio(title_l, cand_title)
                if score > best_score:
                    best_score, best_doi = score, cand_doi
            if best_score >= _TITLE_THRESHOLD:
                found.append({"doi": doi, "title": title, "match": f"title:{best_score}"})
                continue

        missed.append(row.to_dict() | {"_title": title})

    total   = len(all_rep)
    n_found = len(found)
    n_miss  = len(missed)
    recall  = n_found / total * 100 if total else 0

    print(f"\n{'='*60}")
    print(f"SENSITIVITY REPORT")
    print(f"{'='*60}")
    print(f"Known replications : {total:,}")
    print(f"Found in candidates: {n_found:,}  ({recall:.1f}%)")
    print(f"Missed             : {n_miss:,}  ({100-recall:.1f}%)")

    # ── Success / failure breakdown ───────────────────────────────────────────
    outcome_col = next(
        (c for c in all_rep.columns if c.lower() in _OUTCOME_COLS), None
    )
    if outcome_col:
        found_dois = {r["doi"] for r in found if r["doi"]}
        # Focus on the outcomes Lukas cares about for bias analysis
        key_outcomes = {"successful", "success", "failed", "failure", "mixed", "unclear"}
        print(f"\n--- Outcome breakdown (column: '{outcome_col}') ---")
        for outcome_val, grp in all_rep.groupby(outcome_col):
            grp_found = grp[grp["_doi"].isin(found_dois)]
            g_total   = len(grp)
            g_recall  = len(grp_found) / g_total * 100 if g_total else 0
            marker = "  ◄ bias check" if str(outcome_val).lower() in {"successful", "success", "failed", "failure"} else ""
            print(f"  {str(outcome_val):<55} {len(grp_found):>5}/{g_total:<6} ({g_recall:.1f}%){marker}")
    else:
        print(
            f"\nNo outcome column found in all_replications "
            f"(tried: {', '.join(_OUTCOME_COLS)}). "
            "Skipping success/failure breakdown."
        )

    # ── Phrase analysis of missed papers ─────────────────────────────────────
    if missed:
        print(f"\n--- Phrase hits in missed papers (title + abstract) ---")
        phrase_counts: dict[str, int] = {p: 0 for p in SEARCH_PHRASES}
        no_phrase_hit = 0

        for row in missed:
            text = " ".join([
                str(row.get(title_col_rep, "")) if title_col_rep else "",
                str(row.get(abs_col_rep, "")) if abs_col_rep else "",
            ])
            hits = _phrase_hits(text)
            if hits:
                for p in hits:
                    phrase_counts[p] += 1
            else:
                no_phrase_hit += 1

        print(
            f"  Missed papers with at least one phrase in title/abstract "
            f"(already in search list but not fetched yet): "
            f"{n_miss - no_phrase_hit}"
        )
        print(
            f"  Missed papers with NO phrase match at all "
            f"(need new phrases or other sources): "
            f"{no_phrase_hit}"
        )
        print()

        hits_nonzero = [(p, c) for p, c in phrase_counts.items() if c > 0]
        if hits_nonzero:
            print("  Phrases appearing in missed papers:")
            for phrase, count in sorted(hits_nonzero, key=lambda x: -x[1]):
                print(f"    {count:>4}  \"{phrase}\"")

        # Show a sample of papers with no phrase hit — these need new phrases
        no_hit_rows = [
            r for r in missed
            if not _phrase_hits(
                (str(r.get(title_col_rep, "")) if title_col_rep else "")
                + " "
                + (str(r.get(abs_col_rep, "")) if abs_col_rep else "")
            )
        ]
        if no_hit_rows:
            print(f"\n--- Sample missed papers (no phrase match, first 20) ---")
            for r in no_hit_rows[:20]:
                title = str(r.get(title_col_rep, ""))[:90] if title_col_rep else ""
                doi   = str(r.get("_doi", ""))
                print(f"  [{doi or 'no-doi'}] {title or '(no title)'}")

    # ── Summary recommendation ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("RECOMMENDATION")
    print(f"{'='*60}")
    if recall >= 90:
        print("Recall ≥ 90% — phrase list is strong. Focus on Stage 2 precision.")
    elif recall >= 75:
        print(
            "Recall 75–90% — acceptable but worth reviewing 'no phrase match' papers "
            "above to see if any new phrases would recover them."
        )
    else:
        print(
            "Recall < 75% — significant gaps. Review the 'no phrase match' sample above "
            "and consider adding phrases or enabling additional sources (Semantic Scholar, "
            "external lists)."
        )
    print()


if __name__ == "__main__":
    run_sensitivity_check()
