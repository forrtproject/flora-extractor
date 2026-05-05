"""
SourceAdapter — interface every per-source search adapter implements.

Each adapter knows how to construct an OR-bundled phrase query for ONE
upstream source (OpenAlex, Crossref, Semantic Scholar, …) and stream
candidates back page by page. The runner doesn't care which source it's
talking to — same shape, same generator contract.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional

from ..types import (
    ExpandedKeyword,
    RateLimitReport,
    RunFilters,
    SearchPage,
    SourceId,
)


@dataclass
class SearchArgs:
    keywords: list[ExpandedKeyword]
    filters: RunFilters
    cursor: Optional[str] = None


class SourceAdapter(ABC):
    """Yield SearchPage objects until the source has no more results."""

    id: SourceId
    verified_at: datetime

    @abstractmethod
    def search(self, args: SearchArgs) -> Iterator[SearchPage]:
        ...

    @abstractmethod
    def report_limits(self) -> RateLimitReport:
        ...
