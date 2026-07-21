"""FLoRA skip-list — replications the database already has.

Two consumers need this and must agree:
  * Stage 3 (`extract/run_extract.py`) — do not re-extract them.
  * the validation hand-off (`extract/csv_to_db.py`) — do not re-validate them.

It lives in `shared/` rather than in run_extract so csv_to_db can import it without
pulling in the extraction stack (pymupdf, pdfminer, openai, …), which a lean
FLORA_READONLY environment may not have installed.
"""
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, log
from shared.utils import clean_doi

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


def load_flora_skip_dois(sheet_path=None, flora_path=None) -> set:
    """doi_r values already in FLoRA.

    Two sources, unioned:
      * entry sheet — only rows whose validation_status is in
        FLORA_VALIDATED_STATUSES; every other status is still being worked on.
      * flora.csv — the published FLoRA database. It has no validation_status
        column because every row in it is by definition already in FLoRA, so
        doi_r and doi_r_alt are skipped unconditionally.

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
                skip |= found
                log.info("flora.csv: %d already-in-FLoRA DOIs will be skipped", len(found))
            except Exception as exc:
                log.warning("Could not read flora.csv (%s) — its DOIs not skipped", exc)

    skip.discard("")
    return skip


def default_flora_skip_dois() -> set:
    """The skip list built from the standard data/ locations."""
    return load_flora_skip_dois(DATA_DIR / ENTRY_SHEET_NAME, DATA_DIR / FLORA_CSV_NAME)
