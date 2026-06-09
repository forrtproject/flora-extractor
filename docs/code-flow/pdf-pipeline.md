# PDF Acquisition and Parsing — Code Flow

Used by Stage 3 (`extract/link_original.py`) to obtain full-text content for DOI resolution and outcome extraction.

## PDF Acquisition Waterfall

```
pdf_sources.py: fetch_pdf(doi_r)
    │
    ├── Tier 0: OpenAlex XML endpoint (authenticated, requires OPENALEX_API_KEY)
    │       GET content.openalex.org/{openalex_id}/xml
    │       → returns structured GROBID XML (no local PDF needed)
    │
    ├── Tier 1: arXiv
    │       resolve arxiv ID from DOI → download PDF
    │
    ├── Tier 2: OSF (Open Science Framework)
    │       check OSF for preprint PDF
    │
    ├── Tier 3: Unpaywall
    │       GET api.unpaywall.org/v2/{doi}?email={RESEARCHER_EMAIL}
    │       → best_oa_location.url_for_pdf
    │
    ├── Tier 4: CORE
    │       GET core.ac.uk/api-v2/search/works?q=doi:{doi}
    │       → downloadUrl
    │
    └── Tier 5: direct DOI URL
            follow doi.org redirect, check Content-Type: application/pdf

Result cached at: cache/pdf/{key}.pdf
Returns: (path_to_pdf, source_name) or (None, None)
```

## PDF Parsing

```
pdf_parsing.py: parse_all(pdf_path, doi_r, openalex_xml)
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
| `cache/pdf/{key}.pdf` | Downloaded PDF file |
| `cache/parse/parse_{key}.json` | All six parse results (dict by method) |
| `cache/markdown/{key}.md` | MarkItDown raw markdown output |

If a parse cache exists but is missing the `markitdown` key (written before MarkItDown was added), the web app's detail panel runs MarkItDown lazily on first open and updates the cache.

## Web app parse detail panel

The Extract tab's detail panel shows:
- A **★ USED BY LLM** badge on the winning parse method column
- Each method's score
- The winning method name (via `best_parse_method_name()`)
