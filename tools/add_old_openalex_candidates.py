"""
add_old_openalex_candidates.py — Port openalex-sourced rows from the prior
extraction pass (data/all_replications.csv, pathway_source == "openalex")
into data/candidates.csv so Stage 2/3 can process them.

data/all_replications.csv predates this pipeline's schema: it has no
authors_r, journal_r, or openalex_id_r columns. Those are left blank here —
Stage 3's linking step (shared/openalex_client.fetch_openalex_full_metadata)
already re-fetches full metadata per DOI, so nothing is permanently lost.

Reuses search.run_search's index-based merge so dedup against the existing
2M+ row candidates.csv (and the resulting index/file growth) follows the
same rules as a normal Stage 1 search run.

Usage:
    python -m tools.add_old_openalex_candidates
"""
import pandas as pd

from shared.config import DATA_DIR
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi
from search.run_search import _merge_into_candidates_csv

OLD_PIPELINE_CSV = DATA_DIR / "all_replications.csv"


def _load_openalex_rows() -> pd.DataFrame:
    old = pd.read_csv(OLD_PIPELINE_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    old = old[old["pathway_source"] == "openalex"]

    return pd.DataFrame({
        "doi_r":         old["doi_r"].apply(clean_doi),
        "title_r":       old["study_r"],
        "abstract_r":    old["abstract_r"],
        "year_r":        old["year_r"],
        "authors_r":     "",
        "journal_r":     "",
        "url_r":         old["url_r"],
        "openalex_id_r": "",
        "source":        "openalex",
        "ref_r":         old["ref_r"],
    })[CANDIDATES_COLS]


def main() -> None:
    new_df = _load_openalex_rows()
    print(f"Loaded {len(new_df)} openalex rows from {OLD_PIPELINE_CSV.name}")
    _merge_into_candidates_csv(new_df, DATA_DIR / "candidates.csv")


if __name__ == "__main__":
    main()
