"""Split the Observatory comparison into what FLoRA can take and what needs a human.

Two outputs, from `extracted_offline.csv` joined back to the Observatory's own rows:

  `flora_entry_rows.csv`   rows where BOTH pipelines name the same original DOI and
                           the same outcome, in the FLoRA entry sheet's 20 columns and
                           its vocabulary, ready to append.
  `disagreements.csv`      every row where they differ, with both answers side by side
                           and what each was read from — the analysis set.

**Agreement means agreement about the same claim.** Same original DOI, then same
outcome. Matching on the outcome alone would count a row where we read a different
original and happened to land on the same verdict, which is not corroboration.

Two independent pipelines agreeing on the original is itself strong evidence, so rows
we carry at `link_confidence: low` are included — 31 of the 117 — with the confidence
written into `prep_notes` so a validator sees it rather than having to ask. Nothing
here is validated: `validation_status` is left blank so these enter the normal queue,
and `Coder` is `AI`, the value the sheet already uses for machine-coded rows.

    .venv/bin/python -m analysis.mo_observatory.build_flora_entries
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from shared.utils import clean_doi

csv.field_size_limit(10 ** 9)

HERE = Path(__file__).resolve().parent
ENTRIES = HERE / "flora_entry_rows.csv"
DISAGREE = HERE / "disagreements.csv"

# The FLoRA entry sheet, in order. Anything we cannot source is written empty rather
# than guessed — a blank is a question for a validator, a guess is a wrong answer.
SHEET_COLS = ("ref_o", "doi_o", "url_o", "ref_r", "doi_r", "url_r", "abstract_r",
              "outcome", "outcome_quote", "out_quote_source", "prep_notes",
              "quote validated", "year", "target_match_liberal", "Coder",
              "validation_status", "validator", "validator_notes",
              "alt_identifier_o", "alt_identifier_r")

# Ours → the Observatory's four-value result column, for the agreement test.
TO_MO = {"successful": "success", "failed": "failure", "mixed": "inconclusive",
         "statistically successful but flawed": "inconclusive"}

# Ours → the entry sheet's vocabulary. Only this one differs in spelling; the sheet
# writes it with underscores and "fundamentally".
TO_SHEET = {"statistically successful but flawed":
            "statistically_successful_but_fundamentally_flawed"}

DISAGREE_COLS = ("doi_r", "title_r", "kind", "our_doi_o", "our_title_o", "our_outcome",
                 "mo_doi_o", "mo_outcome", "our_link_method", "our_link_confidence",
                 "our_outcome_quote", "out_quote_source", "mo_replication_type",
                 "mo_discipline", "mo_source", "mo_confidence", "mo_ai_version",
                 "our_link_evidence")


def observatory() -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = defaultdict(list)
    source = next(HERE.glob("replications_database_*.csv"), None)
    if source is None:
        raise SystemExit("No Observatory CSV under analysis/mo_observatory/.")
    with source.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            doi = clean_doi(str(raw.get("replication_url") or ""))
            if doi.startswith("10."):
                rows[doi].append(raw)
    return rows


def partition(extracted: Path, mo: dict[str, list[dict]]
              ) -> tuple[list[tuple[dict, dict]], list[tuple[dict, Optional[dict]]]]:
    """(agreeing, differing). A row with no original of ours can only differ."""
    agree, differ = [], []
    with extracted.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            ours_o = clean_doi(row.get("doi_o") or "")
            partner = next((m for m in mo.get(row["doi_r"], [])
                            if ours_o and clean_doi(str(m.get("original_url") or "")) == ours_o),
                           None)
            if partner is None:
                differ.append((row, next(iter(mo.get(row["doi_r"], [])), None)))
                continue
            if TO_MO.get(row.get("outcome") or "") == (partner.get("result") or "").strip():
                agree.append((row, partner))
            else:
                differ.append((row, partner))
    return agree, differ


def _url(doi: str) -> str:
    return f"https://doi.org/{doi}" if doi else ""


def sheet_row(row: dict, partner: dict) -> dict:
    outcome = row.get("outcome") or ""
    notes = (f"flora-extractor offline run 2026-09-21 (works outside the survivor pool); "
             f"link_method={row.get('link_method') or '?'}, "
             f"link_confidence={row.get('link_confidence') or '?'}. "
             f"Corroborated by the Metascience Observatory: same original DOI and same "
             f"outcome ({partner.get('result')}). "
             f"{(row.get('link_evidence') or '')[:200]}")
    return {
        "ref_o": row.get("ref_o") or "",
        "doi_o": row.get("doi_o") or "",
        "url_o": _url(clean_doi(row.get("doi_o") or "")),
        "ref_r": row.get("ref_r") or "",
        "doi_r": row.get("doi_r") or "",
        "url_r": _url(clean_doi(row.get("doi_r") or "")),
        "abstract_r": row.get("abstract_r") or "",
        "outcome": TO_SHEET.get(outcome, outcome),
        "outcome_quote": row.get("outcome_phrase") or "",
        "out_quote_source": row.get("out_quote_source") or "",
        "prep_notes": notes.strip(),
        "quote validated": "",
        "year": row.get("year_r") or "",
        "target_match_liberal": "",
        "Coder": "AI",
        "validation_status": "",
        "validator": "",
        "validator_notes": "",
        "alt_identifier_o": "",
        "alt_identifier_r": "",
    }


def disagreement_row(row: dict, partner: Optional[dict]) -> dict:
    ours_o = clean_doi(row.get("doi_o") or "")
    theirs_o = clean_doi(str((partner or {}).get("original_url") or ""))
    if not ours_o:
        kind = "we found no original"
    elif not theirs_o:
        kind = "MO names no original DOI"
    elif ours_o != theirs_o:
        kind = "different original"
    else:
        kind = "same original, different outcome"
    return {
        "doi_r": row["doi_r"], "title_r": row.get("title_r") or "", "kind": kind,
        "our_doi_o": ours_o, "our_title_o": row.get("title_o") or "",
        "our_outcome": row.get("outcome") or "",
        "mo_doi_o": theirs_o, "mo_outcome": (partner or {}).get("result") or "",
        "our_link_method": row.get("link_method") or "",
        "our_link_confidence": row.get("link_confidence") or "",
        "our_outcome_quote": (row.get("outcome_phrase") or "")[:400],
        "out_quote_source": row.get("out_quote_source") or "",
        "mo_replication_type": (partner or {}).get("replication_type") or "",
        "mo_discipline": (partner or {}).get("discipline") or "",
        "mo_source": (partner or {}).get("source") or "",
        "mo_confidence": (partner or {}).get("confidence") or "",
        "mo_ai_version": (partner or {}).get("ai_version") or "",
        "our_link_evidence": (row.get("link_evidence") or "")[:400],
    }


def _write(path: Path, cols: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(cols))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Optional[list[str]] = None) -> int:
    mo = observatory()
    agree, differ = partition(HERE / "extracted_offline.csv", mo)

    entries = [sheet_row(r, p) for r, p in agree]
    entries.sort(key=lambda r: (r["doi_r"], r["doi_o"]))
    _write(ENTRIES, SHEET_COLS, entries)

    rows = [disagreement_row(r, p) for r, p in differ]
    rows.sort(key=lambda r: (r["kind"], r["doi_r"]))
    _write(DISAGREE, DISAGREE_COLS, rows)

    works = len({r["doi_r"] for r in entries})
    print(f"wrote {ENTRIES.name}: {len(entries)} row(s) over {works} work(s)")
    low = sum(1 for r, _ in agree if (r.get("link_confidence") or "") == "low")
    print(f"  of which link_confidence=low: {low}")
    print(f"wrote {DISAGREE.name}: {len(rows)} row(s)")
    from collections import Counter
    for kind, n in Counter(r["kind"] for r in rows).most_common():
        print(f"  {kind:34s} {n:4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
