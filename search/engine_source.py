"""
Adapter that exposes the YAML-spec discovery engine as a Stage-1 source.

Wraps ``search.engine.runner.run_discovery`` and returns a DataFrame in the
canonical ``CANDIDATES_COLS`` schema, so it can be concatenated alongside
``fetch_openalex_candidates()`` etc. in ``run_search.py``.

Opt-in via the ``FLORA_USE_ENGINE`` env var (any truthy value). When opt-in
is off and there is no ``OPENALEX_API_KEY``, the engine is skipped silently
to avoid surprise calls.
"""

import os
from pathlib import Path

import pandas as pd

from shared.config import log
from shared.schema import CANDIDATES_COLS
from shared.utils import clean_doi

from .engine.runner import RunConfig, build_default_adapters, run_discovery
from .engine.types import NormalizedCandidate, RunFilters, SourceId

DEFAULT_SOURCES: list[SourceId] = ["openalex", "crossref", "semantic_scholar"]


def _flatten(c: NormalizedCandidate) -> dict:
    authors = "; ".join(a.name for a in (c.authors or []) if a.name) or None
    return {
        "doi_r": clean_doi(c.doi or ""),
        "title_r": c.title,
        "abstract_r": c.abstract,
        "year_r": c.year,
        "authors_r": authors,
        "journal_r": c.journal,
        "url_r": c.url,
        "openalex_id_r": c.source_record_id if c.source == "openalex" else None,
        "source": c.source,
    }


def fetch_engine_candidates(
    sources: list[SourceId] | None = None,
    user_keywords: list[str] | None = None,
    max_per_source: int = 0,
    year_from: int | None = None,
    year_to: int | None = None,
    spec_dir: Path | None = None,
) -> pd.DataFrame:
    """Run the YAML-spec engine and return a CANDIDATES_COLS DataFrame.

    ``max_per_source = 0`` means no cap. ``user_keywords`` are extra wildcards
    on top of the spec — leave empty for spec-only runs.
    """
    spec_dir = spec_dir or Path(__file__).parent / "spec"
    requested = sources or DEFAULT_SOURCES

    if not os.getenv("OPENALEX_API_KEY") and "openalex" in requested:
        log.warning(
            "OPENALEX_API_KEY not set — OpenAlex requires it since Feb 13, 2026. "
            "Skipping OpenAlex in the engine source."
        )
        requested = [s for s in requested if s != "openalex"]

    adapters = build_default_adapters(spec_dir, sources=requested)
    if not adapters:
        log.warning("Engine source: no adapters available; returning empty DataFrame")
        return pd.DataFrame(columns=CANDIDATES_COLS)

    log.info("Engine sources: %s", ", ".join(sorted(adapters.keys())))

    rows: list[dict] = []
    seen_dois: set[str] = set()

    def on_candidate(c: NormalizedCandidate) -> None:
        if not c.doi or c.doi in seen_dois:
            return
        seen_dois.add(c.doi)
        rows.append(_flatten(c))

    config = RunConfig(
        keywords=user_keywords or [],
        filters=RunFilters(
            languages=["en"],
            sources=list(adapters.keys()),
            max_candidates_per_source=max_per_source,
            skip_dois_in_flora=False,
            year_from=year_from,
            year_to=year_to,
        ),
        spec_dir=spec_dir,
    )

    result = run_discovery(config, adapters, on_candidate, log=log)
    log.info(
        "Engine source: status=%s seen=%d kept=%d excluded=%d unique=%d",
        result.status,
        result.stats.candidates_seen,
        result.stats.candidates_kept,
        result.stats.candidates_excluded,
        len(rows),
    )
    if result.error:
        log.warning("Engine source error: %s", result.error)

    return pd.DataFrame(rows, columns=CANDIDATES_COLS)


def is_engine_enabled() -> bool:
    """Env-var gate: ``FLORA_USE_ENGINE=1`` (or true/yes/on) to opt in."""
    val = os.getenv("FLORA_USE_ENGINE", "").strip().lower()
    return val in {"1", "true", "yes", "on"}
