"""
SemanticScholarSourceAdapter — OR-bundled phrase search against
Semantic Scholar's /paper/search endpoint.

Strategy mirrors OpenAlex/Crossref:
  - One ?query="p1" | "p2" | ... per source per run (S2 uses pipe for OR)
  - Offset/limit pagination (S2 caps total at offset 999, limit 100 → 1000)
  - Auth: x-api-key header from SEMANTIC_SCHOLAR_API_KEY
"""

import time
import urllib.parse
from datetime import datetime
from typing import Iterator, Optional

import requests

from ..types import (
    CandidateAuthor,
    ExpandedKeyword,
    MatchedKeyword,
    RateLimitReport,
    RawCandidate,
    RunFilters,
    SearchPage,
    SourceId,
)
from .source_adapter import SearchArgs, SourceAdapter
from .token_bucket import TokenBucket

S2_HARD_RATE_CAP_PER_SEC = 10


class SemanticScholarSourceAdapter(SourceAdapter):
    id: SourceId = "semantic_scholar"

    def __init__(
        self,
        verified_at: datetime,
        rate_per_sec: float,
        api_key: Optional[str] = None,
        or_operator: str = " | ",
        phrase_quote: str = '"',
        max_phrases_per_query: int = 100,
        per_page: int = 100,
        max_total: int = 1000,
        session: Optional[requests.Session] = None,
    ):
        if not (0 < rate_per_sec <= S2_HARD_RATE_CAP_PER_SEC):
            raise ValueError(
                f"S2 rate_per_sec must be in (0, {S2_HARD_RATE_CAP_PER_SEC}] (got {rate_per_sec})"
            )
        self.verified_at = verified_at
        self._api_key = api_key
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec)
        self._or = or_operator
        self._q = phrase_quote
        self._max_phrases = max_phrases_per_query
        self._per_page = per_page
        self._max_total = max_total
        self._session = session or requests.Session()
        self._consecutive_429 = 0
        self._last_limit_report = RateLimitReport()

    def report_limits(self) -> RateLimitReport:
        return self._last_limit_report

    def search(self, args: SearchArgs) -> Iterator[SearchPage]:
        if not args.keywords:
            return
        phrases = [k.permutation for k in args.keywords[: self._max_phrases]]
        query_expr = self._build_or_expression(phrases)

        try:
            offset = int(args.cursor) if args.cursor is not None else 0
        except ValueError:
            offset = 0
        if offset < 0:
            offset = 0

        while offset < self._max_total:
            self._bucket.take()
            url = self._build_url(query_expr, args.filters, offset)
            headers = {"Accept": "application/json"}
            if self._api_key:
                headers["x-api-key"] = self._api_key

            resp = self._session.get(url, headers=headers, timeout=30)

            if resp.status_code == 429:
                self._consecutive_429 += 1
                if self._consecutive_429 >= 3:
                    raise RuntimeError("Semantic Scholar 429 threshold exceeded — paused by adapter")
                retry_after = int(resp.headers.get("Retry-After", "5"))
                self._bucket.set_rate(self._bucket.get_rate() / 2)
                time.sleep(retry_after)
                continue

            self._consecutive_429 = 0
            resp.raise_for_status()
            data = resp.json()

            items = data.get("data") or []
            candidates = [c for c in (
                self._paper_to_raw(p, args.keywords) for p in items
            ) if c is not None]
            nxt = data.get("next")
            next_cursor = str(nxt) if nxt is not None and nxt < self._max_total else None

            yield SearchPage(candidates=candidates, next_cursor=next_cursor)

            if next_cursor is None:
                return
            offset = int(next_cursor)

    def _build_url(
        self,
        query: str,
        filters: RunFilters,
        offset: int,
    ) -> str:
        params = {
            "query": query,
            "limit": str(self._per_page),
            "offset": str(offset),
            "fields": "title,abstract,year,authors,externalIds,venue",
        }
        if filters.year_from is not None or filters.year_to is not None:
            yf = filters.year_from if filters.year_from is not None else ""
            yt = filters.year_to if filters.year_to is not None else ""
            params["year"] = f"{yf}-{yt}"
        return (
            "https://api.semanticscholar.org/graph/v1/paper/search?"
            + urllib.parse.urlencode(params, safe=":,|*")
        )

    @staticmethod
    def _paper_to_raw(
        paper: dict,
        keywords: list[ExpandedKeyword],
    ) -> Optional[RawCandidate]:
        ext = paper.get("externalIds") or {}
        doi = (ext.get("DOI") or "").lower()
        if not doi:
            return None

        authors: list[CandidateAuthor] = []
        for a in paper.get("authors") or []:
            name = (a.get("name") or "").strip()
            if name:
                authors.append(CandidateAuthor(name=name))

        first_kw = keywords[0]
        return RawCandidate(
            source="semantic_scholar",
            source_record_id=paper.get("paperId"),
            doi=doi,
            title=(paper.get("title") or "").strip() or None,
            abstract=(paper.get("abstract") or "").strip() or None,
            year=paper.get("year"),
            authors=authors or None,
            journal=(paper.get("venue") or "").strip() or None,
            url=f"https://doi.org/{doi}",
            matched_keyword=MatchedKeyword(
                id=first_kw.id,
                field="title" if "title" in first_kw.fields else "abstract",
                permutation=first_kw.permutation,
            ),
        )
