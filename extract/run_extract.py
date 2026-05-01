"""
run_extract.py — Stage 3 orchestrator.

Routes each DOI to the single-original or multi-original pipeline,
then writes data/extracted.csv.

Usage:
    python extract/run_extract.py
"""
import pandas as pd

from shared.config import DATA_DIR, log
from shared.schema import EXTRACTED_COLS
from shared.utils import clean_doi
from extract.link_original import run_for_doi
from extract.multi_original import run_multi_original_for_doi
from extract.code_outcome import extract_outcome


def run_extract() -> pd.DataFrame:
    """Run Stage 3 and write data/extracted.csv."""
    filtered_path = DATA_DIR / "filtered.csv"
    if not filtered_path.exists():
        raise FileNotFoundError(f"filtered.csv not found at {filtered_path}. Run Stage 2 first.")

    df = pd.read_csv(filtered_path, dtype=str, encoding="utf-8-sig").fillna("")
    log.info("Stage 3: loaded %d filtered rows", len(df))

    # Only process confirmed replications/reproductions, skip false positives
    df = df[df["filter_status"].isin(["replication", "reproduction"])].copy()
    log.info("Stage 3: %d rows after dropping false_positives", len(df))

    output_rows: list[dict] = []

    for _, row in df.iterrows():
        doi_r = clean_doi(str(row.get("doi_r", "")))
        match_type = str(row.get("original_match_type", "single_original")).strip()

        try:
            if match_type == "multiple_original":
                # Case C: multi-original path → may expand to N rows
                result = run_multi_original_for_doi(doi_r, df, df)
                if result.get("is_false_positive") or result.get("n_originals", 0) == 0:
                    # Treat as single
                    single = run_for_doi(doi_r)
                    output_rows.append(_merge_row(row, single, rank=1, n=1))
                else:
                    originals = result.get("originals", [])
                    for orig in originals:
                        output_rows.append(_merge_multi_row(row, result, orig, n=len(originals)))
            else:
                # Case A/B: single-original path
                result = run_for_doi(doi_r)
                output_rows.append(_merge_row(row, result, rank=1, n=1))
        except Exception as e:
            log.error("[%s] extraction failed: %s", doi_r, e)
            output_rows.append(_empty_row(row))

    out_df = pd.DataFrame(output_rows)
    for col in EXTRACTED_COLS:
        if col not in out_df.columns:
            out_df[col] = ""

    out_path = DATA_DIR / "extracted.csv"
    out_df[EXTRACTED_COLS].to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("Stage 3 complete: %d rows → %s", len(out_df), out_path)
    return out_df


def _merge_row(filter_row: pd.Series, result: dict,
               rank: int, n: int) -> dict:
    """Build one extracted.csv row from a filter row + pipeline result."""
    row = filter_row.to_dict()
    row.update({
        "doi_o"           : result.get("resolved_doi_o",   ""),
        "title_o"         : result.get("resolved_title_o", ""),
        "year_o"          : result.get("resolved_year_o",  ""),
        "authors_o"       : result.get("resolved_author_o",""),
        "link_method"     : result.get("resolution_method","target_pending"),
        "link_evidence"   : result.get("llm_evidence",     ""),
        "link_confidence" : result.get("resolution_score", 0.0),
        "outcome"         : "pending",
        "outcome_phrase"  : "",
        "outcome_confidence": 0.0,
        "out_quote_source": "",
        "type"            : str(filter_row.get("filter_status", "replication")),
        "original_rank"   : rank,
        "n_originals"     : n,
    })
    return row


def _merge_multi_row(filter_row: pd.Series, result: dict,
                     orig: dict, n: int) -> dict:
    """Build one extracted.csv row for a multi-original result."""
    row = filter_row.to_dict()
    row.update({
        "doi_o"           : orig.get("doi",          ""),
        "title_o"         : orig.get("title",        ""),
        "year_o"          : orig.get("year",         ""),
        "authors_o"       : orig.get("first_author", ""),
        "link_method"     : "llm_fulltext",
        "link_evidence"   : orig.get("evidence",     ""),
        "link_confidence" : {"high": 1.0, "medium": 0.6, "low": 0.3}.get(
                                orig.get("confidence", "low"), 0.3),
        "outcome"         : "pending",
        "outcome_phrase"  : "",
        "outcome_confidence": 0.0,
        "out_quote_source": "",
        "type"            : "replication",
        "original_rank"   : orig.get("rank", 1),
        "n_originals"     : n,
    })
    return row


def _empty_row(filter_row: pd.Series) -> dict:
    """Return a row with target_pending status when extraction fails."""
    row = filter_row.to_dict()
    row.update({
        "doi_o": "", "title_o": "", "year_o": "", "authors_o": "",
        "link_method": "target_pending", "link_evidence": "", "link_confidence": 0.0,
        "outcome": "pending", "outcome_phrase": "", "outcome_confidence": 0.0,
        "out_quote_source": "", "type": "", "original_rank": 1, "n_originals": 1,
    })
    return row


if __name__ == "__main__":
    run_extract()
