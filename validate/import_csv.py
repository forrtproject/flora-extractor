"""
import_csv.py — Load data/flora_all.csv into the SQLite database.

Run once before starting the Flask app:
    python -m validate.import_csv

Column mapping from flora_all.csv → Replication model:
    study_r                 → title_r
    abstract_r              → abstract_r
    resolved_doi_o          → doi_o
    resolved_title_o        → title_o
    resolved_year_o         → year_o
    resolved_author_o       → authors_o
    resolution_method       → link_method
    llm_evidence            → link_evidence
    llm_confidence          → link_confidence  (categorical: high/medium/low)
    outcome                 → outcome
    outcome_quote           → outcome_phrase
    outcome_confidence      → outcome_confidence  (categorical)
    flora_validation_status → flora_status
    user_val_status         → validation_status  (blank → "pending")

Upsert on (doi_r, original_rank=1). Safe to re-run.
"""
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, log
from shared.utils import clean_doi
from validate.models import db, Replication

DB_PATH    = DATA_DIR / "flora.db"
SOURCE_CSV = DATA_DIR / "flora_all.csv"


def _safe_int(val: str) -> int | None:
    try:
        return int(float(val)) if val else None
    except (ValueError, TypeError):
        return None


def import_csv(csv_path: Path = SOURCE_CSV) -> int:
    """Load csv_path into SQLite. Returns number of newly inserted rows."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str).fillna("")

    inserted = 0
    for _, row in df.iterrows():
        doi_r = clean_doi(row.get("doi_r", "").strip())
        if not doi_r:
            continue

        title_r            = row.get("study_r",                "").strip()
        abstract_r         = row.get("abstract_r",             "").strip()
        doi_o              = clean_doi(row.get("resolved_doi_o",   "").strip())
        title_o            = row.get("resolved_title_o",       "").strip()
        year_o             = _safe_int(row.get("resolved_year_o", ""))
        authors_o          = row.get("resolved_author_o",      "").strip()
        link_method        = row.get("resolution_method",      "").strip()
        link_evidence      = row.get("llm_evidence",           "").strip()
        link_confidence    = row.get("llm_confidence",         "").strip()
        outcome            = row.get("outcome",                "").strip()
        outcome_phrase     = row.get("outcome_quote",          "").strip()
        outcome_confidence = row.get("outcome_confidence",     "").strip()
        flora_status       = row.get("flora_validation_status","").strip()
        val_status         = row.get("user_val_status",        "").strip() or "pending"

        existing = Replication.query.filter_by(doi_r=doi_r, original_rank=1).first()

        if existing:
            existing.title_r            = title_r
            existing.abstract_r         = abstract_r
            existing.doi_o              = doi_o
            existing.title_o            = title_o
            existing.year_o             = year_o
            existing.authors_o          = authors_o
            existing.link_method        = link_method
            existing.link_evidence      = link_evidence
            existing.link_confidence    = link_confidence
            existing.outcome            = outcome
            existing.outcome_phrase     = outcome_phrase
            existing.outcome_confidence = outcome_confidence
            existing.flora_status       = flora_status
            existing.validation_status  = val_status
        else:
            db.session.add(Replication(
                doi_r=doi_r,
                original_rank=1,
                n_originals=1,
                title_r=title_r,
                abstract_r=abstract_r,
                doi_o=doi_o,
                title_o=title_o,
                year_o=year_o,
                authors_o=authors_o,
                link_method=link_method,
                link_evidence=link_evidence,
                link_confidence=link_confidence,
                outcome=outcome,
                outcome_phrase=outcome_phrase,
                outcome_confidence=outcome_confidence,
                flora_status=flora_status,
                validation_status=val_status,
            ))
            inserted += 1

    db.session.commit()
    return inserted


if __name__ == "__main__":
    from validate.app import create_app
    _app = create_app()
    with _app.app_context():
        n = import_csv()
        log.info("Imported %d new rows into %s", n, DB_PATH)
