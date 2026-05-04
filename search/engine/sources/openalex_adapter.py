"""
OpenAlexSourceAdapter — OR-bundled phrase search against /works.

Strategy (per source-configs.yaml openalex.query.strategy = "or_bundle"):
  - Build ONE big ?search=("p1" OR "p2" OR ...) query containing every phrase.
  - Per-keyword/per-field attribution is computed POST-fetch by the runner
    via the spec phrase regexes — no separate title/abstract calls.
  - Cursor-paginate up to max_pages_per_query.
  - 429 → halve bucket rate, sleep Retry-After, retry same cursor.

Auth: API key required since Feb 13, 2026 (OPENALEX_API_KEY env var). See
search/RATE_LIMITS_VERIFIED.md for the deprecation notice and current cap.
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

OPENALEX_HARD_RATE_CAP_PER_SEC = 100


def _abstract_index_to_text(idx: dict[str, list[int]] | None) -> Optional[str]:
    """Reconstruct an abstract from OpenAlex's inverted-index format."""
    if not idx:
        return None
    positions: list[tuple[int, str]] = []
    for word, pos_list in idx.items():
        for p in pos_list:
            positions.append((p, word))
    positions.sort(key=lambda x: x[0])
    return " ".join(w for _, w in positions) if positions else None


class OpenAlexSourceAdapter(SourceAdapter):
    id: SourceId = "openalex"

    def __init__(
        self,
        verified_at: datetime,
        rate_per_sec: float,
        api_key: Optional[str] = None,
        mailto: Optional[str] = None,
        or_operator: str = " OR ",
        phrase_quote: str = '"',
        max_phrases_per_query: int = 100,
        per_page: int = 50,
        max_pages_per_query: int = 20,
        session: Optional[requests.Session] = None,
    ):
        if not (0 < rate_per_sec <= OPENALEX_HARD_RATE_CAP_PER_SEC):
            raise ValueError(
                f"OpenAlex rate_per_sec must be in (0, {OPENALEX_HARD_RATE_CAP_PER_SEC}] "
                f"(got {rate_per_sec})"
            )
        self.verified_at = verified_at
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, burst=5)
        self._api_key = api_key
        self._mailto = mailto
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
        search_expr = self._build_or_expression(phrases)

        cursor = args.cursor or "*"
        page = 0

        while True:
            self._bucket.take()
            url = self._build_url(search_expr, args.filters, cursor)
            headers = {"Accept": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            resp = self._session.get(url, headers=headers, timeout=30)

            if resp.status_code == 429:
                self._consecutive_429 += 1
                if self._consecutive_429 >= 3:
                    raise RuntimeError("OpenAlex 429 threshold exceeded — paused by adapter")
                retry_after = int(resp.headers.get("Retry-After", "5"))
                self._bucket.set_rate(self._bucket.get_rate() / 2)
                time.sleep(retry_after)
                continue

            self._consecutive_429 = 0
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results") or []
            candidates = [c for c in (
                self._work_to_raw(w, args.keywords) for w in results
            ) if c is not None]
            next_cursor = (data.get("meta") or {}).get("next_cursor")

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
        # OpenAlex phrase syntax doesn't support nested quotes — strip defensively.
        return phrase.replace('"', "").replace("\\", "")

    def _build_url(
        self,
        search_expression: str,
        filters: RunFilters,
        cursor: str,
    ) -> str:
        filter_parts = ["type:article", "has_abstract:true"]
        if filters.year_from is not None or filters.year_to is not None:
            yf = filters.year_from if filters.year_from is not None else ""
            yt = filters.year_to if filters.year_to is not None else ""
            filter_parts.append(f"publication_year:{yf}-{yt}")
        if filters.languages:
            filter_parts.append(f"language:{'|'.join(filters.languages)}")

        params = {
            "search": search_expression,
            "filter": ",".join(filter_parts),
            "select": (
                "id,doi,title,abstract_inverted_index,publication_year,"
                "authorships,primary_location,language"
            ),
            "per-page": str(self._per_page),
            "cursor": cursor,
        }
        if not self._api_key and self._mailto:
            params["mailto"] = self._mailto

        return "https://api.openalex.org/works?" + urllib.parse.urlencode(params, safe=":,*")

    @staticmethod
    def _work_to_raw(
        work: dict,
        keywords: list[ExpandedKeyword],
    ) -> Optional[RawCandidate]:
        raw_doi = (work.get("doi") or "")
        if raw_doi.startswith("https://doi.org/"):
            raw_doi = raw_doi[len("https://doi.org/"):]
        elif raw_doi.startswith("http://doi.org/"):
            raw_doi = raw_doi[len("http://doi.org/"):]
        if not raw_doi:
            return None

        abstract = _abstract_index_to_text(work.get("abstract_inverted_index"))
        authors: list[CandidateAuthor] = []
        for a in work.get("authorships") or []:
            author_obj = a.get("author") or {}
            name = (author_obj.get("display_name") or "").strip()
            if not name:
                continue
            orcid = (author_obj.get("orcid") or "").strip() or None
            authors.append(CandidateAuthor(name=name, orcid=orcid))

        loc = work.get("primary_location") or {}
        src = loc.get("source") or {}
        oa_id = (work.get("id") or "").replace("https://openalex.org/", "")

        first_kw = keywords[0]
        return RawCandidate(
            source="openalex",
            source_record_id=oa_id or None,
            doi=raw_doi,
            title=(work.get("title") or "").strip() or None,
            abstract=abstract,
            year=work.get("publication_year"),
            authors=authors or None,
            journal=(src.get("display_name") or "").strip() or None,
            url=f"https://doi.org/{raw_doi}" if raw_doi else None,
            language=work.get("language"),
            matched_keyword=MatchedKeyword(
                id=first_kw.id,
                field="title" if "title" in first_kw.fields else "abstract",
                permutation=first_kw.permutation,
            ),
        )
