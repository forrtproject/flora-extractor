"""
sanity_check.py — Post-extraction integrity pass over extracted.csv.

Runs automatically at the end of every `python -m extract.run_extract` (on normal
completion AND on Ctrl-C, via the __main__ finally block), and standalone:

    python -m extract.sanity_check                       # extracted.csv
    python -m extract.sanity_check --input data/extracted-test.csv
    python -m extract.sanity_check --deep                # also network-verify doi_o
    python -m extract.sanity_check --report-only         # move nothing, just report

Rows that do not belong in the resolved set are moved OUT to a dedicated set-aside
CSV (the same files the dashboard's "set-aside" tab reads), one bucket per problem.
The set-asides belong to the CSV being checked (`set_aside_dir()` in shared/schema.py):
extracted.csv's sit in data/ as they always have, and any other input — the
--extracted-test sandbox — gets data/<stem>-set-aside/. A resume treats every key in a
settled set-aside file as settled, so a shared directory let a test-run quarantine
settle a paper the production run then skipped without ever processing it.

That split is forward-only, so the production set-asides were checked for keys left
behind by the shared-directory era (2026-08-06): of the 76 keys extracted-test.csv has
ever held across its git history, exactly one — 10.31234/osf.io/2h59n, in
fabricated_original_doi.csv — also sits in a production set-aside, and its row is the
PRODUCTION extracted.csv row (identical bibtex_ref_o/bibtex_ref_r, which the test file's
copy of that paper never had). Nothing was quarantined out of a test run into a
production file, so there is nothing to reconcile. Buckets:

    screen_disagreement→ screen_disagreement.csv   the two Q1 classifiers disagreed
    non_article        → not_a_replication.csv     doi_r is a figshare data record
                                                   or a peer-review object (DOI pattern)
    title_search_provisional → provisional_title_search.csv  link_method ==
                                                   llm_title_search: a provisional
                                                   link awaiting human confirmation
    target_pending     → target_pending.csv        link_method == target_pending,
                                                   plus rows demoted to it below
    not_a_replication  → not_a_replication.csv     outcome == not_a_replication
    api_error          → api_error.csv             link_method == api_error: a
                                                   transient failure the next run retries
    no_original_found  → no_original_found.csv     the LLM ran and concluded no
                                                   identifiable original exists
    self_link          → unresolved_self_links.csv doi_o == doi_r
    doi_mismatch       → unresolved_doi_mismatch.csv doi_o_verification == mismatch
    non_article_type   → not_a_replication.csv     the registry types doi_r as a
                                                   non-study object (dataset, software,
                                                   peer-review, supplementary ...)
                                                   (only with --deep: metadata lookup)
    fabricated_doi_o   → fabricated_original_doi.csv doi_o present but registered nowhere
                                                    (only with --deep: doi.org 404 check)

What is left in extracted.csv is validation-ready and nothing else: every remaining row
has a resolved link_method, a doi_o and an outcome. That is the contract the validation
import reads — its own link_method/status filters are a second lock on the same door,
not the door.
A row that is merely unresolved, errored or malformed lives in a set-aside file, and a
row demoted here (a resolved link_method with no doi_o) is filed as target_pending.

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
from collections import Counter
from pathlib import Path
from typing import Mapping, Optional

import pandas as pd
import requests

from shared.config import DATA_DIR, RESEARCHER_EMAIL, log
from shared.schema import (EXTRACTED_COLS, RESOLVED_LINK_METHODS,
                           SET_ASIDE_DESTINATIONS, SETTLED_SET_ASIDE_FILES,
                           YEAR_COLS, set_aside_dir, year_str)
from shared.doi_verify import fetch_doi_metadata
from shared.row_key import primary_key
from shared.utils import bare_work_id, clean_doi, csv_lock, non_article_doi, non_article_type

# The demotion note, and the cap that keeps a long link_evidence from growing without
# bound when the same row is demoted on successive passes.
_DEMOTION_NOTE = "demoted by sanity_check: resolved link_method with no doi_o; "
_EVIDENCE_CAP = 2000


def demote_malformed(row: Mapping) -> Optional[dict]:
    """The field changes that turn a malformed row into a `target_pending` one, or None.

    A resolved link_method with no doi_o claims a target it cannot name. It is a
    malformed row rather than a finding, so it is rewritten BEFORE it is bucketed and
    filed as what it is. Returned as a change set rather than applied in place because
    two callers need it: this module, which rewrites the frame it read off disk, and
    `extract/export.py`, which renders the same decision out of a stored verdict payload
    and must reach the same file.
    """
    if (str(row.get("link_method", "") or "") in RESOLVED_LINK_METHODS
            and not clean_doi(str(row.get("doi_o", "") or ""))):
        return {"link_method": "target_pending",
                "link_evidence": (_DEMOTION_NOTE
                                  + str(row.get("link_evidence", "") or ""))[:_EVIDENCE_CAP]}
    return None


def classify_row(row: Mapping) -> Optional[str]:
    """The quarantine BUCKET one row belongs in, or None to leave it in extracted.csv.

    The whole rule list, in order, as a pure function of one row — no frame, no disk,
    no network. First match wins, which is what makes a row land in exactly one
    set-aside file; `SET_ASIDE_DESTINATIONS` in `shared/schema.py` maps the name to
    that file.

    Pure and per-row because the partition has two producers now. `run_sanity_check`
    applies it to a frame read back off disk; `extract/export.py` applies it to rows it
    is about to write for the first time, so that a row reaches the same file whether
    it was quarantined after the fact or routed there on the way out. A second copy of
    the rules would be two answers to "where does this row belong".

    The link_method rules come first and the outcome rule last of the discard buckets:
    WHERE a row stands in the pipeline decides which file it belongs in, and what its
    outcome column happens to say is a fact about that file's contents, not about its
    identity.

    The two `--deep` buckets (`non_article_type`, `fabricated_doi_o`) are not here:
    each needs a network lookup, so they are not a property of the row.
    """
    method = str(row.get("link_method", "") or "")
    doi_o = clean_doi(str(row.get("doi_o", "") or ""))
    doi_r = str(row.get("doi_r", "") or "")
    if method == "screen_disagreement":
        return "screen_disagreement"
    if non_article_doi(doi_r):
        return "non_article"
    if method == "llm_title_search":
        return "title_search_provisional"
    if method == "target_pending":
        return "target_pending"
    if method == "prescreen_discard":
        return "prescreen_discard"
    if str(row.get("outcome", "") or "") == "not_a_replication":
        return "not_a_replication"
    if method == "api_error":
        return "api_error"
    if method == "no_original_found":
        return "no_original_found"
    if doi_o and doi_o == clean_doi(doi_r):
        return "self_link"
    if str(row.get("doi_o_verification", "") or "") == "mismatch":
        return "doi_mismatch"
    return None


# The order `classify_row` decides in, paired with the file each bucket lands in.
# Reported in this order too, so the printed report reads like the rule list.
_BUCKET_FILES = tuple(
    (name, SET_ASIDE_DESTINATIONS[name]) for name in (
        "screen_disagreement", "non_article", "title_search_provisional",
        "target_pending", "prescreen_discard", "not_a_replication", "api_error",
        "no_original_found", "self_link", "doi_mismatch"))


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    """Schema-normalise a frame: every column present, no NaN, bare-integer years.

    Every to_csv in this module writes a frame that came through here, so this is
    where the float-year artifact ("2018.0", #140) is kept out of the set-aside CSVs
    — whether it arrived from an older row on disk or from a float-typed read.
    """
    for c in EXTRACTED_COLS:
        if c not in df.columns:
            df[c] = ""
    out = df[EXTRACTED_COLS].fillna("")
    for c in YEAR_COLS:
        out[c] = out[c].map(year_str)
    return out


def _dedup_key(r) -> str:
    """Row identity — `shared.row_key.primary_key`, the same key run_extract resumes on.

    This used to be its own chain (bare work id first, then DOI, then title), so a
    paper carrying both identifiers was deduped here under `oa:W…` and read back by
    `_screen_set_aside_keys` under its DOI: the set-aside file said one thing to the
    pass that wrote it and another to the run that reads it. One definition now, and
    the ordering difference costs nothing — a row with no identifier at all still gets
    no key, and `_quarantine` keeps those rather than collapsing them into one.
    """
    return primary_key(r)


def _quarantine(df: pd.DataFrame, mask: pd.Series, dest: Path) -> int:
    """Append df[mask] to dest (schema-normalised, deduped), return count moved.
    Does not modify df — caller drops the moved rows.

    The append is a read-modify-write, so it runs under the destination's own
    `csv_lock` — two runs (or a run and a hand-invoked `python -m extract.sanity_check`)
    quarantining into the same file would otherwise each write back what it read and
    lose the other's rows. One lock, held over one file, never nested inside another:
    the input CSV's lock is taken and released separately (see run_sanity_check), and
    filelock's flock-based locks are not reentrant across two handles in one process.
    """
    move = df[mask].copy()
    if move.empty:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with csv_lock(dest):
        existing = _norm(pd.read_csv(dest, dtype=str, keep_default_na=False)) \
            if dest.exists() else pd.DataFrame(columns=EXTRACTED_COLS)
        for frame in (existing, move):
            blank = frame["oa_work_id_r"].str.strip() == ""
            frame.loc[blank, "oa_work_id_r"] = frame.loc[blank, "openalex_id_r"].map(bare_work_id)
        combined = pd.concat([existing, _norm(move)], ignore_index=True)
        combined["_k"] = combined.apply(_dedup_key, axis=1)
        # Rows that share "no identifier" are not the same paper, so only keyed rows dedup.
        dup = (combined["_k"] != "") & combined["_k"].duplicated(keep="last")
        combined = combined[~dup].drop(columns="_k")
        combined.to_csv(dest, index=False, encoding="utf-8-sig")
    return len(move)


def _fingerprints(df: pd.DataFrame) -> list[str]:
    """One string per row identifying it by its whole content, in schema order.

    Row identity for the merge below has to be the CONTENT, not the resume key: a
    paper legitimately has several rows, and the pass quarantines particular ones.
    Both sides of the comparison come through `_norm`, so the years and the missing
    columns match on either side of a rewrite.
    """
    frame = _norm(df.copy())
    return ["\x1f".join(str(v) for v in rec) for rec in frame[EXTRACTED_COLS].values]


def _rewrite_without(path: Path, removed: pd.DataFrame) -> int:
    """Re-read the CSV under its lock and drop the rows this pass moved out.

    Read-modify-write with a long gap in the middle: the quarantine rules run over a
    frame read minutes ago (hours, under --deep, which makes a network call per row),
    and a concurrent run_extract appends to the same file the whole time. Writing that
    stale frame back deleted every one of those appends.

    Holding the lock across the whole pass would stop the deletions and stall the run
    instead — run_extract takes this lock for every row it writes, so a --deep check
    would block Stage 3 for as long as it runs. So the lock is taken only here, at the
    end, and the pass's decision is applied to the file's CURRENT contents: drop the
    rows it decided to move, keep everything else, including whatever arrived while it
    was thinking.

    Rows are matched by full content and by count, so N identical rows on disk lose
    exactly the N this pass claimed. A row that has since been REWRITTEN in place no
    longer matches and is kept — the right way round: it is a row somebody has touched
    since, and it will be re-examined by the next pass.
    """
    wanted = Counter(_fingerprints(removed))
    with csv_lock(path):
        current = _norm(pd.read_csv(path, dtype=str, keep_default_na=False))
        keep = []
        for fp in _fingerprints(current):
            if wanted[fp]:
                wanted[fp] -= 1
                keep.append(False)
            else:
                keep.append(True)
        current[pd.Series(keep, index=current.index)].to_csv(
            path, index=False, encoding="utf-8-sig")
    dropped = sum(1 for k in keep if not k)
    missing = sum(wanted.values())
    if missing:
        log.warning("sanity_check: %d quarantined row(s) were no longer in %s at "
                    "rewrite time — left alone", missing, path.name)
    return dropped


def _purge_stale_screen_keys(refiled: dict[str, set], out_dir: Path) -> int:
    """Drop papers from a settled set-aside file once this pass filed them elsewhere.

    A resume treats any key in these files as settled (`_load_extracted_rows` in
    run_extract), so a record left behind after the paper moved on strands it. The
    sequence that bites: `--rescreen` reopens a pre-screen discard, the paper is decided
    again and this time comes back `target_pending`, sanity_check files it in
    target_pending.csv — and the old key still sitting in prescreen_discard.csv marks it
    settled on the next ordinary resume, forever. Every settled destination can strand a
    paper the same way, not just the screening ones, so all of them are swept.

    Only keys this pass actively re-filed are touched, and only in the OTHER set-aside
    files. Being in a file is not evidence of belonging there — not_a_replication.csv
    also holds the non_article buckets, whose rows carry any link_method — so nothing
    is inferred from a row's verdict.

    refiled — destination filename → the dedup keys quarantined there this pass.
    out_dir — the set-aside directory of the CSV being checked.
    """
    purged = 0
    for fname in SETTLED_SET_ASIDE_FILES:
        path = out_dir / fname
        if not path.exists():
            continue
        elsewhere = {k for dest, keys in refiled.items() if dest != fname
                     for k in keys if k}
        if not elsewhere:
            continue
        # Same read-modify-write, same lock as _quarantine — and the same file, so a
        # concurrent quarantine into it must not interleave with this rewrite.
        with csv_lock(path):
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
    # Set-asides belong to the CSV being checked, not to DATA_DIR: a test-sandbox
    # quarantine must not settle a paper for the production resume.
    out_dir = set_aside_dir(path)

    df = _norm(pd.read_csv(path, dtype=str, keep_default_na=False))
    # What the file said when this pass read it. The rewrite at the end drops exactly
    # these rows from whatever the file holds THEN, so a row appended in between
    # survives — see _rewrite_without.
    as_read = df.copy()
    n_before = len(df)
    # The two cleaned DOI series the --deep block below still needs a whole column of.
    doi_o = df["doi_o"].map(clean_doi)
    doi_r = df["doi_r"].map(clean_doi)

    # A resolved link_method with no doi_o is a malformed row, not a finding: it claims
    # a target it cannot name. Demote it to target_pending BEFORE the rules, so it is
    # filed as what it is. These rows used to be dropped from the resume state by
    # _load_extracted_rows to force re-processing, which silently DELETED every one
    # whose paper is no longer in filtered.csv (76 rows on 2026-08-05); filing them
    # keeps them on disk and lets a re-run redo the ones still in the pile.
    for i in df.index:
        change = demote_malformed(df.loc[i])
        for key, value in (change or {}).items():
            df.at[i, key] = value

    # One bucket per row, by the shared rule list (`classify_row` above). Every row
    # matches at most one, so a row is never double-counted or duplicated across files.
    bucket = (df.apply(classify_row, axis=1) if not df.empty
              else pd.Series([], dtype=object, index=df.index))

    moved: dict[str, int] = {}
    claimed = pd.Series(False, index=df.index)
    # destination file → keys filed there this pass, so a screening set-aside file can
    # be purged of a paper that has since been decided somewhere else.
    refiled: dict[str, set] = {}
    for name, fname in _BUCKET_FILES:
        mask = bucket == name
        moved[name] = _quarantine(df, mask, out_dir / fname) if move else int(mask.sum())
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
        moved["non_article_type"] = _quarantine(df, by_type, out_dir / "not_a_replication.csv") \
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
        moved["fabricated_doi_o"] = _quarantine(df, fab, out_dir / "fabricated_original_doi.csv") \
            if move else int(fab.sum())
        claimed |= fab

    if move and claimed.any():
        df = df[~claimed]
        _rewrite_without(path, as_read[claimed])

    if move:
        moved["screen_set_aside_purged"] = _purge_stale_screen_keys(refiled, out_dir)

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
    # bucket → file, from shared/schema.py: the same map run_extract's resume reads to
    # decide what is settled, so a destination cannot exist here and be unknown there.
    dest = SET_ASIDE_DESTINATIONS
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
