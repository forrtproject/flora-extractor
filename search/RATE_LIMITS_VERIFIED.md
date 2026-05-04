# Rate-Limits Verified — Replication Discovery

**Verified at:** 2026-05-04 (Munster workshop, Day 1 morning)

This file records the live rate-limit and pricing policies of each source we integrate with, **verified against the providers' current documentation on the date above.** Re-verify any time the value in `discovery/spec/source-configs.yaml`'s `verified_at` field is older than 60 days. The engine will refuse to start a run if it is.

---

## OpenAlex

**Source:** [https://developers.openalex.org/api-reference/authentication](https://developers.openalex.org/api-reference/authentication), [https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication) (redirects to the developers.openalex.org URL).

**Material change since SciMeto's classifier was written:** as of **Feb 13, 2026**, OpenAlex deprecated the polite pool and now requires an API key for any non-trivial use. The `mailto` parameter is no longer accepted as authentication. Sign up at openalex.org to get a free key.

OpenAlex moved to a **credit-based daily allowance** model rather than a pure rate cap. Free tier daily allowance:

| Endpoint type | Free per day | Cost beyond free |
|---|---|---|
| Singleton (DOI / ID lookup) | unlimited | free |
| List + Filter | 10,000 calls | $0.10 / 1,000 |
| **Search** (`default.search`, `title.search`, `abstract.search`) | **1,000 calls** | $1.00 / 1,000 |
| Semantic Search | 1,000 calls | $1.00 / 1,000 |
| Content download (PDF) | 100 PDFs | $10.00 / 1,000 |

**Hard rate cap:** more than 100 requests/second triggers 429.

**Implication for Discovery (which uses Search):** the free tier gives us **1,000 search calls per day**. A typical wide run with 19 spec keywords × ~3 permutations average × 2 fields (title + abstract) × 20 pages = ~2,280 calls, **exhausting free tier in a single run.** For workshop testing keep keyword set small or reduce `max_pages_per_query`. For production, a paid plan or a much narrower run is required.

**Engine settings (use 50% safety factor):**
- `requests_per_second: 5` (well under 100/sec hard cap)
- `requests_per_day: 1000` (free-tier search cap; engine should track and pause when approaching)
- Auth: `OPENALEX_API_KEY` (required); `OPENALEX_MAILTO` is fallback only and no longer guarantees polite-pool treatment

**Pagination:**
- `per_page` max 100; we use 50 for safer pages
- Cursor pagination required beyond 10,000 cumulative results (`cursor=*` initial)
- Max OR filter values: 100

---

## Crossref

**Source:** [https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/](https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/) (effective Dec 1, 2025).

| Pool | Single record | List of records | Concurrency |
|---|---|---|---|
| Public (no `mailto`) | 5 req/sec | 1 req/sec | 1 concurrent |
| **Polite (with `mailto`)** | **10 req/sec** | **3 req/sec** | **up to 3 concurrent** |

For Discovery we always use the list endpoint (search/filter) with `mailto`, so the relevant figure is **3 requests/sec polite**.

**Engine settings (50% safety factor):**
- `requests_per_second: 1.5` (rounded to 2 below 3 ceiling)
- Auth: `CROSSREF_MAILTO` env var, sent as `mailto` query parameter and in `User-Agent` header
- Live rate limits exposed in `X-Rate-Limit-Limit` and `X-Rate-Limit-Interval` response headers — adapter should respect these dynamically

---

## Semantic Scholar

**Source:** [https://www.semanticscholar.org/product/api](https://www.semanticscholar.org/product/api) (academic graph API).

| Tier | Rate limit |
|---|---|
| Unauthenticated (shared) | 5,000 req / 5 min total across ALL unauth users (effective ~16/s but throttled to ~1/s under load) |
| **Free API key** | **1 req/sec dedicated** on most endpoints |

**Hard limits:**
- Search: max 1,000 results per query (`offset` ≤ 999, `limit` ≤ 100)
- Exponential backoff required by S2's terms; 429 means slow down

**Engine settings (50% safety factor):**
- `requests_per_second: 0.5` (1 RPS keyed, halved for safety)
- Auth: `SEMANTIC_SCHOLAR_API_KEY` env var, sent as `x-api-key` header
- `per_page: 100`, max total 1,000 enforced by adapter

---

## Required env vars

```bash
# OpenAlex — REQUIRED (no more polite pool)
OPENALEX_API_KEY=...
OPENALEX_MAILTO=admin@example.com  # fallback only

# Crossref — STRONGLY RECOMMENDED (polite pool 3x faster)
CROSSREF_MAILTO=admin@example.com

# Semantic Scholar — RECOMMENDED (1 RPS dedicated vs shared)
SEMANTIC_SCHOLAR_API_KEY=...
```

These are inputs to Task 3's `source-configs.yaml`. Use the verified-at date `2026-05-04` and the requests-per-second numbers above.

---

## Sources

- [OpenAlex rate limits & authentication](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication)
- [OpenAlex pricing & API key requirements (Feb 13 2026 announcement)](https://groups.google.com/g/openalex-users/c/rI1GIAySpVQ)
- [Crossref REST API rate limit changes (Dec 1 2025)](https://www.crossref.org/blog/announcing-changes-to-rest-api-rate-limits/)
- [Semantic Scholar API product page](https://www.semanticscholar.org/product/api)
