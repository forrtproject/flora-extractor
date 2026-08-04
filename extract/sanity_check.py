"""
sanity_check.py — Post-extraction integrity pass over extracted.csv.

Runs automatically at the end of every `python -m extract.run_extract` (on normal
completion AND on Ctrl-C, via the __main__ finally block), and standalone:

    python -m extract.sanity_check                       # extracted.csv
    python -m extract.sanity_check --input data/extracted-test.csv
    python -m extract.sanity_check --deep                # also network-verify doi_o
    python -m extract.sanity_check --report-only         # move nothing, just report

Rows that do not belong in the resolved set are moved OUT to a dedicated set-aside
CSV (the same files the dashboard's "set-aside" tab reads), one bucket per problem:

    screen_disagreement→ screen_disagreement.csv   the two Q1 classifiers disagreed
    non_article        → not_a_replication.csv     doi_r is a figshare data record
                                                   or a peer-review object (DOI pattern)
    title_search_provisional → provisional_title_search.csv  link_method ==
                                                   llm_title_search: a provisional
                                                   link awaiting human confirmation
    target_pending     → target_pending.csv        link_method == target_pending
    not_a_replication  → not_a_replication.csv     outcome == not_a_replication
    self_link          → unresolved_self_links.csv doi_o == doi_r
    doi_mismatch       → unresolved_doi_mismatch.csv doi_o_verification == mismatch
    non_article_type   → not_a_replication.csv     the registry types doi_r as a
                                                   non-study object (dataset, software,
                                                   peer-review, supplementary ...)
                                                   (only with --deep: metadata lookup)
    fabricated_doi_o   → fabricated_original_doi.csv doi_o present but registered nowhere
                                                    (only with --deep: doi.org 404 check)

Each row lands in the FIRST bucket it matches (rules applied in listed order), so a
row is never double-counted or duplicated across files. Where a row stands in the
pipeline is decided before what its outcome column says: an unresolved link_method
routes on that, so a row whose abstract pass answered not_a_replication while its
target was never resolved (or was demoted by the original-link guard) is awaiting a
target and belongs in target_pending.csv.

cannot_be_determined rows are deliberately KEPT in extracted.csv (a linked original
with an undecidable outcome is still a real record awaiting full text), so that bucket
is reported but never moved. chronology errors, duplicate pair_ids and blank doi_r are
reported too but not moved — the right fix depends on diagnosis (see audit_dois).

"Is doi_o real / the right article" is decided per row during extraction and stored in
doi_o_verification; this pass acts on that column without re-hitting the network, except
under --deep, which additionally resolves doi_o registration and looks up the work type
of every surviving doi_r. To re-verify/fix flagged DOIs run
`python -m extract.audit_dois --apply`.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import requests

from shared.config import DATA_DIR, RESEARCHER_EMAIL
from shared.schema import EXTRACTED_COLS, SCREEN_SET_ASIDE_FILES
from shared.doi_verify import fetch_doi_metadata
from shared.utils import bare_work_id, clean_doi, csv_lock, non_article_doi, non_article_type


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    return df[EXTRACTED_COLS].fillna("")


def _dedup_key(r) -> str:
    """Stable identity. openalex_id_r before doi_r so DOI-less rows (whose pair_id all
    collapse to md5('|')) are not merged into one."""
    oa = bare_work_id(str(r.get("openalex_id_r", "")))
    if oa:
        return f"oa:{oa}"
    doi = clean_doi(str(r.get("doi_r", "")))
    return doi if doi else "t:" + str(r.get("title_r", "")).strip().lower()


def _quarantine(df: pd.DataFrame, mask: pd.Series, dest: Path) -> int:
    """Append df[mask] to dest (schema-normalised, deduped), return count moved.
    Does not modify df — caller drops the moved rows."""
    move = df[mask].copy()
    if move.empty:
        return 0
    existing = _norm(pd.read_csv(dest, dtype=str, keep_default_na=False)) \
        if dest.exists() else pd.DataFrame(columns=EXTRACTED_COLS)
    for frame in (existing, move):
        blank = frame["oa_work_id_r"].str.strip() == ""
        frame.loc[blank, "oa_work_id_r"] = frame.loc[blank, "openalex_id_r"].map(bare_work_id)
    combined = pd.concat([existing, _norm(move)], ignore_index=True)
    combined["_k"] = combined.apply(_dedup_key, axis=1)
    combined = combined.drop_duplicates(subset="_k", keep="last").drop(columns="_k")
    combined.to_csv(dest, index=False, encoding="utf-8-sig")
    return len(move)


def _purge_stale_screen_keys(refiled: dict[str, set]) -> int:
    """Drop papers from a screening set-aside file once this pass filed them elsewhere.

    A resume treats any key in these files as settled (`_load_extracted_rows` in
    run_extract), so a record left behind after the paper moved on strands it. The
    sequence that bites: `--rescreen` reopens a pre-screen discard, the paper is decided
    again and this time comes back `target_pending`, sanity_check files it in
    target_pending.csv — and the old key still sitting in prescreen_discard.csv marks it
    settled on the next ordinary resume, forever.

    Only keys this pass actively re-filed are touched, and only in the OTHER screening
    files. Being in a file is not evidence of belonging there — not_a_replication.csv
    also holds the non_article buckets, whose rows carry any link_method — so nothing
    is inferred from a row's verdict.

    refiled — destination filename → the dedup keys quarantined there this pass.
    """
    purged = 0
    for fname in SCREEN_SET_ASIDE_FILES:
        path = DATA_DIR / fname
        if not path.exists():
            continue
        elsewhere = {k for dest, keys in refiled.items() if dest != fname for k in keys}
        if not elsewhere:
            continue
        frame = _norm(pd.read_csv(path, dtype=str, keep_default_na=False))
        if frame.empty:
            continue
        stale = frame.apply(_dedup_key, axis=1).isin(elsewhere)
        if not stale.any():
            continue
        purged += int(stale.sum())
        frame[~stale].to_csv(path, index=False, encoding="utf-8-sig")
    return purged


def _doi_is_registered(doi: str) -> bool:
    """True if doi.org resolves the DOI (302), False on 404. Network call; used only
    under --deep to confirm a doi_o is real and not an LLM hallucination."""
    doi = clean_doi(doi)
    if not doi:
        return False
    try:
        r = requests.head(f"https://doi.org/{doi}", allow_redirects=False, timeout=20,
                          headers={"User-Agent": f"mailto:{RESEARCHER_EMAIL}"})
        return r.status_code in (301, 302, 303, 307, 308)
    except requests.RequestException:
        return True  # network error ≠ proof of fabrication; keep the row


def _doi_r_non_study_type(doi: str) -> str:
    """Reason string if the registry types *doi* as a non-study object, else "".
    Network call (cached per DOI); used only under --deep."""
    doi = clean_doi(doi)
    if not doi:
        return ""
    meta = fetch_doi_metadata(doi)
    return non_article_type(meta.get("type", "")) if meta else ""


def run_sanity_check(path: "str | Path" = None, move: bool = True,
                     deep: bool = False) -> dict:
    """Quarantine problematic rows into set-aside CSVs, report the rest."""
    path = Path(path) if path else DATA_DIR / "extracted.csv"
    if not path.exists():
        print(f"[sanity] {path} not found — nothing to check.")
        return {}

    df = _norm(pd.read_csv(path, dtype=str, keep_default_na=False))
    n_before = len(df)
    doi_o = df["doi_o"].map(clean_doi)
    doi_r = df["doi_r"].map(clean_doi)

    # (bucket name, destination file, row mask) — first match wins per row.
    # The link_method rules come first, and the outcome rule last of the discard
    # buckets: WHERE a row stands in the pipeline decides which file it belongs in,
    # and what its outcome column happens to say is a fact about that file's contents,
    # not about its identity. not_a_replication.csv is read as "the pipeline settled
    # that this paper replicates nothing"; a row that never got a link has settled
    # nothing, whatever verdict was written beside it.
    rules = [
        # Disagreement first: a disagreement row whose outcome happened to be coded
        # not_a_replication used to win the outcome rule and land in that file,
        # biasing any precision computed over it (audit B6).
        ("screen_disagreement", "screen_disagreement.csv",
         df["link_method"] == "screen_disagreement"),
        # figshare data records / peer-review objects: Stage-2 false positives (#17)
        # that predate the DOI exclusion — today the filter engine's
        # `deposit-doi-prefixes` and `non-article-doi` specs discard them before
        # Stage 3 ever sees them. The replication record itself is bogus,
        # so this is a permanent discard and outranks the unresolved states — a re-run
        # has nothing to gain by retrying it. Routed to the not_a_replication bucket.
        ("non_article", "not_a_replication.csv", df["doi_r"].map(lambda d: bool(non_article_doi(d)))),
        # Provisional: the target was matched against the whole literature by title
        # search rather than picked from a candidate list, at ~50% measured precision,
        # and the failure is invisible to doi_o_verification — the DOI really is the
        # named paper, it just is not this paper's target. Set aside for confirmation.
        ("title_search_provisional", "provisional_title_search.csv",
         df["link_method"] == "llm_title_search"),
        # Unresolved before the outcome buckets: the abstract pass can answer
        # not_a_replication on a row whose link was never resolved (or was demoted by
        # _guard_original_link), and such a row is awaiting a target, not a finding.
        ("target_pending", "target_pending.csv", df["link_method"] == "target_pending"),
        # Before the outcome rule, and in its own file: the cheap pre-screen writes
        # outcome=not_a_replication, but it is a weaker instrument than the validated
        # pair and mixing its discards into not_a_replication.csv would corrupt any
        # precision computed over the screen. Excluded from DB import either way.
        ("prescreen_discard", "prescreen_discard.csv",
         df["link_method"] == "prescreen_discard"),
        ("not_a_replication", "not_a_replication.csv", df["outcome"] == "not_a_replication"),
        ("self_link", "unresolved_self_links.csv", (doi_o != "") & (doi_o == doi_r)),
        ("doi_mismatch", "unresolved_doi_mismatch.csv", df["doi_o_verification"] == "mismatch"),
    ]

    moved: dict[str, int] = {}
    claimed = pd.Series(False, index=df.index)
    # destination file → keys filed there this pass, so a screening set-aside file can
    # be purged of a paper that has since been decided somewhere else.
    refiled: dict[str, set] = {}
    for name, fname, mask in rules:
        mask = mask & ~claimed
        moved[name] = _quarantine(df, mask, DATA_DIR / fname) if move else int(mask.sum())
        if mask.any():
            refiled.setdefault(fname, set()).update(df[mask].apply(_dedup_key, axis=1))
        claimed |= mask

    if deep:
        # doi_r that the registry types as a dataset/software deposit, a peer-review
        # object or supplementary material: the pipeline happily links such a record to
        # the paper it belongs to, producing a plausible but bogus replication (23 of 50
        # hand-checked provisional links). The DOI patterns above catch none of these,
        # so the type has to be fetched — network, hence --deep only.
        by_type = pd.Series(False, index=df.index)
        for i in df.index[(doi_r != "") & ~claimed]:
            if _doi_r_non_study_type(df.at[i, "doi_r"]):
                by_type.at[i] = True
            time.sleep(0.2)
        moved["non_article_type"] = _quarantine(df, by_type, DATA_DIR / "not_a_replication.csv") \
            if move else int(by_type.sum())
        claimed |= by_type

        # Fabricated doi_o: registered-looking but resolves nowhere; candidates are the
        # "unregistered" verification outcomes.
        cand = df.index[(doi_o != "") & df["doi_o_verification"].isin(["no_metadata"]) & ~claimed]
        fab = pd.Series(False, index=df.index)
        for i in cand:
            if not _doi_is_registered(df.at[i, "doi_o"]):
                fab.at[i] = True
            time.sleep(0.2)
        moved["fabricated_doi_o"] = _quarantine(df, fab, DATA_DIR / "fabricated_original_doi.csv") \
            if move else int(fab.sum())
        claimed |= fab

    if move and claimed.any():
        df = df[~claimed]
        with csv_lock(path):
            df.to_csv(path, index=False, encoding="utf-8-sig")

    if move:
        moved["screen_set_aside_purged"] = _purge_stale_screen_keys(refiled)

    # Report-only signals (never moved).
    yo = pd.to_numeric(df["year_o"], errors="coerce")
    yr = pd.to_numeric(df["year_r"], errors="coerce")
    chronology = int(((yo > yr) & yo.notna() & yr.notna()).sum())
    cbd = int((df["outcome"] == "cannot_be_determined").sum())
    blank_doi_r = int((df["doi_r"].map(clean_doi) == "").sum())
    dpid = int(((df["pair_id"].str.strip() != "") & df["pair_id"].duplicated(keep=False)
                & (df["doi_r"].map(clean_doi) != "")).sum())
    unregistered = int(((df["doi_o"].map(clean_doi) != "")
                        & df["doi_o_verification"].isin(["no_metadata"])).sum())

    summary = {"rows_before": n_before, "rows_after": len(df), "moved": moved,
               "chronology_errors": chronology, "cannot_be_determined_kept": cbd,
               "blank_doi_r": blank_doi_r, "duplicate_pair_ids": dpid,
               "unregistered_doi_o": unregistered}

    print("\n" + "=" * 70)
    print("EXTRACTED.CSV SANITY CHECK" + ("  (report-only)" if not move else ""))
    print("=" * 70)
    print(f"  rows {n_before} -> {len(df)}")
    print("  -- moved to set-aside CSVs --")
    dest = {"screen_disagreement": "screen_disagreement.csv",
            "not_a_replication": "not_a_replication.csv", "non_article": "not_a_replication.csv",
            "non_article_type": "not_a_replication.csv",
            "self_link": "unresolved_self_links.csv",
            "doi_mismatch": "unresolved_doi_mismatch.csv",
            "title_search_provisional": "provisional_title_search.csv",
            "target_pending": "target_pending.csv",
            "prescreen_discard": "prescreen_discard.csv",
            "fabricated_doi_o": "fabricated_original_doi.csv"}
    for name in dest:
        if name in moved:
            print(f"  {name:20s} -> {dest[name]:30s} {moved[name]}")
    if moved.get("screen_set_aside_purged"):
        print(f"  {'stale screen keys':20s} -> {'purged from set-aside files':30s} "
              f"{moved['screen_set_aside_purged']}")
    if not deep:
        print(f"  fabricated_doi_o     (skipped -- pass --deep to network-check "
              f"{unregistered} unregistered doi_o)")
        print("  non_article_type     (skipped -- pass --deep to look up the work type "
              "of each doi_r)")
    print("  -- reported, not moved --")
    print(f"  cannot_be_determined (kept in extracted.csv): {cbd}")
    print(f"  chronology errors (year_o > year_r):          {chronology}")
    print(f"  duplicate pair_ids:                           {dpid}")
    print(f"  blank doi_r:                                  {blank_doi_r}")
    if chronology or unregistered:
        print("  -> re-verify/fix flagged DOIs: python -m extract.audit_dois --apply")
    print("=" * 70 + "\n")
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sanity-check + quarantine extracted.csv.")
    p.add_argument("--input", type=Path, default=None,
                   help="CSV to check (default: data/extracted.csv).")
    p.add_argument("--report-only", action="store_true",
                   help="Report only; move nothing.")
    p.add_argument("--deep", action="store_true",
                   help="Network checks: doi.org-verify unregistered doi_o and quarantine "
                        "fabricated ones; look up each doi_r's work type and quarantine "
                        "dataset/software/peer-review/supplementary records.")
    a = p.parse_args()
    run_sanity_check(a.input, move=not a.report_only, deep=a.deep)
