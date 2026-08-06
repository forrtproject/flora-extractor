"""
sanity_check.py — Integrity REPORT over the exported extracted.csv.

    python -m extract.sanity_check                       # data/extracted.csv
    python -m extract.sanity_check --input data/extracted-test.csv
    python -m extract.sanity_check --deep                # also network-verify doi_o

**It moves nothing.** The quarantine happens on the way out, in
`extract/export.py:partition`, through `classify_row` below — one definition of where
a row belongs, applied as the rows are written rather than after the fact. That is
what makes this a report: after an export, every bucket here should read zero, and a
non-zero count means the file on disk and the stored verdicts have drifted apart
(`python -m extract.export --check` says how).

What only this pass can answer is the two `--deep` buckets. Both need a network
lookup per row, so neither is a property of a row the export could apply; they name
rows that ARE in extracted.csv and should not be.

The set-aside files themselves belong to the CSV they came out of
(`set_aside_dir()` in shared/schema.py): extracted.csv's sit in data/, and a sandbox
render (`extract.export --mode validation --out data/extracted-test.csv`) gets
data/extracted-test-set-aside/. Buckets:

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
is never a set-aside. chronology errors, duplicate pair_ids and blank doi_r are
reported too — the right fix depends on diagnosis (see audit_dois).

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
from typing import Mapping, Optional

import pandas as pd
import requests

from shared.config import DATA_DIR, RESEARCHER_EMAIL
from shared.schema import (EXTRACTED_COLS, RESOLVED_LINK_METHODS,
                           SET_ASIDE_DESTINATIONS, YEAR_COLS, year_str)
from shared.doi_verify import fetch_doi_metadata
from shared.utils import clean_doi, non_article_doi, non_article_type

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


def run_sanity_check(path: "str | Path" = None, deep: bool = False) -> dict:
    """Report what is in the exported CSV that should not be. Writes nothing.

    Every bucket should read zero after an export, because the export partitions the
    rows as it writes them, through the same `classify_row`. A non-zero count is
    drift between the file on disk and the verdicts it is rendered from — the fix is
    `python -m extract.export` (and `--check` to see the difference first), not a
    row moved by hand.

    The two `--deep` buckets are the exception, and the reason this pass still
    exists: each needs a network lookup per row, so neither can be decided as a row
    is written, and both name rows that ARE in extracted.csv and should not be.
    """
    path = Path(path) if path else DATA_DIR / "extracted.csv"
    if not path.exists():
        print(f"[sanity] {path} not found — nothing to check.")
        return {}

    df = _norm(pd.read_csv(path, dtype=str, keep_default_na=False))
    n_rows = len(df)
    # The two cleaned DOI series the --deep block below needs a whole column of.
    doi_o = df["doi_o"].map(clean_doi)
    doi_r = df["doi_r"].map(clean_doi)

    # A resolved link_method with no doi_o is a malformed row, not a finding: it
    # claims a target it cannot name. Demoting it before the rules is what makes it
    # bucket as what it is, exactly as `export.partition` does.
    for i in df.index:
        change = demote_malformed(df.loc[i])
        for key, value in (change or {}).items():
            df.at[i, key] = value

    # One bucket per row, by the shared rule list (`classify_row` above). Every row
    # matches at most one, so a row is never counted twice.
    bucket = (df.apply(classify_row, axis=1) if not df.empty
              else pd.Series([], dtype=object, index=df.index))

    flagged: dict[str, int] = {}
    claimed = pd.Series(False, index=df.index)
    for name, _fname in _BUCKET_FILES:
        mask = bucket == name
        flagged[name] = int(mask.sum())
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
        flagged["non_article_type"] = int(by_type.sum())
        claimed |= by_type

        # Fabricated doi_o: registered-looking but resolves nowhere; candidates are the
        # "unregistered" verification outcomes.
        cand = df.index[(doi_o != "") & df["doi_o_verification"].isin(["no_metadata"]) & ~claimed]
        fab = pd.Series(False, index=df.index)
        for i in cand:
            if not _doi_is_registered(df.at[i, "doi_o"]):
                fab.at[i] = True
            time.sleep(0.2)
        flagged["fabricated_doi_o"] = int(fab.sum())
        claimed |= fab

    kept = df[~claimed]
    yo = pd.to_numeric(kept["year_o"], errors="coerce")
    yr = pd.to_numeric(kept["year_r"], errors="coerce")
    chronology = int(((yo > yr) & yo.notna() & yr.notna()).sum())
    cbd = int((kept["outcome"] == "cannot_be_determined").sum())
    blank_doi_r = int((kept["doi_r"].map(clean_doi) == "").sum())
    dpid = int(((kept["pair_id"].str.strip() != "") & kept["pair_id"].duplicated(keep=False)
                & (kept["doi_r"].map(clean_doi) != "")).sum())
    unregistered = int(((kept["doi_o"].map(clean_doi) != "")
                        & kept["doi_o_verification"].isin(["no_metadata"])).sum())

    summary = {"rows": n_rows, "rows_clean": len(kept), "flagged": flagged,
               "chronology_errors": chronology, "cannot_be_determined_kept": cbd,
               "blank_doi_r": blank_doi_r, "duplicate_pair_ids": dpid,
               "unregistered_doi_o": unregistered}

    print("\n" + "=" * 70)
    print(f"{path.name.upper()} SANITY REPORT  (nothing is written)")
    print("=" * 70)
    print(f"  rows {n_rows}, of which {len(kept)} belong in this file")
    print("  -- rows that belong in a set-aside CSV, not here --")
    dest = SET_ASIDE_DESTINATIONS
    for name in dest:
        if name in flagged:
            print(f"  {name:20s} -> {dest[name]:30s} {flagged[name]}")
    if any(flagged.values()):
        print("  -> the export partitions these as it writes; this file has drifted "
              "from the verdicts.")
        print("     python -m extract.export --check   (then re-run without --check)")
    if not deep:
        print(f"  fabricated_doi_o     (skipped -- pass --deep to network-check "
              f"{unregistered} unregistered doi_o)")
        print("  non_article_type     (skipped -- pass --deep to look up the work type "
              "of each doi_r)")
    print("  -- reported, and belonging here --")
    print(f"  cannot_be_determined (kept in extracted.csv): {cbd}")
    print(f"  chronology errors (year_o > year_r):          {chronology}")
    print(f"  duplicate pair_ids:                           {dpid}")
    print(f"  blank doi_r:                                  {blank_doi_r}")
    if chronology or unregistered:
        print("  -> re-verify/fix flagged DOIs: python -m extract.audit_dois --apply")
    print("=" * 70 + "\n")
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Report on the exported extracted.csv. Writes nothing: the "
                    "quarantine happens in extract.export, as the rows are written.")
    p.add_argument("--input", type=Path, default=None,
                   help="CSV to check (default: data/extracted.csv).")
    p.add_argument("--deep", action="store_true",
                   help="Network checks: doi.org-verify unregistered doi_o and flag "
                        "fabricated ones; look up each doi_r's work type and flag "
                        "dataset/software/peer-review/supplementary records.")
    a = p.parse_args()
    run_sanity_check(a.input, deep=a.deep)
