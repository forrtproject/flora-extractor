"""
CrossrefSourceAdapter — OR-bundled phrase search against Crossref /works.

Strategy mirrors OpenAlex:
  - One ?query.bibliographic="p1" OR "p2" OR ... per source per run
  - Local regex pass for per-keyword/per-field attribution (handled by runner)
  - Cursor pagination (deep paging via &cursor=*)
  - Polite pool via User-Agent: ".../1.0 (mailto:CROSSREF_EMAIL)"

Crossref doesn't ship abstracts on most records — when absent, the
candidate's abstract field is None and runner attribution falls back to title.
"""

import re
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

CROSSREF_HARD_RATE_CAP_PER_SEC = 50

_JATS_RE = re.compile(r"</?jats:[a-z]+[^>]*>", re.IGNORECASE)
_P_RE = re.compile(r"</?p>", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _clean_abstract(s: str | None) -> Optional[str]:
    if not s:
        return None
    s = _JATS_RE.sub("", s)
    s = _P_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or None


class CrossrefSourceAdapter(SourceAdapter):
    id: SourceId = "crossref"

    def __init__(
        self,
        verified_at: datetime,
        rate_per_sec: float,
        mailto: str,
        or_operator: str = " OR ",
        phrase_quote: str = '"',
        max_phrases_per_query: int = 100,
        per_page: int = 100,
        max_pages_per_query: int = 20,
        session: Optional[requests.Session] = None,
    ):
        if not (0 < rate_per_sec <= CROSSREF_HARD_RATE_CAP_PER_SEC):
            raise ValueError(
                f"Crossref rate_per_sec must be in (0, {CROSSREF_HARD_RATE_CAP_PER_SEC}] "
                f"(got {rate_per_sec})"
            )
        if not mailto:
            raise ValueError("Crossref adapter: mailto is required for the polite pool")
        self.verified_at = verified_at
        self._mailto = mailto
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, burst=5)
        self._or = or_operator
        self._q = phrase_quote
        self._max_phrases = max_phrases_per_query
        self._per_page = per_page
        self._max_pages = max_pages_per_query
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

        cursor = args.cursor or "*"
        page = 0

        while True:
            self._bucket.take()
            url = self._build_url(query_expr, args.filters, cursor)
            headers = {
                "Accept": "application/json",
                "User-Agent": f"flora-extractor/1.0 (mailto:{self._mailto})",
            }
            resp = self._session.get(url, headers=headers, timeout=30)

            if resp.status_code == 429:
                self._consecutive_429 += 1
                if self._consecutive_429 >= 3:
                    raise RuntimeError("Crossref 429 threshold exceeded — paused by adapter")
                retry_after = int(resp.headers.get("Retry-After", "5"))
                self._bucket.set_rate(self._bucket.get_rate() / 2)
                time.sleep(retry_after)
                continue

            self._consecutive_429 = 0
            resp.raise_for_status()
            data = resp.json()

            msg = data.get("message") or {}
            items = msg.get("items") or []
            candidates = [c for c in (
                self._work_to_raw(w, args.keywords) for w in items
            ) if c is not None]
            next_cursor = msg.get("next-cursor")

            yield SearchPage(candidates=candidates, next_cursor=next_cursor)

            if not next_cursor:
                return
            cursor = next_cursor
            page += 1
            if page >= self._max_pages:
                return

    def _build_or_expression(self, phrases: list[str]) -> str:
        return self._or.join(
            f"{self._q}{self._escape(p)}{self._q}" for p in phrases
        )

    @staticmethod
    def _escape(phrase: str) -> str:
        return phrase.replace('"', "").replace("\\", "")

    def _build_url(
        self,
        query_expression: str,
        filters: RunFilters,
        cursor: str,
    ) -> str:
        filter_parts = ["type:journal-article", "has-abstract:true"]
        if filters.year_from is not None:
            filter_parts.append(f"from-pub-date:{filters.year_from}")
        if filters.year_to is not None:
            filter_parts.append(f"until-pub-date:{filters.year_to}-12-31")
        params = {
            "query.bibliographic": query_expression,
            "filter": ",".join(filter_parts),
            "rows": str(self._per_page),
            "cursor": cursor,
            "mailto": self._mailto,
        }
        return "https://api.crossref.org/works?" + urllib.parse.urlencode(params, safe=":,*")

    @staticmethod
    def _work_to_raw(
        work: dict,
        keywords: list[ExpandedKeyword],
    ) -> Optional[RawCandidate]:
        doi = (work.get("DOI") or "").lower()
        if not doi:
            return None

        title = ""
        if work.get("title"):
            title = (work["title"][0] or "").strip()
        abstract = _clean_abstract(work.get("abstract"))

        authors: list[CandidateAuthor] = []
        for a in work.get("author") or []:
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            name = (a.get("name") or "").strip() or " ".join(p for p in [given, family] if p) or family
            if not name:
                continue
            orcid = (a.get("ORCID") or "").strip() or None
            authors.append(CandidateAuthor(name=name, orcid=orcid))

        journal_titles = work.get("container-title") or []
        journal = (journal_titles[0] if journal_titles else "").strip() or None

        year: Optional[int] = None
        for key in ("published-print", "published", "published-online", "issued", "created"):
            parts = ((work.get(key) or {}).get("date-parts") or [[]])[0]
            if parts:
                year = parts[0]
                break

        first_kw = keywords[0]
        return RawCandidate(
            source="crossref",
            source_record_id=doi,
            doi=doi,
            title=title or None,
            abstract=abstract,
            year=year,
            authors=authors or None,
            journal=journal,
            url=work.get("URL") or f"https://doi.org/{doi}",
            language=work.get("language"),
            matched_keyword=MatchedKeyword(
                id=first_kw.id,
                field="title" if "title" in first_kw.fields else "abstract",
                permutation=first_kw.permutation,
            ),
        )
