"""
token_counter.py — In-process token usage tracker for LLM calls.

Call set_stage(name) before any LLM call block to attribute tokens to a stage.
Stages used in this pipeline:
  filter             — Stage 2 LLM classification
  extract_classify   — Stage 3 match-type classification
  extract_abstract   — Stage 3 abstract-level LLM linking
  extract_fulltext   — Stage 3 fulltext LLM linking
  extract_prescreen  — Stage 3 cheap pre-screen (optional, see shared/prescreen.py)
  extract_outcome    — Stage 3 outcome extraction

Usage:
    from shared import token_counter

    token_counter.set_stage("filter")
    # ... make LLM calls ...
    token_counter.print_summary()
"""
from __future__ import annotations
import threading
from collections import defaultdict

# The stage is per THREAD: Stage 3 and the filter engine's tiers both run their rows
# in a pool, where each worker sets the stage of the call it is about to make. A
# module-wide value would be whatever the last thread to speak had set, so every
# bucket would hold some other row's tokens. A thread that never set one records
# "unknown" rather than inheriting the main thread's.
_local = threading.local()
_lock = threading.Lock()
# { stage -> { provider -> token_count } }
_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))


def set_stage(name: str) -> None:
    """Set this thread's stage so its subsequent record() calls are attributed to it."""
    _local.stage = name


def record(provider: str, n_tokens: int) -> None:
    """Add n_tokens to the current stage's provider bucket."""
    if n_tokens > 0:
        with _lock:
            _counts[getattr(_local, "stage", "unknown")][provider] += n_tokens


def get_summary() -> dict[str, dict[str, int]]:
    """Return a copy of the full usage breakdown."""
    with _lock:
        return {stage: dict(providers) for stage, providers in _counts.items()}


def print_summary() -> None:
    """Print a formatted token-usage table to stdout."""
    summary = get_summary()
    if not summary:
        print("Token usage: no LLM calls recorded (all results from cache).")
        return

    print("\n" + "=" * 56)
    print("  Token usage by stage")
    print("=" * 56)
    total_all = 0
    for stage, providers in sorted(summary.items()):
        stage_total = sum(providers.values())
        total_all += stage_total
        print(f"  {stage:<22}  {stage_total:>9,} tokens")
        for provider, n in sorted(providers.items()):
            print(f"    {provider:<20}  {n:>9,}")
    print("-" * 56)
    print(f"  {'TOTAL':<22}  {total_all:>9,} tokens")
    print("=" * 56 + "\n")
