"""The short version of the DOI-issue sheet for the Observatory's maintainer: only the
rows where a DOI clearly points to the wrong record (another paper, an erratum, a
retraction notice, a review or reply, a journal issue). Unresolvable/malformed DOIs and
Semantic Scholar URLs stay in the full sheet (`observatory_doi_sheet.py`). A NEW private
sheet, id remembered in `observatory_doi_sheet_clear_id.txt`; never shared.

    .venv/bin/python -m analysis.mo_observatory.observatory_doi_sheet_clear
"""
from __future__ import annotations

import csv
from pathlib import Path

from analysis.mo_observatory.observatory_doi_sheet import TABLE, _write
from analysis.mo_observatory.push_to_sheet import _gws

HERE = Path(__file__).resolve().parent
ID_FILE = HERE / "observatory_doi_sheet_clear_id.txt"
TITLE = "Metascience Observatory — DOIs pointing to the wrong record (FLoRA, 2026-09)"
CLEAR = {"DOI for another paper", "erratum / correction notice", "retraction notice",
         "Faculty Opinions record", "author response / peer review",
         "reply / comment instead of the article", "book review, not the work",
         "journal-issue DOI"}
COLS = ["observatory_records", "field", "value_as_given", "stated_title", "registry_title",
        "category", "suggested_doi"]
HEADER = ["rows in your export (1 = first data row)", "field", "DOI as given",
          "title in your export", "what the DOI actually resolves to", "problem", "suggested DOI"]


def main() -> None:
    with TABLE.open(newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["category"] in CLEAR]
    rows.sort(key=lambda r: (r["category"], r["stated_title"]))
    table = [HEADER] + [[r[c] for c in COLS] for r in rows]
    if ID_FILE.exists():
        sid = ID_FILE.read_text().strip()
    else:
        meta = _gws("sheets", "spreadsheets", "create", body={
            "properties": {"title": TITLE},
            "sheets": [{"properties": {"title": "Wrong-record DOIs",
                                       "gridProperties": {"frozenRowCount": 1}}}]})
        sid = meta["spreadsheetId"]
        ID_FILE.write_text(sid + "\n")
    _write(sid, "Wrong-record DOIs", table)
    print(f"https://docs.google.com/spreadsheets/d/{sid}/edit  ({len(rows)} rows)")


if __name__ == "__main__":
    main()
