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
from shared import token_counter
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
               offset: "int | None" = None,
               from_year: "int | None" = None,
               to_year: "int | None" = None,
               source: "str | None" = None) -> pd.DataFrame:
    """Run the filter pipeline, streaming results to data/filtered.csv.

    limit     — stop after processing this many new rows (None = no limit).
    offset    — skip the first N unprocessed rows before starting.
    from_year — only process rows where year_r >= from_year.
    to_year   — only process rows where year_r <= to_year.
    source    — only process rows where the source column equals this value
                (e.g. 'openalex', 'bob_reed', 'i4r', 'semantic_scholar').
                Case-insensitive. None = all sources.
    """
    candidates_path = DATA_DIR / "candidates.csv"
    if not candidates_path.exists():
        raise FileNotFoundError(
            f"candidates.csv not found at {candidates_path}. Run Stage 1 first."
        )

    df = pd.read_csv(candidates_path, dtype=str, encoding="utf-8-sig").fillna("")
    df = df.reindex(columns=CANDIDATES_COLS, fill_value="")
    log.info("Stage 2: loaded %d candidates", len(df))

    # ── Year filter ───────────────────────────────────────────────────────────
    if from_year is not None or to_year is not None:
        def _year_int(v: str) -> "int | None":
            try:
                return int(v)
            except (ValueError, TypeError):
                return None
        years = df["year_r"].apply(_year_int)
        mask  = pd.Series(True, index=df.index)
        if from_year is not None:
            mask &= years.apply(lambda y: y is not None and y >= from_year)
        if to_year is not None:
            mask &= years.apply(lambda y: y is not None and y <= to_year)
        before = len(df)
        df = df[mask].reset_index(drop=True)
        log.info("--year filter %s–%s: %d → %d rows",
                 from_year or "any", to_year or "any", before, len(df))

    # ── Source filter ─────────────────────────────────────────────────────────
    if source is not None:
        before = len(df)
        df = df[df["source"].str.lower() == source.lower()].reset_index(drop=True)
        log.info("--source filter %r: %d → %d rows", source, before, len(df))

    out_path = DATA_DIR / "filtered.csv"

    # Load already-processed rows so an interrupted run can be resumed.
    # Key fallback: doi_r → openalex_id_r → url_r → title_r (same logic as extract stage).
    def _row_key(r) -> str:
        doi = clean_doi(str(r.get("doi_r", "") or ""))
        if doi:
            return doi
        oa = str(r.get("openalex_id_r", "") or "").strip()
        if oa:
            return f"oa:{oa}"
        url = str(r.get("url_r", "") or "").strip()
        if url:
            return f"url:{url}"
        title = str(r.get("title_r", "") or "").lower().strip()
        return f"title:{title}" if title else ""

    already_done: set[str] = set()
    first_write = not out_path.exists()
    if out_path.exists():
        try:
            existing = pd.read_csv(out_path, dtype=str, encoding="utf-8-sig").fillna("")
            for _, er in existing.iterrows():
                k = _row_key(er)
                if k:
                    already_done.add(k)
            log.info("Stage 2: %d rows already in filtered.csv — skipping", len(already_done))
        except Exception as exc:
            log.warning("Stage 2: could not read existing filtered.csv (%s) — starting fresh", exc)
            first_write = True

    output_rows: list[dict] = []
    new_rows = 0
    skipped  = 0   # counts unprocessed rows skipped by --offset

    for _, row in df.iterrows():
        if _row_key(row) in already_done:
            continue

        # --offset: skip the first N unprocessed rows
        if offset is not None and skipped < offset:
            skipped += 1
            continue

        # --limit: stop after N new rows have been written
        if limit is not None and new_rows >= limit:
            log.info("Stage 2: reached --limit %d — stopping", limit)
            break

        doi_r    = str(row.get("doi_r")       or "")
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
        help="Stop after processing N new rows.",
    )
    parser.add_argument(
        "--offset", type=int, default=None, metavar="N",
        help="Skip the first N unprocessed rows before starting.",
    )
    parser.add_argument(
        "--from-year", type=int, default=None, metavar="YYYY",
        help="Only process rows where year_r >= YYYY.",
    )
    parser.add_argument(
        "--to-year", type=int, default=None, metavar="YYYY",
        help="Only process rows where year_r <= YYYY.",
    )
    parser.add_argument(
        "--source", type=str, default=None, metavar="SOURCE",
        help=(
            "Only process rows from this source "
            "(openalex | bob_reed | i4r | semantic_scholar | …). "
            "Case-insensitive."
        ),
    )
    args = parser.parse_args()
    try:
        run_filter(
            limit=args.limit,
            offset=args.offset,
            from_year=args.from_year,
            to_year=args.to_year,
            source=args.source,
        )
    finally:
        token_counter.print_summary()
