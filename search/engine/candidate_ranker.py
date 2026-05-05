"""
Candidate ranker — computes the deterministic search_score for a
NormalizedCandidate using the formula in ranking-weights.yaml.

Pure function. Same candidate + same weights file → same score.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from .types import NormalizedCandidate, SourceId


@dataclass
class RankingContribution:
    field: str
    weight: float
    condition: str | None = None


@dataclass
class RankingWeights:
    contributions: list[RankingContribution]
    cap: float


def load_ranking_weights(spec_dir: Path | str) -> RankingWeights:
    spec_dir = Path(spec_dir)
    with (spec_dir / "ranking-weights.yaml").open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    formula = doc.get("formula", {})
    return RankingWeights(
        contributions=[
            RankingContribution(
                field=c["field"],
                weight=float(c["weight"]),
                condition=c.get("condition"),
            )
            for c in formula.get("contributions", [])
        ],
        cap=float(formula.get("cap", 1.0)),
    )


def _w(weights: RankingWeights, key: str) -> float:
    for c in weights.contributions:
        if c.field == key:
            return c.weight
    return 0.0


def compute_search_score(
    candidate: NormalizedCandidate,
    sources_matched: Iterable[SourceId],
    weights: RankingWeights,
) -> float:
    """Compute search_score (capped at weights.cap).

    Contributions from ranking-weights.yaml:
      - title_match (1.0)            — any keyword matched the title
      - abstract_match (0.5)         — only counted if no title match
      - multi_keyword_bonus (0.2)    — two or more distinct keyword IDs hit
      - source_diversity_bonus (0.1) — two or more sources returned this DOI
    """
    score = 0.0
    title_hit = any(m.field == "title" for m in candidate.matched_keywords)
    abstract_hit = any(m.field == "abstract" for m in candidate.matched_keywords)
    distinct_ids = {m.id for m in candidate.matched_keywords}

    if title_hit:
        score += _w(weights, "title_match")
    elif abstract_hit:
        score += _w(weights, "abstract_match")
    if len(distinct_ids) >= 2:
        score += _w(weights, "multi_keyword_bonus")
    if len(set(sources_matched)) >= 2:
        score += _w(weights, "source_diversity_bonus")

    return min(score, weights.cap)
