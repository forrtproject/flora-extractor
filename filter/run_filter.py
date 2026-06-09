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

from shared.config import CACHE_DIR, DATA_DIR, log
from shared import token_counter
from shared.schema import CANDIDATES_COLS, FILTERED_COLS
from shared.utils import clean_doi
from filter.rule_filter import classify_row as _rule_classify
from filter.llm_filter import classify_with_llm as _llm_classify

# ---------------------------------------------------------------------------
# Filtered index — avoids loading the full filtered.csv to build already_done
# ---------------------------------------------------------------------------

_FILTERED_INDEX_PATH = CACHE_DIR / "filtered_index.txt"


def _row_key(r: "pd.Series | dict") -> str:
    """Single identifying key for a row, used as resume key in filtered index.
    Priority: doi → openalex_id → url → title. Returns '' if none available."""
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


def _load_filtered_index() -> set[str]:
    """Load filtered index from disk, streaming to avoid loading entire file in memory.

    With 100k+ entries, reading all at once can cause MemoryError.
    This reads line-by-line instead, keeping memory usage bounded.
    """
    if not _FILTERED_INDEX_PATH.exists():
        return set()

    index = set()
    try:
        with open(_FILTERED_INDEX_PATH, "r", encoding="utf-8") as f:
            for line in f:
                key = line.strip()
                if key:  # Skip empty lines
                    index.add(key)
        log.info("Filtered index loaded: %d keys from disk", len(index))
    except MemoryError:
        log.error("MemoryError loading filtered index — file may be too large (%s)",
                  _FILTERED_INDEX_PATH.stat().st_size / (1024**2) if _FILTERED_INDEX_PATH.exists() else "unknown")
        raise
    except Exception as e:
        log.error("Failed to load filtered index: %s", e)
        raise

    return index


def _save_filtered_index(index: set[str]) -> None:
    tmp = _FILTERED_INDEX_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(sorted(index)), encoding="utf-8")
    tmp.replace(_FILTERED_INDEX_PATH)


def _build_filtered_index(csv_path) -> set[str]:
    """Build filtered index from filtered.csv in 50k-row chunks. One-time migration."""
    log.info("Building filtered index from %s (reading in chunks)...", csv_path)
    index: set[str] = set()
    for chunk in pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig", chunksize=50_000):
        chunk = chunk.fillna("")
        for _, row in chunk.iterrows():
            k = _row_key(row)
            if k:
                index.add(k)
    log.info("Filtered index built: %d keys — saving to disk", len(index))
    _save_filtered_index(index)
    return index


def _append_key_to_filtered_index(key: str) -> None:
    """Append a single key to the filtered index file (fast incremental update)."""
    with open(_FILTERED_INDEX_PATH, "a", encoding="utf-8") as f:
        f.write(key + "\n")


def _append_row(out_path, row_dict: dict, first: bool) -> None:
    """Write one row to filtered.csv immediately after processing.

    first=True  → create / truncate the file and write the header.
    first=False → append without header.
    """
    # Sanitize row data to handle problematic characters
    for key, val in row_dict.items():
        if isinstance(val, str):
            # Replace control characters and problematic whitespace
            # but preserve newlines within fields (they'll be quoted)
            row_dict[key] = val.replace('\x00', '').replace('\r', ' ')

    row_df = pd.DataFrame([row_dict])
    for col in FILTERED_COLS:
        if col not in row_df.columns:
            row_df[col] = ""

    try:
        row_df[FILTERED_COLS].to_csv(
            out_path, mode="w" if first else "a",
            index=False, encoding="utf-8-sig", header=first,
            quoting=1,  # csv.QUOTE_ALL to quote fields with special characters
            quotechar='"',
        )
    except Exception as e:
        log.error("Failed to write row for DOI %s: %s",
                  row_dict.get("doi_r", "unknown"), str(e))
        raise


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

    # Read candidates.csv in 50k-row chunks to avoid OOM on large files.
    # Rows are filtered by year/source per chunk and collected into a list.
    def _year_int(v: str) -> "int | None":
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    chunks: list[pd.DataFrame] = []
    total_read = 0
    bad_id_count = 0

    for chunk in pd.read_csv(
        candidates_path, dtype=str, encoding="utf-8-sig",
        chunksize=50_000, low_memory=False
    ):
        chunk = chunk.fillna("").reindex(columns=CANDIDATES_COLS, fill_value="")
        total_read += len(chunk)

        # Count rows with no identifying info for the warning
        has_id = (
            chunk["doi_r"].str.strip().astype(bool)
            | chunk["openalex_id_r"].str.strip().astype(bool)
            | chunk["url_r"].str.strip().astype(bool)
            | chunk["title_r"].str.strip().astype(bool)
        )
        bad_id_count += (~has_id).sum()

        # Year filter
        if from_year is not None or to_year is not None:
            years = chunk["year_r"].apply(_year_int)
            mask = pd.Series(True, index=chunk.index)
            if from_year is not None:
                mask &= years.apply(lambda y: y is not None and y >= from_year)
            if to_year is not None:
                mask &= years.apply(lambda y: y is not None and y <= to_year)
            chunk = chunk[mask]

        # Source filter
        if source is not None:
            chunk = chunk[chunk["source"].str.lower() == source.lower()]

        if not chunk.empty:
            chunks.append(chunk)

    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=CANDIDATES_COLS)
    log.info("Stage 2: loaded %d candidates (%d total read)", len(df), total_read)

    if bad_id_count > 0:
        log.warning(
            "Stage 2: %d candidate rows have NO identifying info (doi/oa_id/url/title) — "
            "these cannot be deduplicated and may reprocess on each run.",
            bad_id_count,
        )

    if from_year is not None or to_year is not None:
        log.info("--year filter %s–%s applied during chunked read",
                 from_year or "any", to_year or "any")
    if source is not None:
        log.info("--source filter %r applied during chunked read", source)

    out_path = DATA_DIR / "filtered.csv"
    first_write = not out_path.exists()

    # Load already-processed keys from the index file (fast, avoids reading
    # full filtered.csv). On first run the index is built from filtered.csv
    # in 50k-row chunks, then cached for all future runs.
    if _FILTERED_INDEX_PATH.exists():
        already_done = _load_filtered_index()
        log.info("Stage 2: %d rows already in filtered index — skipping", len(already_done))
    elif out_path.exists():
        try:
            already_done = _build_filtered_index(out_path)
            log.info("Stage 2: %d rows indexed from filtered.csv — skipping", len(already_done))
        except Exception as exc:
            log.warning("Stage 2: could not build filtered index (%s) — starting fresh", exc)
            already_done = set()
            first_write = True
    else:
        already_done = set()

    output_rows: list[dict] = []
    new_rows = 0
    skipped  = 0   # counts unprocessed rows skipped by --offset
    rows_with_empty_keys_input = 0

    for row_idx, row in df.iterrows():
        key = _row_key(row)
        if not key:
            # Fallback: use row index as a unique identifier for rows with no identifiers
            key = f"idx:{row_idx}"
            rows_with_empty_keys_input += 1

        if key in already_done:
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

        # Update the index immediately so resume works even after a crash.
        if key not in already_done:
            already_done.add(key)
            _append_key_to_filtered_index(key)

        log.info("[%s] filter_status=%s — streamed (%d new so far)",
                 doi_r, row_dict.get("filter_status"), new_rows)

    if rows_with_empty_keys_input > 0:
        log.warning(
            "Stage 2: %d input rows had no identifying key (fallback: used row index) — "
            "these will be re-processed if candidates.csv is resorted/reordered",
            rows_with_empty_keys_input,
        )

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
    parser.add_argument(
        "--rebuild-index", action="store_true",
        help="Force rebuild of the filtered index from filtered.csv, then exit.",
    )
    args = parser.parse_args()

    if args.rebuild_index:
        out_path = DATA_DIR / "filtered.csv"
        if out_path.exists():
            idx = _build_filtered_index(out_path)
            print(f"Rebuilt filtered index: {len(idx)} keys → {_FILTERED_INDEX_PATH}")
        else:
            print("filtered.csv not found — nothing to rebuild.")
    else:
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
