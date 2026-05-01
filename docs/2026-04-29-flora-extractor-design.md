# FLoRA Extractor — Design Document
**Date:** 2026-04-29  
**Status:** Approved  
**Repo:** flora-extractor (new standalone repo)

---

## 1. Purpose

Build a standalone Python tool that continuously discovers, extracts, and validates replication and reproduction studies for the FLoRA / FReD database. The tool takes academic paper DOIs as input and produces structured records identifying:
- The original study being replicated/reproduced
- The replication outcome (success / mixed / failure)
- Supporting evidence quotes

---

## 2. Architecture

Four sequential pipeline stages, each reading a CSV and writing a richer CSV. Only the final validation web app uses a database.

```
[APIs + External Lists]
        │
        ▼
Stage 1: SEARCH       → data/candidates.csv
        │
        ▼
Stage 2: FILTER       → data/filtered.csv
        │
        ▼
Stage 3: EXTRACT      → data/extracted.csv
        │
        ▼
Stage 4: VALIDATE     → SQLite DB → data/validated.csv
   (Flask web app)
```

Each stage is independently runnable. Teams use sample CSVs from `misc/` to work in parallel without blocking each other.

---

## 3. Repository Structure

```
flora-extractor/
├── search/
│   ├── openalex_search.py
│   ├── external_lists.py
│   ├── deduplicate.py
│   └── run_search.py               # Orchestrator → data/candidates.csv
├── filter/
│   ├── rule_filter.py
│   ├── llm_filter.py
│   └── run_filter.py               # Orchestrator → data/filtered.csv
├── extract/
│   ├── run_extract.py              # Orchestrator (routes single vs multi-original)
│   ├── link_original.py            # Port of OpenAlexLLM pipeline.py
│   ├── multi_original.py           # Port of OpenAlexLLM multi_original.py (NEEDS IMPROVEMENT)
│   └── code_outcome.py             # New: keyword + LLM outcome extraction
├── validate/
│   ├── app.py
│   ├── import_csv.py
│   ├── models.py
│   ├── state.py
│   └── routes/
│       ├── review.py
│       ├── batch.py
│       ├── multi_originals.py
│       ├── input.py
│       ├── export.py
│       └── dashboard.py
│   └── templates/
├── shared/                         # Ported from OpenAlexLLM, do not rewrite
│   ├── openalex_client.py          # Port of lib/openalex.py
│   ├── llm_client.py               # Port of lib/llm.py
│   ├── pdf_sources.py              # Port of lib/pdf_sources.py
│   ├── grobid.py                   # Port of lib/grobid.py
│   ├── disambiguation.py           # Port of lib/disambiguation.py
│   ├── cache.py                    # Cache management utilities
│   ├── utils.py                    # clean_doi, cache_key
│   ├── config.py                   # Paths + env vars
│   └── schema.py                   # CSV column definitions (the contract)
├── data/                           # gitignored except samples
├── misc/                           # Examples and reference code
├── tests/
├── cache/                          # gitignored
├── .env.example
├── requirements.txt
├── CLAUDE.md
├── RULEBOOK.md
└── README.md
```

---

## 4. CSV Schema Contracts

### Stage 1 Output — `candidates.csv`
| Column | Type | Description |
|--------|------|-------------|
| doi_r | str | Replication paper DOI (cleaned, no https://doi.org/) |
| title_r | str | Paper title |
| abstract_r | str | Abstract text |
| year_r | int | Publication year |
| authors_r | str | Author list (semicolon-separated) |
| journal_r | str | Journal name |
| url_r | str | Open access URL if available |
| openalex_id_r | str | OpenAlex work ID |
| source | str | openalex / bob_reed / i4r / score / semantic_scholar |

### Stage 2 Output — `filtered.csv`
All columns from candidates.csv, plus:
| Column | Type | Description |
|--------|------|-------------|
| filter_status | str | replication / reproduction / false_positive / needs_review |
| filter_method | str | rule_based / llm / both |
| filter_evidence | str | Phrase that triggered classification |
| filter_confidence | float | 0.0–1.0 |
| is_replication | bool | True if confirmed replication |
| is_reproduction | bool | True if confirmed reproduction |
| is_multi_original | bool | True if likely targets multiple originals |

### Stage 3 Output — `extracted.csv`
All columns from filtered.csv, plus:
| Column | Type | Description |
|--------|------|-------------|
| doi_o | str | Original study DOI |
| title_o | str | Original study title |
| year_o | int | Original study publication year |
| authors_o | str | Original study authors |
| link_method | str | author_year_match / llm_abstract / llm_fulltext / target_pending |
| link_evidence | str | Quote or pattern used for linking |
| link_confidence | float | 0.0–1.0 |
| outcome | str | success / failure / mixed / uninformative / pending |
| outcome_phrase | str | Supporting quote for outcome |
| outcome_confidence | float | 0.0–1.0 |
| out_quote_source | str | abstract / fulltext / title |
| type | str | replication / reproduction |
| original_rank | int | 1 for single; 1,2,3... for multi-original |
| n_originals | int | Total originals in this paper (1 for single) |

### Stage 4 Output — `validated.csv`
All columns from extracted.csv, plus:
| Column | Type | Description |
|--------|------|-------------|
| validation_status | str | confirmed / rejected / pending / needs_review |
| vote_count | int | Number of votes received |
| confirm_votes | int | Number of confirm votes |
| reject_votes | int | Number of reject votes |
| validator_notes | str | Aggregated reviewer comments |

---

## 5. Stage 3 Linking — Three Cases

```
For each doi_r in filtered.csv:

  Case A (Single): is_multi_original=False
    → run_for_doi() [port of pipeline.py]
      1. Author-year pattern extraction from title/abstract
      2. OpenAlex referenced works fetch
      3. Same-author/year disambiguation
      4. Early abstract LLM (if 2+ distinct patterns)
      5. PDF acquisition (11-tier waterfall)
      6. GROBID reference extraction
      7. LLM identification (Gemini → OpenAI fallback)
    → 1 output row

  Case B (Multi-match): is_multi_original=False but N candidates found
    → handled inside run_for_doi() — LLM picks best candidate from N
    → 1 output row

  Case C (Multi-original): is_multi_original=True
    → run_multi_original_for_doi() [port of multi_original.py]
    → NOTE: existing implementation has known flaws, team to improve
    → N output rows (one per original, original_rank = 1,2,...N)
    → if is_false_positive=True → treat as Case A
```

---

## 6. Validation Web App

**Stack:** Flask + SQLAlchemy + SQLite

**SQLite schema (3 main tables):**
```
originals:     doi PK, title, year, authors, journal
replications:  id PK, doi_r, doi_o FK→originals, outcome, outcome_phrase,
               link_evidence, link_confidence, validation_status, vote_count
reproductions: id PK, doi_r, doi_o FK→originals, computational_success,
               robustness, validation_status, vote_count
votes:         id PK, record_id, record_type, reviewer_id,
               vote (confirm/reject/unsure), comment, timestamp
```

**Voting logic:** Record confirmed when ≥2 votes with majority `confirm`. Rejected when ≥2 votes with majority `reject`. One vote per reviewer per record (enforced by session cookie username).

**Routes:**
- `GET /` — Dashboard
- `GET /review` — Next record in voting queue
- `POST /vote` — Submit vote
- `GET /admin` — All records, filter by status
- `GET /export` — Download validated.csv
- `GET /batch` — SSE batch pipeline runner
- `GET /multi-originals` — Multi-original pipeline
- `GET /input` — Input data generation
- `GET /dashboard` — Stats overview

---

## 7. Team Assignments

| Team | People | Branch | Owns |
|------|--------|--------|------|
| Search | 2 | feature/search | search/ + data seeding |
| Filter | 1-2 | feature/filter | filter/ |
| Extract | 2 | feature/extract | extract/ + shared/ porting |
| Validate | 2 | feature/validate | validate/ |

**Branch strategy:**
- `main` — protected, PR + 1 review required
- `dev` — integration branch
- Feature branches merge to `dev` first

**Day-by-day merge order:**
- Day 1: All teams work from misc/sample CSVs
- Day 2 PM: feature/search → dev
- Day 3 AM: feature/filter → dev, feature/extract → dev
- Day 3 PM: feature/validate → dev → main

---

## 8. LLM Models

| Purpose | Model | API |
|---------|-------|-----|
| Identification / linking | gemini-3-flash-preview | Gemini (free tier, rotate keys) |
| Filtering classification | gemini-3-flash-preview | Gemini (free tier) |
| Outcome coding | gemini-3-flash-preview | Gemini (free tier) |
| Fallback | gpt-5-mini | OpenAI |

---

## 9. External Sources (Stage 1)

| Source | URL | Method |
|--------|-----|--------|
| OpenAlex | api.openalex.org | Keyword search: "replication of", "direct replication" |
| Bob Reed | replicationnetwork.com/replication-studies/ | HTML scrape |
| I4R | i4replication.org/reports/ | HTML scrape |
| SCORE | via Luke/Theresa | Static CSV import |
| Unpaywall | api.unpaywall.org | OA PDF link lookup |

---

## 10. Existing Code to Port (do not rewrite)

From `OpenAlexLLM/lib/` in `flora_search_approaches`:

| Source file | Destination | Notes |
|-------------|-------------|-------|
| lib/openalex.py | shared/openalex_client.py | Rename only |
| lib/llm.py | shared/llm_client.py | Rename only |
| lib/pdf_sources.py | shared/pdf_sources.py | Rename only |
| lib/grobid.py | shared/grobid.py | Rename only |
| lib/disambiguation.py | shared/disambiguation.py | Rename only |
| lib/utils.py | shared/utils.py | Rename only |
| lib/config.py | shared/config.py | Update paths for new repo |
| lib/pipeline.py | extract/link_original.py | Rename only |
| lib/multi_original.py | extract/multi_original.py | Rename + MARK FOR IMPROVEMENT |
| state.py | validate/state.py | Rename only |
| routes/batch.py | validate/routes/batch.py | Rename only |
| routes/multi_originals.py | validate/routes/multi_originals.py | Rename only |
| routes/input_bp.py | validate/routes/input.py | Rename only |
| routes/disambiguation.py | validate/routes/disambiguation.py | Rename only |

---

## 11. Known Issues / Improvement Areas

1. **multi_original.py** — Current implementation has flaws in detecting and resolving multiple originals. Team Extract should review and improve the detection logic, false-positive handling, and row-expansion in the output CSV.
2. **Outcome coding** — Not yet implemented. `extract/code_outcome.py` is new work.
3. **External list scrapers** — Not yet implemented. `search/external_lists.py` is new work.
4. **Gamified validation voting** — Not yet implemented. `validate/routes/review.py` voting logic is new work.
