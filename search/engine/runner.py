"""
runDiscovery — orchestrates one Discover run end-to-end (file-only mode).

With OR-bundling, one task = one source. The runner streams pages from each
adapter, normalizes + excludes + dedup-merges within a page, and emits
NormalizedCandidate rows via a callback. There is no DB, no checkpoint store,
no pause signal — flora-extractor's Stage 1 runs are short enough that we
treat the whole thing as a single CSV-producing run.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml

from .candidate_normalizer import merge_candidates, normalize_candidate
from .candidate_ranker import RankingWeights, compute_search_score, load_ranking_weights
from .exclusion_filter import (
    ExclusionPattern,
    apply_compiled_exclusions,
    compile_exclusions,
    load_exclusion_patterns,
)
from .keyword_expander import expand_all, load_spec_keywords
from .types import (
    ExpandedKeyword,
    KeywordSpec,
    NormalizedCandidate,
    RunFilters,
    SourceId,
)
from .sources.crossref_adapter import CrossrefSourceAdapter
from .sources.openalex_adapter import OpenAlexSourceAdapter
from .sources.semantic_scholar_adapter import SemanticScholarSourceAdapter
from .sources.source_adapter import SearchArgs, SourceAdapter

SPEC_FRESHNESS_DAYS = 60


@dataclass
class RunStats:
    total_tasks: int = 0
    completed_tasks: int = 0
    candidates_seen: int = 0
    candidates_kept: int = 0
    candidates_excluded: int = 0
    api_calls_per_source: dict[SourceId, int] = field(default_factory=dict)
    errors_per_source: dict[SourceId, int] = field(default_factory=dict)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class RunConfig:
    keywords: list[str]               # raw user keywords (with wildcards)
    filters: RunFilters
    spec_dir: Path
    classify: bool = False             # off by default; flora's Stage 2 does this


@dataclass
class RunResult:
    status: str                        # completed | failed
    stats: RunStats
    error: Optional[str] = None


# --- adapter factory --------------------------------------------------------


def _parse_verified_at(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def load_source_configs(spec_dir: Path) -> dict:
    with (spec_dir / "source-configs.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_default_adapters(
    spec_dir: Path,
    sources: Iterable[SourceId] | None = None,
) -> dict[SourceId, SourceAdapter]:
    """Construct adapters for the sources requested in the spec.

    Reads OPENALEX_API_KEY / OPENALEX_MAILTO / CROSSREF_EMAIL /
    SEMANTIC_SCHOLAR_API_KEY from the environment.
    """
    cfg = load_source_configs(spec_dir)
    requested = set(sources) if sources else {"openalex", "crossref", "semantic_scholar"}
    adapters: dict[SourceId, SourceAdapter] = {}

    if "openalex" in requested and "openalex" in cfg:
        oa = cfg["openalex"]
        oa_api = os.getenv(oa.get("auth", {}).get("api_key_env", ""), "") or None
        oa_mailto = os.getenv(oa.get("auth", {}).get("mailto_env", ""), "") or None
        adapters["openalex"] = OpenAlexSourceAdapter(
            verified_at=_parse_verified_at(oa["rate_limit"]["verified_at"]),
            rate_per_sec=float(oa["rate_limit"]["requests_per_second"]),
            api_key=oa_api,
            mailto=oa_mailto,
            or_operator=oa["query"].get("or_operator", " OR "),
            phrase_quote=oa["query"].get("phrase_quote", '"'),
            max_phrases_per_query=int(oa["query"].get("max_phrases_per_query", 100)),
            per_page=int(oa["pagination"].get("per_page", 50)),
            max_pages_per_query=int(oa["pagination"].get("max_pages_per_query", 20)),
        )

    if "crossref" in requested and "crossref" in cfg:
        cr = cfg["crossref"]
        cr_email = os.getenv(cr.get("auth", {}).get("polite_pool_mailto_env", ""), "") or os.getenv("RESEARCHER_EMAIL", "")
        if not cr_email:
            # Polite pool requires a contact email; skip Crossref rather than be impolite.
            pass
        else:
            adapters["crossref"] = CrossrefSourceAdapter(
                verified_at=_parse_verified_at(cr["rate_limit"]["verified_at"]),
                rate_per_sec=float(cr["rate_limit"]["requests_per_second"]),
                mailto=cr_email,
                or_operator=cr["query"].get("or_operator", " OR "),
                phrase_quote=cr["query"].get("phrase_quote", '"'),
                max_phrases_per_query=int(cr["query"].get("max_phrases_per_query", 100)),
                per_page=int(cr["pagination"].get("per_page", 100)),
                max_pages_per_query=int(cr["pagination"].get("max_pages_per_query", 20)),
            )

    if "semantic_scholar" in requested and "semantic_scholar" in cfg:
        s2 = cfg["semantic_scholar"]
        s2_api = os.getenv(s2.get("auth", {}).get("api_key_env", ""), "") or os.getenv("S2_API_KEY", "") or None
        adapters["semantic_scholar"] = SemanticScholarSourceAdapter(
            verified_at=_parse_verified_at(s2["rate_limit"]["verified_at"]),
            rate_per_sec=float(s2["rate_limit"]["requests_per_second"]),
            api_key=s2_api,
            or_operator=s2["query"].get("or_operator", " | "),
            phrase_quote=s2["query"].get("phrase_quote", '"'),
            max_phrases_per_query=int(s2["query"].get("max_phrases_per_query", 100)),
            per_page=int(s2["pagination"].get("per_page", 100)),
            max_total=int(s2["pagination"].get("max_total", 1000)),
        )
    return adapters


def check_spec_freshness(adapters: dict[SourceId, SourceAdapter]) -> None:
    now = datetime.now(timezone.utc)
    for adapter in adapters.values():
        age_days = (now - adapter.verified_at).total_seconds() / 86_400
        if age_days > SPEC_FRESHNESS_DAYS:
            raise RuntimeError(
                f"Source {adapter.id} verified {int(age_days)} days ago — re-verify before running"
            )


# --- runner -----------------------------------------------------------------


CandidateCallback = Callable[[NormalizedCandidate], None]


def run_discovery(
    config: RunConfig,
    adapters: dict[SourceId, SourceAdapter],
    on_candidate: CandidateCallback,
    spec_keywords: Optional[list[KeywordSpec]] = None,
    exclusions: Optional[list[ExclusionPattern]] = None,
    weights: Optional[RankingWeights] = None,
    log=None,
) -> RunResult:
    """Execute one Discover run; emits each NormalizedCandidate via callback.

    Adapters not in ``config.filters.sources`` are ignored.
    """
    spec_dir = Path(config.spec_dir)
    keywords_spec = spec_keywords or load_spec_keywords(spec_dir)
    exclusion_patterns = exclusions or load_exclusion_patterns(spec_dir)
    compiled_exclusions = compile_exclusions(exclusion_patterns)
    ranking = weights or load_ranking_weights(spec_dir)

    try:
        check_spec_freshness(adapters)
    except RuntimeError as e:
        return RunResult(status="failed", stats=RunStats(), error=str(e))

    expanded: list[ExpandedKeyword] = expand_all(keywords_spec, config.keywords)
    requested = [s for s in config.filters.sources if s in adapters]
    if not requested:
        return RunResult(status="failed", stats=RunStats(), error="no_sources_configured")

    stats = RunStats(
        total_tasks=len(requested),
        api_calls_per_source={s: 0 for s in requested},
        errors_per_source={s: 0 for s in requested},
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    sources_matched: set[SourceId] = set(requested)

    for source in requested:
        adapter = adapters[source]
        try:
            count_for_source = 0
            for page in adapter.search(SearchArgs(keywords=expanded, filters=config.filters)):
                stats.api_calls_per_source[source] += 1
                stats.candidates_seen += len(page.candidates)

                # Normalize, exclude, dedup-merge within the page
                by_doi: dict[str, NormalizedCandidate] = {}
                for raw in page.candidates:
                    normalized = normalize_candidate(raw)
                    text = f"{normalized.title or ''} {normalized.abstract or ''}".strip()
                    if text and apply_compiled_exclusions(text, compiled_exclusions).excluded:
                        stats.candidates_excluded += 1
                        continue
                    if normalized.doi in by_doi:
                        by_doi[normalized.doi] = merge_candidates(by_doi[normalized.doi], normalized)
                    else:
                        by_doi[normalized.doi] = normalized

                # Score and emit
                for cand in by_doi.values():
                    cand.search_score = compute_search_score(cand, sources_matched, ranking)
                    stats.candidates_kept += 1
                    count_for_source += 1
                    on_candidate(cand)
                    if (
                        config.filters.max_candidates_per_source > 0
                        and count_for_source >= config.filters.max_candidates_per_source
                    ):
                        break

                if (
                    config.filters.max_candidates_per_source > 0
                    and count_for_source >= config.filters.max_candidates_per_source
                ):
                    break

            stats.completed_tasks += 1
        except Exception as e:  # noqa: BLE001 — we surface per-source error and continue
            stats.errors_per_source[source] += 1
            if log:
                log.warning("source %s failed: %s", source, e)
            if "threshold exceeded" in str(e):
                stats.completed_at = datetime.now(timezone.utc).isoformat()
                return RunResult(status="failed", stats=stats, error=str(e))

    stats.completed_at = datetime.now(timezone.utc).isoformat()
    return RunResult(status="completed", stats=stats)
