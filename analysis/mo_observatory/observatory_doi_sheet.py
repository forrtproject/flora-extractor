"""Put `observatory_doi_issues.csv` into a NEW private Google Sheet for the Observatory's
maintainer, via `gws`: tab "DOI issues" (the table) and tab "Notes".

Creates the spreadsheet once and remembers its id in `observatory_doi_sheet_id.txt`; a
re-run clears and rewrites both tabs of that same sheet. It never shares the file —
no permission is created, so it stays private to the account `gws` runs as until
Lukas shares it.

    .venv/bin/python -m analysis.mo_observatory.observatory_doi_sheet
"""
from __future__ import annotations

import csv
from pathlib import Path

from analysis.mo_observatory.push_to_sheet import _gws

HERE = Path(__file__).resolve().parent
TABLE = HERE / "observatory_doi_issues.csv"
ID_FILE = HERE / "observatory_doi_sheet_id.txt"
TITLE = "Metascience Observatory — DOI issues found by FLoRA (2026-09)"

NOTES = [
    ["About this sheet"],
    ["DOI problems in the Metascience Observatory export replications_database_2026_09_04_184008.csv, found while "
     "comparing it with FLoRA (Sept 2026). One row per identifier as given; 'observatory_records' lists every row of the "
     "export that carries it."],
    [""],
    ["Row identifiers"],
    ["The export has no id column, so rows are identified by record number: 1 = the first data row after the header "
     "(in a spreadsheet opened with the header in row 1, record N is row N+1). replication_ids gives the replication URL(s) "
     "of those rows as a cross-check."],
    [""],
    ["How the issues were found"],
    ["(1) Pairs where FLoRA (or FLoRA's pipeline) and the Observatory name the same original by title but under different "
     "DOIs, where the Observatory's DOI turned out to point to something else. (2) A scan of every original_url and "
     "replication_url in the export: each DOI looked up in Crossref (2026-09-23), Crossref misses checked against the doi.org "
     "handle API (so DataCite DOIs such as OSF/Zenodo are not flagged), plus identifiers that are not DOIs. "
     "registry_title is what the DOI actually resolves to. suggested_doi is given only where we know it — from Crossref's "
     "own update-to/is-review-of relation, a mechanical repair of the string that resolves, a Crossref search on the title "
     "given (accepted only on a near-identical title and year), or the article DOI FLoRA holds for the same title. "
     "Please verify before applying."],
    [""],
    ["Categories"],
    ["unresolvable DOI — not registered anywhere (typo or truncation)."],
    ["journal-issue DOI — resolves to the whole issue, not the article."],
    ["erratum / correction notice — the DOI is a correction/erratum/addendum notice (usually of the article you meant; "
     "see suggested_doi)."],
    ["retraction notice — the DOI is a retraction (and replacement) notice rather than the article."],
    ["author response / peer review — e.g. an eLife or PCI RR author response instead of the article."],
    ["reply / comment instead of the article — the DOI is a reply or comment, the title given is the article's."],
    ["Faculty Opinions record — a recommendation of the article, not the article."],
    ["book review, not the work — the DOI is a review of the book named."],
    ["withdrawn preprint — the title given reads 'WITHDRAWN'; the row may need checking."],
    ["DOI for another paper — resolves to a different work than the title/year given."],
    ["Semantic Scholar URL, not a DOI — none of these S2 records carries a DOI, and a Crossref title search found none "
     "either; most are books, reports, theses or conference papers. Listed so the field can be relabelled as a URL."],
    ["malformed DOI string — junk appended (' sapp', ' (Case Conflict)', a second DOI), URL-encoding, a doubled "
     "doi.org prefix, or a placeholder."],
    [""],
    ["Not listed here"],
    ["DOIs that are valid but differ from FLoRA's as ALTERNATIVE IDENTIFIERS of the same work (preprint, working paper, "
     "duplicate registration, 10.1037// vs 10.1037/) — both are fine. PubMed and other landing-page URLs in "
     "original_url, empty original_url, and replications without a DOI (OSF, PsychFileDrawer via web.archive.org, "
     "Data Colada, theses) are not errors and are not listed."],
    [""],
    ["One row per effect or lab"],
    ["The export stores one row per effect or per lab, not one per paper (e.g. the ego-depletion multi-lab replication has "
     "24 rows: 22 failure, 1 success, 1 reversal). Any comparison that treats the file as 'one row per paper' must "
     "aggregate first: of the 1,303 papers we compared, 212 list more than one original, 214 original–replication "
     "pairs appear on more than one row, and 114 of those carry more than one distinct result. When we first kept one arbitrary row per paper, agreement looked like 84% on the "
     "original and 74% on the outcome; aggregated per paper it is 87% and 87%."],
]


def _write(sid: str, tab: str, rows: list[list[str]]) -> None:
    _gws("sheets", "spreadsheets", "values", "clear", params={"spreadsheetId": sid, "range": tab}, body={})
    _gws("sheets", "spreadsheets", "values", "update",
         params={"spreadsheetId": sid, "range": f"{tab}!A1", "valueInputOption": "RAW"}, body={"values": rows})


def main() -> None:
    with TABLE.open(newline="", encoding="utf-8-sig") as f:
        table = list(csv.reader(f))
    if ID_FILE.exists():
        sid = ID_FILE.read_text().strip()
        meta = _gws("sheets", "spreadsheets", "get", params={"spreadsheetId": sid, "fields": "sheets.properties"})
    else:
        meta = _gws("sheets", "spreadsheets", "create", body={
            "properties": {"title": TITLE},
            "sheets": [{"properties": {"title": "DOI issues", "gridProperties": {"frozenRowCount": 1}}},
                       {"properties": {"title": "Notes"}}]})
        sid = meta["spreadsheetId"]
        ID_FILE.write_text(sid + "\n")
    ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    _write(sid, "DOI issues", table)
    _write(sid, "Notes", NOTES)
    ncol = len(table[0])
    widths = {"category": 230, "field": 110, "value_as_given": 260, "observatory_records": 130, "n_records": 70,
              "replication_ids": 260, "stated_title": 320, "registry_title": 320, "suggested_doi": 220, "evidence": 420}
    reqs = [{"setBasicFilter": {"filter": {"range": {"sheetId": ids["DOI issues"], "startRowIndex": 0,
                                                     "endRowIndex": len(table), "startColumnIndex": 0, "endColumnIndex": ncol}}}},
            {"repeatCell": {"range": {"sheetId": ids["DOI issues"], "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}},
            {"updateDimensionProperties": {"range": {"sheetId": ids["Notes"], "dimension": "COLUMNS",
                                                     "startIndex": 0, "endIndex": 1},
                                           "properties": {"pixelSize": 900}, "fields": "pixelSize"}},
            {"repeatCell": {"range": {"sheetId": ids["Notes"], "startColumnIndex": 0, "endColumnIndex": 1},
                            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                            "fields": "userEnteredFormat.wrapStrategy"}}]
    for i, name in enumerate(table[0]):
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": ids["DOI issues"], "dimension": "COLUMNS",
                                                             "startIndex": i, "endIndex": i + 1},
                                                   "properties": {"pixelSize": widths.get(name, 150)}, "fields": "pixelSize"}})
    for i, row in enumerate(NOTES):
        if row[0] and len(row[0]) < 40:
            reqs.append({"repeatCell": {"range": {"sheetId": ids["Notes"], "startRowIndex": i, "endRowIndex": i + 1,
                                                  "startColumnIndex": 0, "endColumnIndex": 1},
                                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                                        "fields": "userEnteredFormat.textFormat.bold"}})
    _gws("sheets", "spreadsheets", "batchUpdate", params={"spreadsheetId": sid}, body={"requests": reqs})
    print(f"https://docs.google.com/spreadsheets/d/{sid}/edit  ({len(table) - 1} rows)")


if __name__ == "__main__":
    main()
