"""
state.py — Shared mutable application state, populated by app.py at startup.

Blueprints import from this module rather than passing data through Flask's
app context, keeping the code straightforward.
"""
import threading

import pandas as pd

# ── DataFrames (read-only after startup; refreshed via /api/input/*) ──────────
flora_df:    pd.DataFrame = pd.DataFrame()   # FLoRA entry sheet
cands_df:    pd.DataFrame = pd.DataFrame()   # openalex_candidates.csv
filtered_df: pd.DataFrame = pd.DataFrame()   # multiple_match_candidates (pipeline 1 input)

all_rep_df:      pd.DataFrame = pd.DataFrame()  # all_replications.csv
multi_orig_df:   pd.DataFrame = pd.DataFrame()  # multi_original_candidates (pipeline 2 input)

# ── Pipeline 1: Multiple Matches ──────────────────────────────────────────────
# Maps clean doi_r → full pipeline result dict.
resolved: dict = {}
resolved_lock = threading.Lock()

# ── Pipeline 2: Multiple Originals ────────────────────────────────────────────
# Maps clean doi_r → multi-original result dict (includes list of originals).
multi_orig_resolved: dict = {}
multi_orig_lock = threading.Lock()

# ── Human validations (persisted to cache/validations.json) ───────────────────
# doi_r -> {status: "successful"|"failed"|"recheck", comment: str, timestamp: str}
validations: dict = {}
