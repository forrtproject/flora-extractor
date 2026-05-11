"""
Stage 1 search orchestrator.

Each run fetches new candidates from all configured sources, merges them
into the existing ``data/candidates.csv`` (creating it on first run), and
deduplicates — so the file grows monotonically across runs rather than
being overwritten.

Every run also harvests all cached API page responses from disk before
issuing any new requests.  This means pages downloaded in a previous run
(even under a different year filter) are automatically incorporated without
re-fetching.

OpenAlex and Semantic Scholar phrase jobs are individually resumable:
interrupted runs pick up from the last saved cursor/offset rather than
restarting from page one.

Usage
-----
    python -m search.run_search                          # all years, unlimited
    python -m search.run_search --from-year 2020         # 2020 onwards
    python -m search.run_search --to-year 2023           # up to 2023
    python -m search.run_search --from-year 2020 --to-year 2023
    python -m search.run_search --max-per-phrase 200     # 1 page per phrase (quick test)
    python -m search.run_search --reset-cursors          # wipe all checkpoints and start fresh
"""

import argparse
import datetime
import json
from typing import Optional

import pandas as pd

from shared.config import DATA_DIR, OA_CACHE_DIR, log
from shared.schema import CANDIDATES_COLS
from search.openalex_search import fetch_openalex_candidates
from search.semantic_scholar_search import fetch_semantic_scholar_candidates
from search.external_lists import fetch_i4r, fetch_replication_network
from search.deduplicate import deduplicate_candidates
from search.engine_source import fetch_engine_candidates, is_engine_enabled


# ---------------------------------------------------------------------------
# Checkpoint reset helpers
# ---------------------------------------------------------------------------


def _reset_openalex_cursors() -> None:
    """Delete all OpenAlex cursor files so every phrase job restarts from page one."""
    cursor_files = list(OA_CACHE_DIR.glob("*.cursor.json"))
    if not cursor_files:
        log.info("No cursor files found — nothing to reset.")
        return
    for p in cursor_files:
        p.unlink()
    log.info(
        "Deleted %d cursor file(s) — OpenAlex phrases will restart from the beginning.",
        len(cursor_files),
    )


def _reset_s2_offsets() -> None:
    """Delete all S2 offset files so every phrase job restarts from offset zero."""
    from search.semantic_scholar_search import S2_CACHE_DIR

    offset_files = list(S2_CACHE_DIR.glob("*.offset.json"))
    if not offset_files:
        log.info("No S2 offset files found — nothing to reset.")
        return
    for p in offset_files:
        p.unlink()
    log.info(
        "Deleted %d S2 offset file(s) — S2 phrases will restart from the beginning.",
        len(offset_files),
    )


# ---------------------------------------------------------------------------
# Auto-advance state file helpers
# ---------------------------------------------------------------------------

_SEARCH_STATE_PATH = OA_CACHE_DIR.parent / "search_state.json"


def _load_search_state(from_year: int, to_year: int) -> dict:
    """Load auto-advance state, or initialise if absent / year range changed."""
    if _SEARCH_STATE_PATH.exists():
        try:
            with open(_SEARCH_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("from_year") == from_year and state.get("to_year") == to_year:
                return state
        except Exception:
            pass
    return {
        "from_year":          from_year,
        "to_year":            to_year,
        "current_year":       from_year,
        "current_phrase_idx": 0,
    }


def _save_search_state(state: dict) -> None:
    """Atomically write search state to disk."""
    state["last_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = _SEARCH_STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(_SEARCH_STATE_PATH)


def _advance_state(state: dict, phrase_list: list) -> dict:
    """Increment phrase index, rolling over to next year when all phrases are done."""
    state = dict(state)
    state["current_phrase_idx"] += 1
    if state["current_phrase_idx"] >= len(phrase_list):
        state["current_phrase_idx"] = 0
        state["current_year"] += 1
        if state["current_year"] > state["to_year"]:
            state["current_year"] = state["from_year"]
            log.info(
                "Auto-advance: cycled past to_year=%d — restarting from year %d",
                state["to_year"], state["from_year"],
            )
    return state


# ---------------------------------------------------------------------------
# Cache harvest helpers
# ---------------------------------------------------------------------------


def _harvest_oa_cache() -> pd.DataFrame:
    """Load every cached OpenAlex page response from disk and extract rows.

    Scans all ``*.json`` files in ``OA_CACHE_DIR`` (skipping ``*.cursor.json``
    checkpoint files) and applies the same ``_extract_row`` function used
    during live fetches.  This makes pages downloaded in any previous run —
    regardless of which year filter was active — available to the merge step
    without re-fetching them.

    Returns
    -------
    pd.DataFrame
        All rows extracted from cached pages, with ``CANDIDATES_COLS`` schema.
        Returns an empty DataFrame if the cache directory contains no page files.
    """
    from search.openalex_search import _extract_row

    rows = []
    for path in OA_CACHE_DIR.glob("*.json"):
        if ".cursor." in path.name:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for w in data.get("results") or []:
                if isinstance(w, dict):
                    rows.append(_extract_row(w))
        except Exception:
            continue
    if not rows:
        return pd.DataFrame(columns=CANDIDATES_COLS)
    df = pd.DataFrame(rows, columns=CANDIDATES_COLS)
    log.info(
        "OA cache harvest: %d rows from %d page files",
        len(df),
        sum(1 for _ in OA_CACHE_DIR.glob("*.json")),
    )
    return df


def _harvest_s2_cache() -> pd.DataFrame:
    """Load every cached S2 page response from disk and extract rows.

    Scans all ``*.json`` files in ``S2_CACHE_DIR`` (skipping ``*.offset.json``
    checkpoint files) and applies the same ``_extract_row`` function used
    during live fetches.  Pages from any previous run are included regardless
    of the year filter that was active when they were downloaded.

    Returns
    -------
    pd.DataFrame
        All rows extracted from cached pages, with ``CANDIDATES_COLS`` schema.
        Returns an empty DataFrame if the cache directory contains no page files.
    """
    from search.semantic_scholar_search import S2_CACHE_DIR, _extract_row

    rows = []
    for path in S2_CACHE_DIR.glob("*.json"):
        if ".offset." in path.name:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for p in data.get("data") or []:
                if isinstance(p, dict):
                    rows.append(_extract_row(p))
        except Exception:
            continue
    if not rows:
        return pd.DataFrame(columns=CANDIDATES_COLS)
    df = pd.DataFrame(rows, columns=CANDIDATES_COLS)
    log.info(
        "S2 cache harvest: %d rows from %d page files",
        len(df),
        sum(1 for _ in S2_CACHE_DIR.glob("*.json")),
    )
    return df


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------


def _merge_into_candidates_csv(new_df: pd.DataFrame, out_path: "Path") -> pd.DataFrame:
    """Append *new_df* to the existing candidates CSV, deduplicate, and write back.

    New rows take precedence over existing rows on any key clash, so
    re-fetched metadata replaces stale entries automatically.

    Deduplication is applied in three passes to handle the different
    identifier coverage across sources:

    1. Rows with ``openalex_id_r`` → deduplicate on ``openalex_id_r``.
    2. Rows without ``openalex_id_r`` but with ``doi_r`` → deduplicate on
       ``doi_r`` (covers most Semantic Scholar rows).
    3. Rows with neither identifier → deduplicate on lowercased ``title_r``
       as a best-effort fallback.

    This avoids the naive pitfall of collapsing all rows where
    ``openalex_id_r`` is ``None`` (e.g. all S2 rows) into a single entry.

    Parameters
    ----------
    new_df : pd.DataFrame
        Incoming rows to merge.  May overlap with existing CSV content.
    out_path : Path
        Destination CSV path.  Created if absent.

    Returns
    -------
    pd.DataFrame
        The full deduplicated candidate set after the merge.
    """
    if out_path.exists():
        existing = pd.read_csv(out_path, encoding="utf-8-sig", low_memory=False)
        log.info("Existing candidates.csv: %d rows", len(existing))
        # Concat new first so new rows win on dedup (keep="first").
        combined = pd.concat([new_df, existing], ignore_index=True)
    else:
        combined = new_df.copy()

    before = len(combined)

    def _has(col: str) -> pd.Series:
        """Return a boolean mask for rows where *col* is non-null and non-empty."""
        return combined[col].notna() & (combined[col].astype(str).str.strip() != "")

    oa_mask = _has("openalex_id_r")
    oa_rows = combined[oa_mask].drop_duplicates(subset=["openalex_id_r"], keep="first")

    no_oa = combined[~oa_mask]
    doi_mask = _has("doi_r").reindex(no_oa.index, fill_value=False)
    doi_rows = no_oa[doi_mask].drop_duplicates(subset=["doi_r"], keep="first")

    rest = no_oa[~doi_mask]
    title_key = rest["title_r"].str.lower().str.strip()
    title_rows = rest[~title_key.duplicated(keep="first")]

    combined = pd.concat([oa_rows, doi_rows, title_rows], ignore_index=True)
    log.info(
        "Merged: %d → %d rows after dedup (+%d from new batch)",
        before,
        len(combined),
        len(combined) - (before - len(new_df)),
    )

    combined.to_csv(out_path, index=False, encoding="utf-8-sig")
    return combined


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def run_search(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    max_records_per_phrase: Optional[int] = None,
) -> pd.DataFrame:
    """Run all Stage 1 discovery sources and merge results into ``candidates.csv``.

    The function proceeds in two phases:

    1. **Cache harvest** — all previously downloaded API pages (OpenAlex and
       S2) are read from disk and merged into ``candidates.csv`` regardless
       of which year range was active when they were fetched.
    2. **Live fetch** — each enabled source is queried for new pages under
       the current year filter.  Results are deduplicated within the batch,
       then merged into ``candidates.csv``.

    Parameters
    ----------
    from_year, to_year : int, optional
        Publication year range (inclusive) passed to all sources.  The year
        range is part of the OpenAlex/S2 job identity — a different range
        creates independent checkpoint files without affecting previous jobs.
    max_records_per_phrase : int, optional
        Cap on new rows fetched per phrase per call for OpenAlex and S2.
        Checkpoints are saved at the page boundary so subsequent calls
        continue from that point.  ``None`` = unlimited.

    Returns
    -------
    pd.DataFrame
        The full deduplicated candidate set after the merge.
    """
    yr_label = f"{from_year or 'any'}–{to_year or 'any'}"
    log.info("Stage 1 starting  (years: %s)", yr_label)

    # Phase 1: harvest all cached pages so prior runs flow into the CSV
    # regardless of which year range was active when they were downloaded.
    log.info("Harvesting all cached OpenAlex and Semantic Scholar pages...")
    cached_batch = pd.concat(
        [_harvest_oa_cache(), _harvest_s2_cache()], ignore_index=True
    )
    if not cached_batch.empty:
        log.info(
            "Cache harvest total: %d rows — merging into candidates.csv",
            len(cached_batch),
        )
        _merge_into_candidates_csv(cached_batch, DATA_DIR / "candidates.csv")

    # Phase 2: live fetch from each enabled source.
    frames: list[pd.DataFrame] = []

    if is_engine_enabled():
        log.info("Stage 1: fetching engine candidates (FLORA_USE_ENGINE=1)...")
        frames.append(fetch_engine_candidates(year_from=from_year, year_to=to_year))
    else:
        log.info("Stage 1: fetching OpenAlex candidates...")
        frames.append(
            fetch_openalex_candidates(
                from_year=from_year,
                to_year=to_year,
                max_records_per_phrase=max_records_per_phrase,
            )
        )

    log.info("Stage 1: fetching Semantic Scholar candidates...")
    frames.append(
        fetch_semantic_scholar_candidates(
            from_year=from_year,
            to_year=to_year,
            max_records_per_phrase=max_records_per_phrase,
        )
    )

    log.info("Stage 1: fetching Replication Network sheet...")
    frames.append(fetch_replication_network(from_year=from_year, to_year=to_year))

    log.info("Stage 1: fetching I4R list...")
    frames.append(fetch_i4r(from_year=from_year, to_year=to_year))

    combined = (
        pd.concat(frames, ignore_index=True)
        if any(not f.empty for f in frames)
        else pd.DataFrame(columns=CANDIDATES_COLS)
    )

    new_batch = deduplicate_candidates(combined)
    log.info("New batch (deduped): %d candidates", len(new_batch))

    out_path = DATA_DIR / "candidates.csv"
    result = _merge_into_candidates_csv(new_batch, out_path)

    log.info("Stage 1 complete: %d total candidates in %s", len(result), out_path)
    return result


def run_search_auto_advance(
    from_year: int = 2011,
    to_year:   int = 2021,
    max_records_per_phrase: int = 200,
) -> pd.DataFrame:
    """Process exactly ONE phrase/year combination per invocation.

    Reads state from ``cache/search_state.json``, runs the current phrase for
    the current year, then advances to the next phrase (rolling to the next year
    when all phrases for the current year are done).  Designed to be called
    repeatedly — each call appends results to ``candidates.csv``.

    Run command:
        python -m search.run_search --auto-advance --from-year 2011 --to-year 2021 --max-per-phrase 200
    """
    from search.openalex_search import SEARCH_PHRASES, fetch_phrase

    state  = _load_search_state(from_year, to_year)
    year   = state["current_year"]
    pidx   = state["current_phrase_idx"]
    phrase = SEARCH_PHRASES[pidx % len(SEARCH_PHRASES)]

    log.info(
        "Auto-advance: year=%d  phrase[%d/%d]=%r",
        year, pidx + 1, len(SEARCH_PHRASES), phrase,
    )

    rows = fetch_phrase(phrase, from_year=year, to_year=year,
                        max_records=max_records_per_phrase)

    out_path = DATA_DIR / "candidates.csv"
    if rows:
        batch  = pd.DataFrame(rows, columns=CANDIDATES_COLS)
        result = _merge_into_candidates_csv(deduplicate_candidates(batch), out_path)
    else:
        log.info("Auto-advance: no new rows for phrase=%r year=%d", phrase, year)
        result = (
            pd.read_csv(out_path, encoding="utf-8-sig")
            if out_path.exists()
            else pd.DataFrame(columns=CANDIDATES_COLS)
        )

    new_state = _advance_state(state, SEARCH_PHRASES)
    _save_search_state(new_state)
    log.info(
        "Auto-advance: next run will process year=%d phrase[%d]=%r",
        new_state["current_year"],
        new_state["current_phrase_idx"],
        SEARCH_PHRASES[new_state["current_phrase_idx"]],
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the Stage 1 search runner."""
    parser = argparse.ArgumentParser(
        description="Run Stage 1 candidate search across all sources."
    )
    parser.add_argument(
        "--from-year",
        type=int,
        default=None,
        metavar="YYYY",
        help="Earliest publication year to include (inclusive).",
    )
    parser.add_argument(
        "--to-year",
        type=int,
        default=None,
        metavar="YYYY",
        help="Latest publication year to include (inclusive).",
    )
    parser.add_argument(
        "--max-per-phrase",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Limit new rows per phrase per run for OpenAlex and S2.  "
            "Checkpoints are saved so the next run continues from this point.  "
            "Omit for unlimited fetching."
        ),
    )
    parser.add_argument(
        "--reset-cursors",
        action="store_true",
        help="Delete all saved OpenAlex cursor files and S2 offset files, then start fresh.",
    )
    parser.add_argument(
        "--auto-advance",
        action="store_true",
        help=(
            "Process one phrase/year combo per run (reads/writes cache/search_state.json). "
            "Pair with --from-year, --to-year, --max-per-phrase. "
            "Run repeatedly to advance through the year range."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.reset_cursors:
        _reset_openalex_cursors()
        _reset_s2_offsets()

    if args.auto_advance:
        from_yr = args.from_year or 2011
        to_yr   = args.to_year   or 2021
        max_n   = args.max_per_phrase or 200
        run_search_auto_advance(from_year=from_yr, to_year=to_yr,
                                max_records_per_phrase=max_n)
    else:
        run_search(
            from_year=args.from_year,
            to_year=args.to_year,
            max_records_per_phrase=args.max_per_phrase,
        )
