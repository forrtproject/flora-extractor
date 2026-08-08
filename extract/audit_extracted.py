"""
audit_extracted.py — Pre-validation audit of extracted.csv.

Runs read-only over extracted.csv *before* the validation repo's import hands rows
to human validators, and reports per-row problems at two severities:

  BLOCKER — the row should not reach a validator (unverified doi_o, self-link,
            duplicate pair_id, an unfinished pipeline stage, or a missing display
            field a validator needs to judge the record). An original with no
            registered DOI (doi_o_verification=no_doi) is NOT a blocker as long as
            oa_work_id_o identifies it — books, chapters and pre-DOI-era papers are
            legitimate targets that no DOI check can ever confirm.
  WARNING — the row can go to validators but is flagged (original postdates the
            replication, non-canonical outcome, an outcome quote not actually in
            the abstract, low confidence, or an inconsistent multi-original group).

The tool never writes to extracted.csv; it only writes a report CSV with columns
(pair_id, doi_r, check, severity, detail) and prints a summary. Exit code is 1 if
any BLOCKER fired, so it can gate a shell pipeline. `blocked_pair_ids()` exposes the
BLOCKER set for an importer that wants to drop those rows.

Usage:
    python -m extract.audit_extracted                    # dry-run over data/extracted.csv
    python -m extract.audit_extracted --input data/extracted-test.csv
    python -m extract.audit_extracted --report /tmp/audit.csv
    python -m extract.audit_extracted --doi 10.1037/xyz  # audit one doi_r
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

from shared.config import DATA_DIR, log
from shared.schema import OUTCOME_VALUES
from shared.utils import bare_work_id, clean_doi

_INPUT_PATH  = DATA_DIR / "extracted.csv"
_REPORT_PATH = DATA_DIR / "pre_validation_audit.csv"

BLOCKER = "BLOCKER"
WARNING = "WARNING"

_REPORT_COLS = ["pair_id", "doi_r", "check", "severity", "detail"]

# doi_o_verification values that mean the original DOI is trustworthy enough to
# show a validator. Everything else (mismatch/not_found/no_metadata/api_error/
# skipped/empty) is a blocker. no_doi is the one conditional case: books, chapters
# and pre-DOI-era papers have no DOI to verify, so the row is fine to validate as
# long as it carries an oa_work_id_o to identify (and link to) the original —
# without one there is nothing identifiable to show a validator.
_VERIFIED_OK = {"verified", "corrected"}

_UNRESOLVED_OUTCOMES = {"pending", "api_error"}
_UNRESOLVED_LINK_METHODS = {"target_pending", "api_error", "no_original_found",
                            # The paper names an original nothing could identify: no
                            # DOI, none recoverable, no work id. A validator has no
                            # record to compare against, so the row is unresolved here
                            # however real the link is.
                            "unidentified_original"}

_QUOTE_FUZZ_THRESHOLD = 85  # rapidfuzz partial_ratio; below this the quote is not in the abstract
_YEAR_TOLERANCE = 1         # original may be up to 1 year after replication (in-press ordering)


# Every quote column and the column naming where it was copied from. The two
# reproduction axes each carry their own pair — one quote asked to justify two
# independent judgments is exactly what splitting the axes was for.
_QUOTE_COLUMNS = (
    ("outcome_phrase",               "out_quote_source"),
    ("outcome_computational_quote",  "out_quote_computational_source"),
    ("outcome_robustness_quote",     "out_quote_robust_source"),
)

_QUOTE_JOIN = " | "


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace for containment comparison."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _abstract_quote_check(quote: str, sources: str,
                          abstract: str) -> "tuple[float | None, tuple[int, int] | None]":
    """(worst partial_ratio of a passage claimed to be in the abstract but missing from
    it, count mismatch between passages and sources) — each None when nothing fired.

    A quote may span two sections, joined by " | " with its sources listed in the same
    order. Testing the joined string against the abstract flagged every one of those
    as missing, because half of it was never in the abstract to begin with — so each
    passage is checked against the source named in its own position.

    When the two lists are of different lengths the positional pairing is only partly
    trustworthy, so the mismatch is reported as its own finding and the pairs that do
    align are still checked. Pairing by zip alone dropped the surplus silently, which
    left passages unaudited and said nothing about it.
    """
    quotes = [q.strip() for q in str(quote or "").split(_QUOTE_JOIN)]
    named = [s.strip() for s in str(sources or "").split(_QUOTE_JOIN)]
    mismatch = None
    if any(quotes) and any(named) and len(quotes) != len(named):
        mismatch = (len(quotes), len(named))
    na = _norm(abstract)
    worst: "float | None" = None
    for i in range(min(len(quotes), len(named))):
        passage, source = quotes[i], named[i]
        if not passage or source != "abstract":
            continue
        nq = _norm(passage)
        fuzzy = fuzz.partial_ratio(nq, na) if nq and na else 0.0
        if nq not in na and fuzzy < _QUOTE_FUZZ_THRESHOLD:
            worst = fuzzy if worst is None else min(worst, fuzzy)
    return worst, mismatch


def _as_int(val) -> "int | None":
    try:
        return int(float(str(val).strip()))
    except (TypeError, ValueError):
        return None


def _row_checks(row: pd.Series) -> list[tuple[str, str, str]]:
    """Row-level checks. Returns (check, severity, detail) tuples that fired."""
    out: list[tuple[str, str, str]] = []

    doi_r = clean_doi(str(row.get("doi_r", "") or ""))
    doi_o_raw = str(row.get("doi_o", "") or "")
    doi_o = clean_doi(doi_o_raw)

    # ── BLOCKER ──────────────────────────────────────────────────────────────
    verification = str(row.get("doi_o_verification", "") or "")
    oa_work_id_o = bare_work_id(str(row.get("oa_work_id_o", "") or ""))
    if verification not in _VERIFIED_OK and not (verification == "no_doi" and oa_work_id_o):
        out.append(("doi_o_unverified", BLOCKER,
                    f"doi_o_verification={verification or 'empty'}"
                    + (", no oa_work_id_o" if verification == "no_doi" else "")))

    if doi_o and doi_o == doi_r:
        out.append(("self_link", BLOCKER, f"doi_o == doi_r ({doi_o})"))

    # A no_doi row's identity is its work id, so a self-link there is invisible to the
    # DOI comparison above — the row would reach a validator pointing at itself.
    oa_work_id_r = bare_work_id(str(row.get("oa_work_id_r", "")
                                    or row.get("openalex_id_r", "") or ""))
    if oa_work_id_o and oa_work_id_o == oa_work_id_r:
        out.append(("self_link", BLOCKER,
                    f"oa_work_id_o == oa_work_id_r ({oa_work_id_o})"))

    outcome = str(row.get("outcome", "") or "")
    link_method = str(row.get("link_method", "") or "")
    if outcome in _UNRESOLVED_OUTCOMES or link_method in _UNRESOLVED_LINK_METHODS:
        out.append(("unresolved_stage", BLOCKER,
                    f"outcome={outcome or 'empty'}, link_method={link_method or 'empty'}"))

    missing = [f for f in ("title_r", "title_o", "abstract_r")
               if not str(row.get(f, "") or "").strip()]
    if missing:
        out.append(("missing_display_field", BLOCKER,
                    f"empty: {', '.join(missing)}"))

    # ── WARNING ──────────────────────────────────────────────────────────────
    year_r = _as_int(row.get("year_r"))
    year_o = _as_int(row.get("year_o"))
    if year_r is not None and year_o is not None and year_o > year_r + _YEAR_TOLERANCE:
        out.append(("original_postdates_replication", WARNING,
                    f"year_o={year_o} > year_r={year_r} + {_YEAR_TOLERANCE}"))

    if outcome and outcome not in OUTCOME_VALUES:
        out.append(("outcome_not_canonical", WARNING, f"outcome={outcome}"))

    abstract = str(row.get("abstract_r", "") or "")
    for quote_col, source_col in _QUOTE_COLUMNS:
        fuzzy, mismatch = _abstract_quote_check(str(row.get(quote_col, "") or ""),
                                                str(row.get(source_col, "") or ""), abstract)
        if mismatch is not None:
            out.append(("quote_source_count_mismatch", WARNING,
                        f"{quote_col} has {mismatch[0]} passage(s) but {source_col} "
                        f"names {mismatch[1]} source(s)"))
        if fuzzy is not None:
            out.append(("quote_not_in_abstract", WARNING,
                        f"{quote_col} not found in abstract_r (partial_ratio={fuzzy:.0f})"))

    if str(row.get("link_confidence", "") or "") == "low":
        out.append(("low_link_confidence", WARNING, "link_confidence=low"))

    if str(row.get("outcome_confidence", "") or "") == "low":
        out.append(("low_outcome_confidence", WARNING, "outcome_confidence=low"))

    return out


def _group_checks(df: pd.DataFrame) -> dict[int, list[tuple[str, str, str]]]:
    """Cross-row checks keyed by DataFrame index."""
    fired: dict[int, list[tuple[str, str, str]]] = {}

    # Duplicate pair_id — flag every row of a group that appears more than once.
    pid = df["pair_id"].map(lambda v: str(v or "").strip())
    dup_counts = pid[pid != ""].value_counts()
    dup_ids = set(dup_counts[dup_counts > 1].index)
    for idx, value in pid.items():
        if value in dup_ids:
            fired.setdefault(idx, []).append(
                ("duplicate_pair_id", BLOCKER, f"pair_id appears {dup_counts[value]}×"))

    # Multi-original consistency — within each doi_r group, ranks must be 1..n and
    # n_originals must agree with the group size and across the group's rows.
    for doi_r, group in df.groupby(df["doi_r"].map(lambda v: clean_doi(str(v or "")))):
        if not doi_r or len(group) == 0:
            continue
        n = len(group)
        ranks = [_as_int(v) for v in group["original_rank"]]
        n_orig_values = {_as_int(v) for v in group["n_originals"]}
        bad = (
            None in ranks
            or sorted(ranks) != list(range(1, n + 1))
            or len(n_orig_values) != 1
            or next(iter(n_orig_values)) != n
        )
        if bad:
            detail = (f"n_rows={n}, original_rank={[_as_int(v) for v in group['original_rank']]}, "
                      f"n_originals={sorted(str(v) for v in group['n_originals'])}")
            for idx in group.index:
                fired.setdefault(idx, []).append(
                    ("multi_original_inconsistent", WARNING, detail))

    return fired


def audit_dataframe(df: pd.DataFrame) -> list[dict]:
    """Run all checks over *df*. Returns report rows (dicts with _REPORT_COLS)."""
    group_fired = _group_checks(df)
    report_rows: list[dict] = []
    for idx, row in df.iterrows():
        fired = _row_checks(row) + group_fired.get(idx, [])
        for check, severity, detail in fired:
            report_rows.append({
                "pair_id":  str(row.get("pair_id", "") or ""),
                "doi_r":    str(row.get("doi_r", "") or ""),
                "check":    check,
                "severity": severity,
                "detail":   detail,
            })
    return report_rows


def audit_file(csv_path: Path,
               report_path: "Path | None" = None,
               only_doi: "str | None" = None) -> tuple[list[dict], dict]:
    """Audit *csv_path*, write the report, and return (report_rows, counts)."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found")

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig").fillna("")
    for col in ("pair_id", "doi_r", "doi_o", "doi_o_verification", "outcome",
                "link_method", "title_r", "title_o", "abstract_r", "year_r",
                "year_o", "outcome_phrase", "out_quote_source", "link_confidence",
                "outcome_confidence", "original_rank", "n_originals"):
        if col not in df.columns:
            df[col] = ""

    if only_doi:
        only = clean_doi(only_doi)
        df = df[df["doi_r"].map(lambda v: clean_doi(str(v or ""))) == only].copy()

    report_rows = audit_dataframe(df)

    counts: Counter = Counter()
    for r in report_rows:
        counts[(r["check"], r["severity"])] += 1

    rp = Path(report_path) if report_path else _REPORT_PATH
    pd.DataFrame(report_rows, columns=_REPORT_COLS).to_csv(
        rp, index=False, encoding="utf-8-sig")

    return report_rows, dict(counts)


def blocked_pair_ids(report_path: Path) -> set[str]:
    """pair_ids carrying at least one BLOCKER-severity row in *report_path*."""
    report_path = Path(report_path)
    if not report_path.exists():
        raise FileNotFoundError(f"audit report not found: {report_path}")
    rep = pd.read_csv(report_path, dtype=str, encoding="utf-8-sig").fillna("")
    if rep.empty:
        return set()
    blockers = rep[rep["severity"] == BLOCKER]
    return {p for p in blockers["pair_id"].map(lambda v: str(v or "").strip()) if p}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pre-validation audit of extracted.csv (read-only; writes a report)")
    ap.add_argument("--input", type=Path, default=_INPUT_PATH,
                    help="CSV to audit (default: data/extracted.csv)")
    ap.add_argument("--report", type=Path, default=_REPORT_PATH,
                    help="report CSV path (default: data/pre_validation_audit.csv)")
    ap.add_argument("--doi", help="audit only rows whose doi_r matches (cleaned)")
    args = ap.parse_args()

    report_rows, counts = audit_file(args.input, report_path=args.report,
                                     only_doi=args.doi)

    blocker_pids = {r["pair_id"] for r in report_rows if r["severity"] == BLOCKER}
    n_blockers = sum(n for (_c, sev), n in counts.items() if sev == BLOCKER)
    n_warnings = sum(n for (_c, sev), n in counts.items() if sev == WARNING)

    print(f"\nPre-validation audit of {Path(args.input).name}:")
    print(f"  {'CHECK':<32} {'SEVERITY':<9} COUNT")
    for (check, severity), n in sorted(counts.items(),
                                       key=lambda kv: (kv[0][1] != BLOCKER, kv[0][0])):
        print(f"  {check:<32} {severity:<9} {n}")
    print(f"\n  BLOCKER findings: {n_blockers}  (over {len(blocker_pids)} distinct pair_id)")
    print(f"  WARNING findings: {n_warnings}")
    print(f"  Report: {args.report}")

    if n_blockers:
        log.info("%d BLOCKER finding(s) over %d pair_id — these rows should not go to validators",
                 n_blockers, len(blocker_pids))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
