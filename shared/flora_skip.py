"""Skip lists — works Stage 3 must not extract because someone already has.

Two separate facts, kept apart because they answer different questions:
  * *already in published FLoRA* — the entry sheet plus `flora.csv`
    (`load_flora_skip_dois()`).
  * *already in the validation tables* — the ~1,770 legacy records seeded into
    Supabase `record_metadata` before this pipeline existed
    (`load_validated_skip()`).

The second set is FROZEN: everything validated from here on flows through this
pipeline and is held out by extracted.csv's resume keys instead. So it is a static,
git-tracked CSV built once by `analysis/build_validated_skip.py`, not a Supabase
query on every run — Stage 3 must not need database credentials to know what it
has already done.

Two consumers need the FLoRA list and must agree:
  * Stage 3 (`extract/run_extract.py`) — do not re-extract them.
  * the validation hand-off (`extract/csv_to_db.py`) — do not re-validate them.

It lives in `shared/` rather than in run_extract so csv_to_db can import it without
pulling in the extraction stack (pymupdf, pdfminer, openai, …), which a lean
FLORA_READONLY environment may not have installed.
"""
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from filter.engine.workids import work_id
from shared.config import DATA_DIR, log
from shared.utils import clean_doi

# FLoRA names an OSF record by its URL far more often than by its DOI: over
# flora.csv (2026-08-05) 366 rows carry an osf.io GUID in a URL column and only
# 51 carry an OSF DOI, with 9 in both — so ~357 records FLoRA already holds are
# invisible to a DOI-keyed skip list, and Stage 3 would extract and re-validate
# them. Every OSF GUID resolves to exactly one DOI (`10.17605/OSF.IO/<guid>`),
# which is the form the pool carries, so the two spellings meet by construction.
# Found via the Reproducibility Project rows, whose 92 entries put the aggregate
# paper in `doi_r` and the individual replication's OSF page in `url_r`.
_OSF_URL_COLUMNS = ("url_r", "url_o", "oa_url_r", "oa_url_o")
_OSF_GUID_RE = re.compile(r"osf\.io/([a-z0-9]{5})(?:[/?#]|$)", re.IGNORECASE)


def _osf_doi_keys(df: pd.DataFrame) -> set:
    """`10.17605/osf.io/<guid>` for every OSF page *df* names in a URL column."""
    keys = set()
    for column in _OSF_URL_COLUMNS:
        if column not in df.columns:
            continue
        for value in df[column]:
            found = _OSF_GUID_RE.search(str(value or ""))
            if found:
                keys.add(f"10.17605/osf.io/{found.group(1).lower()}")
    return keys

# Entry-sheet statuses meaning "already adjudicated" — these must not be re-extracted
# or re-validated. 'validated - chosen' was originally missing, which let replications
# already in FLoRA (e.g. 10.1037/per0000041) reach validation a second time.
# 'validated - discarded' is included: the entry was reviewed and rejected, so sending
# it back through costs reviewer time on a question already answered.
# Still NOT skipped: 'help needed', 'on hold', 'awaiting validation' and blank — those
# are in flight and genuinely need the pipeline.
FLORA_VALIDATED_STATUSES = {
    "validated - unchanged",
    "validated - changed",
    "validated - chosen",
    "validated - discarded",
}

ENTRY_SHEET_NAME = "FLoRA entry sheet - replication list.csv"
FLORA_CSV_NAME = "flora.csv"
VALIDATED_SKIP_NAME = "validated_skip.csv"


def validated_work_id(value: object) -> Optional[int]:
    """*value* as the int64 OpenAlex work id, or None if it names no work.

    NULL, blank and unparseable all read the same way: a validation record with no
    work id identifies nothing and can block nothing, and neither can a row whose
    own id is malformed. Both sides of the comparison go through here, so the three
    spellings (`https://openalex.org/W123`, `W123`, `123`) meet as one int.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return work_id(text)
    except ValueError:
        return None


def load_validated_skip(path=None) -> "tuple[set[int], set[str]]":
    """`(work ids, DOIs)` from the static validated-skip CSV.

    A missing file warns and skips nothing — Stage 3 still runs, it just pays again
    for legacy rows — rather than crashing a run over a file the operator has not
    generated yet.
    """
    p = Path(path if path is not None else DATA_DIR / VALIDATED_SKIP_NAME)
    if not p.exists():
        log.warning("validated skip list not found at %s — already-validated works "
                    "will NOT be skipped", p)
        return set(), set()
    try:
        df = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        log.warning("Could not read %s (%s) — already-validated works not skipped",
                    p.name, exc)
        return set(), set()

    ids = {wid for wid in (validated_work_id(v) for v in df.get("work_id", []))
           if wid is not None}
    dois = {clean_doi(d) for d in df.get("doi", []) if str(d).strip()}
    dois.discard("")
    log.info("validated skip list: %d work id(s) and %d DOI(s) will be skipped",
             len(ids), len(dois))
    return ids, dois


def load_flora_skip_dois(sheet_path=None, flora_path=None) -> set:
    """doi_r values already in FLoRA.

    Two sources, unioned:
      * entry sheet — only rows whose validation_status is in
        FLORA_VALIDATED_STATUSES; every other status is still being worked on.
      * flora.csv — the published FLoRA database. It has no validation_status
        column because every row in it is by definition already in FLoRA, so
        doi_r and doi_r_alt are skipped unconditionally.

    Both sources also contribute the OSF records they name by URL, as the DOI
    those URLs resolve to (`_osf_doi_keys()`) — FLoRA identifies an OSF record
    that way five times more often than by DOI.

    A missing or unreadable source warns and contributes nothing, so one bad file
    can never silently disable the entire skip list.
    """
    skip: set = set()

    if sheet_path is not None:
        p = Path(sheet_path)
        if not p.exists():
            log.warning("FLoRA entry sheet not found at %s — its DOIs will not be skipped", p)
        else:
            try:
                df = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
                mask = (df["validation_status"].str.strip().str.lower()
                        .isin(FLORA_VALIDATED_STATUSES))
                found = {clean_doi(d) for d in df.loc[mask, "doi_r"] if d}
                found |= _osf_doi_keys(df.loc[mask])
                skip |= found
                log.info("FLoRA entry sheet: %d already-validated DOIs will be skipped",
                         len(found))
            except Exception as exc:
                log.warning("Could not read FLoRA entry sheet (%s) — its DOIs not skipped", exc)

    if flora_path is not None:
        p = Path(flora_path)
        if not p.exists():
            log.warning("flora.csv not found at %s — its DOIs will not be skipped", p)
        else:
            try:
                df = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
                found = set()
                for col in ("doi_r", "doi_r_alt"):
                    if col in df.columns:
                        found |= {clean_doi(d) for d in df[col] if str(d).strip()}
                found |= _osf_doi_keys(df)
                skip |= found
                log.info("flora.csv: %d already-in-FLoRA DOIs will be skipped", len(found))
            except Exception as exc:
                log.warning("Could not read flora.csv (%s) — its DOIs not skipped", exc)

    skip.discard("")
    return skip


def default_flora_skip_dois() -> set:
    """The skip list built from the standard data/ locations."""
    return load_flora_skip_dois(DATA_DIR / ENTRY_SHEET_NAME, DATA_DIR / FLORA_CSV_NAME)
