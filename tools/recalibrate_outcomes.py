#!/usr/bin/env python3
"""
Recalibrate outcomes for extracted.csv using the improved classification logic.

This script:
1. Reads extracted.csv
2. For each row, re-runs outcome extraction with the new logic
3. Updates outcome columns in place
4. Writes back to extracted.csv

Optional: clear the outcome cache first to force fresh LLM calls.
"""
import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
import pandas as pd

from extract.code_outcome import extract_outcome
from shared.config import DATA_DIR, LLM_CACHE_DIR, OA_XML_CACHE_DIR, PARSE_CACHE_DIR, PDF_CACHE_DIR, log
from shared.pdf_parsing import best_parse_result, parse_all as _parse_all
from shared.utils import cache_key, clean_doi


def _ensure_parse_cache(doi_r: str) -> None:
    """Run all PDF parsers for doi_r and cache to PARSE_CACHE_DIR if not already cached."""
    key      = cache_key(doi_r)
    out_file = PARSE_CACHE_DIR / f"parse_{key}.json"
    if out_file.exists():
        return
    pdf_path = PDF_CACHE_DIR / f"{key}.pdf"
    if not pdf_path.exists():
        pdf_path = None  # type: ignore[assignment]
    oa_xml: dict | None = None
    oa_xml_file = OA_XML_CACHE_DIR / f"oa_xml_{key}.json"
    if oa_xml_file.exists():
        try:
            with oa_xml_file.open(encoding="utf-8") as fh:
                oa_xml = json.load(fh)
        except Exception:
            pass
    results = _parse_all(doi_r, pdf_path, oa_xml=oa_xml)
    try:
        with out_file.open("w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.debug("[%s] _ensure_parse_cache write failed: %s", doi_r, exc)


def _get_best_fulltext(doi_r: str) -> str:
    """Return full text from the best-scoring parse method in the cache.

    Prefers raw_text (full paper including results/discussion/conclusion);
    falls back to abstract + intro when raw_text is empty.
    """
    cache_file = PARSE_CACHE_DIR / f"parse_{cache_key(doi_r)}.json"
    if not cache_file.exists():
        return ""
    try:
        with cache_file.open(encoding="utf-8") as fh:
            results = json.load(fh)
        best = best_parse_result(results)
        if not best:
            return ""
        raw = str(best.get("raw_text", "") or "").strip()
        if raw:
            return raw
        return " ".join(filter(None, [
            str(best.get("abstract", "") or ""),
            str(best.get("intro",    "") or ""),
        ]))
    except Exception:
        return ""


def refresh_cannot_be_determined(extracted_csv: Path) -> int:
    """Filter extracted_csv for outcome==cannot_be_determined and write cannot_be_determined.csv."""
    cbd_path = extracted_csv.parent / "cannot_be_determined.csv"
    try:
        df = pd.read_csv(extracted_csv, dtype=str, encoding="utf-8-sig").fillna("")
        cbd = df[df["outcome"].str.strip().str.lower() == "cannot_be_determined"]
        cbd.to_csv(cbd_path, index=False, encoding="utf-8-sig")
        log.info("Refreshed %s: %d rows", cbd_path.name, len(cbd))
        return len(cbd)
    except Exception as exc:
        log.warning("Could not refresh cannot_be_determined.csv: %s", exc)
        return 0


def clear_outcome_cache():
    """Delete all cached outcome extraction results."""
    outcome_files = list(LLM_CACHE_DIR.glob("outcome_*.json"))
    if not outcome_files:
        log.info("No outcome cache files found")
        return 0

    for f in outcome_files:
        try:
            f.unlink()
        except Exception as e:
            log.warning("Failed to delete %s: %s", f.name, e)

    deleted = len(outcome_files)
    log.info("Deleted %d cached outcome files", deleted)
    return deleted


def recalibrate_outcomes(
    input_csv: Path,
    output_csv: Path = None,
    clear_cache: bool = False,
    dry_run: bool = False,
    limit: int = None,
    only_uncertain: bool = True,
    since_year: int = None,
    tail: int = None,
    use_fulltext: bool = False,
) -> dict:
    """
    Re-run outcome extraction for uncertain rows in a CSV.

    Parameters
    ----------
    input_csv : Path
        Path to extracted.csv (or similar structure)
    output_csv : Path, optional
        Where to write results. If None, overwrites input_csv.
    clear_cache : bool
        If True, delete cached outcomes for uncertain rows (forces fresh LLM calls).
    dry_run : bool
        If True, preview changes without writing.
    limit : int, optional
        Process only first N uncertain rows (for testing).
    only_uncertain : bool, default True
        If True, only process rows with outcome="cannot_be_determined" or no outcome.
        If False, reprocess all rows.

    Returns
    -------
    dict
        Statistics: rows_processed, rows_updated, cache_cleared, errors.
    """
    if output_csv is None:
        output_csv = input_csv

    log.info("=" * 70)
    log.info("Recalibrating Outcomes")
    log.info("=" * 70)
    log.info("Input:  %s", input_csv)
    log.info("Output: %s", output_csv if not dry_run else f"{output_csv} (DRY RUN)")
    log.info("Clear cache: %s", clear_cache)
    log.info("Since year: %s", since_year or "all years")
    log.info("Tail: %s", f"last {tail} rows" if tail else "all rows")
    log.info("Limit: %s rows", limit or "unlimited")

    if not input_csv.exists():
        log.error("File not found: %s", input_csv)
        return {"error": "File not found"}

    # Load the FULL CSV — df_full is never sliced, so the output always contains
    # every row, even when --tail or --since-year restricts what gets reprocessed.
    df_full = pd.read_csv(input_csv, encoding="utf-8-sig", low_memory=False)
    log.info("Loaded %d rows from %s", len(df_full), input_csv.name)

    # Back up the input before any writes so the original is always recoverable.
    if not dry_run:
        backup_path = input_csv.with_suffix(".csv.bak")
        shutil.copy2(input_csv, backup_path)
        log.info("Backup created: %s", backup_path)

    required_cols = ["doi_r", "title_r", "abstract_r"]
    missing = [c for c in required_cols if c not in df_full.columns]
    if missing:
        log.error("Missing columns: %s", missing)
        return {"error": f"Missing columns: {missing}"}

    # Build a boolean mask over df_full.index for which rows to process.
    mask = pd.Series(True, index=df_full.index)

    if tail is not None:
        tail_mask = pd.Series(False, index=df_full.index)
        tail_mask.iloc[-tail:] = True
        mask = mask & tail_mask
        log.info("Tail filter: last %d of %d rows selected", tail, len(df_full))

    if since_year is not None:
        years = pd.to_numeric(df_full.get("year_r", pd.Series(dtype=float)), errors="coerce").fillna(0)
        before = mask.sum()
        mask = mask & (years >= since_year)
        log.info("Year filter (%d+): %d → %d rows", since_year, before, mask.sum())

    if only_uncertain:
        uncertain_mask = (
            df_full["outcome"].isna() |
            (df_full["outcome"].astype(str).str.strip() == "") |
            (df_full["outcome"].astype(str).str.lower() == "cannot_be_determined") |
            (df_full["outcome"].astype(str).str.lower() == "pending")
        )
        before = mask.sum()
        mask = mask & uncertain_mask
        log.info("Uncertain filter: %d → %d rows to process", before, mask.sum())

    # The indices we will actually iterate over (honouring --limit)
    candidate_indices = df_full.index[mask].tolist()
    if limit:
        candidate_indices = candidate_indices[:limit]
    log.info("Will process %d rows", len(candidate_indices))

    # Clear outcome cache for selected rows if requested
    cache_cleared = 0
    if clear_cache:
        if not only_uncertain:
            cache_cleared = clear_outcome_cache()
        else:
            for idx in candidate_indices:
                doi_clean = clean_doi(str(df_full.at[idx, "doi_r"]))
                if doi_clean:
                    cache_file = LLM_CACHE_DIR / f"outcome_{cache_key(doi_clean)}.json"
                    if cache_file.exists():
                        try:
                            cache_file.unlink()
                            cache_cleared += 1
                        except Exception as e:
                            log.warning("Failed to delete %s: %s", cache_file.name, e)
            log.info("Cleared %d outcome cache files", cache_cleared)

    rows_processed = 0
    rows_updated = 0
    errors: list[dict] = []

    def _save(label: str = "") -> None:
        if dry_run:
            return
        # Write to a temp file in the same directory, then atomically replace the
        # target — a KeyboardInterrupt mid-write can no longer corrupt the output.
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=output_csv.parent, prefix=".recalibrate_tmp_", suffix=".csv"
        )
        try:
            os.close(tmp_fd)
            df_full.to_csv(tmp_path, index=False, encoding="utf-8-sig")
            os.replace(tmp_path, output_csv)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        tag = f"[{label}] " if label else ""
        log.info("%sSaved %d rows → %s  (%d updated so far)", tag, len(df_full), output_csv, rows_updated)

    try:
        for pos, orig_idx in enumerate(candidate_indices):
            row = df_full.loc[orig_idx]
            rows_processed += 1
            doi_r = clean_doi(str(row.get("doi_r", "")))
            title_r = str(row.get("title_r", ""))
            abstract_r = str(row.get("abstract_r", ""))

            if not abstract_r or not abstract_r.strip():
                log.debug("[%s] Skipping — no abstract", doi_r)
                continue

            log.info(
                "[%d/%d] [%s] Extracting outcome: %s...",
                pos + 1,
                len(candidate_indices),
                doi_r,
                title_r[:60],
            )

            try:
                original_title = str(row.get("title_o", ""))
                original_authors = str(row.get("authors_o", ""))
                original_year = str(row.get("year_o", ""))

                fulltext = ""
                if use_fulltext and doi_r:
                    _ensure_parse_cache(doi_r)
                    fulltext = _get_best_fulltext(doi_r)
                    # Bust cached abstract-only LLM result so the new call uses fulltext.
                    if fulltext and not clear_cache:
                        outcome_cache = LLM_CACHE_DIR / f"outcome_{cache_key(doi_r)}.json"
                        if outcome_cache.exists():
                            try:
                                outcome_cache.unlink()
                            except Exception:
                                pass
                    if fulltext:
                        log.info("  [%s] fulltext available (%d chars)", doi_r, len(fulltext))
                    else:
                        log.debug("  [%s] no fulltext — falling back to abstract only", doi_r)

                result = extract_outcome(
                    doi_r=doi_r,
                    title_r=title_r,
                    abstract_r=abstract_r,
                    fulltext=fulltext,
                    original_title=original_title if original_title and original_title != "nan" else "",
                    original_authors=original_authors if original_authors and original_authors != "nan" else "",
                    original_year=original_year if original_year and original_year != "nan" else "",
                )

                old_outcome = str(row.get("outcome", ""))
                new_outcome = result.get("outcome", "")
                if old_outcome != new_outcome:
                    rows_updated += 1
                    log.info(
                        "  [%s] %s → %s  (confidence: %s)",
                        doi_r, old_outcome or "(empty)", new_outcome,
                        result.get("outcome_confidence", "unknown"),
                    )
                else:
                    log.debug("  [%s] Outcome unchanged: %s", doi_r, new_outcome)

                # Update df_full in-place so partial progress is always writable
                df_full.at[orig_idx, "outcome"] = result.get("outcome", "")
                df_full.at[orig_idx, "outcome_phrase"] = result.get("outcome_phrase", "")
                df_full.at[orig_idx, "outcome_confidence"] = result.get("outcome_confidence", "")
                df_full.at[orig_idx, "out_quote_source"] = result.get("out_quote_source", "")
                df_full.at[orig_idx, "outcome_reasoning"] = result.get("outcome_reasoning", "")

            except Exception as e:
                log.error("[%s] Error: %s", doi_r, str(e))
                errors.append({"doi": doi_r, "error": str(e)})

            # Checkpoint every 10 rows so progress survives a crash
            if rows_processed % 10 == 0:
                _save("checkpoint")

            time.sleep(0.5)

    except KeyboardInterrupt:
        log.warning("\nInterrupted after %d/%d rows — saving progress...", rows_processed, len(candidate_indices))
    finally:
        _save()

    # Refresh cannot_be_determined.csv from the updated output file.
    cbd_count = 0
    if not dry_run:
        cbd_count = refresh_cannot_be_determined(output_csv)

    log.info("=" * 70)
    log.info("SUMMARY:")
    log.info("  Rows processed:  %d", rows_processed)
    log.info("  Rows updated:    %d", rows_updated)
    log.info("  Cache cleared:   %d files", cache_cleared)
    log.info("  Errors:          %d", len(errors))
    log.info("  cannot_be_determined remaining: %d", cbd_count)
    log.info("=" * 70)

    if errors:
        log.warning("\nFirst 5 errors:")
        for err in errors[:5]:
            log.warning("  [%s] %s", err["doi"], err["error"])

    return {
        "rows_processed": rows_processed,
        "rows_updated": rows_updated,
        "cache_cleared": cache_cleared,
        "errors": len(errors),
        "output_file": str(output_csv) if not dry_run else None,
        "interrupted": rows_processed < len(candidate_indices),
        "cannot_be_determined_remaining": cbd_count,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Recalibrate outcomes for extracted.csv using improved classification logic."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DATA_DIR / "extracted.csv",
        help="Input CSV file (default: data/extracted.csv)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV file (default: overwrites input)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete cached outcomes first (forces fresh LLM calls)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N uncertain rows (for testing)",
    )
    parser.add_argument(
        "--reprocess-all",
        action="store_true",
        help="Reprocess ALL rows (not just uncertain ones). Default: only uncertain rows.",
    )
    parser.add_argument(
        "--since-year",
        type=int,
        default=None,
        metavar="YEAR",
        help="Only process rows where year_r >= YEAR (e.g. --since-year 2020).",
    )
    parser.add_argument(
        "--tail",
        type=int,
        nargs="?",
        const=50,
        default=None,
        metavar="N",
        help="Only process the last N rows of the CSV (recently added entries). Defaults to 50 if N is omitted.",
    )
    parser.add_argument(
        "--fulltext",
        action="store_true",
        help=(
            "Download PDFs and use full text (abstract + intro) when calling the outcome LLM. "
            "Automatically busts the cached abstract-only result for each row that has fulltext. "
            "Useful for rows stuck at cannot_be_determined because the abstract lacked enough detail."
        ),
    )
    parser.add_argument(
        "--refresh-cbd",
        action="store_true",
        help=(
            "Refresh data/cannot_be_determined.csv from the input CSV and exit without "
            "running any recalibration. Use this to update the file after a run."
        ),
    )
    args = parser.parse_args()

    if args.refresh_cbd:
        n = refresh_cannot_be_determined(args.input)
        print(f"cannot_be_determined.csv refreshed: {n} rows")
        return {"cannot_be_determined_remaining": n}

    result = recalibrate_outcomes(
        input_csv=args.input,
        output_csv=args.output,
        clear_cache=args.clear_cache,
        dry_run=args.dry_run,
        limit=args.limit,
        only_uncertain=not args.reprocess_all,
        since_year=args.since_year,
        tail=args.tail,
        use_fulltext=args.fulltext,
    )

    return result


if __name__ == "__main__":
    result = main()
    print("\n" + json.dumps(result, indent=2))
