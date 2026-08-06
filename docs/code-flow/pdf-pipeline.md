# PDF Acquisition and Parsing — Code Flow

Used by Stage 3 (`extract/link_original.py`) to obtain full-text content for DOI resolution and outcome extraction.

## PDF Acquisition Waterfall

```
pdf_sources.py: acquire_pdf(doi_r, title="", openalex_id="")
    │
    ├── (before Tier 1) the PDF is already on disk → replay its recorded tier
    │
    ├── Tier 0: OpenAlex GROBID XML   — structured content, no PDF file needed;
    │                                   needs openalex_id. A result WITH content ends
    │                                   the waterfall — the download tiers never run
    ├── Tier 1: arXiv direct          — before any API call
    ├── Tier 2: OSF preprint
    ├── Tier 3: OpenAlex OA URL
    ├── Tier 4: Unpaywall direct PDFs — one round-trip, reused by Tier 8
    ├── Tier 5: Semantic Scholar
    ├── Tier 6: CORE
    ├── Tier 7: Europe PMC
    ├── Tier 8: scrape the Unpaywall landing pages from Tier 4
    ├── Tier 9: SerpAPI               — quota-limited, last HTTP resort
    └── Tier 10: Playwright headless Chromium

Tiers 1–10 stop at the first successful download. Tier 0 short-circuits the rest:
a content-bearing XML result IS the document (link_original parses it the same way
it parses a downloaded PDF), so acquire_pdf returns straight away with
pdf_source="openalex_xml" and pdf_ok=False — there is no PDF file. A content-free
XML result is discarded and the download tiers run.

Result cached at: cache/pdfs/{key}.pdf  (PDF_CACHE_DIR in shared/config.py)
Returns: {pdf_url, pdf_source, pdf_path, pdf_ok, pdf_url_tried, openalex_xml}
```

The tier list above is the comment structure of `acquire_pdf()` — read the function
when the order has to be exactly right.

**A PDF already on disk skips the waterfall, and reports the tier that really got
it.** After Tier 0 and before Tier 1, `acquire_pdf` checks `cached_pdf(doi_r)` and,
if there is a file, reads `cache/pdfs/pdfsrc_<key>.json` (`_read_provenance()`) for
`{"source": <tier label>, "url": <the URL>}` and returns that. The record is written
by `_try()` on every successful download, cache hits included.

This used to happen by accident: the short-circuit lived inside the `download_pdf()`
cache hit of whichever tier first re-derived a URL for the DOI, so which tier
"supplied" the document depended on tier order and on every URL lookup above it. A
PDF saved before the record existed has none and falls through to the waterfall as
before — where the first cache hit writes the record for next time.

**Two retry records, both 14-day delays, never verdicts.**

| Record | Scope | Written when |
| ------ | ----- | ------------ |
| `cache/pdfs/retry_<key>.json` | One DOI, `{tier: timestamp}` | A whole tier came back empty for this DOI. Skips that tier for `PDF_RETRY_AFTER_DAYS` (14) |
| `cache/pdfs/retry_<key-of-url:URL>.json` | One URL | The server answered **404 or 410** for it (`_PERMANENT_HTTP_STATUS`). Holds back that one URL, so the other URLs the same tier offers are still tried |
| `cache/openalex_xml/retry_<key>.json` | One work, slot `content_free` | Tier 0 returned a content-free shell. `OA_XML_RETRY_AFTER_DAYS`, also 14 |

Three properties hold for all of them:

- A record is a retry DELAY, never a verdict. Nothing is stored as definitive, and an
  unreadable or unparseable log degrades to "probe everything".
- Only an answer counts as evidence of absence. A timeout, a connection error, a 429
  and every 5xx are the server failing to answer, and a 401/403 is a refusal to serve
  a document that does exist — none is recorded. Nor is a tier SKIPPED for a missing
  API key or package: a key added tomorrow must take effect tomorrow.
- The per-DOI file is deleted the moment the row obtains a document (a download, or
  an XML result with content), so a later cache loss re-opens every tier immediately
  rather than holding them for two weeks.

The tier that supplied the document is written to the row's `pdf_source`, and the
parser that won the scoring below to `parse_method` — full-text provenance is a
property of the row, not something a later audit has to reconstruct from cache
timestamps.

**A content-free OpenAlex XML result is no document.** Every result cached before
2026-08 was a 174-byte shell (`{"abstract":"","intro":"","methods":"","references":[]}`),
and since a shell is truthy the ladder's "no document" guard passed it through and
coded the row as `llm_fulltext` with no full text behind it.
`openalex_xml_has_content()` in `shared/pdf_sources.py` is the predicate: a result
with no section text and no references is dropped at the guard, is never cached as a
success, and a cached shell is ignored on read. Each drop logs a warning naming the
work, because a shell means the fetch or the TEI parse is broken upstream.

**The guard is here and only here.** `get_openalex_fulltext` neither returns nor
caches a shell as a success, and `acquire_pdf` never lets one out as a document, so
a shell cannot reach the ladder at all. The second, belt-and-braces demotion that
used to sit in `run_for_doi` is gone — a duplicate guard on a path nothing can
travel is a claim that the first one might fail.

## PDF Parsing

```
pdf_parsing.py: parse_all(doi_r, pdf_path, oa_xml=None, no_llm=False)
    │
    ├── openalex_xml   — parse structured XML from OpenAlex content endpoint
    │                    extracts: abstract, intro, methods, references
    │
    ├── pdfminer       — extract raw text from PDF with pdfminer.six
    │                    extracts: raw_text, (best-effort abstract, intro)
    │
    ├── grobid         — send PDF to GROBID server (GROBID_URL)
    │                    extracts: abstract, intro, methods, structured references
    │                    fallback: skip if GROBID not running
    │
    ├── docpluck       — docpluck library for structured extraction
    │                    extracts: abstract, intro, references
    │
    ├── opendataloader — OpenDataLoader for PDF-to-markdown
    │                    extracts: full markdown with section headings
    │
    └── markitdown     — MarkItDown (Microsoft) for PDF-to-markdown
                         cached at: cache/markdown/{key}.md
                         extracts: full markdown

Returns: dict keyed by method name
         Each value: {abstract, intro, methods, raw_text, refs, error}
```

## Parse Scoring

```
best_parse_result(parse_dict) → winner_result

score = refs × 300 + abstract_len + intro_len × 2 + min(raw_text_len ÷ 5, 1000)

Higher weight for refs: a result with structured references is much more useful
for citation pattern matching than one without.
```

The winner's `abstract + intro` is fed to the LLM. If the winner has no references, the LLM prompt's reference section will be thin (acceptable — citation matching runs as a rule-based step before the LLM fires).

## Cache

| Cache location | Contents |
|----------------|----------|
| `cache/pdfs/{key}.pdf` | Downloaded PDF file |
| `cache/pdfs/pdfsrc_{key}.json` | `{source, url}` — which tier really supplied the saved PDF, replayed by the up-front on-disk check |
| `cache/pdfs/retry_{key}.json` | Per-DOI `{tier: timestamp}` of tiers that came back empty; suppresses re-probing for 14 days, deleted on a later success. The same file shape, keyed on `url:<URL>`, holds one 404/410 URL back |
| `cache/openalex_xml/retry_{key}.json` | Same, one `content_free` slot, for the Tier 0 XML fetch |
| `cache/parse/parse_{key}.json` | All six parse results (dict by method) |
| `cache/markdown/{key}.md` | MarkItDown raw markdown output |

If a parse cache exists but is missing the `markitdown` key (written before MarkItDown was added), the web app's detail panel runs MarkItDown lazily on first open and updates the cache.

## Web app parse detail panel

The Extract tab's detail panel shows:
- A **★ USED BY LLM** badge on the winning parse method column
- Each method's score

The winner is whatever `best_parse_result()` returns; its `source` field names the
parser, and that is what `link_original.py` writes to the row's `parse_method`.
