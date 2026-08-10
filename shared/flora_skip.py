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
  * the validation hand-off, which runs from the `flora-validation` repo — do not
    re-validate them.

It lives in `shared/` rather than in run_extract so a hand-off can import it without
pulling in the extraction stack (pymupdf, pdfminer, openai, …), which a lean
read-only hosting environment may not have installed.
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


def _split_identifiers(cell: object) -> list[str]:
    """The identifiers in one skip-list cell — usually one, sometimes several.

    31 of flora.csv's 2,504 `alt_identifier_r` cells hold comma-separated DOI
    pairs (measured 2026-08-10), and an unsplit pair makes one key that matches
    no row — those works were re-extractable. The cell is split on commas ONLY
    when every fragment is its own `10.`-prefixed identifier: a comma may
    legally occur inside a single DOI, and old Wiley DOIs carry internal
    semicolons, so any blunter split corrupts real identifiers.
    """
    text = str(cell or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in text.split(",")]
    if len(parts) > 1 and all(p.startswith("10.") for p in parts if p):
        return [p for p in parts if p]
    return [text]


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


class SkipListUnreadable(RuntimeError):
    """A skip list is on disk but could not be read.

    Absent and unreadable are different facts and are answered differently. An ABSENT
    list is a legitimate state — a lean checkout has no flora.csv, and validated_skip.csv
    is generated — so it warns, and its absence is visible in the run's skip counts. A
    list that EXISTS and cannot be parsed is a broken environment, and continuing means
    skipping nothing: re-paying the provider bill for rows already adjudicated and
    pushing them into the validation queue a second time. That is silent, expensive and
    hard to undo, so it stops the run instead.
    """


def _read_skip_csv(path: Path, what: str) -> pd.DataFrame:
    """*path* as a DataFrame, or raise SkipListUnreadable naming the file and the error."""
    try:
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        raise SkipListUnreadable(
            f"{what} exists but could not be read: {path} ({type(exc).__name__}: {exc}). "
            "Continuing would skip nothing and re-extract and re-import records that "
            "are already adjudicated — fix or remove the file."
        ) from exc


def load_validated_skip(path=None) -> "tuple[set[int], set[str]]":
    """`(work ids, DOIs)` from the static validated-skip CSV.

    A missing file warns and skips nothing — Stage 3 still runs, it just pays again
    for legacy rows — rather than crashing a run over a file the operator has not
    generated yet. A file that is present but unreadable raises (see
    `SkipListUnreadable`).
    """
    p = Path(path if path is not None else DATA_DIR / VALIDATED_SKIP_NAME)
    if not p.exists():
        log.warning("validated skip list not found at %s — already-validated works "
                    "will NOT be skipped", p)
        return set(), set()
    df = _read_skip_csv(p, "the validated skip list")

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
        doi_r and alt_identifier_r are skipped unconditionally. (fred-data
        renamed that column from `doi_r_alt` after 2026-07; only the current
        spelling is read, so a stale local copy contributes its primary DOIs
        and nothing else rather than failing loudly.)

    Both sources also contribute the OSF records they name by URL, as the DOI
    those URLs resolve to (`_osf_doi_keys()`) — FLoRA identifies an OSF record
    that way five times more often than by DOI.

    A missing source warns and contributes nothing. A source that is present but
    unreadable raises `SkipListUnreadable`: an empty skip list is indistinguishable
    from "nothing to skip" downstream, and the cost of that mistake is a re-extracted,
    re-imported record.
    """
    skip: set = set()

    if sheet_path is not None:
        p = Path(sheet_path)
        if not p.exists():
            log.warning("FLoRA entry sheet not found at %s — its DOIs will not be skipped", p)
        else:
            df = _read_skip_csv(p, "the FLoRA entry sheet")
            missing = [c for c in ("validation_status", "doi_r") if c not in df.columns]
            if missing:
                raise SkipListUnreadable(
                    f"the FLoRA entry sheet {p} has no {', '.join(missing)} column — "
                    "it cannot say which rows are already validated.")
            mask = (df["validation_status"].str.strip().str.lower()
                    .isin(FLORA_VALIDATED_STATUSES))
            found = {clean_doi(d) for d in df.loc[mask, "doi_r"] if d}
            found |= _osf_doi_keys(df.loc[mask])
            skip |= found
            log.info("FLoRA entry sheet: %d already-validated DOIs will be skipped",
                     len(found))

    if flora_path is not None:
        p = Path(flora_path)
        if not p.exists():
            log.warning("flora.csv not found at %s — its DOIs will not be skipped", p)
        else:
            df = _read_skip_csv(p, "flora.csv")
            found = set()
            for col in ("doi_r", "alt_identifier_r"):
                if col in df.columns:
                    for cell in df[col]:
                        found |= {clean_doi(d) for d in _split_identifiers(cell)}
            found |= _osf_doi_keys(df)
            found.discard("")
            skip |= found
            log.info("flora.csv: %d already-in-FLoRA DOIs will be skipped", len(found))

    skip.discard("")
    return skip


def default_flora_skip_dois(data_dir=None) -> set:
    """The skip list built from the two standard filenames under *data_dir*.

    The one place the two filenames live: both consumers (Stage 3 and the validation
    hand-off) come through here, so neither can drift onto a different file. Stage 3
    passes its own DATA_DIR, which a test run can point elsewhere.
    """
    base = Path(data_dir) if data_dir is not None else DATA_DIR
    return load_flora_skip_dois(base / ENTRY_SHEET_NAME, base / FLORA_CSV_NAME)
