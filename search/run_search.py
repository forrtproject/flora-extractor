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
    """Load auto-advance state, or initialise if absent / year range changed.

    Migrates old states that used ``current_phrase_idx`` (OpenAlex-only) to
    the new ``current_job_idx`` key that spans all sources.
    """
    if _SEARCH_STATE_PATH.exists():
        try:
            with open(_SEARCH_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("from_year") == from_year and state.get("to_year") == to_year:
                # Migrate old single-source state format transparently.
                if "current_phrase_idx" in state and "current_job_idx" not in state:
                    state["current_job_idx"] = state.pop("current_phrase_idx")
                return state
        except Exception:
            pass
    return {
        "from_year":       from_year,
        "to_year":         to_year,
        "current_year":    from_year,
        "current_job_idx": 0,
    }


def _save_search_state(state: dict) -> None:
    """Atomically write search state to disk."""
    state["last_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = _SEARCH_STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(_SEARCH_STATE_PATH)


def _advance_state(state: dict, job_list: list, rows_this_job: int = 0) -> dict:
    """Increment job index, rolling over to next year when all jobs for this year are done.

    rows_this_job — new rows fetched in this iteration; accumulated into
    current_cycle_rows so we can detect a fully-exhausted cycle (zero new
    rows across all jobs in the cycle).
    """
    import datetime
    state = dict(state)
    state["current_cycle_rows"] = state.get("current_cycle_rows", 0) + rows_this_job
    state["current_job_idx"] = state.get("current_job_idx", 0) + 1
    if state["current_job_idx"] >= len(job_list):
        state["current_job_idx"] = 0
        state["current_year"] += 1
        if state["current_year"] > state["to_year"]:
            state["current_year"] = state["from_year"]
            state["cycles_completed"]      = state.get("cycles_completed", 0) + 1
            state["last_cycle_completed_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            state["last_cycle_new_rows"]   = state["current_cycle_rows"]
            state["current_cycle_rows"]    = 0   # reset for the next cycle
            _print_cycle_complete_banner(state)
    return state


def _print_cycle_complete_banner(state: dict) -> None:
    """Print a visible completion banner to stdout when a full cycle finishes."""
    cycles   = state.get("cycles_completed", 1)
    ts       = state.get("last_cycle_completed_at", "")
    new_rows = state.get("last_cycle_new_rows", 0)
    sep      = "=" * 64
    exhausted = (new_rows == 0)
    print("", flush=True)
    print(sep, flush=True)
    print(f"  SEARCH CYCLE {cycles} COMPLETE", flush=True)
    print(f"  Years     : {state.get('from_year')}-{state.get('to_year')}", flush=True)
    print(f"  Time      : {ts}", flush=True)
    print(f"  New rows  : {new_rows}", flush=True)
    if exhausted:
        print( "  ** ALL CANDIDATES FETCHED — no new rows this cycle.", flush=True)
        print( "  ** The loop will now stop.", flush=True)
    else:
        print( "  More rows may still be available — continuing to next cycle.", flush=True)
    print(sep, flush=True)
    print("", flush=True)
    log.info("Auto-advance: cycle %d complete — %d new rows (years %d-%d).",
             cycles, new_rows, state.get("from_year"), state.get("to_year"))


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
        try:
            existing = pd.read_csv(out_path, encoding="utf-8-sig", low_memory=False)
        except Exception:
            log.warning(
                "candidates.csv has a parse error — retrying with Python engine (bad lines skipped)"
            )
            existing = pd.read_csv(
                out_path, encoding="utf-8-sig", low_memory=False,
                engine="python", on_bad_lines="skip",
            )
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

    # Write to a temp file first, then rename. On Windows (especially in
    # Google Drive folders) another process may briefly hold the target open,
    # so retry a few times before giving up.
    tmp_path = out_path.with_suffix(".tmp.csv")
    combined.to_csv(tmp_path, index=False, encoding="utf-8-sig")
    for attempt in range(10):
        try:
            tmp_path.replace(out_path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            import time
            time.sleep(2 ** attempt * 0.5)  # 0.5s, 1s, 2s, 4s, …
    return combined


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


_ALL_SOURCES = frozenset({"openalex", "semantic_scholar", "engine"})


def run_search(
    from_year: Optional[int] = None,
    to_year: Optional[int] = None,
    max_records_per_phrase: Optional[int] = None,
    sources: "Optional[set[str]]" = None,
) -> pd.DataFrame:
    """Run Stage 1 discovery sources and merge results into ``candidates.csv``.

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
    sources : set[str], optional
        Restrict fetching to these sources only.  Valid values:
        ``openalex``, ``semantic_scholar``, ``engine``.
        ``None`` = all enabled sources.

    Returns
    -------
    pd.DataFrame
        The full deduplicated candidate set after the merge.
    """
    if sources is not None:
        sources = {s.lower().strip() for s in sources}
        # normalise alias
        if "replication_network" in sources:
            sources.add("bob_reed")
    yr_label = f"{from_year or 'any'}–{to_year or 'any'}"
    log.info("Stage 1 starting  (years: %s)", yr_label)

    # Phase 1: harvest all cached pages so prior runs flow into the CSV
    # regardless of which year range was active when they were downloaded.
    # Skipped when --source excludes both openalex and semantic_scholar,
    # since the cache only contains pages from those two sources.
    def _want(*names: str) -> bool:
        return sources is None or bool(sources.intersection(names))

    if _want("openalex", "semantic_scholar", "engine"):
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
    else:
        log.info("Cache harvest skipped (source filter excludes openalex and semantic_scholar)")

    # Phase 2: live fetch from each enabled source.
    frames: list[pd.DataFrame] = []

    if is_engine_enabled():
        if _want("engine"):
            log.info("Stage 1: fetching engine candidates (FLORA_USE_ENGINE=1)...")
            frames.append(fetch_engine_candidates(year_from=from_year, year_to=to_year))
        else:
            log.info("Stage 1: engine source skipped (not in --source list)")
    else:
        if _want("openalex"):
            log.info("Stage 1: fetching OpenAlex candidates...")
            frames.append(
                fetch_openalex_candidates(
                    from_year=from_year,
                    to_year=to_year,
                    max_records_per_phrase=max_records_per_phrase,
                )
            )
        else:
            log.info("Stage 1: OpenAlex source skipped (not in --source list)")

    if _want("semantic_scholar"):
        log.info("Stage 1: fetching Semantic Scholar candidates...")
        frames.append(
            fetch_semantic_scholar_candidates(
                from_year=from_year,
                to_year=to_year,
                max_records_per_phrase=max_records_per_phrase,
            )
        )
    else:
        log.info("Stage 1: Semantic Scholar source skipped (not in --source list)")

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
    sources: "Optional[set[str]]" = None,
) -> pd.DataFrame:
    """Process exactly ONE (source, phrase, year) job per invocation.

    Jobs cycle through all OpenAlex phrases then all Semantic Scholar phrases
    for the current year before advancing to the next year.  At the start of
    each year's first job, the curated lists (I4R, Replication Network) are
    also fetched and merged (unless --source excludes them).

    State is persisted in ``cache/search_state.json`` and resumes across
    invocations.  Old state files that only tracked OpenAlex (``current_phrase_idx``)
    are migrated automatically.

    sources : set[str], optional
        Restrict to these sources only (same values as run_search).
        The JOBS list is filtered so the state index cycles only over the
        requested sources.

    Run command:
        python -m search.run_search --auto-advance --from-year 2011 --to-year 2021 --max-per-phrase 200
    """
    from search.openalex_search import SEARCH_PHRASES as OA_PHRASES, fetch_phrase as oa_fetch
    from search.semantic_scholar_search import SEARCH_PHRASES as S2_PHRASES, fetch_phrase as s2_fetch

    if sources is not None:
        sources = {s.lower().strip() for s in sources}
        if "replication_network" in sources:
            sources.add("bob_reed")

    def _want_src(*names: str) -> bool:
        return sources is None or bool(sources.intersection(names))

    # Build the job list filtered to requested sources.
    ALL_JOBS = (
        [("openalex",          p) for p in OA_PHRASES]
        + [("semantic_scholar", p) for p in S2_PHRASES]
    )
    JOBS = [j for j in ALL_JOBS if _want_src(j[0])] if sources else ALL_JOBS

    if not JOBS:
        log.warning(
            "Auto-advance: no jobs match --source %s — nothing to do. "
            "Use --source openalex or --source semantic_scholar for phrase cycling.",
            sources,
        )
        return True  # always bool — callers check this, not the DataFrame

    state  = _load_search_state(from_year, to_year)
    year   = state["current_year"]
    jidx   = state.get("current_job_idx", 0) % len(JOBS)
    source, phrase = JOBS[jidx]

    log.info(
        "Auto-advance: year=%d  [%s] job[%d/%d] phrase=%r",
        year, source, jidx + 1, len(JOBS), phrase,
    )

    out_path = DATA_DIR / "candidates.csv"

    # Fetch this job's phrase from the appropriate source.
    if source == "openalex":
        rows = oa_fetch(phrase, from_year=year, to_year=year,
                        max_records=max_records_per_phrase)
    else:
        rows = s2_fetch(phrase, from_year=year, to_year=year,
                        max_records=max_records_per_phrase)

    rows_this_job = len(rows) if rows else 0
    if rows:
        batch  = pd.DataFrame(rows, columns=CANDIDATES_COLS)
        _merge_into_candidates_csv(deduplicate_candidates(batch), out_path)
    else:
        log.info("Auto-advance: no new rows for [%s] phrase=%r year=%d",
                 source, phrase, year)

    prev_cycles = state.get("cycles_completed", 0)
    new_state   = _advance_state(state, JOBS, rows_this_job=rows_this_job)
    cycle_done  = new_state.get("cycles_completed", 0) > prev_cycles

    # Only signal "done" (exit code 2) when a cycle completes with zero new
    # rows — meaning every phrase+year cursor has reached the end of the data.
    exhausted = cycle_done and new_state.get("last_cycle_new_rows", -1) == 0

    _save_search_state(new_state)
    next_source, next_phrase = JOBS[new_state["current_job_idx"] % len(JOBS)]
    log.info(
        "Auto-advance: next run -> year=%d  [%s] job[%d] phrase=%r",
        new_state["current_year"],
        next_source,
        new_state["current_job_idx"],
        next_phrase,
    )
    return exhausted


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
    parser.add_argument(
        "--source",
        action="append",
        metavar="SOURCE",
        dest="sources",
        help=(
            "Only fetch from this source (repeatable for multiple). "
            "Values: openalex, semantic_scholar, engine. "
            "Default: all sources."
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
        cycle_done = run_search_auto_advance(
            from_year=from_yr, to_year=to_yr, max_records_per_phrase=max_n,
            sources=set(args.sources) if args.sources else None,
        )
        # Exit code 2 signals a full cycle completed.
        # PowerShell: do { python ... } until ($LASTEXITCODE -eq 2)
        if cycle_done:
            raise SystemExit(2)
    else:
        run_search(
            from_year=args.from_year,
            to_year=args.to_year,
            max_records_per_phrase=args.max_per_phrase,
            sources=set(args.sources) if args.sources else None,
        )
