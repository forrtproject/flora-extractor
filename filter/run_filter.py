"""
run_filter.py — Stage 2 orchestrator.

Reads data/candidates.csv, applies the rule filter then the LLM uplift
per row, and streams results to data/filtered.csv one row at a time.

DOIs already present in filtered.csv are skipped, so an interrupted run
can be resumed without reprocessing completed rows.

Usage:
    python -m filter.run_filter
"""
import pandas as pd

from shared.config import DATA_DIR, log
from shared.schema import CANDIDATES_COLS, FILTERED_COLS
from shared.utils import clean_doi
from filter.rule_filter import classify_row as _rule_classify
from filter.llm_filter import classify_with_llm as _llm_classify


def _append_row(out_path, row_dict: dict, first: bool) -> None:
    """Write one row to filtered.csv immediately after processing.

    first=True  → create / truncate the file and write the header.
    first=False → append without header.
    """
    row_df = pd.DataFrame([row_dict])
    for col in FILTERED_COLS:
        if col not in row_df.columns:
            row_df[col] = ""
    row_df[FILTERED_COLS].to_csv(
        out_path, mode="w" if first else "a",
        index=False, encoding="utf-8-sig", header=first,
    )


def run_filter(limit: "int | None" = None,
               offset: "int | None" = None) -> pd.DataFrame:
    """Run the filter pipeline, streaming results to data/filtered.csv.

    limit  — stop after processing this many new rows (None = no limit).
    offset — skip the first N unprocessed rows before starting (None = start from beginning).
             Useful for targeted reruns on a slice of the CSV.
    """
    candidates_path = DATA_DIR / "candidates.csv"
    if not candidates_path.exists():
        raise FileNotFoundError(
            f"candidates.csv not found at {candidates_path}. Run Stage 1 first."
        )

    df = pd.read_csv(candidates_path, dtype=str, encoding="utf-8-sig").fillna("")
    df = df.reindex(columns=CANDIDATES_COLS, fill_value="")
    log.info("Stage 2: loaded %d candidates", len(df))

    out_path = DATA_DIR / "filtered.csv"

    # Load already-processed DOIs so an interrupted run can be resumed.
    already_done: set[str] = set()
    first_write = not out_path.exists()
    if out_path.exists():
        try:
            existing = pd.read_csv(out_path, dtype=str, encoding="utf-8-sig").fillna("")
            already_done = {clean_doi(d) for d in existing["doi_r"] if d}
            log.info("Stage 2: %d rows already in filtered.csv — skipping", len(already_done))
        except Exception as exc:
            log.warning("Stage 2: could not read existing filtered.csv (%s) — starting fresh", exc)
            first_write = True

    output_rows: list[dict] = []
    new_rows = 0
    skipped  = 0   # counts unprocessed rows skipped by --offset

    for _, row in df.iterrows():
        doi_r = clean_doi(str(row.get("doi_r", "")))
        if doi_r in already_done:
            continue

        # --offset: skip the first N unprocessed rows
        if offset is not None and skipped < offset:
            skipped += 1
            continue

        # --limit: stop after N new rows have been written
        if limit is not None and new_rows >= limit:
            log.info("Stage 2: reached --limit %d — stopping", limit)
            break

        title    = str(row.get("title_r")    or "")
        abstract = str(row.get("abstract_r") or "")

        # Rule filter
        row_dict = row.to_dict()
        row_dict.update(_rule_classify(row_dict))

        # LLM uplift for rows the rule filter couldn't decide
        if row_dict.get("filter_status") == "needs_review":
            verdict = _llm_classify(title, abstract)
            if verdict:
                row_dict["filter_status"]     = verdict["filter_status"]
                row_dict["filter_confidence"] = verdict["filter_confidence"]
                prior = str(row_dict.get("filter_evidence") or "")
                row_dict["filter_evidence"] = (
                    f"{prior} | llm:{verdict['filter_evidence']}"
                    if prior else f"llm:{verdict['filter_evidence']}"
                )
                row_dict["filter_method"] = (
                    "both" if row_dict.get("filter_method") == "rule_based" else "llm"
                )

        _append_row(out_path, row_dict, first=first_write)
        first_write = False
        new_rows += 1
        output_rows.append(row_dict)
        log.info("[%s] filter_status=%s — streamed (%d new so far)",
                 doi_r, row_dict.get("filter_status"), new_rows)

    log.info("Stage 2 complete: %d new rows written → %s", new_rows, out_path)
    return pd.DataFrame(output_rows)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Stage 2 Filter pipeline")
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Stop after processing N new rows (None = unlimited).",
    )
    parser.add_argument(
        "--offset", type=int, default=None, metavar="N",
        help="Skip the first N unprocessed rows before starting.",
    )
    args = parser.parse_args()
    run_filter(limit=args.limit, offset=args.offset)
