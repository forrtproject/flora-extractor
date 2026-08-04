# PDF Acquisition and Parsing — Code Flow

Used by Stage 3 (`extract/link_original.py`) to obtain full-text content for DOI resolution and outcome extraction.

## PDF Acquisition Waterfall

```
pdf_sources.py: acquire_pdf(doi_r, title="", openalex_id="")
    │
    ├── Tier 0: OpenAlex GROBID XML   — structured content, no PDF file needed;
    │                                   needs openalex_id. Does NOT stop the waterfall
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

Tiers 1–10 stop at the first successful download. Tier 0 is independent: it can
succeed and a PDF tier still run, so a row may carry both structured XML and a PDF.

Result cached at: cache/pdfs/{key}.pdf  (PDF_CACHE_DIR in shared/config.py)
Returns: {pdf_url, pdf_source, pdf_path, pdf_ok, pdf_url_tried, openalex_xml}
```

The tier list above is the comment structure of `acquire_pdf()` — read the function
when the order has to be exactly right.

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
| `cache/parse/parse_{key}.json` | All six parse results (dict by method) |
| `cache/markdown/{key}.md` | MarkItDown raw markdown output |

If a parse cache exists but is missing the `markitdown` key (written before MarkItDown was added), the web app's detail panel runs MarkItDown lazily on first open and updates the cache.

## Web app parse detail panel

The Extract tab's detail panel shows:
- A **★ USED BY LLM** badge on the winning parse method column
- Each method's score

The winner is whatever `best_parse_result()` returns; its `source` field names the
parser, and that is what `link_original.py` writes to the row's `parse_method`.
