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

    # Subclasses set these in __init__ (OR-join token and phrase-quote character).
    _or: str
    _q: str

    @abstractmethod
    def search(self, args: SearchArgs) -> Iterator[SearchPage]:
        ...

    @abstractmethod
    def report_limits(self) -> RateLimitReport:
        ...

    def _build_or_expression(self, phrases: list[str]) -> str:
        return self._or.join(
            f"{self._q}{self._escape(p)}{self._q}" for p in phrases
        )

    @staticmethod
    def _escape(phrase: str) -> str:
        # None of the source query syntaxes support nested quotes/escapes — strip defensively.
        return phrase.replace('"', "").replace("\\", "")
