"""
import_csv.py — Load data/flora_selected.csv into the SQLite database.

Run once before starting the Flask app:
    python -m validate.import_csv

Column mapping:
    study_r                 → title_r
    resolved_doi_o          → doi_o
    resolved_title_o        → title_o
    user_val_status         → validation_status  (blank → "pending")
    flora_validation_status → flora_status

Upsert on (doi_r, original_rank=1). Safe to re-run.
"""
from pathlib import Path

import pandas as pd

from shared.config import DATA_DIR, log
from shared.utils import clean_doi
from validate.models import db, Replication

DB_PATH    = DATA_DIR / "flora.db"
SOURCE_CSV = DATA_DIR / "flora_selected.csv"


def import_csv(csv_path: Path = SOURCE_CSV) -> int:
    """Load csv_path into SQLite. Returns number of newly inserted rows."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str).fillna("")

    inserted = 0
    for _, row in df.iterrows():
        doi_r = clean_doi(row.get("doi_r", "").strip())
        if not doi_r:
            continue

        title_r      = row.get("study_r", "").strip()
        doi_o        = clean_doi(row.get("resolved_doi_o", "").strip())
        title_o      = row.get("resolved_title_o", "").strip()
        val_status   = row.get("user_val_status", "").strip() or "pending"
        flora_status = row.get("flora_validation_status", "").strip()

        existing = Replication.query.filter_by(
            doi_r=doi_r, original_rank=1
        ).first()

        if existing:
            existing.title_r           = title_r
            existing.doi_o             = doi_o
            existing.title_o           = title_o
            existing.validation_status = val_status
            existing.flora_status      = flora_status
        else:
            db.session.add(Replication(
                doi_r=doi_r,
                original_rank=1,
                n_originals=1,
                title_r=title_r,
                doi_o=doi_o,
                title_o=title_o,
                validation_status=val_status,
                flora_status=flora_status,
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
