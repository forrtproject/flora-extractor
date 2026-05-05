"""
Core type definitions for the replication-discovery engine.

Mirrors apps/worker/src/services/replication/discovery/types.ts in the SciMeto
TS engine. Anything that crosses a module boundary is defined here.
"""

from dataclasses import dataclass, field
from typing import Literal, Optional

SourceId = Literal[
    "openalex",
    "crossref",
    "semantic_scholar",
    "bob_reed",
    "i4r",
    "fred_data",
]
SearchField = Literal["title", "abstract", "default"]
ClassifierStatus = Literal[
    "pending",
    "accepted",
    "ambiguous",
    "needs_more_metadata",
    "rejected",
    "errored",
]


@dataclass
class KeywordSpec:
    """One entry from search-keywords.yaml.

    Either ``permutations`` is set (explicit phrase variants) or ``template`` +
    ``qualifiers`` is set (template substitution). Mutually exclusive.
    """
    id: str
    weight: float
    fields: list[SearchField]
    phrase: Optional[str] = None
    template: Optional[str] = None
    qualifiers: Optional[list[str]] = None
    permutations: Optional[list[str]] = None
    notes: Optional[str] = None


@dataclass
class ExpandedKeyword:
    """A single (keyword id, phrase variant) pair after expansion."""
    id: str
    permutation: str
    weight: float
    fields: list[SearchField]


@dataclass
class ExclusionPattern:
    """One entry from exclusion-patterns.yaml."""
    id: str
    regex: str
    flags: list[str] = field(default_factory=list)
    description: Optional[str] = None


@dataclass
class RunFilters:
    languages: list[str]
    sources: list[SourceId]
    max_candidates_per_source: int
    skip_dois_in_flora: bool = True
    year_from: Optional[int] = None
    year_to: Optional[int] = None


@dataclass
class CandidateAuthor:
    name: str
    orcid: Optional[str] = None


@dataclass
class MatchedKeyword:
    id: str
    field: SearchField
    permutation: str


@dataclass
class RawCandidate:
    """One unnormalized candidate as returned by a source adapter."""
    source: SourceId
    doi: str
    matched_keyword: MatchedKeyword
    source_record_id: Optional[str] = None
    title: Optional[str] = None
    abstract: Optional[str] = None
    year: Optional[int] = None
    authors: Optional[list[CandidateAuthor]] = None
    journal: Optional[str] = None
    url: Optional[str] = None
    language: Optional[str] = None


@dataclass
class NormalizedCandidate:
    """A canonicalized candidate; ``matched_keywords`` accumulates across hits."""
    source: SourceId
    doi: str
    matched_keywords: list[MatchedKeyword]
    source_record_id: Optional[str] = None
    title: Optional[str] = None
    abstract: Optional[str] = None
    year: Optional[int] = None
    authors: Optional[list[CandidateAuthor]] = None
    journal: Optional[str] = None
    url: Optional[str] = None
    language: Optional[str] = None
    search_score: float = 0.0


@dataclass
class SearchPage:
    """One page of results streamed from a source adapter."""
    candidates: list[RawCandidate]
    next_cursor: Optional[str] = None


@dataclass
class RateLimitReport:
    requests_remaining: Optional[int] = None
    reset_at: Optional[float] = None  # epoch seconds
