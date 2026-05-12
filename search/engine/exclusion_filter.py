"""
Exclusion filter — applies post-fetch regex patterns from exclusion-patterns.yaml
to drop non-scholarly replication contexts (DNA replication, code/data replication,
replication fork/origin/stress/timing, etc.).

Patterns are applied AFTER the API search returns, not as part of the OR-bundle —
no public search API supports negative regex search reliably.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .types import ExclusionPattern


@dataclass
class ExclusionResult:
    excluded: bool
    reason: Optional[str] = None


def _compile(pattern: ExclusionPattern) -> re.Pattern:
    flag_bits = 0
    for f in pattern.flags or []:
        if f.lower() == "i":
            flag_bits |= re.IGNORECASE
        elif f.lower() == "m":
            flag_bits |= re.MULTILINE
        elif f.lower() == "s":
            flag_bits |= re.DOTALL
    return re.compile(pattern.regex, flag_bits)


def apply_exclusions(
    text: str,
    patterns: list[ExclusionPattern],
) -> ExclusionResult:
    """Test text against the configured exclusion patterns.

    Returns the FIRST matching pattern's id as ``reason``. The patterns list is
    typically small enough that recompiling per call is fine; if profiling shows
    this in a hot path, swap in ``compile_exclusions`` below.
    """
    if not text:
        return ExclusionResult(excluded=False)
    for p in patterns:
        if _compile(p).search(text):
            return ExclusionResult(excluded=True, reason=p.id)
    return ExclusionResult(excluded=False)


def compile_exclusions(patterns: list[ExclusionPattern]) -> list[tuple[str, re.Pattern]]:
    """Pre-compile exclusion patterns once for hot loops."""
    return [(p.id, _compile(p)) for p in patterns]


def apply_compiled_exclusions(
    text: str,
    compiled: list[tuple[str, re.Pattern]],
) -> ExclusionResult:
    """Same as ``apply_exclusions`` but using pre-compiled patterns."""
    if not text:
        return ExclusionResult(excluded=False)
    for pid, regex in compiled:
        if regex.search(text):
            return ExclusionResult(excluded=True, reason=pid)
    return ExclusionResult(excluded=False)


def load_exclusion_patterns(spec_dir: Path | str) -> list[ExclusionPattern]:
    """Parse exclusion-patterns.yaml from a spec directory."""
    spec_dir = Path(spec_dir)
    with (spec_dir / "exclusion-patterns.yaml").open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return [
        ExclusionPattern(
            id=p["id"],
            regex=p["regex"],
            flags=list(p.get("flags", [])),
            description=p.get("description"),
        )
        for p in doc.get("patterns", [])
    ]
