"""
import_csv.py — Load data/extracted.csv into the SQLite database.

Run once before starting the Flask app:
    python validate/import_csv.py

Creates (or updates) the SQLite database at data/flora.db with rows
from data/extracted.csv. Existing records are updated in place.
"""
import pandas as pd

from shared.config import DATA_DIR, log
from shared.schema import EXTRACTED_COLS
from shared.utils import clean_doi
from validate.models import db, Replication


DB_PATH = DATA_DIR / "flora.db"
EXTRACTED_CSV = DATA_DIR / "extracted.csv"


def import_csv() -> int:
    """
    Load extracted.csv into SQLite. Returns number of rows imported.
    """
    # TODO: implement CSV → SQLite import
    raise NotImplementedError("import_csv is not yet implemented")


if __name__ == "__main__":
    count = import_csv()
    log.info("Imported %d rows into %s", count, DB_PATH)
